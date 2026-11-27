from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from config import DATA_DIR

DEFAULT_STATE_PATH = DATA_DIR / "reset_bell_state.json"
CODEX_RESETS_SITE_URL = "https://codex-resets.com/ko"
CODEX_RESETS_ENDPOINTS = (
    "https://codex-resets.com/api/v1/status",
    "https://codex-resets.com/api/resets?locale=ko",
)


@dataclass
class ResetNotification:
    event_id: str
    kind: str       # "upcoming" | "completed" | "prediction"
    title: str
    body: str
    status: str
    url: str = CODEX_RESETS_SITE_URL


@dataclass
class ResetBellStatus:
    ok: bool
    summary: str
    latest_title: str = ""
    latest_status: str = ""
    active_watch_text: str = ""
    checked_at: float | None = None
    new_notifications: list[ResetNotification] = field(default_factory=list)


def play_alert_sound() -> None:
    """Play a gentle Windows notification chime in a background thread."""
    if sys.platform != "win32":
        return

    def _beep() -> None:
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    threading.Thread(target=_beep, name="ResetBellSound", daemon=True).start()


def _parse_iso_short(ts_val: Any) -> str:
    if not ts_val:
        return ""
    if isinstance(ts_val, (int, float)):
        t = float(ts_val) / 1000.0 if float(ts_val) > 1e12 else float(ts_val)
        return datetime.fromtimestamp(t).strftime("%m/%d %H:%M")
    if isinstance(ts_val, str):
        text = ts_val.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).astimezone().strftime("%m/%d %H:%M")
        except ValueError:
            return text[:16]
    return ""


def _normalize_event(raw: dict[str, Any], default_status: str) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or default_status).strip().lower()
    if status in ("active", "scheduled", "pending", "forecast"):
        status = "upcoming"
    elif status in ("done", "resolved", "past"):
        status = "completed"
    elif status not in ("upcoming", "completed"):
        status = default_status

    ts_raw = (
        raw.get("expected_at")
        or raw.get("completed_at")
        or raw.get("timestamp")
        or raw.get("date")
        or raw.get("created_at")
        or ""
    )
    ev_id = str(raw.get("id") or raw.get("slug") or f"{status}:{ts_raw}:{raw.get('summary') or raw.get('title') or ''}").strip()
    if not ev_id:
        return None

    summary = str(raw.get("summary") or raw.get("title") or raw.get("description") or raw.get("type") or "Codex 리셋 공지").strip()
    when_str = _parse_iso_short(ts_raw)
    url = str(raw.get("source_url") or raw.get("url") or CODEX_RESETS_SITE_URL)
    return {
        "id": ev_id,
        "status": status,
        "title": summary,
        "when": when_str,
        "url": url,
    }


def parse_codex_resets_payload(payload: Any) -> tuple[list[dict[str, str]], dict[str, str] | None]:
    """Extract normalized confirmed events and optional active_watch prediction."""
    if not isinstance(payload, dict):
        return [], None

    root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    events: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    def _add_list(items: Any, default_status: str) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            norm = _normalize_event(item, default_status)
            if norm and norm["id"] not in seen_ids:
                seen_ids.add(norm["id"])
                events.append(norm)

    _add_list(root.get("upcoming"), "upcoming")
    _add_list(payload.get("upcoming"), "upcoming")
    _add_list(root.get("events"), "completed")
    _add_list(payload.get("events"), "completed")
    _add_list(root.get("completed"), "completed")
    _add_list(payload.get("completed"), "completed")

    latest_reset = root.get("latest_reset") or payload.get("latest_reset")
    if isinstance(latest_reset, dict):
        norm = _normalize_event(latest_reset, "completed")
        if norm and norm["id"] not in seen_ids:
            seen_ids.add(norm["id"])
            events.append(norm)

    watch_raw = (
        root.get("active_watch")
        or payload.get("active_watch")
        or root.get("forecast")
        or payload.get("forecast")
    )
    watch: dict[str, str] | None = None
    if isinstance(watch_raw, dict) and watch_raw:
        level = str(watch_raw.get("level") or "").strip()
        chance = watch_raw.get("reset_chance_percent") or watch_raw.get("next_reset_probability") or watch_raw.get("probability")
        window = str(watch_raw.get("forecast_window") or watch_raw.get("window") or "").strip()
        parts: list[str] = []
        if level:
            parts.append(level)
        if chance is not None:
            parts.append(f"{chance}%")
        if window:
            parts.append(window)
        if parts:
            w_text = " · ".join(parts)
            watch = {
                "id": f"watch:{level}:{chance}:{window}",
                "text": f"예측: {w_text}",
            }

    return events, watch


