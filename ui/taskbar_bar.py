from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from PySide6.QtCore import QObject, QPoint, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from config import AppConfig, save_config, set_windows_startup
from monitors.ai_usage import AIProviderStats, AIUsageMonitor, AIUsageSnapshot, format_tokens
from monitors.reset_bell import (
    CODEX_RESETS_SITE_URL,
    ResetBellMonitor,
    ResetBellStatus,
    play_alert_sound,
)
from monitors.system_monitor import SystemMetrics, SystemMonitor
from ui.detail_popup import DetailPopup
from ui.settings_dialog import SettingsDialog

# Win32 constants
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010


class _Win32Helper:
    """Thin Win32 wrapper for taskbar rect discovery, Z-order, and fullscreen detection."""

    def __init__(self) -> None:
        self.available = sys.platform == "win32"
        if not self.available:
            return
        u32 = ctypes.windll.user32
        self._u32 = u32
        u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u32.FindWindowW.restype = wintypes.HWND
        u32.FindWindowExW.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        ]
        u32.FindWindowExW.restype = wintypes.HWND
        u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u32.GetWindowRect.restype = wintypes.BOOL
        u32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        u32.SetWindowPos.restype = wintypes.BOOL
        u32.GetForegroundWindow.argtypes = []
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetClassNameW.restype = ctypes.c_int

        self._get_ex = getattr(u32, "GetWindowLongPtrW", u32.GetWindowLongW)
        self._set_ex = getattr(u32, "SetWindowLongPtrW", u32.SetWindowLongW)
        self._get_ex.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_ex.restype = ctypes.c_ssize_t
        self._set_ex.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._set_ex.restype = ctypes.c_ssize_t

    def apply_no_activate_style(self, hwnd_int: int) -> None:
        if not self.available or not hwnd_int:
            return
        hwnd = wintypes.HWND(hwnd_int)
        style = self._get_ex(hwnd, GWL_EXSTYLE)
        new_style = style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if new_style != style:
            self._set_ex(hwnd, GWL_EXSTYLE, new_style)

    def ensure_topmost(self, hwnd_int: int) -> None:
        if not self.available or not hwnd_int:
            return
        self._u32.SetWindowPos(
            wintypes.HWND(hwnd_int),
            wintypes.HWND(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )

    def get_taskbar_and_tray_rects(
        self,
    ) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]:
        if not self.available:
            return None, None
        tb_hwnd = self._u32.FindWindowW("Shell_TrayWnd", None)
        if not tb_hwnd:
            return None, None
        tb_rect = wintypes.RECT()
        if not self._u32.GetWindowRect(tb_hwnd, ctypes.byref(tb_rect)):
            return None, None

        tray_hwnd = self._u32.FindWindowExW(tb_hwnd, None, "TrayNotifyWnd", None)
        tray_tuple = None
        if tray_hwnd:
            tr = wintypes.RECT()
            if self._u32.GetWindowRect(tray_hwnd, ctypes.byref(tr)):
                tray_tuple = (tr.left, tr.top, tr.right, tr.bottom)
        return (tb_rect.left, tb_rect.top, tb_rect.right, tb_rect.bottom), tray_tuple

    def is_foreground_fullscreen(self, own_hwnd_int: int, screen_phys_rect: tuple[int, int, int, int]) -> bool:
        if not self.available:
            return False
        fg = self._u32.GetForegroundWindow()
        if not fg or int(fg) == own_hwnd_int:
            return False
        buf = ctypes.create_unicode_buffer(128)
        self._u32.GetClassNameW(fg, buf, 128)
        cls_name = buf.value
        if cls_name in ("Shell_TrayWnd", "Progman", "WorkerW", "Shell_SecondaryTrayWnd"):
            return False
        r = wintypes.RECT()
        if not self._u32.GetWindowRect(fg, ctypes.byref(r)):
            return False
        sx0, sy0, sx1, sy1 = screen_phys_rect
        return r.left <= sx0 and r.top <= sy0 and r.right >= sx1 and r.bottom >= sy1


class _AIScanWorker(QObject):
    finished = Signal(object)

    def __init__(self, monitor: AIUsageMonitor) -> None:
        super().__init__()
        self._monitor = monitor

    @Slot(str, str, str, str, str, str, bool)
    def run(
        self,
        claude_window: str,
        codex_window: str,
        gemini_window: str,
        claude_dir: str,
        codex_dir: str,
        gemini_dir: str,
        force_agy: bool,
    ) -> None:
        snap = self._monitor.scan(
            time_window=claude_window,
            claude_dir=claude_dir,
            codex_dir=codex_dir,
            gemini_dir=gemini_dir,
            force_agy=force_agy,
            claude_window=claude_window,
            codex_window=codex_window,
            gemini_window=gemini_window,
            force_claude=force_agy,
        )
        self.finished.emit(snap)


