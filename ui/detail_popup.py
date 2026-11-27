from __future__ import annotations

import time
from datetime import datetime
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import AppConfig
from monitors.ai_usage import AIProviderStats, AIUsageSnapshot, format_tokens
from monitors.reset_bell import CODEX_RESETS_SITE_URL, ResetBellStatus
from monitors.system_monitor import SystemMetrics


def _pct_color(pct: float) -> str:
    if pct >= 90.0:
        return "#EF4444"
    if pct >= 75.0:
        return "#F59E0B"
    return "#38BDF8"


def _format_reset_time(ts: float | None, now_ts: float | None = None) -> str:
    if ts is None:
        return ""
    now = now_ts if now_ts is not None else time.time()
    diff = ts - now
    if diff <= 0:
        return "곧 리셋"
    dt = datetime.fromtimestamp(ts)
    if diff < 86400:
        total_min = int(diff // 60)
        h, m = divmod(total_min, 60)
        if h > 0:
            return f"리셋 {dt.strftime('%H:%M')} ({h}시간 {m}분 후)"
        return f"리셋 {dt.strftime('%H:%M')} ({m}분 후)"
    d = int(diff // 86400)
    h = int((diff % 86400) // 3600)
    return f"리셋 {dt.strftime('%m/%d %H:%M')} ({d}일 {h}시간 후)"


class _ProviderCard(QFrame):
    def __init__(self, accent_5h: str, accent_7d: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._accent_5h = accent_5h
        self._accent_7d = accent_7d
        self.setObjectName("providerCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color: {accent_5h}; font-size: 11px; background: transparent;")
        self.name_lbl = QLabel()
        self.name_lbl.setStyleSheet("font-weight: 700; font-size: 12px; color: #F1F5F9; background: transparent;")
        self.main_val_lbl = QLabel()
        self.main_val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.main_val_lbl.setStyleSheet("font-weight: 700; font-size: 13px; color: #FFFFFF; background: transparent;")
        top.addWidget(self.dot)
        top.addWidget(self.name_lbl)
        top.addStretch(1)
        top.addWidget(self.main_val_lbl)
        layout.addLayout(top)

        self.sub_lbl = QLabel()
        self.sub_lbl.setStyleSheet("font-size: 10px; color: #F59E0B; background: transparent;")
        self.sub_lbl.setWordWrap(True)
        self.sub_lbl.hide()
        layout.addWidget(self.sub_lbl)

        row_5h = QHBoxLayout()
        row_5h.setContentsMargins(0, 0, 0, 0)
        self.bar_5h_lbl = QLabel("5시간 --")
        self.bar_5h_lbl.setStyleSheet("font-size: 10.5px; color: #CBD5E1; background: transparent;")
        self.reset_5h_lbl = QLabel("")
        self.reset_5h_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.reset_5h_lbl.setStyleSheet("font-size: 10px; color: #94A3B8; background: transparent;")
        row_5h.addWidget(self.bar_5h_lbl)
        row_5h.addStretch(1)
        row_5h.addWidget(self.reset_5h_lbl)

        self.bar_5h = QProgressBar()
        self.bar_5h.setFixedHeight(5)
        self.bar_5h.setTextVisible(False)
        self.bar_5h.setRange(0, 100)

        row_7d = QHBoxLayout()
        row_7d.setContentsMargins(0, 2, 0, 0)
        self.bar_7d_lbl = QLabel("주간 --")
        self.bar_7d_lbl.setStyleSheet("font-size: 10.5px; color: #CBD5E1; background: transparent;")
        self.reset_7d_lbl = QLabel("")
        self.reset_7d_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.reset_7d_lbl.setStyleSheet("font-size: 10px; color: #94A3B8; background: transparent;")
        row_7d.addWidget(self.bar_7d_lbl)
        row_7d.addStretch(1)
        row_7d.addWidget(self.reset_7d_lbl)

        self.bar_7d = QProgressBar()
        self.bar_7d.setFixedHeight(5)
        self.bar_7d.setTextVisible(False)
        self.bar_7d.setRange(0, 100)

        layout.addLayout(row_5h)
        layout.addWidget(self.bar_5h)
        layout.addLayout(row_7d)
        layout.addWidget(self.bar_7d)
        self._set_bar_color(self.bar_5h, accent_5h)
        self._set_bar_color(self.bar_7d, accent_7d)

    @staticmethod
    def _set_bar_color(bar: QProgressBar, color: str) -> None:
        bar.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: rgba(255, 255, 255, 22);
                border: none;
                border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background-color: {color};
                border-radius: 2px;
            }}
            """
        )

    def _color_for_rem(self, rem_pct: float, normal: str) -> str:
        if rem_pct <= 10.0:
            return "#EF4444"
        if rem_pct <= 25.0:
            return "#F59E0B"
        return normal

    def update_stats(
        self,
        stats: AIProviderStats,
        budget_5h: int,
        budget_7d: int,
        time_window: str,
        display_mode: str,
    ) -> None:
        short_names = {
            "Claude Code": "Claude",
            "ChatGPT (Codex)": "ChatGPT",
            "Gemini (CLI · AGY)": "Gemini",
        }
        self.name_lbl.setText(short_names.get(stats.name, stats.name))

        rem_5h = stats.remaining_5h_percent(budget_5h)
        rem_7d = stats.remaining_7d_percent(budget_7d)
        used_5h = stats.used_5h_percent(budget_5h)
        used_7d = stats.used_7d_percent(budget_7d)
        is_7d = time_window == "7d"
        active_rem = rem_7d if is_7d else rem_5h
        active_used = used_7d if is_7d else used_5h
        used_mode = display_mode == "used_percent"
        win_tag = "주간" if is_7d else "5h"

        if used_mode:
            self.bar_5h.setValue(max(0, min(100, int(round(used_5h)))))
            self._set_bar_color(self.bar_5h, "#EF4444" if used_5h >= 90.0 else self._accent_5h)
            self.bar_5h_lbl.setText(f"5시간 사용 {used_5h:.0f}%")

            self.bar_7d.setValue(max(0, min(100, int(round(used_7d)))))
            self._set_bar_color(self.bar_7d, "#EF4444" if used_7d >= 90.0 else self._accent_7d)
            self.bar_7d_lbl.setText(f"주간 사용 {used_7d:.0f}%")

            self.main_val_lbl.setText(f"{win_tag} 사용 {active_used:.0f}%")
        else:
            self.bar_5h.setValue(max(0, min(100, int(round(rem_5h)))))
            self._set_bar_color(self.bar_5h, "#EF4444" if rem_5h <= 10.0 else self._accent_5h)
            self.bar_5h_lbl.setText(f"5시간 남음 {rem_5h:.0f}%")

            self.bar_7d.setValue(max(0, min(100, int(round(rem_7d)))))
            self._set_bar_color(self.bar_7d, "#EF4444" if rem_7d <= 10.0 else self._accent_7d)
            self.bar_7d_lbl.setText(f"주간 남음 {rem_7d:.0f}%")

            self.main_val_lbl.setText(f"{win_tag} 남음 {active_rem:.0f}%")

        self.reset_5h_lbl.setText(_format_reset_time(stats.rate_limit_resets_at))
        self.reset_7d_lbl.setText(_format_reset_time(stats.rate_limit_7d_resets_at))

        if stats.error:
            self.sub_lbl.setText(f"오류: {stats.error}")
            self.sub_lbl.show()
        elif stats.agy_stale or stats.rate_limit_stale:
            self.sub_lbl.setText("갱신 실패 · 이전 값 표시")
            self.sub_lbl.show()
        elif stats.extra_limit_note:
            self.sub_lbl.setText(stats.extra_limit_note)
            self.sub_lbl.show()
        else:
            self.sub_lbl.hide()


class _SysRow(QWidget):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)

        top = QHBoxLayout()
        self.title_lbl = QLabel(label)
        self.title_lbl.setStyleSheet("font-size: 11px; font-weight: 600; color: #CBD5E1; background: transparent;")
        self.val_lbl = QLabel("--")
        self.val_lbl.setStyleSheet("font-size: 11px; font-weight: 700; color: #F8FAFC; background: transparent;")
        self.val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.title_lbl)
        top.addStretch(1)
        top.addWidget(self.val_lbl)
        layout.addLayout(top)

        self.bar = QProgressBar()
        self.bar.setFixedHeight(6)
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        layout.addWidget(self.bar)
        self._set_bar_color("#38BDF8")

    def _set_bar_color(self, color: str) -> None:
        self.bar.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: rgba(255, 255, 255, 22);
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background-color: {color};
                border-radius: 3px;
            }}
            """
        )

    def set_metric(self, pct: float | None, detail_text: str) -> None:
        if pct is None:
            self.val_lbl.setText(detail_text)
            self.bar.setValue(0)
            self._set_bar_color("#64748B")
            return
        clamped = max(0.0, min(100.0, pct))
        self.val_lbl.setText(detail_text)
        self.bar.setValue(int(round(clamped)))
        self._set_bar_color(_pct_color(clamped))


class DetailPopup(QWidget):
    """TrafficMonitor-style detailed popup shown when clicking the taskbar overlay."""

    refresh_requested = Signal()
    reset_check_requested = Signal()
    settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(368)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.container = QFrame()
        self.container.setObjectName("popupRoot")
        self.container.setStyleSheet(
            """
            #popupRoot {
                background-color: rgba(18, 22, 31, 245);
                border: 1px solid rgba(148, 163, 184, 55);
                border-radius: 12px;
            }
            #providerCard {
                background-color: rgba(30, 37, 52, 210);
                border: 1px solid rgba(148, 163, 184, 30);
                border-radius: 8px;
            }
            QPushButton#winBtn {
                background-color: rgba(255, 255, 255, 16);
                color: #CBD5E1;
                border: 1px solid rgba(255, 255, 255, 22);
                border-radius: 6px;
                padding: 3px 9px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#winBtn[active="true"] {
                background-color: #2563EB;
                color: #FFFFFF;
                border-color: #3B82F6;
            }
            QPushButton#iconBtn {
                background-color: rgba(255, 255, 255, 16);
                color: #E2E8F0;
                border: 1px solid rgba(255, 255, 255, 22);
                border-radius: 6px;
                padding: 3px 8px;
                font-size: 11px;
            }
            QPushButton#iconBtn:hover, QPushButton#winBtn:hover {
                background-color: rgba(255, 255, 255, 34);
            }
            """
        )
        outer.addWidget(self.container)

        root = QVBoxLayout(self.container)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("AI & 시스템 모니터")
        title.setStyleSheet("font-size: 13px; font-weight: 700; color: #F8FAFC; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch(1)

        self.refresh_btn = QPushButton("새로고침")
        self.refresh_btn.setObjectName("iconBtn")
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        self.settings_btn = QPushButton("설정")
        self.settings_btn.setObjectName("iconBtn")
        self.settings_btn.clicked.connect(self._on_settings)
        hdr.addWidget(self.refresh_btn)
        hdr.addWidget(self.settings_btn)
        root.addLayout(hdr)

        # AI section
        self.ai_header = QLabel("AI CLI 사용량")
        self.ai_header.setStyleSheet("font-size: 11px; font-weight: 700; color: #94A3B8; background: transparent;")
        root.addWidget(self.ai_header)

        self.claude_card = _ProviderCard("#E06D3B", "#F6B898")
        self.codex_card = _ProviderCard("#10B981", "#86EFAC")
        self.gemini_card = _ProviderCard("#3B82F6", "#93C5FD")
        root.addWidget(self.claude_card)
        root.addWidget(self.codex_card)
        root.addWidget(self.gemini_card)

        # System section
        self.sys_box = QFrame()
        self.sys_box.setObjectName("providerCard")
        sys_layout = QVBoxLayout(self.sys_box)
        sys_layout.setContentsMargins(10, 8, 10, 8)
        sys_layout.setSpacing(6)

        sys_hdr = QLabel("시스템 리소스")
        sys_hdr.setStyleSheet("font-size: 11px; font-weight: 700; color: #94A3B8; background: transparent;")
        sys_layout.addWidget(sys_hdr)

        self.cpu_row = _SysRow("CPU")
        self.ram_row = _SysRow("RAM")
        self.gpu_row = _SysRow("GPU")
        sys_layout.addWidget(self.cpu_row)
        sys_layout.addWidget(self.ram_row)
        sys_layout.addWidget(self.gpu_row)
        root.addWidget(self.sys_box)

        # Codex Reset Bell section (codex-resets.com)
        self.reset_box = QFrame()
        self.reset_box.setObjectName("providerCard")
        reset_layout = QVBoxLayout(self.reset_box)
        reset_layout.setContentsMargins(10, 8, 10, 8)
        reset_layout.setSpacing(4)

        reset_hdr = QHBoxLayout()
        reset_title = QLabel("리셋 알림 (codex-resets.com)")
        reset_title.setStyleSheet("font-size: 11px; font-weight: 700; color: #94A3B8; background: transparent;")
        self.reset_check_btn = QPushButton("확인")
        self.reset_check_btn.setObjectName("iconBtn")
        self.reset_check_btn.clicked.connect(self.reset_check_requested.emit)
        self.reset_site_btn = QPushButton("사이트")
        self.reset_site_btn.setObjectName("iconBtn")
        self.reset_site_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(CODEX_RESETS_SITE_URL))
        )
        reset_hdr.addWidget(reset_title)
        reset_hdr.addStretch(1)
        reset_hdr.addWidget(self.reset_check_btn)
        reset_hdr.addWidget(self.reset_site_btn)
        reset_layout.addLayout(reset_hdr)

        self.reset_summary_lbl = QLabel("확인 대기 중...")
        self.reset_summary_lbl.setStyleSheet("font-size: 10.5px; color: #E2E8F0; background: transparent;")
        self.reset_summary_lbl.setWordWrap(True)
        reset_layout.addWidget(self.reset_summary_lbl)

        self.reset_watch_lbl = QLabel("")
        self.reset_watch_lbl.setStyleSheet("font-size: 10px; color: #F59E0B; background: transparent;")
        self.reset_watch_lbl.setWordWrap(True)
        self.reset_watch_lbl.hide()
        reset_layout.addWidget(self.reset_watch_lbl)
        root.addWidget(self.reset_box)

        self.footer_lbl = QLabel("")
        self.footer_lbl.setStyleSheet("font-size: 10px; color: #64748B; background: transparent;")
        self.footer_lbl.setAlignment(Qt.AlignRight)
        root.addWidget(self.footer_lbl)

    def _on_settings(self) -> None:
        self.hide()
        self.settings_requested.emit()

    def update_data(
        self,
        cfg: AppConfig,
        ai: AIUsageSnapshot | None,
        sys_metrics: SystemMetrics | None,
        reset_status: ResetBellStatus | None = None,
    ) -> None:
        self.claude_card.setVisible(cfg.show_claude)
        self.codex_card.setVisible(cfg.show_codex)
        self.gemini_card.setVisible(cfg.show_gemini)
        any_ai = cfg.show_claude or cfg.show_codex or cfg.show_gemini
        self.ai_header.setVisible(any_ai)

        if ai is not None:
            self.claude_card.update_stats(
                ai.claude, cfg.claude_budget_5h, cfg.claude_budget_7d, cfg.claude_window, cfg.ai_display_mode
            )
            self.codex_card.update_stats(
                ai.codex, cfg.codex_budget_5h, cfg.codex_budget_7d, cfg.codex_window, cfg.ai_display_mode
            )
            self.gemini_card.update_stats(
                ai.gemini, cfg.gemini_budget_5h, cfg.gemini_budget_7d, cfg.gemini_window, cfg.ai_display_mode
            )
            ts_str = datetime.fromtimestamp(ai.updated_at).strftime("%H:%M:%S")
            if cfg.claude_window == cfg.codex_window == cfg.gemini_window:
                win_txt = "주간" if cfg.claude_window == "7d" else "5시간"
            else:
                win_parts: list[str] = []
                for enabled, tag, w in (
                    (cfg.show_claude, "CLD", cfg.claude_window),
                    (cfg.show_codex, "GPT", cfg.codex_window),
                    (cfg.show_gemini, "GEM", cfg.gemini_window),
                ):
                    if enabled:
                        win_parts.append(f"{tag} {'주간' if w == '7d' else '5h'}")
                win_txt = " · ".join(win_parts) if win_parts else "없음"
            self.footer_lbl.setText(f"표기 기준: {win_txt} · 업데이트 {ts_str}")
        else:
            self.footer_lbl.setText("AI 로그 스캔 중...")

        self.cpu_row.setVisible(cfg.show_cpu)
        self.ram_row.setVisible(cfg.show_ram)
        self.gpu_row.setVisible(cfg.show_gpu)
        self.sys_box.setVisible(cfg.show_cpu or cfg.show_ram or cfg.show_gpu)

        if sys_metrics is not None:
            self.cpu_row.set_metric(sys_metrics.cpu_percent, f"{sys_metrics.cpu_percent:.1f}%")
            self.ram_row.set_metric(
                sys_metrics.ram_percent,
                f"{sys_metrics.ram_percent:.0f}% ({sys_metrics.ram_used_gb:.1f}/{sys_metrics.ram_total_gb:.1f} GB)",
            )
            if sys_metrics.gpu_percent is not None:
                vram = ""
                if sys_metrics.gpu_mem_used_mb is not None and sys_metrics.gpu_mem_total_mb:
                    vram = f" · VRAM {sys_metrics.gpu_mem_used_mb/1024:.1f}/{sys_metrics.gpu_mem_total_mb/1024:.1f} GB"
                temp = f" · {sys_metrics.gpu_temp_c:.0f}°C" if sys_metrics.gpu_temp_c is not None else ""
                self.gpu_row.set_metric(
                    sys_metrics.gpu_percent,
                    f"{sys_metrics.gpu_percent:.0f}%{vram}{temp}",
                )
            else:
                self.gpu_row.set_metric(None, "감지 안 됨 (NVIDIA 미지원 또는 대기)")

        self.reset_box.setVisible(cfg.enable_reset_bell)
        if cfg.enable_reset_bell:
            if reset_status is None:
                self.reset_summary_lbl.setText("확인 대기 중...")
                self.reset_watch_lbl.hide()
            else:
                checked_txt = (
                    f" · {datetime.fromtimestamp(reset_status.checked_at).strftime('%H:%M')}"
                    if reset_status.checked_at
                    else ""
                )
                self.reset_summary_lbl.setText(f"{reset_status.summary}{checked_txt}")
                if cfg.reset_bell_predictions and reset_status.active_watch_text:
                    self.reset_watch_lbl.setText(reset_status.active_watch_text)
                    self.reset_watch_lbl.show()
                else:
                    self.reset_watch_lbl.hide()

        self.adjustSize()
