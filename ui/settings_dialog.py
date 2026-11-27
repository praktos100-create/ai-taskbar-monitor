from __future__ import annotations

from dataclasses import replace
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config import AppConfig


class SettingsDialog(QDialog):
    """TrafficMonitor-style settings dialog with display and appearance tabs."""

    config_applied = Signal(AppConfig)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 작업표시줄 모니터 설정")
        self.setMinimumWidth(390)
        self._source_config = config

        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs)

        # --- Tab 1: 표시 설정 ---
        tab_items = QWidget()
        items_layout = QVBoxLayout(tab_items)

        ai_group = QGroupBox("AI 사용량 및 % 표시")
        ai_grid = QGridLayout(ai_group)
        ai_grid.setColumnStretch(1, 1)

        def _make_window_combo() -> QComboBox:
            cmb = QComboBox()
            cmb.addItem("5시간 한도 (5h)", "5h")
            cmb.addItem("주간 한도 (7일)", "7d")
            return cmb

        self.chk_claude = QCheckBox("Claude")
        self.chk_codex = QCheckBox("ChatGPT (Codex)")
        self.chk_gemini = QCheckBox("Gemini")
        self.cmb_claude_window = _make_window_combo()
        self.cmb_codex_window = _make_window_combo()
        self.cmb_gemini_window = _make_window_combo()

        self.chk_claude.toggled.connect(self.cmb_claude_window.setEnabled)
        self.chk_codex.toggled.connect(self.cmb_codex_window.setEnabled)
        self.chk_gemini.toggled.connect(self.cmb_gemini_window.setEnabled)

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("남은 용량 % (예: 84%)", "remaining")
        self.cmb_mode.addItem("사용량 % (예: 16%)", "used_percent")

        ai_grid.addWidget(self.chk_claude, 0, 0)
        ai_grid.addWidget(self.cmb_claude_window, 0, 1)
        ai_grid.addWidget(self.chk_codex, 1, 0)
        ai_grid.addWidget(self.cmb_codex_window, 1, 1)
        ai_grid.addWidget(self.chk_gemini, 2, 0)
        ai_grid.addWidget(self.cmb_gemini_window, 2, 1)
        ai_grid.addWidget(QLabel("표기 단위:"), 3, 0)
        ai_grid.addWidget(self.cmb_mode, 3, 1)
        items_layout.addWidget(ai_group)

        sys_group = QGroupBox("시스템 리소스 표시 (TrafficMonitor 스타일)")
        sys_form = QVBoxLayout(sys_group)
        self.chk_cpu = QCheckBox("CPU 사용률 (%)")
        self.chk_ram = QCheckBox("RAM 메모리 사용률 (%)")
        self.chk_gpu = QCheckBox("GPU 사용률 (%) - NVIDIA GPU")
        sys_form.addWidget(self.chk_cpu)
        sys_form.addWidget(self.chk_ram)
        sys_form.addWidget(self.chk_gpu)
        items_layout.addWidget(sys_group)

        layout_group = QGroupBox("배치 방식")
        layout_form = QFormLayout(layout_group)
        self.cmb_layout = QComboBox()
        self.cmb_layout.addItem("2줄 자동 균등 배치", "two_row")
        self.cmb_layout.addItem("1줄 가로 나열", "one_row")
        layout_form.addRow("바 레이아웃:", self.cmb_layout)
        items_layout.addWidget(layout_group)
        items_layout.addStretch(1)
        tabs.addTab(tab_items, "표시 설정")

        # --- Tab 2: 외관 및 동작 ---
        tab_ui = QWidget()
        ui_layout = QVBoxLayout(tab_ui)

        look_group = QGroupBox("외관")
        look_form = QFormLayout(look_group)
        self.cmb_theme = QComboBox()
        self.cmb_theme.addItem("다크 (Windows 11 다크 작업표시줄)", "dark")
        self.cmb_theme.addItem("라이트 (밝은 작업표시줄)", "light")
        self.cmb_theme.addItem("투명 (배경 최소화 · 검은 글씨)", "transparent")

        op_row = QHBoxLayout()
        self.sld_opacity = QSlider(Qt.Horizontal)
        self.sld_opacity.setRange(30, 100)
        self.lbl_opacity = QLabel("92%")
        self.sld_opacity.valueChanged.connect(lambda v: self.lbl_opacity.setText(f"{v}%"))
        op_row.addWidget(self.sld_opacity)
        op_row.addWidget(self.lbl_opacity)

        self.spn_font = QSpinBox()
        self.spn_font.setRange(7, 14)
        self.spn_font.setSuffix(" pt")

        look_form.addRow("테마:", self.cmb_theme)
        look_form.addRow("불투명도:", op_row)
        look_form.addRow("글꼴 크기:", self.spn_font)
        ui_layout.addWidget(look_group)

        beh_group = QGroupBox("동작")
        beh_layout = QVBoxLayout(beh_group)
        self.chk_dock = QCheckBox("작업표시줄 트레이 옆에 자동 도킹")
        self.chk_lock = QCheckBox("위치 잠금 (드래그 이동 방지)")
        self.chk_fullscreen = QCheckBox("전체화면 앱 실행 시 자동 숨김")
        self.chk_startup = QCheckBox("Windows 시작 시 자동 실행")
        beh_layout.addWidget(self.chk_dock)
        beh_layout.addWidget(self.chk_lock)
        beh_layout.addWidget(self.chk_fullscreen)
        beh_layout.addWidget(self.chk_startup)
        ui_layout.addWidget(beh_group)

        bell_group = QGroupBox("리셋 알림 (codex-resets.com)")
        bell_layout = QVBoxLayout(bell_group)
        self.chk_reset_bell = QCheckBox("Codex 리셋 공지 알림 켜기")
        self.chk_reset_sound = QCheckBox("새 공지 시 알림 소리 재생")
        self.chk_reset_pred = QCheckBox("가능성·예측 소식도 알리기")
        bell_int_row = QHBoxLayout()
        bell_int_lbl = QLabel("확인 간격:")
        self.cmb_reset_interval = QComboBox()
        self.cmb_reset_interval.addItem("1분", 1)
        self.cmb_reset_interval.addItem("5분", 5)
        self.cmb_reset_interval.addItem("10분", 10)
        bell_int_row.addWidget(bell_int_lbl)
        bell_int_row.addWidget(self.cmb_reset_interval)
        bell_int_row.addStretch(1)
        bell_layout.addWidget(self.chk_reset_bell)
        bell_layout.addWidget(self.chk_reset_sound)
        bell_layout.addWidget(self.chk_reset_pred)
        bell_layout.addLayout(bell_int_row)
        ui_layout.addWidget(bell_group)
        ui_layout.addStretch(1)
        tabs.addTab(tab_ui, "외관 및 동작")

        # Dialog buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Apply | QDialogButtonBox.Cancel
        )
        btns.button(QDialogButtonBox.Ok).setText("확인")
        btns.button(QDialogButtonBox.Apply).setText("적용")
        btns.button(QDialogButtonBox.Cancel).setText("취소")
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        btns.button(QDialogButtonBox.Apply).clicked.connect(self._on_apply)
        root.addWidget(btns)

        self.load_from_config(config)

    def load_from_config(self, cfg: AppConfig) -> None:
        self._source_config = cfg
        self.chk_claude.setChecked(cfg.show_claude)
        self.chk_codex.setChecked(cfg.show_codex)
        self.chk_gemini.setChecked(cfg.show_gemini)
        self.chk_cpu.setChecked(cfg.show_cpu)
        self.chk_ram.setChecked(cfg.show_ram)
        self.chk_gpu.setChecked(cfg.show_gpu)

        self._select_combo(self.cmb_layout, cfg.layout_mode)
        self._select_combo(self.cmb_claude_window, cfg.claude_window)
        self._select_combo(self.cmb_codex_window, cfg.codex_window)
        self._select_combo(self.cmb_gemini_window, cfg.gemini_window)
        self.cmb_claude_window.setEnabled(cfg.show_claude)
        self.cmb_codex_window.setEnabled(cfg.show_codex)
        self.cmb_gemini_window.setEnabled(cfg.show_gemini)
        self._select_combo(self.cmb_mode, cfg.ai_display_mode)
        self._select_combo(self.cmb_theme, cfg.theme)

        self.sld_opacity.setValue(cfg.opacity)
        self.lbl_opacity.setText(f"{cfg.opacity}%")
        self.spn_font.setValue(cfg.font_size)

        self.chk_dock.setChecked(cfg.dock_to_taskbar)
        self.chk_lock.setChecked(cfg.lock_position)
        self.chk_fullscreen.setChecked(cfg.hide_on_fullscreen)
        self.chk_startup.setChecked(cfg.run_at_startup)
        self.chk_reset_bell.setChecked(cfg.enable_reset_bell)
        self.chk_reset_sound.setChecked(cfg.reset_bell_sound)
        self.chk_reset_pred.setChecked(cfg.reset_bell_predictions)
        idx_ri = self.cmb_reset_interval.findData(cfg.reset_bell_interval_min)
        if idx_ri >= 0:
            self.cmb_reset_interval.setCurrentIndex(idx_ri)

    @staticmethod
    def _select_combo(cmb: QComboBox, value: str) -> None:
        idx = cmb.findData(value)
        if idx >= 0:
            cmb.setCurrentIndex(idx)

    def build_config(self) -> AppConfig:
        return replace(
            self._source_config,
            show_claude=self.chk_claude.isChecked(),
            show_codex=self.chk_codex.isChecked(),
            show_gemini=self.chk_gemini.isChecked(),
            show_cpu=self.chk_cpu.isChecked(),
            show_ram=self.chk_ram.isChecked(),
            show_gpu=self.chk_gpu.isChecked(),
            layout_mode=str(self.cmb_layout.currentData()),
            claude_window=str(self.cmb_claude_window.currentData()),
            codex_window=str(self.cmb_codex_window.currentData()),
            gemini_window=str(self.cmb_gemini_window.currentData()),
            ai_display_mode=str(self.cmb_mode.currentData()),
            theme=str(self.cmb_theme.currentData()),
            opacity=self.sld_opacity.value(),
            font_size=self.spn_font.value(),
            dock_to_taskbar=self.chk_dock.isChecked(),
            lock_position=self.chk_lock.isChecked(),
            hide_on_fullscreen=self.chk_fullscreen.isChecked(),
            run_at_startup=self.chk_startup.isChecked(),
            enable_reset_bell=self.chk_reset_bell.isChecked(),
            reset_bell_sound=self.chk_reset_sound.isChecked(),
            reset_bell_predictions=self.chk_reset_pred.isChecked(),
            reset_bell_interval_min=int(self.cmb_reset_interval.currentData()),
        ).validate()

    def _on_apply(self) -> None:
        cfg = self.build_config()
        self._source_config = cfg
        self.config_applied.emit(cfg)

    def _on_ok(self) -> None:
        self._on_apply()
        self.accept()
