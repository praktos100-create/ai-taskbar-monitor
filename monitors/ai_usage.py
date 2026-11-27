from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import antigravity_usage, claude_usage


@dataclass
class UsageEvent:
    timestamp: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def quota_tokens(self) -> int:
        """Cache-weighted tokens for quota estimation (cache_read weighted at 10%)."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_write_tokens
            + (self.cache_read_tokens // 10)
        )


@dataclass
class AIProviderStats:
    name: str
    short_label: str
    color: str
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    tokens_5h: int = 0
    tokens_7d: int = 0
    quota_tokens_5h: int = 0
    quota_tokens_7d: int = 0
    sessions: int = 0
    estimated_cost_usd: float = 0.0
    rate_limit_percent: float | None = None
    rate_limit_label: str = ""
    rate_limit_resets_at: float | None = None
    rate_limit_7d_percent: float | None = None
    rate_limit_7d_resets_at: float | None = None
    rate_limit_source: str = ""
    rate_limit_stale: bool = False
    extra_limit_note: str = ""
    agy_text: str = ""
    agy_stale: bool = False
    last_activity_ts: float | None = None
    log_dir_exists: bool = False
    error: str = ""

    def used_percent(self, budget: int) -> float:
        if self.rate_limit_percent is not None:
            return max(0.0, min(100.0, self.rate_limit_percent))
        pct = (self.total_tokens / max(1, budget)) * 100.0
        return max(0.0, min(100.0, pct))

    def remaining_percent(self, budget: int) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent(budget)))

    def used_5h_percent(self, budget_5h: int) -> float:
        if self.rate_limit_percent is not None:
            return max(0.0, min(100.0, self.rate_limit_percent))
        pct = (self.quota_tokens_5h / max(1, budget_5h)) * 100.0
        return max(0.0, min(100.0, pct))

    def remaining_5h_percent(self, budget_5h: int) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_5h_percent(budget_5h)))

    def used_7d_percent(self, budget_7d: int) -> float:
        if self.rate_limit_7d_percent is not None:
            return max(0.0, min(100.0, self.rate_limit_7d_percent))
        pct = (self.quota_tokens_7d / max(1, budget_7d)) * 100.0
        return max(0.0, min(100.0, pct))

    def remaining_7d_percent(self, budget_7d: int) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_7d_percent(budget_7d)))

    def primary_used_percent(self, budget_5h: int, budget_7d: int) -> float:
        if self.rate_limit_percent is not None:
            return self.used_5h_percent(budget_5h)
        if self.rate_limit_7d_percent is not None:
            return self.used_7d_percent(budget_7d)
        return self.used_5h_percent(budget_5h)

    def primary_remaining_percent(self, budget_5h: int, budget_7d: int) -> float:
        return max(0.0, min(100.0, 100.0 - self.primary_used_percent(budget_5h, budget_7d)))


@dataclass
class AIUsageSnapshot:
    claude: AIProviderStats
    codex: AIProviderStats
    gemini: AIProviderStats
    updated_at: float
    agy_snapshot: dict[str, Any] | None = None


def format_tokens(n: int) -> str:
    if n < 0:
        n = 0
    if n < 1000:
        return str(n)
    if n < 100_000:
        return f"{n / 1000:.1f}K"
    if n < 1_000_000:
        return f"{n / 1000:.0f}K"
    if n < 100_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n / 1_000_000:.1f}M"


def _parse_timestamp(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)):
        val = float(value)
        return val / 1000.0 if val > 1e12 else val
    if isinstance(value, str) and value:
        text = value.strip()
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            pass
    return fallback


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _window_start_ts(time_window: str, now_ts: float) -> float:
    if time_window == "5h":
        return now_ts - 5 * 3600
    if time_window == "7d":
        return now_ts - 7 * 86400
    # Default "today": local midnight
    dt = datetime.fromtimestamp(now_ts)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _estimate_event_cost(provider: str, ev: UsageEvent) -> float:
    """Estimate API-equivalent USD cost based on model family."""
    model = (ev.model or "").lower()
    # Per 1M tokens: (input, output, cache_read, cache_write)
    if provider == "claude":
        if "opus" in model:
            rates = (15.0, 75.0, 1.50, 18.75)
        elif "haiku" in model:
            rates = (0.80, 4.0, 0.08, 1.00)
        else:
            rates = (3.0, 15.0, 0.30, 3.75)
    elif provider == "codex":
        if "mini" in model or "nano" in model:
            rates = (0.40, 1.60, 0.10, 0.0)
        elif "o3" in model or "pro" in model:
            rates = (2.0, 8.0, 0.50, 0.0)
        else:
            rates = (1.50, 6.0, 0.375, 0.0)
    else:  # gemini
        if "flash" in model:
            rates = (0.15, 0.60, 0.0375, 0.0)
        else:
            rates = (1.25, 10.0, 0.31, 0.0)

    return (
        ev.input_tokens * rates[0]
        + ev.output_tokens * rates[1]
        + ev.cache_read_tokens * rates[2]
        + ev.cache_write_tokens * rates[3]
    ) / 1_000_000.0


def _iter_recent_files(root: Path, suffix: str, cutoff_mtime: float) -> list[Path]:
    result: list[Path] = []
    if not root.exists():
        return result
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False) and entry.name.endswith(suffix):
                            st = entry.stat()
                            if st.st_mtime >= cutoff_mtime:
                                result.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return result


class AIUsageMonitor:
    """Parses local CLI logs for Claude Code (~/.claude), Codex CLI (~/.codex), and Gemini CLI (~/.gemini)."""

    def __init__(self) -> None:
        # cache key -> (mtime, size, events, metadata)
        self._file_cache: dict[str, tuple[float, int, list[UsageEvent], dict[str, Any]]] = {}

    def _get_cached_or_parse(self, path: Path, parser) -> tuple[list[UsageEvent], dict[str, Any]]:
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            return [], {}
        cached = self._file_cache.get(key)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return cached[2], cached[3]
        events, meta = parser(path, st.st_mtime)
        self._file_cache[key] = (st.st_mtime, st.st_size, events, meta)
        return events, meta

    @staticmethod
    def _parse_claude_jsonl(path: Path, mtime: float) -> tuple[list[UsageEvent], dict[str, Any]]:
        deduped: dict[str, UsageEvent] = {}
        anon_events: list[UsageEvent] = []
        seq = 0
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue

                    msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
                    usage = (msg.get("usage") if msg else None) or obj.get("usage")
                    if not isinstance(usage, dict):
                        continue

                    inp = _safe_int(usage.get("input_tokens"))
                    out = _safe_int(usage.get("output_tokens"))
                    cache_read = _safe_int(usage.get("cache_read_input_tokens"))
                    cache_write = _safe_int(usage.get("cache_creation_input_tokens"))
                    if inp + out + cache_read + cache_write == 0:
                        continue

                    ts = _parse_timestamp(obj.get("timestamp"), mtime)
                    model = str((msg.get("model") if msg else None) or obj.get("model") or "")
                    ev = UsageEvent(
                        timestamp=ts,
                        input_tokens=inp,
                        output_tokens=out,
                        cache_read_tokens=cache_read,
                        cache_write_tokens=cache_write,
                        model=model,
                    )

                    msg_id = (
                        (msg.get("id") if msg else None)
                        or obj.get("requestId")
                        or obj.get("uuid")
                    )
                    if isinstance(msg_id, str) and msg_id:
                        prev = deduped.get(msg_id)
                        if prev is None or (ev.output_tokens, ev.total_tokens, ev.timestamp) >= (
                            prev.output_tokens,
                            prev.total_tokens,
                            prev.timestamp,
                        ):
                            deduped[msg_id] = ev
                    else:
                        seq += 1
                        anon_events.append(ev)
        except OSError:
            return [], {}

        events = list(deduped.values()) + anon_events
        return events, {}

    @staticmethod
    def _parse_codex_jsonl(path: Path, mtime: float) -> tuple[list[UsageEvent], dict[str, Any]]:
        events: list[UsageEvent] = []
        last_total_tuple: tuple[int, int, int, int] | None = None
        current_model = ""
        latest_rate_pct: float | None = None
        latest_rate_label = ""
        latest_rate_resets_at: float | None = None
        latest_rate_5h_ts = 0.0
        latest_rate_7d_pct: float | None = None
        latest_rate_7d_resets_at: float | None = None
        latest_rate_7d_ts = 0.0

        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue

                    ts = _parse_timestamp(obj.get("timestamp"), mtime)
                    ev_type = obj.get("type")
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

                    if ev_type == "turn_context" and isinstance(payload.get("model"), str):
                        current_model = payload["model"]
                        continue

                    if ev_type == "event_msg" and payload.get("type") == "token_count":
                        rate_limits = payload.get("rate_limits")
                        if isinstance(rate_limits, dict):
                            for slot_name in ("primary", "secondary"):
                                bucket = rate_limits.get(slot_name)
                                if not isinstance(bucket, dict) or bucket.get("used_percent") is None:
                                    continue
                                try:
                                    pct = max(0.0, min(100.0, float(bucket["used_percent"])))
                                except (TypeError, ValueError):
                                    continue
                                win_min = _safe_int(bucket.get("window_minutes"))
                                resets_raw = bucket.get("resets_at")
                                r_ts = _parse_timestamp(resets_raw, 0.0) if resets_raw is not None else 0.0
                                reset_val = r_ts if r_ts > 0 else None

                                if win_min >= 1440 or slot_name == "secondary":
                                    if ts >= latest_rate_7d_ts:
                                        latest_rate_7d_ts = ts
                                        latest_rate_7d_pct = pct
                                        latest_rate_7d_resets_at = reset_val
                                else:
                                    if ts >= latest_rate_5h_ts:
                                        latest_rate_5h_ts = ts
                                        latest_rate_pct = pct
                                        latest_rate_resets_at = reset_val
                                        if win_min >= 60 and win_min % 60 == 0:
                                            latest_rate_label = f"{win_min // 60}h 한도"
                                        elif win_min > 0:
                                            latest_rate_label = f"{win_min}m 한도"
                                        else:
                                            latest_rate_label = "세션 한도"

                        info = payload.get("info")
                        if not isinstance(info, dict):
                            continue
                        total_u = info.get("total_token_usage")
                        last_u = info.get("last_token_usage")

                        total_tuple: tuple[int, int, int, int] | None = None
                        if isinstance(total_u, dict):
                            total_tuple = (
                                _safe_int(total_u.get("input_tokens")),
                                _safe_int(total_u.get("cached_input_tokens")),
                                _safe_int(total_u.get("output_tokens")),
                                _safe_int(total_u.get("reasoning_output_tokens")),
                            )
                            if total_tuple == last_total_tuple:
                                # Duplicate status snapshot; skip double-counting
                                continue

                        if isinstance(last_u, dict):
                            cached_in = _safe_int(last_u.get("cached_input_tokens"))
                            raw_in = _safe_int(last_u.get("input_tokens"))
                            non_cached_in = max(0, raw_in - cached_in) if raw_in >= cached_in else raw_in
                            out = _safe_int(last_u.get("output_tokens"))
                            if non_cached_in + cached_in + out > 0:
                                events.append(
                                    UsageEvent(
                                        timestamp=ts,
                                        input_tokens=non_cached_in,
                                        output_tokens=out,
                                        cache_read_tokens=cached_in,
                                        cache_write_tokens=0,
                                        model=current_model,
                                    )
                                )
                        elif total_tuple is not None:
                            prev = last_total_tuple or (0, 0, 0, 0)
                            d_in_raw = max(0, total_tuple[0] - prev[0])
                            d_cached = max(0, total_tuple[1] - prev[1])
                            d_out = max(0, total_tuple[2] - prev[2])
                            d_in = max(0, d_in_raw - d_cached) if d_in_raw >= d_cached else d_in_raw
                            if d_in + d_cached + d_out > 0:
                                events.append(
                                    UsageEvent(
                                        timestamp=ts,
                                        input_tokens=d_in,
                                        output_tokens=d_out,
                                        cache_read_tokens=d_cached,
                                        cache_write_tokens=0,
                                        model=current_model,
                                    )
                                )
                        if total_tuple is not None:
                            last_total_tuple = total_tuple

                    elif isinstance(obj.get("usage"), dict):
                        u = obj["usage"]
                        inp = _safe_int(u.get("input_tokens"))
                        out = _safe_int(u.get("output_tokens"))
                        cached = _safe_int(u.get("cached_input_tokens") or u.get("cache_read_input_tokens"))
                        if inp + out + cached > 0:
                            events.append(
                                UsageEvent(
                                    timestamp=ts,
                                    input_tokens=inp,
                                    output_tokens=out,
                                    cache_read_tokens=cached,
                                    cache_write_tokens=0,
                                    model=str(obj.get("model") or current_model),
                                )
                            )
        except OSError:
            return [], {}

        meta: dict[str, Any] = {}
        if latest_rate_pct is not None:
            meta["rate_limit_percent"] = latest_rate_pct
            meta["rate_limit_label"] = latest_rate_label
            meta["rate_limit_resets_at"] = latest_rate_resets_at
            meta["rate_limit_5h_ts"] = latest_rate_5h_ts
        if latest_rate_7d_pct is not None:
            meta["rate_limit_7d_percent"] = latest_rate_7d_pct
            meta["rate_limit_7d_resets_at"] = latest_rate_7d_resets_at
            meta["rate_limit_7d_ts"] = latest_rate_7d_ts
        return events, meta

    @staticmethod
    def _parse_antigravity_jsonl(path: Path, mtime: float) -> tuple[list[UsageEvent], dict[str, Any]]:
        events: list[UsageEvent] = []
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue

                    inp = _safe_int(obj.get("input_tokens"))
                    out = _safe_int(obj.get("output_tokens"))
                    cache_read = _safe_int(obj.get("cache_read_tokens"))
                    cache_write = _safe_int(obj.get("cache_write_tokens"))
                    if inp + out + cache_read + cache_write == 0:
                        continue

                    ts = _parse_timestamp(obj.get("created_at") or obj.get("timestamp"), mtime)
                    model = str(obj.get("model") or "gemini")
                    events.append(
                        UsageEvent(
                            timestamp=ts,
                            input_tokens=inp,
                            output_tokens=out,
                            cache_read_tokens=cache_read,
                            cache_write_tokens=cache_write,
                            model=model,
                        )
                    )
        except OSError:
            return [], {}
        return events, {}

    @staticmethod
    def _parse_gemini_json(path: Path, mtime: float) -> tuple[list[UsageEvent], dict[str, Any]]:
        events: list[UsageEvent] = []
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            return [], {}

        if not isinstance(data, dict):
            return [], {}

        file_ts = _parse_timestamp(data.get("lastUpdated") or data.get("startTime"), mtime)
        messages = data.get("messages")
        if not isinstance(messages, list):
            messages = []

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            ts = _parse_timestamp(msg.get("timestamp"), file_ts)
            model = str(msg.get("model") or data.get("model") or "")
            tokens = msg.get("tokens")
            usage_meta = msg.get("usageMetadata")

            if isinstance(tokens, dict):
                inp = _safe_int(tokens.get("input"))
                out = _safe_int(tokens.get("output")) + _safe_int(tokens.get("thoughts")) + _safe_int(tokens.get("tool"))
                cached = _safe_int(tokens.get("cached"))
                if inp + out + cached > 0:
                    events.append(
                        UsageEvent(
                            timestamp=ts,
                            input_tokens=inp,
                            output_tokens=out,
                            cache_read_tokens=cached,
                            cache_write_tokens=0,
                            model=model,
                        )
                    )
            elif isinstance(usage_meta, dict):
                inp = _safe_int(usage_meta.get("promptTokenCount"))
                out = _safe_int(usage_meta.get("candidatesTokenCount")) + _safe_int(
                    usage_meta.get("thoughtsTokenCount")
                )
                cached = _safe_int(usage_meta.get("cachedContentTokenCount"))
                if inp + out + cached > 0:
                    events.append(
                        UsageEvent(
                            timestamp=ts,
                            input_tokens=inp,
                            output_tokens=out,
                            cache_read_tokens=cached,
                            cache_write_tokens=0,
                            model=model,
                        )
                    )

        return events, {}

    def _scan_claude(
        self, root: Path, cutoff_mtime: float, window_start: float, now: float
    ) -> AIProviderStats:
        stats = AIProviderStats(
            name="Claude Code",
            short_label="CLD",
            color="#E88C5D",
            log_dir_exists=root.exists(),
        )
        if not root.exists():
            return stats
        start_5h = now - 5 * 3600
        start_7d = now - 7 * 86400
        try:
            projects_dir = root / "projects"
            search_root = projects_dir if projects_dir.exists() else root
            files = _iter_recent_files(search_root, ".jsonl", cutoff_mtime)
            for f in files:
                events, _ = self._get_cached_or_parse(f, self._parse_claude_jsonl)
                matched = False
                for ev in events:
                    if ev.timestamp >= start_5h:
                        stats.tokens_5h += ev.total_tokens
                        stats.quota_tokens_5h += ev.quota_tokens
                    if ev.timestamp >= start_7d:
                        stats.tokens_7d += ev.total_tokens
                        stats.quota_tokens_7d += ev.quota_tokens
                    if ev.timestamp < window_start:
                        continue
                    matched = True
                    stats.input_tokens += ev.input_tokens
                    stats.output_tokens += ev.output_tokens
                    stats.cache_tokens += ev.cache_read_tokens + ev.cache_write_tokens
                    stats.total_tokens += ev.total_tokens
                    stats.estimated_cost_usd += _estimate_event_cost("claude", ev)
                    if stats.last_activity_ts is None or ev.timestamp > stats.last_activity_ts:
                        stats.last_activity_ts = ev.timestamp
                if matched:
                    stats.sessions += 1
        except Exception as exc:
            stats.error = str(exc)
        return stats

    def _scan_codex(
        self, root: Path, cutoff_mtime: float, window_start: float, now: float
    ) -> AIProviderStats:
        stats = AIProviderStats(
            name="ChatGPT (Codex)",
            short_label="GPT",
            color="#34D399",
            log_dir_exists=root.exists(),
        )
        if not root.exists():
            return stats
        start_5h = now - 5 * 3600
        start_7d = now - 7 * 86400
        try:
            files: list[Path] = []
            for sub in ("sessions", "archived_sessions"):
                sub_dir = root / sub
                if sub_dir.exists():
                    files.extend(_iter_recent_files(sub_dir, ".jsonl", cutoff_mtime))
            if not files:
                files = _iter_recent_files(root, ".jsonl", cutoff_mtime)

            best_rate_5h_ts = 0.0
            best_rate_7d_ts = 0.0
            for f in files:
                events, meta = self._get_cached_or_parse(f, self._parse_codex_jsonl)
                if "rate_limit_percent" in meta:
                    r5_ts = float(meta.get("rate_limit_5h_ts", 0.0))
                    if r5_ts >= best_rate_5h_ts:
                        best_rate_5h_ts = r5_ts
                        stats.rate_limit_percent = meta["rate_limit_percent"]
                        stats.rate_limit_label = meta.get("rate_limit_label", "")
                        stats.rate_limit_resets_at = meta.get("rate_limit_resets_at")
                if "rate_limit_7d_percent" in meta:
                    r7_ts = float(meta.get("rate_limit_7d_ts", 0.0))
                    if r7_ts >= best_rate_7d_ts:
                        best_rate_7d_ts = r7_ts
                        stats.rate_limit_7d_percent = meta["rate_limit_7d_percent"]
                        stats.rate_limit_7d_resets_at = meta.get("rate_limit_7d_resets_at")

                matched = False
                for ev in events:
                    if ev.timestamp >= start_5h:
                        stats.tokens_5h += ev.total_tokens
                        stats.quota_tokens_5h += ev.quota_tokens
                    if ev.timestamp >= start_7d:
                        stats.tokens_7d += ev.total_tokens
                        stats.quota_tokens_7d += ev.quota_tokens
                    if ev.timestamp < window_start:
                        continue
                    matched = True
                    stats.input_tokens += ev.input_tokens
                    stats.output_tokens += ev.output_tokens
                    stats.cache_tokens += ev.cache_read_tokens + ev.cache_write_tokens
                    stats.total_tokens += ev.total_tokens
                    stats.estimated_cost_usd += _estimate_event_cost("codex", ev)
                    if stats.last_activity_ts is None or ev.timestamp > stats.last_activity_ts:
                        stats.last_activity_ts = ev.timestamp
                if matched:
                    stats.sessions += 1

            # If a recorded limit window has already reset, treat it as 0% used (100% remaining)
            if stats.rate_limit_resets_at is not None and stats.rate_limit_resets_at <= now:
                stats.rate_limit_percent = 0.0
            if stats.rate_limit_7d_resets_at is not None and stats.rate_limit_7d_resets_at <= now:
                stats.rate_limit_7d_percent = 0.0
            if stats.rate_limit_percent is not None or stats.rate_limit_7d_percent is not None:
                stats.rate_limit_source = "codex"
        except Exception as exc:
            stats.error = str(exc)
        return stats

    def _scan_gemini(
        self, root: Path, cutoff_mtime: float, window_start: float, now: float
    ) -> AIProviderStats:
        stats = AIProviderStats(
            name="Gemini (CLI · AGY)",
            short_label="GEM",
            color="#60A5FA",
            log_dir_exists=root.exists(),
        )
        if not root.exists():
            return stats
        start_5h = now - 5 * 3600
        start_7d = now - 7 * 86400
        try:
            # 1. Gemini CLI JSON sessions
            cli_files: list[Path] = []
            for sub in ("tmp", "history"):
                sub_dir = root / sub
                if sub_dir.exists():
                    cli_files.extend(_iter_recent_files(sub_dir, ".json", cutoff_mtime))

            for f in cli_files:
                events, _ = self._get_cached_or_parse(f, self._parse_gemini_json)
                if not events:
                    continue
                matched = False
                for ev in events:
                    if ev.timestamp >= start_5h:
                        stats.tokens_5h += ev.total_tokens
                        stats.quota_tokens_5h += ev.quota_tokens
                    if ev.timestamp >= start_7d:
                        stats.tokens_7d += ev.total_tokens
                        stats.quota_tokens_7d += ev.quota_tokens
                    if ev.timestamp < window_start:
                        continue
                    matched = True
                    stats.input_tokens += ev.input_tokens
                    stats.output_tokens += ev.output_tokens
                    stats.cache_tokens += ev.cache_read_tokens + ev.cache_write_tokens
                    stats.total_tokens += ev.total_tokens
                    stats.estimated_cost_usd += _estimate_event_cost("gemini", ev)
                    if stats.last_activity_ts is None or ev.timestamp > stats.last_activity_ts:
                        stats.last_activity_ts = ev.timestamp
                if matched:
                    stats.sessions += 1

            # 2. Antigravity transcripts (transcript.jsonl only, exclude non-Gemini models)
            ag_brain = root / "antigravity" / "brain"
            if ag_brain.exists():
                ag_files = [
                    p
                    for p in _iter_recent_files(ag_brain, "transcript.jsonl", cutoff_mtime)
                    if p.name == "transcript.jsonl"
                ]
                for f in ag_files:
                    events, _ = self._get_cached_or_parse(f, self._parse_antigravity_jsonl)
                    if not events:
                        continue
                    matched = False
                    for ev in events:
                        m_low = (ev.model or "").lower()
                        if "claude" in m_low or "gpt" in m_low:
                            continue
                        if ev.timestamp >= start_5h:
                            stats.tokens_5h += ev.total_tokens
                            stats.quota_tokens_5h += ev.quota_tokens
                        if ev.timestamp >= start_7d:
                            stats.tokens_7d += ev.total_tokens
                            stats.quota_tokens_7d += ev.quota_tokens
                        if ev.timestamp < window_start:
                            continue
                        matched = True
                        stats.input_tokens += ev.input_tokens
                        stats.output_tokens += ev.output_tokens
                        stats.cache_tokens += ev.cache_read_tokens + ev.cache_write_tokens
                        stats.total_tokens += ev.total_tokens
                        stats.estimated_cost_usd += _estimate_event_cost("gemini", ev)
                        if stats.last_activity_ts is None or ev.timestamp > stats.last_activity_ts:
                            stats.last_activity_ts = ev.timestamp
                    if matched:
                        stats.sessions += 1
        except Exception as exc:
            stats.error = str(exc)
        return stats

    def scan(
        self,
        time_window: str = "5h",
        claude_dir: str = "",
        codex_dir: str = "",
        gemini_dir: str = "",
        now_ts: float | None = None,
        force_agy: bool = False,
        include_agy: bool = True,
        claude_window: str | None = None,
        codex_window: str | None = None,
        gemini_window: str | None = None,
        force_claude: bool = False,
        include_claude_oauth: bool = True,
    ) -> AIUsageSnapshot:
        now = now_ts if now_ts is not None else time.time()
        c_start = _window_start_ts(claude_window or time_window, now)
        x_start = _window_start_ts(codex_window or time_window, now)
        g_start = _window_start_ts(gemini_window or time_window, now)
        # Skip files older than 8 days for fast scanning
        cutoff_mtime = min(c_start, x_start, g_start, now - 8 * 86400) - 3600

        home = Path.home()
        c_root = Path(claude_dir).expanduser() if claude_dir.strip() else home / ".claude"
        x_root = Path(codex_dir).expanduser() if codex_dir.strip() else home / ".codex"
        g_root = Path(gemini_dir).expanduser() if gemini_dir.strip() else home / ".gemini"

        claude_stats = self._scan_claude(c_root, cutoff_mtime, c_start, now)
        codex_stats = self._scan_codex(x_root, cutoff_mtime, x_start, now)
        gemini_stats = self._scan_gemini(g_root, cutoff_mtime, g_start, now)

        if include_claude_oauth:
            claude_snap = claude_usage.snapshot(claude_dir=claude_dir, force=force_claude)
            if claude_snap.get("available"):
                c_metrics = claude_snap.get("metrics") or {}
                c_resets = claude_snap.get("resets") or {}
                c_scoped = claude_snap.get("scoped") or []
                claude_stats.rate_limit_source = "claude_oauth"
                claude_stats.log_dir_exists = True
                claude_stats.rate_limit_stale = bool(claude_snap.get("stale", False))
                if "5h" in c_metrics:
                    claude_stats.rate_limit_percent = max(0.0, min(100.0, float(c_metrics["5h"])))
                    claude_stats.rate_limit_label = "5h 한도"
                if "5h" in c_resets:
                    claude_stats.rate_limit_resets_at = float(c_resets["5h"])
                if "7d" in c_metrics:
                    claude_stats.rate_limit_7d_percent = max(0.0, min(100.0, float(c_metrics["7d"])))
                if "7d" in c_resets:
                    claude_stats.rate_limit_7d_resets_at = float(c_resets["7d"])
                if c_scoped:
                    scoped_parts = [
                        f"{item['name']} {float(item['used']):.0f}%"
                        for item in c_scoped
                        if isinstance(item, dict) and item.get("name") is not None and item.get("used") is not None
                    ]
                    if scoped_parts:
                        claude_stats.extra_limit_note = " · ".join(scoped_parts)

        agy_snap: dict[str, Any] | None = None
        if include_agy:
            agy_snap = antigravity_usage.snapshot(force=force_agy)
            if agy_snap.get("available"):
                metrics = agy_snap.get("metrics") or {}
                groups = agy_snap.get("groups") or {}
                resets = agy_snap.get("resets") or {}
                stale = bool(agy_snap.get("stale", False))

                if "gemini" in metrics:
                    gm = metrics["gemini"]
                    gr = resets.get("gemini") or {}
                    gemini_stats.agy_text = str(groups.get("gemini", ""))
                    gemini_stats.agy_stale = stale
                    gemini_stats.rate_limit_source = "antigravity"
                    gemini_stats.log_dir_exists = True
                    if "5h" in gm:
                        gemini_stats.rate_limit_percent = max(0.0, min(100.0, 100.0 - gm["5h"]))
                        gemini_stats.rate_limit_label = "Antigravity 5h"
                    if "weekly" in gm:
                        gemini_stats.rate_limit_7d_percent = max(0.0, min(100.0, 100.0 - gm["weekly"]))
                    if "5h" in gr:
                        gemini_stats.rate_limit_resets_at = gr["5h"]
                    if "weekly" in gr:
                        gemini_stats.rate_limit_7d_resets_at = gr["weekly"]

                if "claudeGpt" in groups and claude_stats.rate_limit_source != "claude_oauth":
                    claude_stats.agy_text = str(groups["claudeGpt"])
                    claude_stats.agy_stale = stale
                    if claude_stats.tokens_7d == 0 and "claudeGpt" in metrics:
                        cm = metrics["claudeGpt"]
                        cr = resets.get("claudeGpt") or {}
                        claude_stats.rate_limit_source = "antigravity"
                        claude_stats.log_dir_exists = True
                        if "5h" in cm:
                            claude_stats.rate_limit_percent = max(0.0, min(100.0, 100.0 - cm["5h"]))
                            claude_stats.rate_limit_label = "Antigravity Claude·GPT"
                        if "weekly" in cm:
                            claude_stats.rate_limit_7d_percent = max(0.0, min(100.0, 100.0 - cm["weekly"]))
                        if "5h" in cr:
                            claude_stats.rate_limit_resets_at = cr["5h"]
                        if "weekly" in cr:
                            claude_stats.rate_limit_7d_resets_at = cr["weekly"]
            else:
                gemini_stats.agy_text = str(agy_snap.get("text") or "Antigravity 한도 조회 실패")
                gemini_stats.agy_stale = True

        return AIUsageSnapshot(
            claude=claude_stats,
            codex=codex_stats,
            gemini=gemini_stats,
            updated_at=now,
            agy_snapshot=agy_snap,
        )
