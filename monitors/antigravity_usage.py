from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None
_CACHE_AT = 0.0
REFRESH_SECONDS = 15

_SAFE_ENV_KEYS = frozenset({
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "PUBLIC",
    "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    "USERNAME", "USERDOMAIN", "COMPUTERNAME",
    "LANG", "LC_ALL", "LC_CTYPE", "PYTHONUTF8", "PYTHONIOENCODING",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
})

_GROUP_MAP = {
    "Gemini Models": ("gemini", "Gemini"),
    "Claude and GPT models": ("claudeGpt", "Claude·GPT"),
}

_WINDOW_MAP = {
    "weekly": "주간",
    "5h": "5시간",
}


def _child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.upper() in _SAFE_ENV_KEYS}


def find_agy() -> str | None:
    exe = "agy.exe" if os.name == "nt" else "agy"
    found = shutil.which(exe)
    if found:
        return found
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        path = Path(os.environ["LOCALAPPDATA"]) / "agy" / "bin" / exe
        if path.is_file():
            return str(path)
    return None


def _parse_reset_ts(bucket: dict[str, Any], now_ts: float) -> float | None:
    for key in ("reset_time", "resets_at", "reset_at", "refresh_at", "resetTime", "resetsAt"):
        val = bucket.get(key)
        if val is None or isinstance(val, bool):
            continue
        if isinstance(val, (int, float)) and math.isfinite(val):
            ts = float(val)
            return ts / 1000.0 if ts > 1e12 else ts
        if isinstance(val, str) and val.strip():
            text = val.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(text).timestamp()
            except ValueError:
                continue

    for key in ("reset_in_seconds", "seconds_until_reset", "resetInSeconds"):
        val = bucket.get(key)
        if not isinstance(val, bool) and isinstance(val, (int, float)) and math.isfinite(val) and val > 0:
            return now_ts + float(val)
    return None


def parse_usage_full(
    data: Any, now_ts: float | None = None
) -> tuple[dict[str, str], dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    now = now_ts if now_ts is not None else time.time()
    groups: dict[str, str] = {}
    metrics: dict[str, dict[str, float]] = {}
    resets: dict[str, dict[str, float]] = {}
    if not isinstance(data, dict):
        return groups, metrics, resets
    cmd = data.get("command")
    if not isinstance(cmd, dict):
        return groups, metrics, resets
    cmd_data = cmd.get("data")
    if not isinstance(cmd_data, dict):
        return groups, metrics, resets
    raw_groups = cmd_data.get("groups")
    if not isinstance(raw_groups, list):
        return groups, metrics, resets

    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        mapped = _GROUP_MAP.get(group.get("name"))
        if not mapped:
            continue
        key, label = mapped
        buckets_raw = group.get("buckets")
        if not isinstance(buckets_raw, list):
            continue
        buckets: list[str] = []
        group_metrics: dict[str, float] = {}
        group_resets: dict[str, float] = {}
        for bucket in buckets_raw:
            if not isinstance(bucket, dict):
                continue
            fraction = bucket.get("remaining_fraction")
            if (
                isinstance(fraction, bool)
                or not isinstance(fraction, (float, int))
                or not math.isfinite(fraction)
            ):
                continue
            win = bucket.get("window")
            period = _WINDOW_MAP.get(win)
            if period:
                pct = max(0.0, min(100.0, float(fraction) * 100.0))
                buckets.append(f"{period} {pct:.0f}%")
                group_metrics[str(win)] = pct
                r_ts = _parse_reset_ts(bucket, now)
                if r_ts is not None:
                    group_resets[str(win)] = r_ts
        if buckets:
            groups[key] = f"{label} " + " / ".join(buckets) + " 남음"
            metrics[key] = group_metrics
            if group_resets:
                resets[key] = group_resets
    return groups, metrics, resets


def parse_usage_data(data: Any) -> tuple[dict[str, str], dict[str, dict[str, float]]]:
    groups, metrics, _ = parse_usage_full(data)
    return groups, metrics


def parse_usage_groups(data: Any) -> dict[str, str]:
    groups, _ = parse_usage_data(data)
    return groups


def reset_cache() -> None:
    global _CACHE, _CACHE_AT
    with _LOCK:
        _CACHE = None
        _CACHE_AT = 0.0


def snapshot(force: bool = False) -> dict[str, Any]:
    global _CACHE, _CACHE_AT
    with _LOCK:
        if not force and _CACHE is not None and time.monotonic() - _CACHE_AT < REFRESH_SECONDS:
            return deepcopy(_CACHE)
        try:
            exe = find_agy()
            if not exe:
                raise RuntimeError()
            with tempfile.TemporaryDirectory(prefix="agy_usage_") as work:
                result = subprocess.run(
                    [exe, "-p", "/usage", "--output-format", "json", "--print-timeout", "10s"],
                    cwd=work,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    env=_child_env(),
                )
            if result.returncode:
                raise RuntimeError()
            groups, metrics, resets = parse_usage_full(json.loads(result.stdout))
            text = " · ".join(groups.values())
            if not text:
                raise ValueError()
            value: dict[str, Any] = {
                "engine": "antigravity",
                "text": text,
                "groups": groups,
                "metrics": metrics,
                "resets": resets,
                "available": True,
                "stale": False,
                "checkedAt": datetime.now(timezone.utc).isoformat(),
            }
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError, subprocess.TimeoutExpired):
            if _CACHE and _CACHE.get("available"):
                value = {
                    **_CACHE,
                    "text": _CACHE["text"].removesuffix(" · 갱신 실패") + " · 갱신 실패",
                    "stale": True,
                }
            else:
                value = {
                    "engine": "antigravity",
                    "text": "Antigravity 한도 조회 실패",
                    "groups": {},
                    "metrics": {},
                    "resets": {},
                    "available": False,
                    "stale": True,
                }
        _CACHE, _CACHE_AT = value, time.monotonic()
        return deepcopy(value)