def _default_fetcher(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "AITaskbarMonitor/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


class ResetBellMonitor:
    """Checks codex-resets.com for new reset announcements without double-alerting."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._state_path = state_path or DEFAULT_STATE_PATH
        self._lock = threading.Lock()
        self._state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {
                        "initialized": bool(data.get("initialized", False)),
                        "seen": dict(data.get("seen") or {}),
                        "seen_watch": str(data.get("seen_watch") or ""),
                    }
            except Exception:
                pass
        return {"initialized": False, "seen": {}, "seen_watch": ""}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def check(
        self,
        include_predictions: bool = False,
        fetcher: Callable[[str], Any] | None = None,
    ) -> ResetBellStatus:
        fetch = fetcher or _default_fetcher
        now = time.time()
        payload: Any = None
        last_err: Exception | None = None

        for url in CODEX_RESETS_ENDPOINTS:
            try:
                payload = fetch(url)
                if payload is not None:
                    break
            except Exception as exc:
                last_err = exc
                continue

        if payload is None:
            _ = last_err
            return ResetBellStatus(
                ok=False,
                summary="리셋 공지 확인 실패",
                checked_at=now,
            )

        events, watch = parse_codex_resets_payload(payload)
        notifications: list[ResetNotification] = []

        with self._lock:
            initialized = bool(self._state.get("initialized", False))
            seen: dict[str, str] = dict(self._state.get("seen") or {})
            seen_watch = str(self._state.get("seen_watch") or "")

            if not initialized:
                for ev in events:
                    seen[ev["id"]] = ev["status"]
                if watch:
                    seen_watch = watch["id"]
                self._state = {
                    "initialized": True,
                    "seen": seen,
                    "seen_watch": seen_watch,
                }
                self._save_state()
            else:
                for ev in events:
                    ev_id = ev["id"]
                    ev_status = ev["status"]
                    prev_status = seen.get(ev_id)
                    when_suffix = f" ({ev['when']})" if ev["when"] else ""
                    if prev_status is None:
                        kind_title = "Codex 리셋 예정" if ev_status == "upcoming" else "Codex 리셋 완료"
                        notifications.append(
                            ResetNotification(
                                event_id=ev_id,
                                kind=ev_status,
                                title=kind_title,
                                body=f"{ev['title']}{when_suffix}",
                                status=ev_status,
                                url=ev["url"],
                            )
                        )
                    elif prev_status == "upcoming" and ev_status == "completed":
                        notifications.append(
                            ResetNotification(
                                event_id=ev_id,
                                kind="completed",
                                title="Codex 리셋 완료",
                                body=f"{ev['title']}{when_suffix}",
                                status="completed",
                                url=ev["url"],
                            )
                        )
                    seen[ev_id] = ev_status

                if include_predictions and watch and watch["id"] != seen_watch:
                    notifications.append(
                        ResetNotification(
                            event_id=watch["id"],
                            kind="prediction",
                            title="Codex 리셋 예측",
                            body=watch["text"],
                            status="prediction",
                            url=CODEX_RESETS_SITE_URL,
                        )
                    )
                if watch:
                    seen_watch = watch["id"]

                # Bound persisted seen dict size to most recent 200 entries
                if len(seen) > 200:
                    keys = list(seen.keys())[-200:]
                    seen = {k: seen[k] for k in keys}

                self._state = {
                    "initialized": True,
                    "seen": seen,
                    "seen_watch": seen_watch,
                }
                self._save_state()

        upcoming = [e for e in events if e["status"] == "upcoming"]
        completed = [e for e in events if e["status"] == "completed"]
        latest = upcoming[0] if upcoming else (completed[0] if completed else None)

        if latest:
            prefix = "예정 공지" if latest["status"] == "upcoming" else "최근 완료"
            when_txt = f" ({latest['when']})" if latest["when"] else ""
            summary = f"{prefix}: {latest['title']}{when_txt}"
            latest_title = latest["title"]
            latest_status = latest["status"]
        else:
            summary = "새 리셋 공지 없음"
            latest_title = ""
            latest_status = ""

        return ResetBellStatus(
            ok=True,
            summary=summary,
            latest_title=latest_title,
            latest_status=latest_status,
            active_watch_text=watch["text"] if watch else "",
            checked_at=now,
            new_notifications=notifications,
        )
