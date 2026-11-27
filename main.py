from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from config import load_config
from ui.taskbar_bar import TaskbarOverlayWidget


def _acquire_single_instance_mutex():
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Global\\AITaskbarMonitor_SingleInstance_v1")
    last_err = kernel32.GetLastError()
    if last_err == 183:  # ERROR_ALREADY_EXISTS
        return False
    return handle


def main() -> int:
    smoke_test = sys.argv[1:] == ["--smoke-test"]
    if not smoke_test and _acquire_single_instance_mutex() is False:
        return 0

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv[:1] if smoke_test else sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Malgun Gothic", 9))

    cfg = load_config()
    widget = TaskbarOverlayWidget(cfg)
    try:
        if smoke_test:
            widget.show()
            QTimer.singleShot(0, app.quit)
        else:
            widget.start()
        return app.exec()
    finally:
        widget.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
