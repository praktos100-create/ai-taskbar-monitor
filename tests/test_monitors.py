from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from config import AppConfig, load_config, save_config
from monitors import antigravity_usage, claude_usage
from monitors.ai_usage import AIUsageMonitor, format_tokens
from monitors.reset_bell import ResetBellMonitor
from monitors.system_monitor import SystemMonitor


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class TestConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = AppConfig()
        self.assertEqual(cfg.ai_display_mode, "remaining")
        self.assertTrue(cfg.lock_position)

    def test_roundtrip_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg = AppConfig(
                show_claude=False,
                show_gpu=False,
                time_window="5h",
                claude_window="5h",
                codex_window="7d",
                gemini_window="7d",
                ai_display_mode="used_percent",
                layout_mode="one_row",
                theme="light",
                opacity=15,  # clamped to 30
                font_size=20,  # clamped to 14
            )
            save_config(cfg, cfg_path)
            loaded = load_config(cfg_path)
            self.assertFalse(loaded.show_claude)
            self.assertFalse(loaded.show_gpu)
            self.assertEqual(loaded.time_window, "5h")
            self.assertEqual(loaded.claude_window, "5h")
            self.assertEqual(loaded.codex_window, "7d")
            self.assertEqual(loaded.gemini_window, "7d")
            self.assertEqual(loaded.ai_display_mode, "used_percent")
            self.assertEqual(loaded.layout_mode, "one_row")
            self.assertEqual(loaded.theme, "light")
            self.assertEqual(loaded.opacity, 30)
            self.assertEqual(loaded.font_size, 14)

            # Legacy config.json without per-AI window fields inherits time_window
            legacy_path = Path(td) / "legacy_config.json"
            legacy_path.write_text(json.dumps({"time_window": "7d"}), encoding="utf-8")
            legacy_loaded = load_config(legacy_path)
            self.assertEqual(legacy_loaded.claude_window, "7d")
            self.assertEqual(legacy_loaded.codex_window, "7d")
            self.assertEqual(legacy_loaded.gemini_window, "7d")

            # Legacy modes ("percent", "tokens", "cost") map cleanly to "remaining"
            for old_mode in ("percent", "tokens", "cost"):
                legacy = AppConfig(ai_display_mode=old_mode).validate()
                self.assertEqual(legacy.ai_display_mode, "remaining")
            # Legacy time_window "today" maps cleanly to "5h"
            self.assertEqual(AppConfig(time_window="today").validate().time_window, "5h")


