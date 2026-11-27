from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AITaskbarMonitor"
    if getattr(sys, "frozen", False)
    else APP_DIR
)
CONFIG_PATH = DATA_DIR / "config.json"
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_REG_NAME = "AITaskbarMonitor"


@dataclass
class AppConfig:
    # Visibility toggles
    show_claude: bool = True
    show_codex: bool = True
    show_gemini: bool = True
    show_cpu: bool = True
    show_ram: bool = True
    show_gpu: bool = True

    # AI usage options
    time_window: str = "5h"            # "5h" | "7d" | "today"
    claude_window: str = "5h"          # "5h" | "7d"
    codex_window: str = "5h"           # "5h" | "7d"
    gemini_window: str = "5h"          # "5h" | "7d"
    ai_display_mode: str = "remaining" # "remaining" | "used_percent" | "tokens" | "cost"
    claude_budget_5h: int = 3_000_000
    claude_budget_7d: int = 30_000_000
    codex_budget_5h: int = 3_000_000
    codex_budget_7d: int = 30_000_000
    gemini_budget_5h: int = 3_000_000
    gemini_budget_7d: int = 30_000_000
    claude_dir: str = ""
    codex_dir: str = ""
    gemini_dir: str = ""

    # UI / TrafficMonitor options
    layout_mode: str = "two_row"       # "two_row" | "one_row"
    theme: str = "dark"                # "dark" | "light" | "transparent"
    opacity: int = 92                  # 30..100
    font_size: int = 9                 # 7..14
    dock_to_taskbar: bool = True
    lock_position: bool = True
    hide_on_fullscreen: bool = True
    pos_x: int | None = None
    pos_y: int | None = None
    sys_interval_ms: int = 1000
    ai_interval_sec: int = 15
    run_at_startup: bool = False

    # Codex reset announcement bell (codex-resets.com)
    enable_reset_bell: bool = True
    reset_bell_interval_min: int = 5   # 1 | 5 | 10
    reset_bell_sound: bool = True
    reset_bell_predictions: bool = False

    def validate(self) -> "AppConfig":
        if self.time_window not in ("5h", "7d"):
            self.time_window = "5h"
        for attr in ("claude_window", "codex_window", "gemini_window"):
            if getattr(self, attr) not in ("5h", "7d"):
                setattr(self, attr, self.time_window)
        if self.ai_display_mode not in ("remaining", "used_percent"):
            self.ai_display_mode = "remaining"
        if self.layout_mode not in ("two_row", "one_row"):
            self.layout_mode = "two_row"
        if self.theme not in ("dark", "light", "transparent"):
            self.theme = "dark"
        self.opacity = max(30, min(100, int(self.opacity)))
        self.font_size = max(7, min(14, int(self.font_size)))
        self.claude_budget_5h = max(1000, int(self.claude_budget_5h))
        self.claude_budget_7d = max(1000, int(self.claude_budget_7d))
        self.codex_budget_5h = max(1000, int(self.codex_budget_5h))
        self.codex_budget_7d = max(1000, int(self.codex_budget_7d))
        self.gemini_budget_5h = max(1000, int(self.gemini_budget_5h))
        self.gemini_budget_7d = max(1000, int(self.gemini_budget_7d))
        self.sys_interval_ms = max(250, min(10000, int(self.sys_interval_ms)))
        self.ai_interval_sec = max(3, min(300, int(self.ai_interval_sec)))
        if int(self.reset_bell_interval_min) not in (1, 5, 10):
            self.reset_bell_interval_min = 5
        else:
            self.reset_bell_interval_min = int(self.reset_bell_interval_min)
        return self


def load_config(path: Path | None = None) -> AppConfig:
    target = path or CONFIG_PATH
    if not target.exists():
        return AppConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return AppConfig()
        defaults = asdict(AppConfig())
        filtered = {k: raw[k] for k in defaults if k in raw}
        fallback_win = raw.get("time_window", "5h")
        for key in ("claude_window", "codex_window", "gemini_window"):
            if key not in filtered:
                filtered[key] = fallback_win
        return AppConfig(**filtered).validate()
    except Exception:
        return AppConfig()


def save_config(cfg: AppConfig, path: Path | None = None) -> None:
    target = path or CONFIG_PATH
    cfg.validate()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def set_windows_startup(enabled: bool, target_path: str | None = None) -> tuple[bool, str]:
    """Register or remove Windows HKCU Run entry for auto-start."""
    if sys.platform != "win32":
        return False, "Windows only"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_REG_PATH,
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        ) as key:
            if enabled:
                if not target_path:
                    vbs = APP_DIR / "run_silent.vbs"
                    if getattr(sys, "frozen", False):
                        target_path = f'"{sys.executable}"'
                    elif vbs.exists():
                        target_path = f'wscript.exe "{vbs}"'
                    else:
                        target_path = f'"{sys.executable}" "{APP_DIR / "main.py"}"'
                winreg.SetValueEx(key, RUN_REG_NAME, 0, winreg.REG_SZ, target_path)
            else:
                try:
                    winreg.DeleteValue(key, RUN_REG_NAME)
                except FileNotFoundError:
                    pass
        return True, ""
    except Exception as exc:
        return False, str(exc)


def get_windows_startup_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            winreg.QueryValueEx(key, RUN_REG_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
