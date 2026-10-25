"""Release metadata helpers for build scripts.

The release version has a single source of truth: release.yaml at the project
root. Build scripts read it here and embed the value into packaged artifacts.
"""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RELEASE_FILE = PROJECT_ROOT / "release.yaml"
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


def read_release_version(path: Path | None = None) -> str:
    release_file = path or RELEASE_FILE
    try:
        text = release_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"缺少发布版本文件: {release_file}")
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line.startswith("version:"):
            continue
        value = line.split(":", 1)[1].strip().strip("'\"")
        if value.startswith("v"):
            value = value[1:]
        if not VERSION_PATTERN.fullmatch(value):
            raise SystemExit(f"release.yaml 中的版本号非法: {value!r}（示例: 0.3.0）")
        return value
    raise SystemExit(f"release.yaml 缺少 version 字段: {release_file}")


def write_version_file(path: Path | None = None, version: str | None = None) -> Path:
    target = path or (PROJECT_ROOT / "VERSION")
    value = version or read_release_version()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value + "\n", encoding="utf-8")
    return target