class TestAIUsageMonitor(unittest.TestCase):
    def test_format_tokens(self) -> None:
        self.assertEqual(format_tokens(0), "0")
        self.assertEqual(format_tokens(850), "850")
        self.assertEqual(format_tokens(12450), "12.4K")
        self.assertEqual(format_tokens(250_000), "250K")
        self.assertEqual(format_tokens(1_820_000), "1.82M")

    def test_claude_codex_gemini_parsing_and_windows(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            # 1. Claude Code JSONL with streaming duplicates + old entry
            claude_proj = root / ".claude" / "projects" / "demo"
            claude_proj.mkdir(parents=True)
            claude_file = claude_proj / "session1.jsonl"
            claude_lines = [
                # Streaming chunk 1 for msg_1
                json.dumps({
                    "timestamp": _iso(now - 600),
                    "message": {
                        "id": "msg_1",
                        "model": "claude-sonnet-4-5",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "cache_read_input_tokens": 500,
                            "cache_creation_input_tokens": 50,
                        },
                    },
                }),
                # Streaming final chunk for msg_1 (should replace chunk 1, not double count)
                json.dumps({
                    "timestamp": _iso(now - 595),
                    "message": {
                        "id": "msg_1",
                        "model": "claude-sonnet-4-5",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 80,
                            "cache_read_input_tokens": 500,
                            "cache_creation_input_tokens": 50,
                        },
                    },
                }),
                # Old event outside 5h window (2 days ago)
                json.dumps({
                    "timestamp": _iso(now - 2 * 86400),
                    "message": {
                        "id": "msg_old",
                        "model": "claude-sonnet-4-5",
                        "usage": {
                            "input_tokens": 1000,
                            "output_tokens": 500,
                        },
                    },
                }),
            ]
            claude_file.write_text("\n".join(claude_lines), encoding="utf-8")

            # 2. Codex CLI JSONL with duplicate total_token_usage + rate_limits
            codex_sess = root / ".codex" / "sessions" / "2026"
            codex_sess.mkdir(parents=True)
            codex_file = codex_sess / "rollout.jsonl"
            codex_lines = [
                json.dumps({
                    "timestamp": _iso(now - 500),
                    "type": "turn_context",
                    "payload": {"model": "gpt-5-codex"},
                }),
                json.dumps({
                    "timestamp": _iso(now - 490),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 400,
                                "output_tokens": 200,
                                "reasoning_output_tokens": 50,
                            },
                            "last_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 400,
                                "output_tokens": 200,
                            },
                        },
                        "rate_limits": {
                            "primary": {"used_percent": 37.5, "window_minutes": 300},
                            "secondary": {"used_percent": 22.0, "window_minutes": 10080},
                        },
                    },
                }),
                # Exact duplicate status snapshot (must not double count)
                json.dumps({
                    "timestamp": _iso(now - 489),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 400,
                                "output_tokens": 200,
                                "reasoning_output_tokens": 50,
                            },
                            "last_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 400,
                                "output_tokens": 200,
                            },
                        },
                    },
                }),
            ]
            codex_file.write_text("\n".join(codex_lines), encoding="utf-8")

            # 3. Gemini CLI chat JSON (both tokens dict and usageMetadata)
            gemini_chats = root / ".gemini" / "tmp" / "proj1" / "chats"
            gemini_chats.mkdir(parents=True)
            gemini_file = gemini_chats / "session-abc.json"
            gemini_data = {
                "startTime": _iso(now - 300),
                "lastUpdated": _iso(now - 200),
                "messages": [
                    {
                        "timestamp": _iso(now - 250),
                        "model": "gemini-2.5-pro",
                        "tokens": {"input": 300, "output": 120, "cached": 100, "thoughts": 30},
                    },
                    {
                        "timestamp": _iso(now - 200),
                        "model": "gemini-2.5-flash",
                        "usageMetadata": {
                            "promptTokenCount": 200,
                            "candidatesTokenCount": 50,
                            "cachedContentTokenCount": 0,
                        },
                    },
                ],
            }
            gemini_file.write_text(json.dumps(gemini_data), encoding="utf-8")

            # 4. Antigravity transcript.jsonl + transcript_full.jsonl (full must be ignored)
            ag_logs = root / ".gemini" / "antigravity" / "brain" / "conv-1" / ".system_generated" / "logs"
            ag_logs.mkdir(parents=True)
            (ag_logs / "transcript.jsonl").write_text(
                "\n".join([
                    json.dumps({
                        "step_index": 0,
                        "source": "USER_EXPLICIT",
                        "created_at": _iso(now - 120),
                        "content": "hello",
                    }),
                    json.dumps({
                        "step_index": 1,
                        "source": "MODEL",
                        "created_at": _iso(now - 110),
                        "model": "gemini-4-neon-medium",
                        "input_tokens": 1000,
                        "cache_read_tokens": 2000,
                        "output_tokens": 500,
                    }),
                    # Non-Gemini model inside Antigravity must not be added to Gemini total
                    json.dumps({
                        "step_index": 2,
                        "source": "MODEL",
                        "created_at": _iso(now - 100),
                        "model": "claude-sonnet-4-5",
                        "input_tokens": 50000,
                        "cache_read_tokens": 0,
                        "output_tokens": 1000,
                    }),
                ]),
                encoding="utf-8",
            )
            (ag_logs / "transcript_full.jsonl").write_text(
                json.dumps({
                    "step_index": 1,
                    "source": "MODEL",
                    "created_at": _iso(now - 110),
                    "model": "gemini-4-neon-medium",
                    "input_tokens": 999999,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                }),
                encoding="utf-8",
            )

            mon = AIUsageMonitor()
            snap_5h = mon.scan(
                time_window="5h",
                claude_dir=str(root / ".claude"),
                codex_dir=str(root / ".codex"),
                gemini_dir=str(root / ".gemini"),
                now_ts=now,
                include_agy=False,
            )

            # Claude 5h: raw=730, quota_tokens=280; 7d: raw=2230, quota_tokens=1780
            self.assertEqual(snap_5h.claude.total_tokens, 730)
            self.assertEqual(snap_5h.claude.tokens_5h, 730)
            self.assertEqual(snap_5h.claude.tokens_7d, 2230)
            self.assertEqual(snap_5h.claude.quota_tokens_5h, 280)
            self.assertEqual(snap_5h.claude.quota_tokens_7d, 1780)
            self.assertEqual(snap_5h.claude.output_tokens, 80)
            self.assertEqual(snap_5h.claude.sessions, 1)
            self.assertAlmostEqual(snap_5h.claude.remaining_5h_percent(560), 50.0, places=2)
            self.assertAlmostEqual(snap_5h.claude.remaining_7d_percent(3560), 50.0, places=2)

            # Codex 5h: 1200; primary 37.5 -> 62.5% remaining, secondary 22.0 -> 78.0% remaining
            self.assertEqual(snap_5h.codex.total_tokens, 1200)
            self.assertEqual(snap_5h.codex.rate_limit_percent, 37.5)
            self.assertEqual(snap_5h.codex.rate_limit_7d_percent, 22.0)
            self.assertEqual(snap_5h.codex.rate_limit_label, "5h 한도")
            self.assertAlmostEqual(snap_5h.codex.remaining_5h_percent(5_000_000), 62.5, places=2)
            self.assertAlmostEqual(snap_5h.codex.remaining_7d_percent(50_000_000), 78.0, places=2)

            # Gemini 5h: CLI raw=800 (quota=710) + AGY raw=3500 (quota=1700) -> raw=4300, quota=2410
            self.assertEqual(snap_5h.gemini.total_tokens, 4300)
            self.assertEqual(snap_5h.gemini.quota_tokens_5h, 2410)
            self.assertEqual(snap_5h.gemini.quota_tokens_7d, 2410)
            self.assertEqual(snap_5h.gemini.sessions, 2)
            self.assertAlmostEqual(snap_5h.gemini.remaining_5h_percent(4820), 50.0, places=2)

            # 7d window includes old Claude message: 730 + 1500 = 2230
            snap_7d = mon.scan(
                time_window="7d",
                claude_dir=str(root / ".claude"),
                codex_dir=str(root / ".codex"),
                gemini_dir=str(root / ".gemini"),
                now_ts=now,
                include_agy=False,
            )
            self.assertEqual(snap_7d.claude.total_tokens, 2230)

    def test_codex_168h_weekly_and_expired_reset(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            codex_sess = root / ".codex" / "sessions"
            codex_sess.mkdir(parents=True)
            # Case A: primary carries 168h (10080m) weekly limit with future resets_at -> must map to 7d
            (codex_sess / "weekly_only.jsonl").write_text(
                json.dumps({
                    "timestamp": _iso(now - 60),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
                        "rate_limits": {
                            "primary": {
                                "used_percent": 86.0,
                                "window_minutes": 10080,
                                "resets_at": _iso(now + 3600),
                            }
                        },
                    },
                }),
                encoding="utf-8",
            )
            mon = AIUsageMonitor()
            snap = mon.scan(
                claude_dir=str(root / ".claude"),
                codex_dir=str(root / ".codex"),
                gemini_dir=str(root / ".gemini"),
                now_ts=now,
                include_agy=False,
            )
            self.assertIsNone(snap.codex.rate_limit_percent)
            self.assertEqual(snap.codex.rate_limit_7d_percent, 86.0)
            self.assertAlmostEqual(snap.codex.remaining_7d_percent(30_000_000), 14.0, places=2)
            self.assertAlmostEqual(snap.codex.primary_remaining_percent(3_000_000, 30_000_000), 14.0, places=2)

            # Case B: expired resets_at <= now resets used_percent to 0.0 (100% remaining)
            weekly_log = codex_sess / "weekly_only.jsonl"
            previous_mtime = weekly_log.stat().st_mtime
            weekly_log.write_text(
                json.dumps({
                    "timestamp": _iso(now - 10),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20}},
                        "rate_limits": {
                            "primary": {
                                "used_percent": 99.0,
                                "window_minutes": 10080,
                                "resets_at": _iso(now - 5),
                            }
                        },
                    },
                }),
                encoding="utf-8",
            )
            # A same-size rewrite can share a timestamp on fast Windows runners.
            os.utime(weekly_log, (now, previous_mtime + 2))
            snap2 = mon.scan(
                claude_dir=str(root / ".claude"),
                codex_dir=str(root / ".codex"),
                gemini_dir=str(root / ".gemini"),
                now_ts=now,
                include_agy=False,
            )
            self.assertEqual(snap2.codex.rate_limit_7d_percent, 0.0)
            self.assertAlmostEqual(snap2.codex.primary_remaining_percent(3_000_000, 30_000_000), 100.0, places=2)


