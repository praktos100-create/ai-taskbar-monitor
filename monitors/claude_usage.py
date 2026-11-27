from __future__ import annotations

import ctypes
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_BETA = "oauth-2025-04-20"
USAGE_USER_AGENT = "claude-code/2.0.31"
KEYRING_SERVICES = ("Claude Code-credentials", "Claude Code", "Claude")

REFRESH_SECONDS = 60
RATE_LIMIT_BACKOFF_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 15

_FILE_WRITE_LOCK = threading.Lock()


class ClaudeAuthError(RuntimeError):
    """Base error for Claude OAuth credential handling."""


class ClaudeMissingCredentialsError(ClaudeAuthError):
    """Raised when no Claude Code OAuth credentials are present."""


class ClaudeRefreshRejectedError(ClaudeAuthError):
    """Raised when the OAuth token refresh endpoint rejects the refresh token."""


class ClaudeUsageHttpError(RuntimeError):
    """Raised when the usage API returns an HTTP error or transport failure."""

    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]


SendFn = Callable[[str, str, dict[str, str], bytes | None, float], HttpResponse]
KeyringReaderFn = Callable[[str], tuple[str, bytes] | None]


@dataclass(frozen=True)
class OAuthToken:
    access_token: str
    refresh_token: str | None
    expires_at_ms: int
    source_path: Path | None = None

    def is_expired(self, now_ms: int | None = None) -> bool:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        return self.expires_at_ms <= current + 60_000


def credential_paths(claude_dir: str = "") -> list[Path]:
    custom = claude_dir.strip()
    if custom:
        return [Path(custom).expanduser() / ".credentials.json"]

    paths = [Path.home() / ".claude" / ".credentials.json"]
    for env_key in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env_key, "").strip()
        if base:
            base_path = Path(base)
            paths.append(base_path / "Claude" / ".credentials.json")
            paths.append(base_path / "Claude Code" / ".credentials.json")
    return paths


def _read_windows_credential(target: str) -> tuple[str, bytes] | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.windll.advapi32
    cred_ptr = ctypes.POINTER(_CREDENTIALW)()
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None

    CRED_TYPE_GENERIC = 1
    if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
        return None
    try:
        cred = cred_ptr.contents
        user = cred.UserName or ""
        size = int(cred.CredentialBlobSize)
        if size <= 0 or not cred.CredentialBlob:
            blob = b""
        else:
            blob = bytes(ctypes.cast(cred.CredentialBlob, ctypes.POINTER(ctypes.c_ubyte * size)).contents)
        return user, blob
    finally:
        advapi32.CredFree(cred_ptr)


def _decode_credential_blob(blob: bytes) -> list[str]:
    if not blob:
        return []
    candidates: list[str] = []

    utf16_blob = blob[2:] if blob.startswith(b"\xff\xfe") else blob
    if len(utf16_blob) % 2 == 0:
        try:
            text16 = utf16_blob.decode("utf-16-le").rstrip("\x00")
            if text16 and text16.lstrip().startswith("{"):
                candidates.append(text16)
        except UnicodeDecodeError:
            pass

    utf8_blob = blob[3:] if blob.startswith(b"\xef\xbb\xbf") else blob
    try:
        text8 = utf8_blob.decode("utf-8").rstrip("\x00")
        if text8 and text8 not in candidates:
            candidates.append(text8)
    except UnicodeDecodeError:
        pass

    return candidates


def _to_int(val: Any) -> int:
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float) and math.isfinite(val):
        return int(val)
    if isinstance(val, str):
        return int(val.strip())
    raise ValueError("Invalid integer")


def parse_token(raw: str | bytes, source_path: Path | None = None) -> OAuthToken:
    try:
        if isinstance(raw, bytes):
            raw = raw.lstrip(b"\xef\xbb\xbf").decode("utf-8")
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClaudeAuthError("Invalid credentials JSON") from exc

    if not isinstance(data, dict):
        raise ClaudeAuthError("Expected credentials JSON object")

    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth:
        oauth = data

    if "accessToken" not in oauth or "refreshToken" not in oauth or "expiresAt" not in oauth:
        raise ClaudeAuthError("Missing required OAuth credential fields")

    access = oauth["accessToken"]
    if not isinstance(access, str) or not access:
        raise ClaudeAuthError("Invalid OAuth accessToken")

    refresh_val = oauth["refreshToken"]
    if refresh_val is not None and not isinstance(refresh_val, str):
        refresh_val = str(refresh_val)

    try:
        expires_at_ms = _to_int(oauth["expiresAt"])
    except (TypeError, ValueError) as exc:
        raise ClaudeAuthError("Invalid OAuth expiresAt") from exc

    return OAuthToken(
        access_token=access,
        refresh_token=refresh_val,
        expires_at_ms=expires_at_ms,
        source_path=source_path,
    )


