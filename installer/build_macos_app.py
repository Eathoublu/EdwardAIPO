#!/usr/bin/env python3
"""Build a double-clickable macOS installer app for Edward.

Produces:
  dist/edward                         native scheduler CLI (PyInstaller onefile)
  dist/Edward Installer.app           double-click installer
  dist/Edward-Installer-macos.zip     zipped app

The app embeds the native edward binary, so the installed scheduler does not
need a system Python. Playwright collectors still need the target environment's
Python/Playwright setup.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from release import read_release_version, write_version_file


INSTALLER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = INSTALLER_DIR.parent
RESOURCE_ROOT = PROJECT_ROOT
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build" / "macos"
PAYLOAD_ROOT = BUILD_DIR / "payload"
PAYLOAD_IGNORE = shutil.ignore_patterns(".DS_Store", "__pycache__", "*.pyc", "*.pyo")
NATIVE_DIST = BUILD_DIR / "native-dist"
NATIVE_WORK = BUILD_DIR / "native-work"
NATIVE_SPEC = BUILD_DIR / "native-spec"
APP_WORK = BUILD_DIR / "app-work"
APP_SPEC = BUILD_DIR / "app-spec"
APP_NAME = "Edward Installer"
APP_PATH = DIST_DIR / f"{APP_NAME}.app"
NATIVE_BIN = DIST_DIR / "edward"
ZIP_PATH = DIST_DIR / "Edward-Installer-macos.zip"
VERSION_FILE = BUILD_DIR / "release" / "EDWARD_VERSION"


def ensure_version_file() -> Path:
    return write_version_file(VERSION_FILE, read_release_version())


def run(command: list[str], cwd: Path | None = None) -> None:
    print("$", " ".join(command))
    subprocess.run(command, cwd=str(cwd) if cwd else None, check=True)


def pyinstaller_command() -> list[str]:
    if shutil.which("pyinstaller"):
        return ["pyinstaller"]
    if shutil.which("uvx"):
        return ["uvx", "pyinstaller"]
    raise SystemExit("需要 pyinstaller 或 uvx。安装：python3 -m pip install pyinstaller")


def build_native() -> Path:
    command = [
        *pyinstaller_command(),
        "--onefile",
        "--name",
        "edward",
        "--distpath",
        str(NATIVE_DIST),
        "--workpath",
        str(NATIVE_WORK),
        "--specpath",
        str(NATIVE_SPEC),
        "--clean",
        "--noconfirm",
    ]
    machine = platform.machine()
    if machine in {"arm64", "x86_64"}:
        command.extend(["--target-arch", machine])
    version_file = ensure_version_file()
    command.extend(["--add-data", f"{version_file}:."])
    command.append(str(RESOURCE_ROOT / "scripts" / "edward.py"))
    run(command)
    built = NATIVE_DIST / "edward"
    if not built.exists():
        raise SystemExit(f"native build missing: {built}")
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, NATIVE_BIN)
    NATIVE_BIN.chmod(0o755)
    return NATIVE_BIN


def prepare_payload(native_bin: Path) -> Path:
    if PAYLOAD_ROOT.exists():
        shutil.rmtree(PAYLOAD_ROOT)
    target_opencode = PAYLOAD_ROOT / ".opencode"
    target_opencode.mkdir(parents=True, exist_ok=True)

    write_version_file(target_opencode / "VERSION", read_release_version())
    shutil.copy2(RESOURCE_ROOT / "ui.json", target_opencode / "ui.json")
    shutil.copytree(RESOURCE_ROOT / "agents", target_opencode / "agents", ignore=PAYLOAD_IGNORE)
    shutil.copytree(RESOURCE_ROOT / "templates", target_opencode / "templates", ignore=PAYLOAD_IGNORE)
    (target_opencode / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(RESOURCE_ROOT / "scripts" / "edward.py", target_opencode / "scripts" / "edward.py")
    (target_opencode / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(native_bin, target_opencode / "bin" / "edward")
    (target_opencode / "bin" / "edward").chmod(0o755)
    return PAYLOAD_ROOT


def build_app(payload: Path) -> Path:
    command = [
        *pyinstaller_command(),
        "--onedir",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(APP_WORK),
        "--specpath",
        str(APP_SPEC),
        "--add-data",
        f"{payload}:payload",
        "--add-data",
        f"{ensure_version_file()}:.",
        "--osx-bundle-identifier",
        "com.por2m.edward.installer",
        "--hidden-import",
        "tkinter",
        "--hidden-import",
        "tkinter.ttk",
        "--hidden-import",
        "tkinter.filedialog",
        "--hidden-import",
        "tkinter.messagebox",
        "--clean",
        "--noconfirm",
        str(INSTALLER_DIR / "macos_app.py"),
    ]
    run(command)
    if not APP_PATH.exists():
        raise SystemExit(f"app build missing: {APP_PATH}")

    # PyInstaller onedir also leaves a standalone folder next to the .app.
    standalone = DIST_DIR / APP_NAME
    if standalone.exists() and standalone.is_dir():
        shutil.rmtree(standalone)

    if shutil.which("codesign"):
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(APP_PATH)],
            check=False,
        )
    return APP_PATH


def zip_app(app: Path) -> Path | None:
    if not shutil.which("ditto"):
        return None
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    run(["ditto", "-c", "-k", "--keepParent", str(app), str(ZIP_PATH)])
    return ZIP_PATH


def main() -> int:
    if sys.platform != "darwin":
        print("此脚本只能在 macOS 上构建 .app", file=sys.stderr)
        return 1
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    native = build_native()
    payload = prepare_payload(native)
    app = build_app(payload)
    archive = zip_app(app)
    print("\n完成:")
    print(f"  版本:       {read_release_version()}")
    print(f"  native CLI: {native}")
    print(f"  macOS app:  {app}")
    if archive:
        print(f"  zip:        {archive}")
    print("\n双击 .app 选择目标项目即可安装。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