class TestAntigravityUsage(unittest.TestCase):
    def setUp(self) -> None:
        antigravity_usage.reset_cache()

    def tearDown(self) -> None:
        antigravity_usage.reset_cache()

    def test_parse_usage_groups_whitelist_and_clamping(self) -> None:
        payload = {
            "account": {"email": "secret@example.com"},
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {"window": "weekly", "remaining_fraction": 0.93, "reset_time": "2026-12-03T16:40:00Z"},
                                {"window": "5h", "remaining_fraction": 0.8, "reset_in_seconds": 7380},
                                {"window": "5h", "remaining_fraction": True},  # bool ignored
                                {"window": "weekly", "remaining_fraction": math.nan},
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [
                                {"window": "weekly", "remaining_fraction": 1.25},  # clamped 100%
                                {"window": "5h", "remaining_fraction": -0.1},     # clamped 0%
                            ],
                        },
                        {
                            "name": "Internal Debug Group",
                            "buckets": [{"window": "5h", "remaining_fraction": 0.5}],
                        },
                    ]
                }
            },
        }
        groups, metrics, resets = antigravity_usage.parse_usage_full(payload, now_ts=1_795_700_000.0)
        self.assertEqual(groups["gemini"], "Gemini 주간 93% / 5시간 80% 남음")
        self.assertEqual(groups["claudeGpt"], "Claude·GPT 주간 100% / 5시간 0% 남음")
        self.assertNotIn("Internal Debug Group", groups)
        self.assertAlmostEqual(metrics["gemini"]["weekly"], 93.0, places=2)
        self.assertAlmostEqual(metrics["gemini"]["5h"], 80.0, places=2)
        self.assertAlmostEqual(resets["gemini"]["5h"], 1_795_707_380.0, places=2)
        self.assertIn("weekly", resets["gemini"])

    def test_child_env_filters_secrets(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-secret",
                "ANTHROPIC_API_KEY": "sk-ant-secret",
                "GEMINI_API_KEY": "AIza-secret",
                "PATH": os.environ.get("PATH", ""),
            },
            clear=False,
        ):
            env = antigravity_usage._child_env()
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("GEMINI_API_KEY", env)
            self.assertIn("PATH", env)

    def test_snapshot_caching_force_and_stale_fallback(self) -> None:
        ok_stdout = json.dumps({
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {"window": "weekly", "remaining_fraction": 0.74},
                                {"window": "5h", "remaining_fraction": 0.96},
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [
                                {"window": "weekly", "remaining_fraction": 0.88},
                                {"window": "5h", "remaining_fraction": 0.91},
                            ],
                        },
                    ]
                }
            }
        })
        proc_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=ok_stdout, stderr="")
        with mock.patch.object(antigravity_usage, "find_agy", return_value="agy.exe"), mock.patch.object(
            antigravity_usage.subprocess, "run", return_value=proc_ok
        ) as run_mock:
            # Cold failure first (separate cache)
            run_mock.side_effect = subprocess.TimeoutExpired(cmd="agy", timeout=15)
            cold_fail = antigravity_usage.snapshot()
            self.assertFalse(cold_fail["available"])
            self.assertTrue(cold_fail["stale"])
            self.assertEqual(cold_fail["text"], "Antigravity 한도 조회 실패")

            # Force success
            run_mock.side_effect = None
            run_mock.return_value = proc_ok
            ok = antigravity_usage.snapshot(force=True)
            self.assertTrue(ok["available"])
            self.assertFalse(ok["stale"])
            self.assertEqual(ok["groups"]["gemini"], "Gemini 주간 74% / 5시간 96% 남음")
            calls_after_ok = run_mock.call_count

            # Cached within 15s (does not call subprocess.run again)
            cached = antigravity_usage.snapshot(force=False)
            self.assertEqual(run_mock.call_count, calls_after_ok)
            self.assertEqual(cached["text"], ok["text"])

            # Failure on force=True preserves stale cache with suffix
            run_mock.side_effect = RuntimeError("internal error details must not leak")
            stale = antigravity_usage.snapshot(force=True)
            self.assertTrue(stale["available"])
            self.assertTrue(stale["stale"])
            self.assertTrue(stale["text"].endswith(" · 갱신 실패"))
            self.assertNotIn("internal error details", stale["text"])

    def test_ai_usage_monitor_integrates_agy_snapshot(self) -> None:
        now = time.time()
        fake_snap = {
            "engine": "antigravity",
            "text": "Gemini 주간 74% / 5시간 96% 남음 · Claude·GPT 주간 80% / 5시간 90% 남음",
            "groups": {
                "gemini": "Gemini 주간 74% / 5시간 96% 남음",
                "claudeGpt": "Claude·GPT 주간 80% / 5시간 90% 남음",
            },
            "metrics": {
                "gemini": {"weekly": 74.0, "5h": 96.0},
                "claudeGpt": {"weekly": 80.0, "5h": 90.0},
            },
            "resets": {
                "gemini": {"5h": now + 3700, "weekly": now + 2 * 86400 + 3600},
                "claudeGpt": {"5h": now + 1800},
            },
            "available": True,
            "stale": False,
        }
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            antigravity_usage, "snapshot", return_value=fake_snap
        ):
            mon = AIUsageMonitor()
            snap = mon.scan(
                claude_dir=str(Path(td) / ".claude"),
                codex_dir=str(Path(td) / ".codex"),
                gemini_dir=str(Path(td) / ".gemini"),
                now_ts=now,
                include_agy=True,
            )
            self.assertAlmostEqual(snap.gemini.remaining_5h_percent(3_000_000), 96.0, places=2)
            self.assertAlmostEqual(snap.gemini.remaining_7d_percent(30_000_000), 74.0, places=2)
            self.assertEqual(snap.gemini.rate_limit_source, "antigravity")
            self.assertAlmostEqual(snap.gemini.rate_limit_resets_at, now + 3700, places=1)
            self.assertAlmostEqual(snap.gemini.rate_limit_7d_resets_at, now + 2 * 86400 + 3600, places=1)
            self.assertAlmostEqual(snap.claude.remaining_5h_percent(3_000_000), 90.0, places=2)
            self.assertAlmostEqual(snap.claude.remaining_7d_percent(30_000_000), 80.0, places=2)
            self.assertAlmostEqual(snap.claude.rate_limit_resets_at, now + 1800, places=1)

    def test_format_reset_time(self) -> None:
        from ui.detail_popup import _format_reset_time

        now = 1_795_700_000.0
        self.assertEqual(_format_reset_time(None, now), "")
        self.assertEqual(_format_reset_time(now - 10, now), "곧 리셋")
        self.assertIn("30분 후", _format_reset_time(now + 1800, now))
        self.assertIn("2시간 3분 후", _format_reset_time(now + 7380, now))
        self.assertIn("2일 3시간 후", _format_reset_time(now + 2 * 86400 + 3 * 3600, now))