def _create_tray_icon() -> QIcon:
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#1E293B"))
    p.setPen(QColor("#38BDF8"))
    p.drawRoundedRect(2, 2, 28, 28, 7, 7)

    p.setPen(QColor("#E88C5D"))
    p.drawLine(7, 21, 7, 13)
    p.setPen(QColor("#34D399"))
    p.drawLine(14, 21, 14, 9)
    p.setPen(QColor("#60A5FA"))
    p.drawLine(21, 21, 21, 12)
    p.setPen(QColor("#F8FAFC"))
    p.drawLine(25, 21, 25, 16)
    p.end()
    return QIcon(pix)


class _MiniBarWidget(QWidget):
    """Compact horizontal gauge widget: single bar or dual stacked bars (5h top / 7d bottom)."""

    def __init__(self, dual: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dual = dual
        self._pct1: float | None = None
        self._color1 = QColor("#38BDF8")
        self._pct2: float | None = None
        self._color2 = QColor("#94A3B8")
        self._track = QColor(255, 255, 255, 38)
        self.setFixedSize(28, 12)

    def set_track_color(self, color: QColor) -> None:
        if self._track != color:
            self._track = color
            self.update()

    def set_values(
        self,
        pct1: float | None,
        color1: str,
        pct2: float | None = None,
        color2: str = "#94A3B8",
    ) -> None:
        self._pct1 = None if pct1 is None else max(0.0, min(100.0, float(pct1)))
        self._color1 = QColor(color1)
        self._pct2 = None if pct2 is None else max(0.0, min(100.0, float(pct2)))
        self._color2 = QColor(color2)
        self.update()

    def _draw_bar(self, p: QPainter, y: int, h: int, pct: float | None, col: QColor) -> None:
        w = self.width()
        r = 1.8 if h <= 4 else 2.5
        p.setBrush(self._track)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, y, w, h, r, r)
        if pct is not None and pct > 0.0:
            fill_w = max(2, int(round((w * pct) / 100.0)))
            p.setBrush(col)
            p.drawRoundedRect(0, y, min(w, fill_w), h, r, r)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._dual:
            self._draw_bar(p, 1, 4, self._pct1, self._color1)
            self._draw_bar(p, 7, 4, self._pct2, self._color2)
        else:
            self._draw_bar(p, 3, 6, self._pct1, self._color1)
        p.end()


