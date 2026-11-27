from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from importlib.metadata import version
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
APP_NAME = "AITaskbarMonitor"


def add_notices(bundle: Path) -> None:
    notices = bundle / "licenses"
    notices.mkdir()
    qt = version("PySide6-Essentials")
    qt_series = qt.rsplit(".", 1)[0]
    qt_sources = (
        f"https://download.qt.io/official_releases/qt/{qt_series}/{qt}/single/"
        f"qt-everywhere-src-{qt}.tar.xz"
    )
    pyside_sources = (
        f"https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{qt}-src/"
        f"pyside-setup-everywhere-src-{qt}.tar.xz"
    )
    for license_name in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt"):
        url = f"https://raw.githubusercontent.com/qt/qtbase/v{qt}/LICENSES/{license_name}"
        with urllib.request.urlopen(url, timeout=30) as response:
            (notices / license_name).write_bytes(response.read())
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", notices / "Python-LICENSE.txt")
    (notices / "Third-party-notices.txt").write_text(
        f"This package uses Qt, PySide6, and Shiboken6 {qt} under LGPL version 3.\n"
        f"Qt source: {qt_sources}\n"
        f"PySide6 and Shiboken6 source: {pyside_sources}\n\n"
        "Compatible Qt and PySide6 libraries can be replaced in _internal. You may modify\n"
        "and reverse-engineer the package as needed to debug those library modifications.\n\n"
        f"Includes CPython {platform.python_version()}; see Python-LICENSE.txt.\n"
        f"Packaged with PyInstaller {version('PyInstaller')} using its bootloader exception.\n"
        "https://pyinstaller.org/en/stable/license.html\n",
        encoding="utf-8",
    )


def main() -> None:
    machine = platform.machine().lower()
    if sys.platform != "win32" or sys.maxsize <= 2**32 or machine not in {"amd64", "arm64"}:
        raise SystemExit("Build with native 64-bit Python on Windows x64 or ARM64.")
    architecture = "x64" if machine == "amd64" else "arm64"
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
         "--name", APP_NAME, "--distpath", str(DIST), str(ROOT / "main.py")],
        cwd=ROOT, check=True,
    )
    bundle = DIST / APP_NAME
    subprocess.run(
        [str(bundle / f"{APP_NAME}.exe"), "--smoke-test"],
        cwd=bundle, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, check=True, timeout=30,
    )
    add_notices(bundle)
    shutil.copyfile(ROOT / "README.md", bundle / "README.md")
    archive = shutil.make_archive(str(DIST / f"{APP_NAME}-windows-{architecture}"), "zip", DIST, APP_NAME)
    print(f"Built {archive}")


if __name__ == "__main__":
    main()