class TestClaudeOAuthUsage(unittest.TestCase):
    def test_parse_limits_current_and_legacy(self) -> None:
        current = claude_usage.parse_limits({
            "limits": [
                {"kind": "session", "percent": 37, "resets_at": "2026-09-21T18:30:00Z", "is_active": True},
                {"kind": "weekly_scoped", "percent": 14, "scope": {"model": {"display_name": "Opus"}}, "is_active": False},
            ]
        })
        self.assertAlmostEqual(current["metrics"]["5h"], 37.0, places=2)
        self.assertAlmostEqual(current["metrics"]["7d"], 14.0, places=2)
        self.assertEqual(current["scoped"][0]["name"], "Opus")
        self.assertIn("5h", current["resets"])

        legacy = claude_usage.parse_limits({
            "five_hour": {"utilization": 12.5, "resets_at": "2026-09-21T18:30:00Z"},
            "seven_day": {"utilization": 48.0, "resets_at": None},
            "seven_day_sonnet": {"utilization": 22.0, "resets_at": None},
        })
        self.assertAlmostEqual(legacy["metrics"]["5h"], 12.5, places=2)
        self.assertAlmostEqual(legacy["metrics"]["7d"], 48.0, places=2)
        self.assertEqual(legacy["scoped"][0]["name"], "Sonnet")

    def test_load_credentials_file_and_keyring_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            self.assertIsNone(claude_usage.load_credentials(claude_dir=str(cdir)))

            cred_file = cdir / ".credentials.json"
            cred_file.write_text("not json", encoding="utf-8")
            with self.assertRaises(claude_usage.ClaudeAuthError):
                claude_usage.load_credentials(claude_dir=str(cdir))

            cred_file.write_text(
                json.dumps({
                    "claudeAiOauth": {
                        "accessToken": "tok-file",
                        "refreshToken": "ref-file",
                        "expiresAt": 4_102_444_800_000,
                    }
                }),
                encoding="utf-8",
            )
            loaded = claude_usage.load_credentials(claude_dir=str(cdir))
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.access_token, "tok-file")
            self.assertEqual(loaded.source_path, cred_file)

        # Keyring fallback (UTF-16LE and UTF-8) when no credentials file exists
        payload_json = json.dumps({
            "claudeAiOauth": {
                "accessToken": "tok-kr",
                "refreshToken": "ref-kr",
                "expiresAt": "4102444800000",
            }
        })
        utf16_blob = b"\xff\xfe" + payload_json.encode("utf-16-le")
        with mock.patch.object(claude_usage, "credential_paths", return_value=[]):
            kr_tok = claude_usage.load_credentials(
                username="tester",
                keyring_reader=lambda target: ("tester", utf16_blob) if target == "Claude Code" else None,
            )
            self.assertIsNotNone(kr_tok)
            self.assertEqual(kr_tok.access_token, "tok-kr")
            self.assertIsNone(kr_tok.source_path)

            utf8_blob = payload_json.encode("utf-8")
            kr_tok2 = claude_usage.load_credentials(
                username="tester",
                keyring_reader=lambda target: ("other", utf8_blob) if target == "default@Claude Code-credentials" else None,
            )
            self.assertIsNotNone(kr_tok2)
            self.assertEqual(kr_tok2.access_token, "tok-kr")

    def test_refresh_save_401_retry_and_429_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            cred_file = cdir / ".credentials.json"
            cred_file.write_text(
                json.dumps({
                    "unrelated": {"keep": 123},
                    "claudeAiOauth": {
                        "keepOauth": True,
                        "accessToken": "expired-tok",
                        "refreshToken": "ref-1",
                        "expiresAt": 1000,
                    },
                }),
                encoding="utf-8",
            )

            calls: list[tuple[str, str, str]] = []
            script = [
                # 1. Expired -> refresh POST -> returns tok-2 + ref-2
                claude_usage.HttpResponse(
                    200,
                    json.dumps({"access_token": "tok-2", "refresh_token": "ref-2", "expires_in": 3600}).encode(),
                    {},
                ),
                # 2. Usage GET with tok-2 -> 401
                claude_usage.HttpResponse(401, b"{}", {}),
                # 3. Refresh POST with ref-2 -> returns tok-3
                claude_usage.HttpResponse(
                    200,
                    json.dumps({"access_token": "tok-3", "expires_in": 3600}).encode(),
                    {},
                ),
                # 4. Usage GET with tok-3 -> 200 OK
                claude_usage.HttpResponse(
                    200,
                    json.dumps({
                        "limits": [
                            {"kind": "session", "percent": 24.0, "resets_at": "2026-09-21T18:30:00Z", "is_active": True},
                            {"kind": "weekly_all", "percent": 61.0, "resets_at": "2026-09-26T00:00:00Z", "is_active": True},
                        ]
                    }).encode(),
                    {},
                ),
            ]

            def fake_send(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
                auth = headers.get("Authorization", "")
                calls.append((method, url, auth))
                return script.pop(0)

            now_mono = [100.0]
            client = claude_usage.ClaudeUsageClient(
                send=fake_send,
                refresh_seconds=60,
                backoff_seconds=120,
                clock=lambda: now_mono[0],
                wall_clock_ms=lambda: 1_000_000,
            )

            snap = client.snapshot(claude_dir=str(cdir))
            self.assertTrue(snap["available"])
            self.assertFalse(snap["stale"])
            self.assertAlmostEqual(snap["metrics"]["5h"], 24.0, places=2)
            self.assertAlmostEqual(snap["metrics"]["7d"], 61.0, places=2)
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[3][2], "Bearer tok-3")

            saved = json.loads(cred_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["unrelated"], {"keep": 123})
            self.assertTrue(saved["claudeAiOauth"]["keepOauth"])
            self.assertEqual(saved["claudeAiOauth"]["accessToken"], "tok-3")
            self.assertEqual(saved["claudeAiOauth"]["refreshToken"], "ref-2")

            # Rotate credentials on disk; cached token gets 401 then 400 invalid_grant -> reloads disk token
            cred_file.write_text(
                json.dumps({
                    "claudeAiOauth": {
                        "accessToken": "tok-reloaded",
                        "refreshToken": "ref-reloaded",
                        "expiresAt": 4_102_444_800_000,
                    }
                }),
                encoding="utf-8",
            )
            script.extend([
                claude_usage.HttpResponse(401, b"{}", {}),
                claude_usage.HttpResponse(400, b'{"error":"invalid_grant","secret":"should-not-leak"}', {}),
                claude_usage.HttpResponse(
                    200,
                    json.dumps({"five_hour": {"utilization": 55.0}, "seven_day": {"utilization": 70.0}}).encode(),
                    {},
                ),
            ])
            snap2 = client.snapshot(claude_dir=str(cdir), force=True)
            self.assertTrue(snap2["available"])
            self.assertAlmostEqual(snap2["metrics"]["5h"], 55.0, places=2)
            self.assertAlmostEqual(snap2["metrics"]["7d"], 70.0, places=2)
            self.assertEqual(calls[-1][2], "Bearer tok-reloaded")

            # 429 triggers backoff and serves stale cached snapshot without secret leakage
            script.append(claude_usage.HttpResponse(429, b"secret-rate-limit-body", {"Retry-After": "180"}))
            snap429 = client.snapshot(claude_dir=str(cdir), force=True)
            self.assertTrue(snap429["available"])
            self.assertTrue(snap429["stale"])
            self.assertAlmostEqual(snap429["metrics"]["5h"], 55.0, places=2)
            self.assertNotIn("secret-rate-limit-body", snap429["error"])

            # During backoff, no network request is issued even with force=True
            prev_call_count = len(calls)
            now_mono[0] += 30.0
            snap_backoff = client.snapshot(claude_dir=str(cdir), force=True)
            self.assertEqual(len(calls), prev_call_count)
            self.assertTrue(snap_backoff["available"])
            self.assertTrue(snap_backoff["stale"])

    def test_ai_usage_monitor_prioritizes_claude_oauth_over_antigravity(self) -> None:
        fake_claude = {
            "engine": "claude_oauth",
            "available": True,
            "stale": False,
            "metrics": {"5h": 18.0, "7d": 44.0},
            "resets": {"5h": 1_800_000_000.0, "7d": 1_800_500_000.0},
            "scoped": [{"name": "Opus", "used": 12.0, "resets_at": None, "is_active": True}],
            "error": "",
        }
        fake_agy = {
            "engine": "antigravity",
            "text": "Gemini 주간 74% / 5시간 96% 남음 · Claude·GPT 주간 80% / 5시간 90% 남음",
            "groups": {
                "gemini": "Gemini 주간 74% / 5시간 96% 남음",
                "claudeGpt": "Claude·GPT 주간 80% / 5시간 90% 남음",
            },
            "metrics": {
                "gemini": {"weekly": 74.0, "5h": 96.0},
                "claudeGpt": {"weekly": 80.0, "5h": 90.0},
            },
            "available": True,
            "stale": False,
        }
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            claude_usage, "snapshot", return_value=fake_claude
        ), mock.patch.object(antigravity_usage, "snapshot", return_value=fake_agy):
            mon = AIUsageMonitor()
            snap = mon.scan(
                claude_dir=str(Path(td) / ".claude"),
                codex_dir=str(Path(td) / ".codex"),
                gemini_dir=str(Path(td) / ".gemini"),
                include_agy=True,
                include_claude_oauth=True,
            )
            self.assertEqual(snap.claude.rate_limit_source, "claude_oauth")
            self.assertAlmostEqual(snap.claude.used_5h_percent(3_000_000), 18.0, places=2)
            self.assertAlmostEqual(snap.claude.remaining_5h_percent(3_000_000), 82.0, places=2)
            self.assertAlmostEqual(snap.claude.used_7d_percent(30_000_000), 44.0, places=2)
            self.assertAlmostEqual(snap.claude.remaining_7d_percent(30_000_000), 56.0, places=2)
            self.assertEqual(snap.claude.rate_limit_resets_at, 1_800_000_000.0)
            self.assertEqual(snap.claude.extra_limit_note, "Opus 12%")
            # Gemini still comes from Antigravity
            self.assertEqual(snap.gemini.rate_limit_source, "antigravity")
            self.assertAlmostEqual(snap.gemini.remaining_5h_percent(3_000_000), 96.0, places=2)