def load_credentials(
    claude_dir: str = "",
    username: str | None = None,
    keyring_reader: KeyringReaderFn | None = None,
) -> OAuthToken | None:
    for path in credential_paths(claude_dir):
        if path.is_file():
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise ClaudeAuthError("Failed to read credentials file") from exc
            return parse_token(raw, source_path=path)

    if claude_dir.strip():
        return None

    reader = keyring_reader or _read_windows_credential
    user_name = os.environ.get("USERNAME", "") if username is None else username
    for service in KEYRING_SERVICES:
        for user in ("default", user_name):
            entry = reader(service)
            if entry is not None:
                cred_user, blob = entry
                if not user or cred_user == user:
                    for decoded in _decode_credential_blob(blob):
                        try:
                            return parse_token(decoded, source_path=None)
                        except ClaudeAuthError:
                            continue

            compound = f"{user}@{service}"
            entry2 = reader(compound)
            if entry2 is not None:
                _, blob2 = entry2
                for decoded in _decode_credential_blob(blob2):
                    try:
                        return parse_token(decoded, source_path=None)
                    except ClaudeAuthError:
                        continue

    return None


def save_token(token: OAuthToken) -> None:
    if token.source_path is None:
        return
    path = token.source_path
    with _FILE_WRITE_LOCK:
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    raise ClaudeAuthError("Expected credentials JSON object")
            except json.JSONDecodeError:
                data = {}
            except OSError as exc:
                raise ClaudeAuthError("Failed to read credentials file") from exc

        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            oauth = {}
            data["claudeAiOauth"] = oauth

        oauth["accessToken"] = token.access_token
        oauth["refreshToken"] = token.refresh_token
        oauth["expiresAt"] = token.expires_at_ms

        payload = json.dumps(data, indent=2, ensure_ascii=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise ClaudeAuthError("Failed to persist refreshed credentials") from exc


def _urllib_send(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return HttpResponse(status=int(resp.status), body=resp.read(), headers=resp_headers)
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        err_body = exc.read() if exc.fp is not None else b""
        return HttpResponse(status=int(exc.code), body=err_body, headers=resp_headers)


def _parse_retry_after(headers: dict[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        secs = float(raw.strip())
        if math.isfinite(secs) and secs > 0:
            return secs
    except ValueError:
        return None
    return None


def refresh_token(
    token: OAuthToken,
    send: SendFn = _urllib_send,
    now_ms: int | None = None,
) -> OAuthToken:
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
    ).encode("utf-8")
    resp = send(
        "POST",
        TOKEN_URL,
        {"Content-Type": "application/json"},
        body,
        REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status == 400:
        raise ClaudeRefreshRejectedError("Session expired. Sign in to Claude Code again.")
    if resp.status >= 400:
        raise ClaudeUsageHttpError(resp.status, f"OAuth refresh failed (HTTP {resp.status})")

    try:
        payload = json.loads(resp.body.lstrip(b"\xef\xbb\xbf").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClaudeAuthError("Invalid OAuth refresh response") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise ClaudeAuthError("Invalid OAuth refresh payload")

    expires_in = 3600
    if payload.get("expires_in") is not None:
        try:
            expires_in = max(1, _to_int(payload["expires_in"]))
        except (TypeError, ValueError) as exc:
            raise ClaudeAuthError("Invalid OAuth expires_in") from exc

    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    new_refresh = payload["refresh_token"] if "refresh_token" in payload else token.refresh_token
    if new_refresh is not None and not isinstance(new_refresh, str):
        new_refresh = str(new_refresh)

    refreshed = OAuthToken(
        access_token=payload["access_token"],
        refresh_token=new_refresh,
        expires_at_ms=current_ms + expires_in * 1000,
        source_path=token.source_path,
    )
    save_token(refreshed)
    return refreshed


def _parse_iso_ts(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        ts = float(value)
        return ts / 1000.0 if ts > 1e12 else ts
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def _parse_percent(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raw = 1.0 if value else 0.0
    elif isinstance(value, (int, float)):
        raw = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        raw = float(text)
    else:
        raise ValueError("Invalid usage percentage")
    if not math.isfinite(raw):
        raise ValueError("Non-finite usage percentage")
    return max(0.0, min(100.0, raw))


def parse_limits(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Expected a usage response object")

    metrics: dict[str, float] = {}
    resets: dict[str, float] = {}
    scoped: list[dict[str, Any]] = []

    raw_limits = payload.get("limits")
    if isinstance(raw_limits, list) and raw_limits:
        parsed_any = False
        for item in raw_limits:
            if not isinstance(item, dict):
                continue
            parsed_any = True
            kind = str(item.get("kind") or "")
            pct = _parse_percent(item.get("percent"))
            reset_ts = _parse_iso_ts(item.get("resets_at"))
            is_active = bool(item.get("is_active"))

            if kind == "session":
                metrics["5h"] = pct
                if reset_ts is not None:
                    resets["5h"] = reset_ts
            elif kind == "weekly_all":
                metrics["7d"] = pct
                if reset_ts is not None:
                    resets["7d"] = reset_ts
            elif kind == "weekly_scoped":
                scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
                model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
                name = str(model.get("display_name") or "scoped")
                scoped.append(
                    {
                        "name": name,
                        "used": pct,
                        "resets_at": reset_ts,
                        "is_active": is_active,
                    }
                )

        if "7d" not in metrics and scoped:
            chosen = next((s for s in scoped if s["is_active"]), scoped[0])
            metrics["7d"] = chosen["used"]
            if chosen["resets_at"] is not None:
                resets["7d"] = chosen["resets_at"]

        if parsed_any and ("5h" in metrics or "7d" in metrics or scoped):
            return {"metrics": metrics, "resets": resets, "scoped": scoped}

    five_hour = payload.get("five_hour")
    seven_day = payload.get("seven_day")
    if isinstance(five_hour, dict) or isinstance(seven_day, dict):
        if isinstance(five_hour, dict):
            metrics["5h"] = _parse_percent(five_hour.get("utilization"))
            r5 = _parse_iso_ts(five_hour.get("resets_at"))
            if r5 is not None:
                resets["5h"] = r5
        if isinstance(seven_day, dict):
            metrics["7d"] = _parse_percent(seven_day.get("utilization"))
            r7 = _parse_iso_ts(seven_day.get("resets_at"))
            if r7 is not None:
                resets["7d"] = r7

        for block_key, label in (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet")):
            block = payload.get(block_key)
            if isinstance(block, dict):
                scoped.append(
                    {
                        "name": label,
                        "used": _parse_percent(block.get("utilization")),
                        "resets_at": _parse_iso_ts(block.get("resets_at")),
                        "is_active": True,
                    }
                )
        return {"metrics": metrics, "resets": resets, "scoped": scoped}

    raise ValueError("No recognized Claude usage limit blocks")


@dataclass
class _ClientState:
    token: OAuthToken | None = None
    cache: dict[str, Any] | None = None
    fetched_at: float = 0.0
    backoff_until: float = 0.0


class ClaudeUsageClient:
    """Thread-safe client for Claude Code OAuth usage polling with caching and backoff."""

    def __init__(
        self,
        send: SendFn = _urllib_send,
        keyring_reader: KeyringReaderFn | None = None,
        refresh_seconds: float = REFRESH_SECONDS,
        backoff_seconds: float = RATE_LIMIT_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, _ClientState] = {}
        self._send = send
        self._keyring_reader = keyring_reader
        self._refresh_seconds = refresh_seconds
        self._backoff_seconds = backoff_seconds
        self._clock = clock
        self._wall_clock_ms = wall_clock_ms or (lambda: int(time.time() * 1000))

    def reset_cache(self) -> None:
        with self._lock:
            self._states.clear()

    def _usage_get(self, access_token: str) -> HttpResponse:
        headers = {
            "User-Agent": USAGE_USER_AGENT,
            "anthropic-beta": ANTHROPIC_BETA,
            "Authorization": f"Bearer {access_token}",
        }
        return self._send("GET", USAGE_URL, headers, None, REQUEST_TIMEOUT_SECONDS)

    def _fetch_once(self, token: OAuthToken) -> tuple[dict[str, Any], OAuthToken]:
        now_ms = self._wall_clock_ms()
        if token.is_expired(now_ms):
            token = refresh_token(token, send=self._send, now_ms=now_ms)

        resp = self._usage_get(token.access_token)
        if resp.status == 401:
            token = refresh_token(token, send=self._send, now_ms=self._wall_clock_ms())
            resp = self._usage_get(token.access_token)

        if resp.status == 429:
            retry_after = _parse_retry_after(resp.headers)
            raise ClaudeUsageHttpError(429, "Rate limited (HTTP 429)", retry_after=retry_after)
        if resp.status >= 400:
            raise ClaudeUsageHttpError(resp.status, f"Claude usage HTTP {resp.status}")

        try:
            payload = json.loads(resp.body.lstrip(b"\xef\xbb\xbf").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid Claude usage response JSON") from exc

        parsed = parse_limits(payload)
        return parsed, token

    def snapshot(self, claude_dir: str = "", force: bool = False) -> dict[str, Any]:
        key = claude_dir.strip()
        with self._lock:
            state = self._states.setdefault(key, _ClientState())
            now_mono = self._clock()

            if state.backoff_until > now_mono:
                if state.cache and state.cache.get("available"):
                    return deepcopy({**state.cache, "stale": True})
                return {
                    "engine": "claude_oauth",
                    "available": False,
                    "stale": True,
                    "error": "Claude API 요청 대기 중 (429)",
                }

            if (
                not force
                and state.cache is not None
                and (now_mono - state.fetched_at) < self._refresh_seconds
            ):
                return deepcopy(state.cache)

            had_cached_token = state.token is not None
            try:
                token = state.token
                if token is None:
                    token = load_credentials(key, keyring_reader=self._keyring_reader)
                    if token is None:
                        raise ClaudeMissingCredentialsError("Claude Code 자격 증명 없음")

                try:
                    parsed, updated_token = self._fetch_once(token)
                except ClaudeRefreshRejectedError:
                    if not had_cached_token:
                        raise
                    reloaded = load_credentials(key, keyring_reader=self._keyring_reader)
                    if reloaded is None:
                        raise
                    parsed, updated_token = self._fetch_once(reloaded)

                state.token = updated_token
                value: dict[str, Any] = {
                    "engine": "claude_oauth",
                    "available": True,
                    "stale": False,
                    "metrics": parsed["metrics"],
                    "resets": parsed["resets"],
                    "scoped": parsed["scoped"],
                    "checkedAt": datetime.now(timezone.utc).isoformat(),
                    "error": "",
                }
                state.cache = value
                state.fetched_at = self._clock()
                state.backoff_until = 0.0
                return deepcopy(value)

            except ClaudeUsageHttpError as exc:
                if exc.status == 429:
                    wait = max(self._backoff_seconds, exc.retry_after or 0.0)
                    state.backoff_until = self._clock() + wait
                elif exc.status == 401:
                    state.token = None
                err_msg = str(exc)
            except ClaudeMissingCredentialsError:
                state.token = None
                err_msg = "Claude Code 로그인 필요"
            except ClaudeRefreshRejectedError:
                state.token = None
                err_msg = "Claude 세션 만료 (재로그인 필요)"
            except (ClaudeAuthError, ValueError, OSError, urllib.error.URLError):
                err_msg = "Claude OAuth 사용량 조회 실패"

            if state.cache and state.cache.get("available"):
                stale_val = {
                    **state.cache,
                    "stale": True,
                    "error": err_msg,
                }
                state.cache = stale_val
                state.fetched_at = self._clock()
                return deepcopy(stale_val)

            fail_val: dict[str, Any] = {
                "engine": "claude_oauth",
                "available": False,
                "stale": True,
                "metrics": {},
                "resets": {},
                "scoped": [],
                "error": err_msg,
            }
            state.cache = fail_val
            state.fetched_at = self._clock()
            return deepcopy(fail_val)


_DEFAULT_CLIENT = ClaudeUsageClient()


def reset_cache() -> None:
    _DEFAULT_CLIENT.reset_cache()


def snapshot(claude_dir: str = "", force: bool = False) -> dict[str, Any]:
    return _DEFAULT_CLIENT.snapshot(claude_dir=claude_dir, force=force)