class _MetricPill(QWidget):
    """Single metric item inside the taskbar bar (tag + fixed-width value + inline bar)."""

    def __init__(
        self, tag: str, tag_color: str, dual_bar: bool = False, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._tag = tag
        self._tag_color = tag_color
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.tag_lbl = QLabel(tag)
        self.tag_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.val_lbl = QLabel("--")
        self.val_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.mini_bar = _MiniBarWidget(dual=dual_bar)
        layout.addWidget(self.tag_lbl)
        layout.addWidget(self.val_lbl)
        layout.addWidget(self.mini_bar)

    def set_fixed_sample(self, font_pt: int, sample_text: str) -> None:
        f = QFont("Malgun Gothic", font_pt, QFont.DemiBold)
        fm = QFontMetrics(f)
        self.val_lbl.setFixedWidth(fm.horizontalAdvance(sample_text) + 2)

    def set_style(self, font_pt: int, value_color: str, tag_color: str | None = None) -> None:
        tc = tag_color or self._tag_color
        self.tag_lbl.setStyleSheet(
            f"color: {tc}; font-size: {font_pt}pt; font-weight: 700; background: transparent;"
        )
        self.val_lbl.setStyleSheet(
            f"color: {value_color}; font-size: {font_pt}pt; font-weight: 600; background: transparent;"
        )

    def set_value(self, text: str) -> None:
        self.val_lbl.setText(text)


class TaskbarOverlayWidget(QWidget):
    """TrafficMonitor-style always-on-top taskbar widget showing AI usage and CPU/RAM/GPU."""

    _trigger_ai_scan = Signal(str, str, str, str, str, str, bool)
    _reset_bell_ready = Signal(object)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)

        self.config = config
        self._win32 = _Win32Helper()
        self._sys_monitor = SystemMonitor(gpu_poll_interval=2.0)
        self._ai_monitor = AIUsageMonitor()
        self._reset_monitor = ResetBellMonitor()
        self._latest_ai: AIUsageSnapshot | None = None
        self._latest_sys: SystemMetrics | None = None
        self._latest_reset: ResetBellStatus | None = None
        self._scan_in_flight = False
        self._reset_in_flight = False
        self._has_unread_reset = False
        self._hidden_for_fullscreen = False
        self._dragging = False
        self._drag_moved = False
        self._drag_offset = QPoint()
        self._last_layout_sig: tuple | None = None
        self._last_size_sig: tuple | None = None
        self._last_dock_mode: bool = config.dock_to_taskbar

        self._build_ui()
        self._setup_tray()
        self._setup_ai_thread()
        self._reset_bell_ready.connect(self._on_reset_bell_ready)

        self.popup = DetailPopup(None)
        self.popup.refresh_requested.connect(lambda: self.request_ai_scan(force=True))
        self.popup.reset_check_requested.connect(self.request_reset_check)
        self.popup.settings_requested.connect(self.open_settings)

        self._settings_dlg: SettingsDialog | None = None

        self._sys_timer = QTimer(self)
        self._sys_timer.timeout.connect(self._on_sys_tick)

        self._ai_timer = QTimer(self)
        self._ai_timer.timeout.connect(self._on_ai_timer_tick)

        self._reset_timer = QTimer(self)
        self._reset_timer.timeout.connect(self.request_reset_check)

        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._on_topmost_tick)

        self._apply_config(self.config, save=False)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.frame = QFrame()
        self.frame.setObjectName("barFrame")
        outer.addWidget(self.frame)

        self._frame_layout = QVBoxLayout(self.frame)
        self._frame_layout.setContentsMargins(8, 3, 8, 3)
        self._frame_layout.setSpacing(1)

        self.row1 = QWidget()
        self.row1_layout = QHBoxLayout(self.row1)
        self.row1_layout.setContentsMargins(0, 0, 0, 0)
        self.row1_layout.setSpacing(10)
        self.row1_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.row2 = QWidget()
        self.row2_layout = QHBoxLayout(self.row2)
        self.row2_layout.setContentsMargins(0, 0, 0, 0)
        self.row2_layout.setSpacing(10)
        self.row2_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.pill_claude = _MetricPill("CLD", "#E06D3B", dual_bar=True)
        self.pill_codex = _MetricPill("GPT", "#10B981", dual_bar=True)
        self.pill_gemini = _MetricPill("GEM", "#3B82F6", dual_bar=True)
        self.divider = QLabel("|")
        self.pill_cpu = _MetricPill("CPU", "#94A3B8", dual_bar=False)
        self.pill_ram = _MetricPill("RAM", "#94A3B8", dual_bar=False)
        self.pill_gpu = _MetricPill("GPU", "#94A3B8", dual_bar=False)
        self.bell_badge = QLabel("●")
        self.bell_badge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.bell_badge.setStyleSheet("color: #F59E0B; font-size: 9pt; font-weight: 700; background: transparent;")
        self.bell_badge.setToolTip("새 Codex 리셋 공지")
        self.bell_badge.hide()
        self.empty_lbl = QLabel("AI Monitor")

        self._frame_layout.addWidget(self.row1)
        self._frame_layout.addWidget(self.row2)
        self._frame_layout.addWidget(self.empty_lbl)

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(_create_tray_icon(), self)
        self.tray.setToolTip("AI 작업표시줄 모니터")
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setContextMenu(self._build_context_menu())
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _setup_ai_thread(self) -> None:
        self._ai_thread = QThread(self)
        self._ai_worker = _AIScanWorker(self._ai_monitor)
        self._ai_worker.moveToThread(self._ai_thread)
        self._trigger_ai_scan.connect(self._ai_worker.run)
        self._ai_worker.finished.connect(self._on_ai_scan_finished)
        self._ai_thread.start()

    def start(self) -> None:
        self._sys_monitor.start()
        self._on_sys_tick()
        self.request_ai_scan()
        self._sys_timer.start(self.config.sys_interval_ms)
        self._ai_timer.start(self.config.ai_interval_sec * 1000)
        if self.config.enable_reset_bell:
            self._reset_timer.start(self.config.reset_bell_interval_min * 60_000)
            QTimer.singleShot(3000, self.request_reset_check)
        self._topmost_timer.start(1000)
        self.show()
        self._win32.apply_no_activate_style(int(self.winId()))
        self._reposition()
        QTimer.singleShot(50, self._reposition)

    def shutdown(self) -> None:
        self._sys_timer.stop()
        self._ai_timer.stop()
        self._reset_timer.stop()
        self._topmost_timer.stop()
        self._sys_monitor.stop()
        self.tray.hide()
        self._ai_thread.quit()
        self._ai_thread.wait(1500)

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.toggle_popup()

    def _build_context_menu(self) -> QMenu:
        menu = QMenu(self)

        act_detail = menu.addAction("상세 보기")
        act_detail.triggered.connect(self.toggle_popup)
        menu.addSeparator()

        win_menu = menu.addMenu("% 표시 기준")
        for label, attr in (
            ("Claude", "claude_window"),
            ("ChatGPT (Codex)", "codex_window"),
            ("Gemini", "gemini_window"),
        ):
            sub = win_menu.addMenu(label)
            cur_win = getattr(self.config, attr)
            for key, w_label in (
                ("5h", "5시간 한도 (5h)"),
                ("7d", "주간 한도 (7일)"),
            ):
                act = sub.addAction(w_label)
                act.setCheckable(True)
                act.setChecked(cur_win == key)
                act.triggered.connect(lambda _=False, a=attr, k=key: self._set_provider_window(a, k))
        win_menu.addSeparator()
        for key, w_label in (
            ("5h", "모두 5시간 (5h)"),
            ("7d", "모두 주간 (7일)"),
        ):
            act = win_menu.addAction(w_label)
            act.triggered.connect(lambda _=False, k=key: self._set_time_window(k))

        mode_menu = menu.addMenu("표기 단위")
        for key, label in (
            ("remaining", "남은 용량 (%)"),
            ("used_percent", "사용량 (%)"),
        ):
            act = mode_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.config.ai_display_mode == key)
            act.triggered.connect(lambda _=False, k=key: self._set_display_mode(k))

        vis_menu = menu.addMenu("표시 항목")
        toggles = [
            ("Claude", "show_claude"),
            ("ChatGPT (Codex)", "show_codex"),
            ("Gemini", "show_gemini"),
            ("CPU 사용률", "show_cpu"),
            ("RAM 사용률", "show_ram"),
            ("GPU 사용률", "show_gpu"),
        ]
        for label, attr in toggles:
            act = vis_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(bool(getattr(self.config, attr)))
            act.triggered.connect(lambda checked, a=attr: self._toggle_field(a, checked))

        act_dock = menu.addAction("작업표시줄 자동 도킹")
        act_dock.setCheckable(True)
        act_dock.setChecked(self.config.dock_to_taskbar)
        act_dock.triggered.connect(lambda checked: self._toggle_field("dock_to_taskbar", checked))

        act_redock = menu.addAction("작업표시줄 위치 다시 맞추기")
        act_redock.triggered.connect(self._redock_now)

        act_lock = menu.addAction("위치 잠금")
        act_lock.setCheckable(True)
        act_lock.setChecked(self.config.lock_position)
        act_lock.triggered.connect(lambda checked: self._toggle_field("lock_position", checked))

        bell_menu = menu.addMenu("리셋 알림")
        act_bell_on = bell_menu.addAction("리셋 알림 켜기")
        act_bell_on.setCheckable(True)
        act_bell_on.setChecked(self.config.enable_reset_bell)
        act_bell_on.triggered.connect(lambda checked: self._toggle_field("enable_reset_bell", checked))

        act_bell_snd = bell_menu.addAction("소리 알림 켜기")
        act_bell_snd.setCheckable(True)
        act_bell_snd.setChecked(self.config.reset_bell_sound)
        act_bell_snd.triggered.connect(lambda checked: self._toggle_field("reset_bell_sound", checked))

        act_bell_pred = bell_menu.addAction("가능성·예측 소식도 알리기")
        act_bell_pred.setCheckable(True)
        act_bell_pred.setChecked(self.config.reset_bell_predictions)
        act_bell_pred.triggered.connect(lambda checked: self._toggle_field("reset_bell_predictions", checked))

        int_menu = bell_menu.addMenu("확인 간격")
        for mins in (1, 5, 10):
            act = int_menu.addAction(f"{mins}분")
            act.setCheckable(True)
            act.setChecked(self.config.reset_bell_interval_min == mins)
            act.triggered.connect(lambda _=False, m=mins: self._set_reset_interval(m))

        bell_menu.addSeparator()
        act_bell_now = bell_menu.addAction("지금 확인")
        act_bell_now.triggered.connect(self.request_reset_check)
        act_bell_test = bell_menu.addAction("알림 소리 테스트")
        act_bell_test.triggered.connect(play_alert_sound)
        act_bell_site = bell_menu.addAction("codex-resets.com 열기")
        act_bell_site.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(CODEX_RESETS_SITE_URL)))

        menu.addSeparator()
        act_refresh = menu.addAction("지금 새로고침")
        act_refresh.triggered.connect(lambda: self.request_ai_scan(force=True))

        act_settings = menu.addAction("설정...")
        act_settings.triggered.connect(self.open_settings)

        menu.addSeparator()
        act_quit = menu.addAction("종료")
        act_quit.triggered.connect(self._quit_app)
        return menu

    def _quit_app(self) -> None:
        self.shutdown()
        app = QApplication.instance()
        if app:
            app.quit()

    def _toggle_field(self, attr: str, value: bool) -> None:
        setattr(self.config, attr, value)
        self._apply_config(self.config, save=True)

    def _set_provider_window(self, attr: str, window: str) -> None:
        if getattr(self.config, attr) != window:
            setattr(self.config, attr, window)
            save_config(self.config)
            self._refresh_display_values()
            self.tray.setContextMenu(self._build_context_menu())
            if self.popup.isVisible():
                self.popup.update_data(self.config, self._latest_ai, self._latest_sys)
            self.request_ai_scan()

    def _set_time_window(self, window: str) -> None:
        changed = (
            self.config.time_window != window
            or self.config.claude_window != window
            or self.config.codex_window != window
            or self.config.gemini_window != window
        )
        if changed:
            self.config.time_window = window
            self.config.claude_window = window
            self.config.codex_window = window
            self.config.gemini_window = window
            save_config(self.config)
            self._refresh_display_values()
            self.tray.setContextMenu(self._build_context_menu())
            if self.popup.isVisible():
                self.popup.update_data(self.config, self._latest_ai, self._latest_sys)
            self.request_ai_scan()

    def _set_display_mode(self, mode: str) -> None:
        if self.config.ai_display_mode != mode:
            self.config.ai_display_mode = mode
            self._apply_config(self.config, save=True)

    def _set_reset_interval(self, minutes: int) -> None:
        self.config.reset_bell_interval_min = minutes
        self._apply_config(self.config, save=True)

    def _redock_now(self) -> None:
        self.config.dock_to_taskbar = True
        save_config(self.config)
        self._reposition()
        self.tray.setContextMenu(self._build_context_menu())

    def open_settings(self) -> None:
        if self._settings_dlg is None:
            self._settings_dlg = SettingsDialog(self.config, None)
            self._settings_dlg.config_applied.connect(self._on_settings_applied)
        else:
            self._settings_dlg.load_from_config(self.config)
        self._settings_dlg.show()
        self._settings_dlg.raise_()
        self._settings_dlg.activateWindow()

    def _on_settings_applied(self, new_cfg: AppConfig) -> None:
        prev_windows = (self.config.claude_window, self.config.codex_window, self.config.gemini_window)
        prev_dirs = (self.config.claude_dir, self.config.codex_dir, self.config.gemini_dir)
        prev_startup = self.config.run_at_startup
        self._apply_config(new_cfg, save=True)

        if new_cfg.run_at_startup != prev_startup:
            ok, err = set_windows_startup(new_cfg.run_at_startup)
            if not ok and err:
                QMessageBox.warning(self, "시작프로그램 설정 오류", f"레지스트리 등록 실패:\n{err}")

        if (new_cfg.claude_window, new_cfg.codex_window, new_cfg.gemini_window) != prev_windows or (
            new_cfg.claude_dir,
            new_cfg.codex_dir,
            new_cfg.gemini_dir,
        ) != prev_dirs:
            self.request_ai_scan()

    def _apply_config(self, cfg: AppConfig, save: bool, force_reposition: bool = False) -> None:
        self.config = cfg.validate()
        if save:
            save_config(self.config)

        self._sys_timer.setInterval(self.config.sys_interval_ms)
        self._ai_timer.setInterval(self.config.ai_interval_sec * 1000)
        if self.config.enable_reset_bell:
            self._reset_timer.start(self.config.reset_bell_interval_min * 60_000)
        else:
            self._reset_timer.stop()
            self._has_unread_reset = False

        layout_sig = (
            self.config.show_claude,
            self.config.show_codex,
            self.config.show_gemini,
            self.config.show_cpu,
            self.config.show_ram,
            self.config.show_gpu,
            self.config.layout_mode,
            self._has_unread_reset and self.config.enable_reset_bell,
        )
        size_sig = (self.config.font_size, self.config.ai_display_mode)
        layout_changed = layout_sig != self._last_layout_sig
        size_changed = size_sig != self._last_size_sig
        dock_turned_on = (not self._last_dock_mode) and self.config.dock_to_taskbar

        self._last_layout_sig = layout_sig
        self._last_size_sig = size_sig
        self._last_dock_mode = self.config.dock_to_taskbar

        if layout_changed:
            self._rebuild_bar_layout()
        self._apply_theme()
        self._refresh_display_values()
        if layout_changed or size_changed:
            self._fit_size()
        if force_reposition or layout_changed or size_changed or dock_turned_on:
            self._reposition()
            QTimer.singleShot(50, self._reposition)

        self.tray.setContextMenu(self._build_context_menu())
        if self.popup.isVisible():
            self.popup.update_data(self.config, self._latest_ai, self._latest_sys, self._latest_reset)

    def _clear_layout(self, layout: QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)

    def _rebuild_bar_layout(self) -> None:
        self._clear_layout(self.row1_layout)
        self._clear_layout(self.row2_layout)

        ai_pills: list[QWidget] = []
        if self.config.show_claude:
            ai_pills.append(self.pill_claude)
        if self.config.show_codex:
            ai_pills.append(self.pill_codex)
        if self.config.show_gemini:
            ai_pills.append(self.pill_gemini)

        sys_pills: list[QWidget] = []
        if self.config.show_cpu:
            sys_pills.append(self.pill_cpu)
        if self.config.show_ram:
            sys_pills.append(self.pill_ram)
        if self.config.show_gpu:
            sys_pills.append(self.pill_gpu)

        all_pills = ai_pills + sys_pills
        if not all_pills:
            self.row1.hide()
            self.row2.hide()
            self.divider.hide()
            self.empty_lbl.show()
            return

        self.empty_lbl.hide()

        if self.config.layout_mode == "one_row" or len(all_pills) == 1:
            for p in ai_pills:
                p.show()
                self.row1_layout.addWidget(p)
            if ai_pills and sys_pills:
                self.divider.show()
                self.row1_layout.addWidget(self.divider)
            else:
                self.divider.hide()
            for p in sys_pills:
                p.show()
                self.row1_layout.addWidget(p)
            if self._has_unread_reset and self.config.enable_reset_bell:
                self.bell_badge.show()
                self.row1_layout.addWidget(self.bell_badge)
            else:
                self.bell_badge.hide()
            self.row1.show()
            self.row2.hide()
        else:
            self.divider.hide()
            split = (len(all_pills) + 1) // 2
            for p in all_pills[:split]:
                p.show()
                self.row1_layout.addWidget(p)
            if self._has_unread_reset and self.config.enable_reset_bell:
                self.bell_badge.show()
                self.row1_layout.addWidget(self.bell_badge)
            else:
                self.bell_badge.hide()
            for p in all_pills[split:]:
                p.show()
                self.row2_layout.addWidget(p)
            self.row1.show()
            self.row2.show()

    def _theme_colors(self) -> tuple[str, str, str, str, str]:
        alpha = int(round(self.config.opacity * 2.55))
        if self.config.theme == "light":
            bg = f"rgba(248, 250, 252, {alpha})"
            border = "rgba(15, 23, 42, 42)"
            text = "#0F172A"
            sys_tag = "#475569"
            div_col = "rgba(15, 23, 42, 60)"
        elif self.config.theme == "transparent":
            bg = "rgba(255, 255, 255, 10)"
            border = "rgba(0, 0, 0, 18)"
            text = "#000000"
            sys_tag = "#000000"
            div_col = "rgba(0, 0, 0, 70)"
        else:  # dark
            bg = f"rgba(22, 27, 38, {alpha})"
            border = "rgba(255, 255, 255, 34)"
            text = "#F1F5F9"
            sys_tag = "#94A3B8"
            div_col = "rgba(255, 255, 255, 55)"
        return bg, border, text, sys_tag, div_col

    def _apply_theme(self) -> None:
        bg, border, text, sys_tag, div_col = self._theme_colors()
        pt = self.config.font_size
        self.frame.setStyleSheet(
            f"""
            #barFrame {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 7px;
            }}
            """
        )
        self.divider.setStyleSheet(f"color: {div_col}; font-size: {pt}pt; background: transparent;")
        self.empty_lbl.setStyleSheet(f"color: {text}; font-size: {pt}pt; font-weight: 600; background: transparent;")

        self.pill_claude.set_style(pt, text)
        self.pill_codex.set_style(pt, text)
        self.pill_gemini.set_style(pt, text)
        self.pill_cpu.set_style(pt, text, sys_tag)
        self.pill_ram.set_style(pt, text, sys_tag)
        self.pill_gpu.set_style(pt, text, sys_tag)

        track = QColor(15, 23, 42, 40) if self.config.theme in ("light", "transparent") else QColor(255, 255, 255, 38)
        for pill in (
            self.pill_claude,
            self.pill_codex,
            self.pill_gemini,
            self.pill_cpu,
            self.pill_ram,
            self.pill_gpu,
        ):
            pill.mini_bar.set_track_color(track)

        ai_sample = "100%"
        self.pill_claude.set_fixed_sample(pt, ai_sample)
        self.pill_codex.set_fixed_sample(pt, ai_sample)
        self.pill_gemini.set_fixed_sample(pt, ai_sample)
        self.pill_cpu.set_fixed_sample(pt, "100%")
        self.pill_ram.set_fixed_sample(pt, "100%")
        self.pill_gpu.set_fixed_sample(pt, "100%")

    def _fit_size(self) -> None:
        if self.row1.layout():
            self.row1.layout().activate()
        if self.row2.layout():
            self.row2.layout().activate()
        if self.frame.layout():
            self.frame.layout().activate()
        if self.layout():
            self.layout().activate()
        hint = self.sizeHint()
        if hint.isValid() and hint.width() > 0 and hint.height() > 0:
            self.resize(hint)
        else:
            self.adjustSize()

    def _format_ai_value(
        self, stats: AIProviderStats, budget_5h: int, budget_7d: int, window: str
    ) -> str:
        is_7d = window == "7d"
        if self.config.ai_display_mode == "used_percent":
            val = stats.used_7d_percent(budget_7d) if is_7d else stats.used_5h_percent(budget_5h)
        else:
            val = stats.remaining_7d_percent(budget_7d) if is_7d else stats.remaining_5h_percent(budget_5h)
        return f"{val:.0f}%"

    @staticmethod
    def _remaining_color(rem_pct: float, normal_color: str) -> str:
        if rem_pct <= 10.0:
            return "#EF4444"
        if rem_pct <= 25.0:
            return "#F59E0B"
        return normal_color

    @staticmethod
    def _bar_critical_color(pct: float, normal_color: str, used_mode: bool) -> str:
        if (used_mode and pct >= 90.0) or (not used_mode and pct <= 10.0):
            return "#EF4444"
        return normal_color

    def _refresh_display_values(self) -> None:
        _, _, text, sys_tag, _ = self._theme_colors()
        pt = self.config.font_size
        used_mode = self.config.ai_display_mode == "used_percent"

        if self._latest_ai is not None:
            specs = (
                (
                    self.pill_claude,
                    self._latest_ai.claude,
                    self.config.claude_budget_5h,
                    self.config.claude_budget_7d,
                    self.config.claude_window,
                    "#E06D3B",
                    "#F6B898",
                ),
                (
                    self.pill_codex,
                    self._latest_ai.codex,
                    self.config.codex_budget_5h,
                    self.config.codex_budget_7d,
                    self.config.codex_window,
                    "#10B981",
                    "#86EFAC",
                ),
                (
                    self.pill_gemini,
                    self._latest_ai.gemini,
                    self.config.gemini_budget_5h,
                    self.config.gemini_budget_7d,
                    self.config.gemini_window,
                    "#3B82F6",
                    "#93C5FD",
                ),
            )
            for pill, stats, b5, b7, win, c5, c7 in specs:
                is_7d = win == "7d"
                pill.set_value(self._format_ai_value(stats, b5, b7, win))
                rem_5h = stats.remaining_5h_percent(b5)
                rem_7d = stats.remaining_7d_percent(b7)
                used_5h = stats.used_5h_percent(b5)
                used_7d = stats.used_7d_percent(b7)
                active_rem = rem_7d if is_7d else rem_5h
                active_used = used_7d if is_7d else used_5h
                col = self._threshold_color(active_used, text) if used_mode else self._remaining_color(active_rem, text)
                pill.set_style(pt, col)
                if used_mode:
                    pill.mini_bar.set_values(
                        used_5h,
                        self._bar_critical_color(used_5h, c5, True),
                        used_7d,
                        self._bar_critical_color(used_7d, c7, True),
                    )
                else:
                    pill.mini_bar.set_values(
                        rem_5h,
                        self._bar_critical_color(rem_5h, c5, False),
                        rem_7d,
                        self._bar_critical_color(rem_7d, c7, False),
                    )

        if self._latest_sys is not None:
            m = self._latest_sys
            cpu_col = self._threshold_color(m.cpu_percent, text)
            self.pill_cpu.set_value(f"{m.cpu_percent:.0f}%")
            self.pill_cpu.set_style(pt, cpu_col, sys_tag)
            self.pill_cpu.mini_bar.set_values(m.cpu_percent, self._threshold_color(m.cpu_percent, "#38BDF8"))

            ram_col = self._threshold_color(m.ram_percent, text)
            self.pill_ram.set_value(f"{m.ram_percent:.0f}%")
            self.pill_ram.set_style(pt, ram_col, sys_tag)
            self.pill_ram.mini_bar.set_values(m.ram_percent, self._threshold_color(m.ram_percent, "#38BDF8"))

            if m.gpu_percent is not None:
                gpu_col = self._threshold_color(m.gpu_percent, text)
                self.pill_gpu.set_value(f"{m.gpu_percent:.0f}%")
                self.pill_gpu.set_style(pt, gpu_col, sys_tag)
                self.pill_gpu.mini_bar.set_values(m.gpu_percent, self._threshold_color(m.gpu_percent, "#38BDF8"))
            else:
                self.pill_gpu.set_value("--")
                self.pill_gpu.set_style(pt, sys_tag, sys_tag)
                self.pill_gpu.mini_bar.set_values(None, "#64748B")

    @staticmethod
    def _threshold_color(pct: float, normal_color: str) -> str:
        if pct >= 90.0:
            return "#EF4444"
        if pct >= 75.0:
            return "#F59E0B"
        return normal_color

    def _on_ai_timer_tick(self) -> None:
        if not self.isVisible() and not self.popup.isVisible():
            return
        self.request_ai_scan(force=False)

    def request_ai_scan(self, force: bool = False) -> None:
        if self._scan_in_flight:
            return
        self._scan_in_flight = True
        self._trigger_ai_scan.emit(
            self.config.claude_window,
            self.config.codex_window,
            self.config.gemini_window,
            self.config.claude_dir,
            self.config.codex_dir,
            self.config.gemini_dir,
            bool(force),
        )

    def _on_ai_scan_finished(self, snapshot: AIUsageSnapshot) -> None:
        self._scan_in_flight = False
        self._latest_ai = snapshot
        self._refresh_display_values()
        if self.popup.isVisible():
            self.popup.update_data(self.config, self._latest_ai, self._latest_sys, self._latest_reset)

    def _on_sys_tick(self) -> None:
        self._latest_sys = self._sys_monitor.sample()
        self._refresh_display_values()
        if self.popup.isVisible():
            self.popup.update_data(self.config, self._latest_ai, self._latest_sys, self._latest_reset)

    def request_reset_check(self) -> None:
        if not self.config.enable_reset_bell or self._reset_in_flight:
            return
        self._reset_in_flight = True
        include_pred = bool(self.config.reset_bell_predictions)

        def _worker() -> None:
            status = self._reset_monitor.check(include_predictions=include_pred)
            self._reset_bell_ready.emit(status)

        threading.Thread(target=_worker, name="ResetBellCheck", daemon=True).start()

    @Slot(object)
    def _on_reset_bell_ready(self, status: ResetBellStatus) -> None:
        self._reset_in_flight = False
        self._latest_reset = status
        if status.new_notifications:
            if not self.popup.isVisible() and not self._has_unread_reset:
                self._has_unread_reset = True
                self._apply_config(self.config, save=False)
            latest = status.new_notifications[-1]
            if QSystemTrayIcon.isSystemTrayAvailable():
                self.tray.showMessage(
                    latest.title,
                    latest.body,
                    QSystemTrayIcon.Information,
                    8000,
                )
            if self.config.reset_bell_sound:
                play_alert_sound()
        if self.popup.isVisible():
            self.popup.update_data(self.config, self._latest_ai, self._latest_sys, self._latest_reset)

    def _on_topmost_tick(self) -> None:
        if not self.isVisible() and not self._hidden_for_fullscreen:
            return
        screen = self.screen() or QApplication.primaryScreen()
        if self.config.hide_on_fullscreen and screen is not None:
            dpr = screen.devicePixelRatio() or 1.0
            sg = screen.geometry()
            phys = (
                int(sg.x() * dpr),
                int(sg.y() * dpr),
                int((sg.x() + sg.width()) * dpr),
                int((sg.y() + sg.height()) * dpr),
            )
            fs = self._win32.is_foreground_fullscreen(int(self.winId()), phys)
            if fs and not self._hidden_for_fullscreen:
                self._hidden_for_fullscreen = True
                self.hide()
                return
            if not fs and self._hidden_for_fullscreen:
                self._hidden_for_fullscreen = False
                self.show()
                self._win32.apply_no_activate_style(int(self.winId()))

        if self.isVisible():
            self._win32.ensure_topmost(int(self.winId()))

    def _reposition(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        dpr = screen.devicePixelRatio() or 1.0
        self._fit_size()
        w = max(1, self.width())
        h = max(1, self.height())

        if not self.config.dock_to_taskbar and self.config.pos_x is not None and self.config.pos_y is not None:
            sg = screen.geometry()
            x = max(sg.left(), min(sg.right() - w, self.config.pos_x))
            y = max(sg.top(), min(sg.bottom() - h, self.config.pos_y))
            if self.pos() != QPoint(x, y):
                self.move(x, y)
            return

        tb_rect, tray_rect = self._win32.get_taskbar_and_tray_rects()
        if tb_rect is not None:
            tb_l, tb_t, tb_r, tb_b = tb_rect
            tb_w = (tb_r - tb_l) / dpr
            tb_h = (tb_b - tb_t) / dpr
            # Horizontal bottom/top taskbar
            if tb_w > tb_h:
                if tray_rect is not None:
                    tray_l = tray_rect[0] / dpr
                    x = int(tray_l - w - 8)
                else:
                    x = int((tb_r / dpr) - w - 220)
                y = int((tb_t / dpr) + max(1, (tb_h - h) / 2))
                x = max(int(tb_l / dpr) + 8, x)
                if self.pos() != QPoint(x, y):
                    self.move(x, y)
                return

        # Fallback: bottom-right of primary screen
        sg = screen.geometry()
        x = sg.right() - w - 180
        y = sg.bottom() - h - 4
        if self.pos() != QPoint(x, y):
            self.move(x, y)

    def toggle_popup(self) -> None:
        if self.popup.isVisible():
            self.popup.hide()
            return
        if self._has_unread_reset:
            self._has_unread_reset = False
            self._apply_config(self.config, save=False)
        self.popup.update_data(self.config, self._latest_ai, self._latest_sys, self._latest_reset)
        screen = self.screen() or QApplication.primaryScreen()
        sg = screen.availableGeometry() if screen else self.geometry()
        bar_pos = self.mapToGlobal(QPoint(0, 0))
        px = bar_pos.x() + self.width() - self.popup.width()
        px = max(sg.left() + 8, min(sg.right() - self.popup.width() - 8, px))
        py = bar_pos.y() - self.popup.height() - 8
        if py < sg.top() + 8:
            py = bar_pos.y() + self.height() + 8
        self.popup.move(px, py)
        self.popup.show()
        self.popup.raise_()

    # --- Mouse interaction ---
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_moved = False
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        if event.button() == Qt.RightButton:
            menu = self._build_context_menu()
            menu.adjustSize()
            menu_sz = menu.sizeHint()
            screen = self.screen() or QApplication.primaryScreen()
            ag = screen.availableGeometry() if screen else self.geometry()
            bar_pos = self.mapToGlobal(QPoint(0, 0))
            mx = max(ag.left() + 6, min(ag.right() - menu_sz.width() - 6, int(event.globalPosition().x())))
            my = bar_pos.y() - menu_sz.height() - 6
            if my < ag.top() + 6:
                my = bar_pos.y() + self.height() + 6
            else:
                my = min(my, ag.bottom() - menu_sz.height() - 6)
            menu.exec(QPoint(mx, my))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and (event.buttons() & Qt.LeftButton) and not self.config.lock_position:
            new_pos = event.globalPosition().toPoint() - self._drag_offset
            if (new_pos - self.pos()).manhattanLength() >= 4:
                self._drag_moved = True
                self.move(new_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            if self._drag_moved:
                self.config.dock_to_taskbar = False
                self.config.pos_x = self.x()
                self.config.pos_y = self.y()
                save_config(self.config)
                self.tray.setContextMenu(self._build_context_menu())
            else:
                self.toggle_popup()
            event.accept()
            return
        super().mouseReleaseEvent(event)
