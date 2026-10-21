#!/usr/bin/env python3
"""命令/API 指标采集模板.

复制本文件为某一个指标的固定采集脚本，在 METRIC_COMMAND 里放
稳定的 CLI / curl / 脚本命令。命令可以输出：
  * {"value": 123, "unit": "count"}  （推荐）
  * 123                              （裸数字）
  * 包含数字的普通文本（会取第一个数字）

stdout 只输出一行 JSON；诊断信息写 stderr。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone


def extract_number(text: str) -> float:
    pattern = os.environ.get("METRIC_VALUE_REGEX") or r"-?\d[\d,]*(?:\.\d+)?"
    match = re.search(pattern, text)
    if not match:
        raise ValueError(f"命令输出中找不到数字: {text!r}")
    return float(match.group(0).replace(",", ""))


def main() -> int:
    metric_id = os.environ.get("METRIC_ID", "").strip()
    command = os.environ.get("METRIC_COMMAND", "").strip()
    unit = os.environ.get("METRIC_UNIT", "").strip()
    if not metric_id:
        raise SystemExit("缺少 METRIC_ID")
    if not command:
        raise SystemExit("缺少 METRIC_COMMAND")

    completed = subprocess.run(
        command,
        shell=True,
        cwd=os.environ.get("EDWARD_PROJECT_ROOT") or os.getcwd(),
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("METRIC_TIMEOUT_SECONDS", "60")),
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "command failed").strip())

    raw = completed.stdout.strip()
    payload = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            payload = parsed
        elif isinstance(parsed, (int, float)):
            payload = {"value": parsed}
    except json.JSONDecodeError:
        pass

    if payload is None:
        payload = {"value": extract_number(raw)}

    value = payload.get("value")
    if value is None:
        raise ValueError("命令输出缺少 value")

    print(
        json.dumps(
            {
                "id": payload.get("id", metric_id),
                "value": value,
                "unit": payload.get("unit", unit),
                "source": payload.get("source", "command"),
                "observed_at": payload.get(
                    "observed_at",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"command_metric error: {exc}", file=sys.stderr)
        raise SystemExit(1)
