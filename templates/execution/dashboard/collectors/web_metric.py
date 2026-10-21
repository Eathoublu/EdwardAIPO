#!/usr/bin/env python3
"""Playwright 网页指标采集模板.

复制本文件为某一个指标的固定采集脚本，修改 URL / SELECTOR，
然后在 dashboard/metrics.json 里注册。stdout 只输出一行 JSON。

必要环境变量:
  METRIC_ID       指标 id，必须与 metrics.json 一致
  METRIC_URL      页面地址
  METRIC_SELECTOR CSS selector 或 Playwright locator，如 "#mrr" / "text=MRR"

可选:
  METRIC_UNIT
  METRIC_VALUE_REGEX  从文本中提取数字的正则，默认取第一个数字
  PLAYWRIGHT_STORAGE_STATE  登录态文件路径，不要把该文件提交到仓库
  PLAYWRIGHT_HEADLESS  1/0，默认 1
"""

from __future__ import annotations

import json
import os
import re
import sys

from playwright.sync_api import sync_playwright


def extract_number(text: str) -> float:
    pattern = os.environ.get("METRIC_VALUE_REGEX") or r"-?\d[\d,]*(?:\.\d+)?"
    match = re.search(pattern, text)
    if not match:
        raise ValueError(f"在文本中找不到数字: {text!r}")
    return float(match.group(0).replace(",", ""))


def main() -> int:
    metric_id = os.environ.get("METRIC_ID", "").strip()
    url = os.environ.get("METRIC_URL", "").strip()
    selector = os.environ.get("METRIC_SELECTOR", "body").strip()
    unit = os.environ.get("METRIC_UNIT", "").strip()
    storage_state = os.environ.get("PLAYWRIGHT_STORAGE_STATE", "").strip() or None
    headless = os.environ.get("PLAYWRIGHT_HEADLESS", "1") != "0"

    if not metric_id:
        raise SystemExit("缺少 METRIC_ID")
    if not url:
        raise SystemExit("缺少 METRIC_URL")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=60_000)
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=30_000)
        text = locator.inner_text()
        value = extract_number(text)
        observed_at = page.evaluate("new Date().toISOString()")
        context.close()
        browser.close()

    print(
        json.dumps(
            {
                "id": metric_id,
                "value": value,
                "unit": unit,
                "source": url,
                "observed_at": observed_at,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - collector contract: errors go to stderr
        print(f"web_metric error: {exc}", file=sys.stderr)
        raise SystemExit(1)