class TestResetBellMonitor(unittest.TestCase):
    def test_first_baseline_new_upcoming_completed_prediction_and_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            mon = ResetBellMonitor(state_path=state_path)

            # 1. First check: records existing event without notifying
            p1 = {
                "data": {
                    "upcoming": [{"id": "ev-1", "summary": "Scheduled reset", "expected_at": "2026-11-28T09:00:00Z"}],
                    "completed": [{"id": "ev-0", "summary": "Past reset", "completed_at": "2026-11-26T09:00:00Z"}],
                    "active_watch": {"level": "strong", "reset_chance_percent": 60, "forecast_window": "next 48h"},
                }
            }
            s1 = mon.check(include_predictions=False, fetcher=lambda _u: p1)
            self.assertTrue(s1.ok)
            self.assertEqual(len(s1.new_notifications), 0)
            self.assertTrue(state_path.exists())

            # 2. Second check: new upcoming event -> 1 notification; prediction ignored when False
            p2 = {
                "data": {
                    "upcoming": [
                        {"id": "ev-1", "summary": "Scheduled reset", "expected_at": "2026-11-28T09:00:00Z"},
                        {"id": "ev-2", "summary": "Global reset", "expected_at": "2026-11-29T09:00:00Z"},
                    ],
                    "completed": [{"id": "ev-0", "summary": "Past reset", "completed_at": "2026-11-26T09:00:00Z"}],
                    "active_watch": {"level": "strong", "reset_chance_percent": 80, "forecast_window": "next 24h"},
                }
            }
            s2 = mon.check(include_predictions=False, fetcher=lambda _u: p2)
            self.assertEqual(len(s2.new_notifications), 1)
            self.assertEqual(s2.new_notifications[0].event_id, "ev-2")
            self.assertEqual(s2.new_notifications[0].kind, "upcoming")

            # 3. Third check: ev-2 moves upcoming -> completed AND prediction enabled -> 2 notifications
            p3 = {
                "data": {
                    "upcoming": [{"id": "ev-1", "summary": "Scheduled reset", "expected_at": "2026-11-28T09:00:00Z"}],
                    "completed": [{"id": "ev-2", "summary": "Global reset", "completed_at": "2026-11-29T09:05:00Z"}],
                    "active_watch": {"level": "high", "reset_chance_percent": 90, "forecast_window": "next 12h"},
                }
            }
            s3 = mon.check(include_predictions=True, fetcher=lambda _u: p3)
            kinds = {n.kind for n in s3.new_notifications}
            self.assertIn("completed", kinds)
            self.assertIn("prediction", kinds)

            # 4. Network error handled gracefully
            def _fail(_u: str):
                raise RuntimeError("offline")

            s4 = mon.check(include_predictions=True, fetcher=_fail)
            self.assertFalse(s4.ok)
            self.assertEqual(s4.new_notifications, [])


class TestSystemMonitor(unittest.TestCase):
    def test_cpu_and_ram_sample(self) -> None:
        mon = SystemMonitor()
        time.sleep(0.05)
        m = mon.sample()
        self.assertGreaterEqual(m.cpu_percent, 0.0)
        self.assertLessEqual(m.cpu_percent, 100.0)
        self.assertGreaterEqual(m.ram_percent, 0.0)
        self.assertLessEqual(m.ram_percent, 100.0)
        self.assertGreater(m.ram_total_gb, 0.0)
        if m.gpu_percent is not None:
            self.assertGreaterEqual(m.gpu_percent, 0.0)
            self.assertLessEqual(m.gpu_percent, 100.0)


class TestHeadlessUI(unittest.TestCase):
    def test_widgets_instantiate_and_update(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QCoreApplication, QEvent, Qt
        from PySide6.QtWidgets import QApplication, QSizePolicy
        from ui.settings_dialog import SettingsDialog
        from ui.taskbar_bar import TaskbarOverlayWidget

        app = QApplication.instance() or QApplication([])
        cfg = AppConfig()
        widget = TaskbarOverlayWidget(cfg)
        try:
            widget._on_sys_tick()
            self.assertTrue(widget.pill_claude.mini_bar._dual)
            self.assertTrue(widget.pill_gemini.mini_bar._dual)
            self.assertTrue(widget.pill_codex.mini_bar._dual)
            self.assertFalse(widget.pill_cpu.mini_bar._dual)
            self.assertEqual(widget.pill_claude.sizePolicy().horizontalPolicy(), QSizePolicy.Fixed)
            self.assertEqual(widget.pill_gpu.sizePolicy().horizontalPolicy(), QSizePolicy.Fixed)

            # Balanced 2-row packing: 4 enabled items -> 2 top + 2 bottom
            cfg_4 = AppConfig(
                show_claude=True,
                show_codex=True,
                show_gemini=True,
                show_cpu=False,
                show_ram=False,
                show_gpu=True,
                layout_mode="two_row",
            )
            widget._apply_config(cfg_4, save=False)
            self.assertEqual(widget.row1_layout.count(), 2)
            self.assertEqual(widget.row2_layout.count(), 2)
            self.assertFalse(widget.row2.isHidden())

            # 1 enabled item -> 1 row only
            cfg_1 = AppConfig(
                show_claude=False,
                show_codex=False,
                show_gemini=True,
                show_cpu=False,
                show_ram=False,
                show_gpu=False,
                layout_mode="two_row",
            )
            widget._apply_config(cfg_1, save=False)
            self.assertEqual(widget.row1_layout.count(), 1)
            self.assertTrue(widget.row2.isHidden())

            # Test one_row layout & used_percent mode
            cfg2 = AppConfig(
                show_claude=True,
                show_codex=False,
                show_gemini=True,
                show_cpu=True,
                show_ram=True,
                show_gpu=False,
                layout_mode="one_row",
                theme="transparent",
                time_window="7d",
                ai_display_mode="used_percent",
            )
            widget._apply_config(cfg2, save=False)
            self.assertEqual(widget._theme_colors()[2], "#000000")
            self.assertEqual(widget._theme_colors()[3], "#000000")
            widget.popup.update_data(cfg2, widget._latest_ai, widget._latest_sys)

            cfg_hidden = AppConfig(
                show_claude=True,
                show_codex=True,
                show_gemini=True,
                show_cpu=True,
                show_ram=True,
                show_gpu=False,
                layout_mode="one_row",
                theme="transparent",
                claude_window="5h",
                codex_window="7d",
                gemini_window="5h",
                ai_display_mode="used_percent",
                claude_budget_5h=4_500_000,
                ai_interval_sec=25,
            )
            dlg = SettingsDialog(cfg_hidden)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            built = dlg.build_config()
            self.assertEqual(built.layout_mode, "one_row")
            self.assertEqual(built.claude_window, "5h")
            self.assertEqual(built.codex_window, "7d")
            self.assertEqual(built.gemini_window, "5h")
            self.assertEqual(built.ai_display_mode, "used_percent")
            self.assertTrue(built.show_codex)
            self.assertFalse(built.show_gpu)
            self.assertEqual(built.claude_budget_5h, 4_500_000)
            self.assertEqual(built.ai_interval_sec, 25)

            idx_7d = dlg.cmb_claude_window.findData("7d")
            idx_5h = dlg.cmb_codex_window.findData("5h")
            self.assertGreaterEqual(idx_7d, 0)
            self.assertGreaterEqual(idx_5h, 0)
            dlg.cmb_claude_window.setCurrentIndex(idx_7d)
            dlg.cmb_codex_window.setCurrentIndex(idx_5h)
            built2 = dlg.build_config()
            self.assertEqual(built2.claude_window, "7d")
            self.assertEqual(built2.codex_window, "5h")
            self.assertEqual(built2.gemini_window, "5h")
            dlg.close()

            # Mixed per-AI window rendering in taskbar pills
            from monitors.ai_usage import AIProviderStats, AIUsageSnapshot

            widget._apply_config(
                AppConfig(
                    claude_window="5h",
                    codex_window="7d",
                    gemini_window="5h",
                    ai_display_mode="remaining",
                ),
                save=False,
            )
            widget._latest_ai = AIUsageSnapshot(
                claude=AIProviderStats(name="Claude", short_label="CLD", color="#E88C5D", quota_tokens_5h=300_000, quota_tokens_7d=15_000_000),
                codex=AIProviderStats(name="Codex", short_label="GPT", color="#34D399", rate_limit_percent=10.0, rate_limit_7d_percent=40.0),
                gemini=AIProviderStats(name="Gemini", short_label="GEM", color="#60A5FA", quota_tokens_5h=600_000, quota_tokens_7d=6_000_000),
                updated_at=time.time(),
            )
            widget._refresh_display_values()
            self.assertEqual(widget.pill_claude.val_lbl.text(), "90%")
            self.assertEqual(widget.pill_codex.val_lbl.text(), "60%")
            self.assertEqual(widget.pill_gemini.val_lbl.text(), "80%")

            menu = widget._build_context_menu()
            win_actions = [a for a in menu.actions() if a.text() == "% 표시 기준"]
            self.assertEqual(len(win_actions), 1)
            sub_titles = [a.text() for a in win_actions[0].menu().actions() if not a.isSeparator()]
            self.assertEqual(sub_titles, ["Claude", "ChatGPT (Codex)", "Gemini", "모두 5시간 (5h)", "모두 주간 (7일)"])
        finally:
            widget.popup.close()
            widget.popup.deleteLater()
            widget.close()
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main(verbosity=2)
