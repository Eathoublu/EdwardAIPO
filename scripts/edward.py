#!/usr/bin/env python3
"""Edward — an all-in-one scheduler for the project owner agent.

The service is intentionally dependency-free (Python standard library only):
  * init      create edward/ execution / codex / knowledge from templates
  * serve     wake Edward on schedule, or when a human writes report-reply
  * schedule  let Edward declare the next wake-up time
  * status    inspect scheduler + daemon + pending replies
  * collect   run fixed metric collectors and refresh dashboard
  * dashboard render dashboard from the latest metric snapshot
  * run-now   trigger a shift immediately
  * pause / resume / stop
"""

from __future__ import annotations

import argparse
import contextlib
import html as html_lib
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


VERSION = "0.2.0"
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # Installed as <project>/.opencode/bin/edward
    PROJECT_ROOT = Path(
        os.environ.get("EDWARD_PROJECT_ROOT", str(Path(sys.executable).resolve().parents[2]))
    ).resolve()
    OPENCODE_DIR = PROJECT_ROOT / ".opencode"
    SCRIPT_DIR = OPENCODE_DIR / "scripts"
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
    OPENCODE_DIR = SCRIPT_DIR.parent
    PROJECT_ROOT = Path(os.environ.get("EDWARD_PROJECT_ROOT", str(OPENCODE_DIR.parent))).resolve()

EDWARD_ROOT = PROJECT_ROOT / "edward"
EXECUTION_DIR = EDWARD_ROOT / "execution"
REPORTS_DIR = EXECUTION_DIR / "reports"
REPLIES_DIR = EXECUTION_DIR / "report-reply"
DASHBOARD_DIR = EXECUTION_DIR / "dashboard"
DASHBOARD_FILE = DASHBOARD_DIR / "dashboard.md"
METRICS_FILE = DASHBOARD_DIR / "metrics.json"
DASHBOARD_DATA_DIR = DASHBOARD_DIR / "data"
DASHBOARD_HISTORY_DIR = DASHBOARD_DATA_DIR / "history"
DASHBOARD_LATEST_FILE = DASHBOARD_DATA_DIR / "latest.json"
CODEX_DIR = EDWARD_ROOT / "codex"
KNOWLEDGE_DIR = EDWARD_ROOT / "knowledge"

TEMPLATES_DIR = OPENCODE_DIR / "templates"
STATE_DIR = OPENCODE_DIR / "state"
SCHEDULER_FILE = STATE_DIR / "scheduler.json"
DAEMON_FILE = STATE_DIR / "daemon.json"
UI_CONFIG_FILE = OPENCODE_DIR / "ui.json"
LOG_DIR = STATE_DIR / "logs"
RUN_LOG_DIR = LOG_DIR / "runs"
LOCK_DIR = STATE_DIR / "locks"
STATE_LOCK_FILE = LOCK_DIR / "state.lock"
RUN_LOCK_FILE = LOCK_DIR / "run.lock"
DAEMON_LOCK_FILE = LOCK_DIR / "daemon.lock"
EVENT_LOG = LOG_DIR / "edward.log"
DOC_EDITS_FILE = STATE_DIR / "doc-edits.json"

DEFAULT_INTERVAL_MINUTES = 360
FAILURE_BACKOFF_MINUTES = 10
REPLY_DEBOUNCE_SECONDS = 5
LOOP_TICK_SECONDS = 1
UI_DEFAULT_HOST = "127.0.0.1"
UI_DEFAULT_PORT = 8765
UI_DEFAULT_BASE_PATH = "/"
UI_DEFAULT_DOCUMENT_ROOT = "edward/execution/dashboard"
UI_COOKIE_NAME = "edward_ui_token"
DOC_EDIT_LIMIT = 200
DOC_CONTENT_MAX_BYTES = 512 * 1024

EXECUTION_DOCS = {
    "notebook": ("Notebook · 长期记忆", EXECUTION_DIR / "notebook.md"),
    "goal": ("目标 Goal", EXECUTION_DIR / "goal.md"),
    "plan": ("计划 Plan", EXECUTION_DIR / "plan.md"),
    "current_state": ("当前状态", EXECUTION_DIR / "current_state.md"),
}

EDITABLE_DOC_ROOTS = {
    "codex": ("法典域", CODEX_DIR),
    "knowledge": ("知识域", KNOWLEDGE_DIR),
}

ARTIFACT_MAP = {
    "execution/notebook.md": EXECUTION_DIR / "notebook.md",
    "execution/goal.md": EXECUTION_DIR / "goal.md",
    "execution/plan.md": EXECUTION_DIR / "plan.md",
    "execution/current_state.md": EXECUTION_DIR / "current_state.md",
    "execution/dashboard/dashboard.md": DASHBOARD_FILE,
    "execution/dashboard/metrics.json": METRICS_FILE,
    "execution/dashboard/collectors/web_metric.py": DASHBOARD_DIR / "collectors" / "web_metric.py",
    "execution/dashboard/collectors/command_metric.py": DASHBOARD_DIR / "collectors" / "command_metric.py",
    "execution/dashboard/collectors/README.md": DASHBOARD_DIR / "collectors" / "README.md",
    "codex/index.md": CODEX_DIR / "index.md",
    "codex/constraints.md": CODEX_DIR / "constraints.md",
    "knowledge/index.md": KNOWLEDGE_DIR / "index.md",
    "knowledge/architecture.md": KNOWLEDGE_DIR / "architecture.md",
    "knowledge/lessons.md": KNOWLEDGE_DIR / "lessons.md",
    "knowledge/domain/README.md": KNOWLEDGE_DIR / "domain" / "README.md",
    "knowledge/references/README.md": KNOWLEDGE_DIR / "references" / "README.md",
    "knowledge/explorations/README.md": KNOWLEDGE_DIR / "explorations" / "README.md",
}

# These are reference templates only; they are not copied into edward/.
TEMPLATE_FILES = set(ARTIFACT_MAP) | {
    "execution/report.md",
    "execution/report-reply.md",
    "execution/final-report.md",
}

class LockBusy(RuntimeError):
    pass


@dataclass
class RunResult:
    run_id: str
    reason: str
    started_at: str
    finished_at: str
    exit_code: int
    success: bool
    log_path: str
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Time / JSON helpers
# ---------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now().astimezone()


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now().tzinfo)
    return parsed


def parse_duration(text: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", text.lower())
    if not match:
        raise argparse.ArgumentTypeError(f"无法识别的时间间隔: {text!r}")
    value = float(match.group(1))
    unit = match.group(2) or "m"
    seconds = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }[unit]
    return timedelta(seconds=value * seconds)


def human_delta(target: datetime | None) -> str:
    if target is None:
        return "未设置"
    seconds = int((target - now()).total_seconds())
    if seconds <= 0:
        return "已到期"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes and not days:
        parts.append(f"{minutes}分钟")
    return " ".join(parts) or "不到1分钟"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        print(f"[edward] 读取 {path} 失败: {exc}", file=sys.stderr)
        return default


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def log_event(event: str, **fields: Any) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": iso(now()), "event": event, **fields}
    try:
        with EVENT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def file_lock(path: Path, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    if fcntl is not None:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            handle.close()
            raise LockBusy(str(path)) from exc
    try:
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def state_lock(blocking: bool = True) -> contextlib.AbstractContextManager[None]:
    return file_lock(STATE_LOCK_FILE, blocking=blocking)


def run_lock(blocking: bool = False) -> contextlib.AbstractContextManager[None]:
    return file_lock(RUN_LOCK_FILE, blocking=blocking)


# ---------------------------------------------------------------------------
# Document edit records
# ---------------------------------------------------------------------------


def default_doc_edits() -> dict[str, Any]:
    return {"version": 1, "entries": []}


def load_doc_edits() -> dict[str, Any]:
    data = load_json(DOC_EDITS_FILE, None)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return default_doc_edits()
    data["entries"] = [item for item in data["entries"] if isinstance(item, dict)]
    data.setdefault("version", 1)
    return data


def save_doc_edits(data: dict[str, Any]) -> None:
    entries = [item for item in data.get("entries", []) if isinstance(item, dict)]
    data["entries"] = entries[-DOC_EDIT_LIMIT:]
    data["version"] = 1
    write_json_atomic(DOC_EDITS_FILE, data)


def append_doc_edit(
    data: dict[str, Any], rel_path: str, before_bytes: int, after_bytes: int
) -> dict[str, Any]:
    entry = {
        "id": f"{now().strftime('%Y-%m-%dT%H-%M-%S')}-{secrets.token_hex(3)}",
        "ts": iso(now()),
        "path": rel_path,
        "before_bytes": int(before_bytes),
        "after_bytes": int(after_bytes),
        "consumed_at": None,
        "consumed_run_id": None,
    }
    data.setdefault("entries", []).append(entry)
    return entry


def record_doc_edit(rel_path: str, before_bytes: int, after_bytes: int) -> dict[str, Any]:
    with state_lock():
        data = load_doc_edits()
        entry = append_doc_edit(data, rel_path, before_bytes, after_bytes)
        save_doc_edits(data)
    log_event("doc_edit", path=rel_path, before_bytes=before_bytes, after_bytes=after_bytes)
    return entry


def pending_doc_edits() -> list[dict[str, Any]]:
    return [entry for entry in load_doc_edits().get("entries", []) if not entry.get("consumed_at")]


def consume_doc_edits(ids: list[str], run_id: str) -> int:
    wanted = {str(value) for value in ids if value}
    if not wanted:
        return 0
    consumed_at = iso(now())
    with state_lock():
        data = load_doc_edits()
        count = 0
        for entry in data.get("entries", []):
            if str(entry.get("id")) in wanted and not entry.get("consumed_at"):
                entry["consumed_at"] = consumed_at
                entry["consumed_run_id"] = run_id
                count += 1
        if count:
            save_doc_edits(data)
    return count


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def default_scheduler() -> dict[str, Any]:
    return {
        "version": 1,
        "next_wake_at": iso(now()),
        "default_interval_minutes": DEFAULT_INTERVAL_MINUTES,
        "wake_on_reply": True,
        "paused": False,
        "schedule_source": "default",
        "schedule_revision": 0,
        "initialized_at": iso(now()),
        "updated_at": iso(now()),
        "last_run_id": None,
        "last_run_started_at": None,
        "last_run_finished_at": None,
        "last_run_exit": None,
        "last_wake_reason": None,
        "retry_at": None,
        "seen_replies": {},
    }


def load_scheduler() -> dict[str, Any]:
    data = load_json(SCHEDULER_FILE, None)
    base = default_scheduler()
    if not isinstance(data, dict):
        data = base
    for key, value in base.items():
        data.setdefault(key, value)
    if not isinstance(data.get("seen_replies"), dict):
        data["seen_replies"] = {}
    return data


def save_scheduler(data: dict[str, Any]) -> None:
    data["updated_at"] = iso(now())
    write_json_atomic(SCHEDULER_FILE, data)


def update_scheduler(mutator) -> dict[str, Any]:
    with state_lock():
        data = load_scheduler()
        mutator(data)
        save_scheduler(data)
        return data


def is_due(data: dict[str, Any]) -> bool:
    retry_at = parse_datetime(data.get("retry_at"))
    if retry_at and now() < retry_at:
        return False
    next_wake = parse_datetime(data.get("next_wake_at"))
    return next_wake is None or now() >= next_wake


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Workspace init
# ---------------------------------------------------------------------------


def ensure_dirs() -> None:
    for path in (
        EXECUTION_DIR,
        REPORTS_DIR,
        REPLIES_DIR,
        DASHBOARD_DIR,
        DASHBOARD_DIR / "collectors",
        DASHBOARD_DATA_DIR,
        DASHBOARD_HISTORY_DIR,
        CODEX_DIR,
        KNOWLEDGE_DIR / "domain",
        KNOWLEDGE_DIR / "references",
        KNOWLEDGE_DIR / "explorations",
        STATE_DIR,
        LOG_DIR,
        RUN_LOG_DIR,
        LOCK_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def init_workspace(quiet: bool = False) -> dict[str, Any]:
    ensure_dirs()

    missing_templates = [key for key in TEMPLATE_FILES if not (TEMPLATES_DIR / key).exists()]
    if missing_templates:
        raise FileNotFoundError("缺少模板: " + ", ".join(missing_templates))

    created: list[str] = []
    skipped: list[str] = []

    for key, destination in ARTIFACT_MAP.items():
        source = TEMPLATES_DIR / key
        if destination.exists():
            skipped.append(str(destination.relative_to(PROJECT_ROOT)))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        created.append(str(destination.relative_to(PROJECT_ROOT)))

    (REPORTS_DIR / ".gitkeep").touch()
    (REPLIES_DIR / ".gitkeep").touch()

    state_gitignore = STATE_DIR / ".gitignore"
    if not state_gitignore.exists():
        state_gitignore.write_text("*\n!.gitignore\n", encoding="utf-8")

    if not UI_CONFIG_FILE.exists():
        write_json_atomic(UI_CONFIG_FILE, default_ui_config())
        created.append(str(UI_CONFIG_FILE.relative_to(PROJECT_ROOT)))
    else:
        skipped.append(str(UI_CONFIG_FILE.relative_to(PROJECT_ROOT)))

    with state_lock():
        if not SCHEDULER_FILE.exists():
            data = default_scheduler()
            data["next_wake_at"] = iso(now())
            data["schedule_source"] = "default"
            save_scheduler(data)
            created.append(str(SCHEDULER_FILE.relative_to(PROJECT_ROOT)))
        else:
            skipped.append(str(SCHEDULER_FILE.relative_to(PROJECT_ROOT)))

    agent_file = OPENCODE_DIR / "agents" / "edward.md"
    if not agent_file.exists():
        raise FileNotFoundError(f"缺少 agent 定义: {agent_file}")

    result = {"created": created, "skipped": skipped, "root": str(PROJECT_ROOT)}
    if not quiet:
        if created:
            print("已创建:")
            for item in created:
                print(f"  + {item}")
        if skipped:
            print("已存在，跳过:")
            for item in skipped:
                print(f"  = {item}")
        print(f"\nEdward 工作区已就绪: {EDWARD_ROOT}")
        edward_bin = OPENCODE_DIR / "bin" / "edward"
        print(f"下一步: {edward_bin} serve")
        print(f"或后台运行: {edward_bin} serve --daemon")
    log_event("init", created=len(created), skipped=len(skipped))
    return result


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------


def scan_replies() -> dict[str, dict[str, Any]]:
    REPLIES_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(REPLIES_DIR.glob("*.md")):
        if path.name.startswith("."):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        result[path.name] = {
            "name": path.name,
            "path": str(path),
            "fingerprint": f"{stat.st_mtime_ns}:{stat.st_size}",
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        }
    return result


class ReplyWatcher:
    """Debounce report-reply edits so a half-written file never wakes Edward."""

    def __init__(self) -> None:
        self.candidates: dict[str, dict[str, Any]] = {}

    def poll(self, seen: dict[str, str]) -> list[dict[str, Any]]:
        current = scan_replies()
        now_monotonic = time.monotonic()
        ready: list[dict[str, Any]] = []

        for name in list(self.candidates):
            if name not in current or seen.get(name) == current[name]["fingerprint"]:
                self.candidates.pop(name, None)

        for name, info in current.items():
            if seen.get(name) == info["fingerprint"]:
                self.candidates.pop(name, None)
                continue
            candidate = self.candidates.get(name)
            if candidate is None or candidate["fingerprint"] != info["fingerprint"]:
                self.candidates[name] = {
                    "fingerprint": info["fingerprint"],
                    "since": now_monotonic,
                    "info": info,
                }
            elif now_monotonic - candidate["since"] >= REPLY_DEBOUNCE_SECONDS:
                ready.append(info)

        return ready


def mark_replies_seen(infos: list[dict[str, Any]]) -> None:
    if not infos:
        return

    def mutate(data: dict[str, Any]) -> None:
        seen = data.setdefault("seen_replies", {})
        for info in infos:
            seen[info["name"]] = info["fingerprint"]
        existing = set(scan_replies())
        data["seen_replies"] = {name: value for name, value in seen.items() if name in existing}

    update_scheduler(mutate)


# ---------------------------------------------------------------------------
# Dashboard / metrics
# ---------------------------------------------------------------------------


def load_metrics_config() -> dict[str, Any]:
    data = load_json(METRICS_FILE, {})
    if not isinstance(data, dict):
        return {"version": 1, "metrics": []}
    if not isinstance(data.get("metrics"), list):
        data["metrics"] = []
    return data


def parse_collector_output(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("采集脚本没有输出")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("采集脚本输出不是 JSON 对象")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"采集脚本输出 JSON 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("采集脚本必须输出一个 JSON 对象")
    return payload


def normalize_number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("value 必须是数字")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("%", "")
        return float(cleaned)
    raise ValueError(f"无法解析 value: {value!r}")


def run_collector(metric: dict[str, Any]) -> dict[str, Any]:
    metric_id = str(metric.get("id") or "").strip()
    if not metric_id:
        raise ValueError("指标缺少 id")
    command = str(metric.get("collector") or "").strip()
    if not command:
        raise ValueError("指标缺少 collector 命令")

    timeout = int(metric.get("timeout_seconds") or 60)
    argv = shlex.split(command)
    if not argv:
        raise ValueError("collector 命令为空")

    env = os.environ.copy()
    metric_env = metric.get("env")
    if isinstance(metric_env, dict):
        env.update({str(key): str(value) for key, value in metric_env.items()})

    completed = subprocess.run(
        argv,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        tail = "\n".join(stderr.splitlines()[-4:])
        raise RuntimeError(f"collector exit={completed.returncode}: {tail}")

    payload = parse_collector_output(completed.stdout)
    value = normalize_number(payload.get("value"))
    reported_id = str(payload.get("id") or metric_id)
    if reported_id != metric_id:
        raise ValueError(f"collector 返回 id={reported_id!r}，与配置 id={metric_id!r} 不一致")

    return {
        "id": metric_id,
        "value": value,
        "unit": payload.get("unit", metric.get("unit")),
        "source": payload.get("source", metric.get("source")),
        "observed_at": payload.get("observed_at") or iso(now()),
        "meta": payload.get("meta") or {},
        "status": "ok",
        "error": None,
        "stale": False,
        "collected_at": iso(now()),
    }


def metric_status(record: dict[str, Any]) -> str:
    if record.get("status") == "error":
        return "异常"
    value = record.get("value")
    target = record.get("target")
    if value is None:
        return "无数据"
    if target is None:
        return "—"
    try:
        value_number = float(value)
        target_number = float(target)
    except (TypeError, ValueError):
        return "—"
    direction = str(record.get("direction") or "up").lower()
    achieved = value_number >= target_number if direction == "up" else value_number <= target_number
    return "达标" if achieved else "进行中"


def format_number(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def render_dashboard(latest: dict[str, Any] | None = None) -> Path:
    if latest is None:
        latest = load_json(DASHBOARD_LATEST_FILE, {})
    if not isinstance(latest, dict):
        latest = {}
    config = load_metrics_config()
    configured = [item for item in config.get("metrics", []) if isinstance(item, dict) and item.get("id")]
    config_by_id = {str(item["id"]): item for item in configured}
    metrics = latest.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
    health = latest.get("health", {})
    if not isinstance(health, dict):
        health = {}
    updated_at = latest.get("updated_at")

    lines = [
        "# Dashboard",
        "",
        "> 由固定采集脚本生成。查数字：`cat edward/execution/dashboard/data/latest.json`；",
        "> 刷新：`.opencode/bin/edward collect`。",
        "",
        f"- 更新时间：{updated_at or '尚未采集'}",
        f"- 采集状态：ok={health.get('ok', 0)} / failed={health.get('failed', 0)} / skipped={health.get('skipped', 0)}",
        "",
        "## 指标",
        "",
    ]

    ordered_ids: list[str] = []
    for item in configured:
        metric_id = str(item["id"])
        if metric_id not in ordered_ids:
            ordered_ids.append(metric_id)
    for metric_id in metrics:
        if metric_id not in ordered_ids:
            ordered_ids.append(metric_id)

    if not ordered_ids:
        lines.extend(
            [
                "尚未配置指标。",
                "",
                "1. 编辑 `edward/execution/dashboard/metrics.json`。",
                "2. 在 `edward/execution/dashboard/collectors/` 写固定采集脚本。",
                "3. 运行 `.opencode/bin/edward collect`。",
            ]
        )
    else:
        lines.append("| 指标 | 当前值 | 目标 | 状态 | 来源 | 观测时间 |")
        lines.append("|---|---:|---:|---|---|---|")
        for metric_id in ordered_ids:
            record = metrics.get(metric_id, {})
            config_item = config_by_id.get(metric_id, {})
            name = record.get("name") or config_item.get("name") or metric_id
            unit = record.get("unit") or config_item.get("unit") or ""
            target = record.get("target", config_item.get("target"))
            value_text = format_number(record.get("value"))
            if record.get("value") is not None and unit:
                value_text = f"{value_text} {unit}"
            target_text = format_number(target)
            if target is not None and unit:
                target_text = f"{target_text} {unit}"
            source = str(record.get("source") or config_item.get("source") or "").replace("|", "\\|")
            if not record:
                status = "未启用" if not config_item.get("enabled", True) else "无数据"
            else:
                status = metric_status(record)
            lines.append(
                f"| {name} | {value_text} | {target_text} | {status} | {source or '—'} | {record.get('observed_at') or '—'} |"
            )

        errors = [(metric_id, metrics[metric_id]) for metric_id in ordered_ids if metrics.get(metric_id, {}).get("status") == "error"]
        if errors:
            lines.extend(["", "## 采集异常", "", "| 指标 | 错误 |", "|---|---|"])
            for metric_id, record in errors:
                lines.append(f"| {metric_id} | {str(record.get('error') or '').replace('|', '\\|')} |")

    lines.extend(
        [
            "",
            "## 查询入口",
            "",
            "- 机器可读最新值：`edward/execution/dashboard/data/latest.json`",
            "- 历史快照：`edward/execution/dashboard/data/history/`",
            "- 指标定义：`edward/execution/dashboard/metrics.json`",
            "- 固定采集脚本：`edward/execution/dashboard/collectors/`",
        ]
    )

    DASHBOARD_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return DASHBOARD_FILE


def collect_metrics(only: str | None = None, quiet: bool = False, render: bool = True) -> dict[str, Any]:
    config = load_metrics_config()
    metrics_config = [item for item in config.get("metrics", []) if isinstance(item, dict) and item.get("id")]
    previous = load_json(DASHBOARD_LATEST_FILE, {})
    previous_metrics = previous.get("metrics", {}) if isinstance(previous, dict) else {}

    latest: dict[str, Any] = {
        "updated_at": iso(now()),
        "metrics": {},
        "health": {"ok": 0, "failed": 0, "skipped": 0},
    }

    selected = [item for item in metrics_config if str(item.get("id")) == only] if only else metrics_config
    selected_ids = {str(item["id"]) for item in selected}

    for item in metrics_config:
        metric_id = str(item["id"])
        if only and metric_id not in selected_ids:
            if metric_id in previous_metrics:
                latest["metrics"][metric_id] = previous_metrics[metric_id]
            continue
        if not item.get("enabled", True):
            latest["health"]["skipped"] += 1
            if metric_id in previous_metrics:
                latest["metrics"][metric_id] = previous_metrics[metric_id]
            continue

        try:
            record = run_collector(item)
            record["name"] = item.get("name") or metric_id
            record["target"] = item.get("target")
            record["direction"] = item.get("direction", "up")
            if record.get("unit") is None:
                record["unit"] = item.get("unit")
            latest["health"]["ok"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad collector must not stop the rest
            old = previous_metrics.get(metric_id, {})
            record = {
                "id": metric_id,
                "name": item.get("name") or metric_id,
                "value": old.get("value"),
                "unit": item.get("unit"),
                "target": item.get("target"),
                "direction": item.get("direction", "up"),
                "source": item.get("source"),
                "observed_at": old.get("observed_at"),
                "status": "error",
                "error": str(exc),
                "stale": bool(old),
                "collected_at": iso(now()),
            }
            latest["health"]["failed"] += 1
        latest["metrics"][metric_id] = record

    DASHBOARD_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(DASHBOARD_LATEST_FILE, latest)
    history_name = now().strftime("%Y-%m-%dT%H-%M-%S") + ".json"
    write_json_atomic(DASHBOARD_HISTORY_DIR / history_name, latest)
    if render:
        render_dashboard(latest)

    log_event(
        "collect",
        ok=latest["health"]["ok"],
        failed=latest["health"]["failed"],
        skipped=latest["health"]["skipped"],
    )

    if not quiet:
        print(f"看板已刷新: {DASHBOARD_FILE}")
        print(f"指标: ok={latest['health']['ok']} failed={latest['health']['failed']} skipped={latest['health']['skipped']}")
        if latest["health"]["failed"]:
            for metric_id, record in latest["metrics"].items():
                if record.get("status") == "error":
                    print(f"  - {metric_id}: {record.get('error')}", file=sys.stderr)

    return latest


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------


def next_run_id() -> str:
    base = now().strftime("%Y-%m-%dT%H-%M-%S")
    candidate = base
    index = 2
    while (REPORTS_DIR / f"{candidate}.md").exists() or (RUN_LOG_DIR / f"{candidate}.log").exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def build_prompt(
    reason: str,
    reply_paths: list[str],
    scheduler_data: dict[str, Any],
    doc_edits: list[dict[str, Any]] | None = None,
) -> str:
    if reason == "reply" and reply_paths:
        reply_text = "\n".join(f"- {path}" for path in reply_paths)
        intro = (
            "上班。本次是人类 report-reply 唤醒。\n"
            "请先读取以下回复文件，再按 .opencode/agents/edward.md 的流程推进：\n"
            f"{reply_text}\n"
        )
    elif reason == "retry":
        intro = "上班。上一次运行失败，本次是重试。请先检查 .opencode/state/logs/ 的失败原因，再继续。\n"
    elif reason == "manual":
        intro = "上班。本次是人工手动触发。\n"
    else:
        intro = "上班。本次是定时唤醒。\n"

    intro += (
        "\n请读取 edward/codex/index.md、goal.md、plan.md、current_state.md、notebook.md、"
        "相关 knowledge 和最近一份 report，判断当前阶段并推进本班次工作。\n"
        "\n收工前必须完成：\n"
        "1. 更新 edward/execution/current_state.md（自然语言，不要写调度解析格式）。\n"
        "2. 写 edward/execution/reports/<EDWARD_RUN_ID>.md，包含进度、完成情况、阻塞项、待决策项。\n"
        "3. 用 python3 .opencode/scripts/edward.py schedule --in <时长> 或 --at <时间> 声明下次唤醒。\n"
        "4. 如果目标达成，使用独立 final-report 模板写 reports/final-<EDWARD_RUN_ID>.md，并等待人类在 report-reply 验收。\n"
    )

    if reason == "interval" and scheduler_data.get("schedule_source") == "default":
        intro += (
            "\n【提示】未设置上班时间间隔。调度器本次使用了默认 6h 兜底。"
            "请在收工前务必调用 edward.py schedule 设置下一次唤醒。\n"
        )

    if doc_edits:
        lines = ["\n【人类编辑提醒】以下文档在上一班次后被人类在 UI 中修改，请先复核再推进本班次工作："]
        for entry in doc_edits:
            lines.append(f"- {entry.get('ts') or '—'} · {entry.get('path') or '—'}")
        lines.append(
            "复核后再判断是否需要更新 plan / current_state，并在本班次 report 中说明处理结果。\n"
        )
        intro += "\n".join(lines)

    return intro


def run_agent(
    reason: str,
    reply_paths: list[str] | None = None,
    dry_run: bool = False,
) -> RunResult:
    reply_paths = reply_paths or []
    run_id = next_run_id()
    started = now()
    log_path = RUN_LOG_DIR / f"{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not dry_run and os.environ.get("EDWARD_SKIP_COLLECT", "0") != "1":
        try:
            collect_metrics(quiet=True)
        except Exception as exc:  # noqa: BLE001 - stale dashboard must not block the shift
            log_event("collect_before_run_error", error=str(exc))
            print(f"[edward] 看板采集失败，继续上班: {exc}", file=sys.stderr)

    scheduler_data = load_scheduler()
    doc_edits = pending_doc_edits()
    prompt = build_prompt(reason, reply_paths, scheduler_data, doc_edits=doc_edits)

    opencode_bin = os.environ.get("EDWARD_OPENCODE_BIN", "opencode")
    command = [opencode_bin, "run", "--agent", "edward"]
    if os.environ.get("EDWARD_OPENCODE_AUTO", "1") != "0":
        command.append("--auto")
    command.append(prompt)

    env = os.environ.copy()
    env.update(
        {
            "EDWARD_RUN_ID": run_id,
            "EDWARD_WAKE_REASON": reason,
            "EDWARD_REPLY_FILES": "\n".join(reply_paths),
            "EDWARD_PROJECT_ROOT": str(PROJECT_ROOT),
            "EDWARD_SCHEDULER": str(SCHEDULER_FILE),
            "EDWARD_EXECUTION_DIR": str(EXECUTION_DIR),
        }
    )

    header = (
        f"run_id: {run_id}\n"
        f"reason: {reason}\n"
        f"started_at: {iso(started)}\n"
        f"project_root: {PROJECT_ROOT}\n"
        f"cwd: {PROJECT_ROOT}\n"
        f"command: {' '.join(command[:-1])} <prompt>\n"
        f"reply_files: {reply_paths}\n"
        f"dry_run: {dry_run}\n"
        "\n--- prompt ---\n"
        f"{prompt}\n"
        "--- output ---\n"
    )

    if dry_run:
        log_path.write_text(header + "[dry-run] opencode 未执行\n", encoding="utf-8")
        print(f"[edward] dry-run {run_id} ({reason})")
        print(f"[edward] log: {log_path}")
        return RunResult(
            run_id=run_id,
            reason=reason,
            started_at=iso(started),
            finished_at=iso(now()),
            exit_code=0,
            success=True,
            log_path=str(log_path),
            dry_run=True,
        )

    if shutil.which(opencode_bin) is None:
        log_path.write_text(header + f"错误: 找不到 opencode 可执行文件 {opencode_bin!r}\n", encoding="utf-8")
        print(f"[edward] 找不到 opencode: {opencode_bin}", file=sys.stderr)
        return RunResult(
            run_id=run_id,
            reason=reason,
            started_at=iso(started),
            finished_at=iso(now()),
            exit_code=127,
            success=False,
            log_path=str(log_path),
        )

    timeout_value = os.environ.get("EDWARD_RUN_TIMEOUT", "0").strip()
    timeout = int(timeout_value) if timeout_value.isdigit() and int(timeout_value) > 0 else None
    print(f"[edward] run {run_id} ({reason}) ...")
    log_event("run_start", run_id=run_id, reason=reason, reply_files=reply_paths)

    try:
        with run_lock(blocking=False):
            completed = subprocess.run(
                command,
                cwd=str(PROJECT_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except LockBusy:
        message = "另一个 Edward 班次正在运行，跳过本次触发。\n"
        log_path.write_text(header + message, encoding="utf-8")
        print(f"[edward] {message.strip()}", file=sys.stderr)
        return RunResult(
            run_id=run_id,
            reason=reason,
            started_at=iso(started),
            finished_at=iso(now()),
            exit_code=-1,
            success=False,
            log_path=str(log_path),
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        log_path.write_text(header + output + f"\n超时: {timeout}s\n", encoding="utf-8")
        print(f"[edward] run {run_id} 超时", file=sys.stderr)
        return RunResult(
            run_id=run_id,
            reason=reason,
            started_at=iso(started),
            finished_at=iso(now()),
            exit_code=124,
            success=False,
            log_path=str(log_path),
        )
    except Exception as exc:  # noqa: BLE001 - the service must keep running
        log_path.write_text(header + f"运行异常: {exc}\n", encoding="utf-8")
        print(f"[edward] run {run_id} 异常: {exc}", file=sys.stderr)
        return RunResult(
            run_id=run_id,
            reason=reason,
            started_at=iso(started),
            finished_at=iso(now()),
            exit_code=1,
            success=False,
            log_path=str(log_path),
        )

    if doc_edits:
        try:
            consume_doc_edits([str(entry.get("id") or "") for entry in doc_edits], run_id)
        except Exception as exc:  # noqa: BLE001 - a notice failure must not break the shift
            log_event("doc_edits_consume_error", run_id=run_id, error=str(exc))

    output = header + (completed.stdout or "")
    if completed.stderr:
        output += "\n--- stderr ---\n" + completed.stderr
    output += f"\n--- exit: {completed.returncode} ---\n"
    log_path.write_text(output, encoding="utf-8")
    log_event("run_end", run_id=run_id, reason=reason, exit_code=completed.returncode)

    success = completed.returncode == 0
    print(f"[edward] run {run_id} exit={completed.returncode}")
    print(f"[edward] log: {log_path}")
    if not success:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-8:]
        if tail:
            print("[edward] 失败摘要:", file=sys.stderr)
            for line in tail:
                print(f"  {line}", file=sys.stderr)

    return RunResult(
        run_id=run_id,
        reason=reason,
        started_at=iso(started),
        finished_at=iso(now()),
        exit_code=completed.returncode,
        success=success,
        log_path=str(log_path),
    )


def apply_run_result(result: RunResult, revision_before: int) -> None:
    """Persist run outcome and apply fallback schedule if Edward did not set one."""

    def mutate(data: dict[str, Any]) -> None:
        data["last_run_id"] = result.run_id
        data["last_run_started_at"] = result.started_at
        data["last_run_finished_at"] = result.finished_at
        data["last_run_exit"] = result.exit_code
        data["last_wake_reason"] = result.reason

        revision_after = int(data.get("schedule_revision", 0))
        if revision_after != revision_before:
            if result.success:
                data["retry_at"] = None
            else:
                data["retry_at"] = iso(now() + timedelta(minutes=FAILURE_BACKOFF_MINUTES))
            return

        if result.success:
            interval = int(data.get("default_interval_minutes", DEFAULT_INTERVAL_MINUTES))
            data["next_wake_at"] = iso(now() + timedelta(minutes=interval))
            data["schedule_source"] = "default"
            data["schedule_revision"] = revision_before + 1
            data["retry_at"] = None
        else:
            data["next_wake_at"] = iso(now() + timedelta(minutes=FAILURE_BACKOFF_MINUTES))
            data["schedule_source"] = "retry"
            data["schedule_revision"] = revision_before + 1
            data["retry_at"] = iso(now() + timedelta(minutes=FAILURE_BACKOFF_MINUTES))

    update_scheduler(mutate)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    try:
        init_workspace()
    except Exception as exc:  # noqa: BLE001
        print(f"[edward] init 失败: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    target: datetime
    if args.in_value:
        try:
            target = now() + parse_duration(args.in_value)
        except argparse.ArgumentTypeError as exc:
            print(f"[edward] {exc}", file=sys.stderr)
            return 2
    elif args.at_value:
        try:
            target = parse_datetime(args.at_value)  # type: ignore[assignment]
        except ValueError as exc:
            print(f"[edward] 时间格式错误: {exc}", file=sys.stderr)
            return 2
        if target is None:
            print("[edward] --at 不能为空", file=sys.stderr)
            return 2
    else:
        print("[edward] 需要 --in 或 --at", file=sys.stderr)
        return 2

    def mutate(data: dict[str, Any]) -> None:
        data["next_wake_at"] = iso(target)
        data["schedule_source"] = "agent"
        data["schedule_revision"] = int(data.get("schedule_revision", 0)) + 1
        data["retry_at"] = None

    update_scheduler(mutate)
    delta = human_delta(target)
    suffix = "" if delta == "已到期" else "后"
    print(f"下次唤醒: {iso(target)}（{delta}{suffix}）")
    print("调度来源: agent")
    log_event("schedule_set", next_wake_at=iso(target), source="agent")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    update_scheduler(lambda data: data.__setitem__("paused", True))
    print("Edward 调度已暂停。")
    log_event("pause")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    update_scheduler(lambda data: data.__setitem__("paused", False))
    print("Edward 调度已恢复。")
    log_event("resume")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    daemon = load_json(DAEMON_FILE, {})
    pid = daemon.get("pid")
    if not pid_alive(pid):
        print("Edward 服务未运行。")
        return 0
    print(f"正在停止 Edward 服务 (pid {pid}) ...")
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError as exc:
        print(f"[edward] 停止失败: {exc}", file=sys.stderr)
        return 1
    for _ in range(20):
        time.sleep(0.5)
        if not pid_alive(int(pid)):
            print("已停止。")
            return 0
    print("已发送停止信号，但进程仍在运行。", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    scheduler = load_scheduler()
    daemon = load_json(DAEMON_FILE, {})
    pid = daemon.get("pid")
    running = pid_alive(pid)
    current_replies = scan_replies()
    seen = scheduler.get("seen_replies", {})
    pending = [
        name
        for name, info in current_replies.items()
        if seen.get(name) != info["fingerprint"]
    ]
    next_wake = parse_datetime(scheduler.get("next_wake_at"))
    retry_at = parse_datetime(scheduler.get("retry_at"))
    dashboard_latest = load_json(DASHBOARD_LATEST_FILE, {})
    dashboard_metrics = dashboard_latest.get("metrics", {}) if isinstance(dashboard_latest, dict) else {}
    dashboard_health = dashboard_latest.get("health", {}) if isinstance(dashboard_latest, dict) else {}
    dashboard_updated = dashboard_latest.get("updated_at") if isinstance(dashboard_latest, dict) else None
    try:
        ui_config = load_ui_config(None)
        ui_payload = {
            "enabled": bool(ui_config.get("enabled", True)),
            "url": ui_config_url(ui_config),
            "token_required": bool(ui_config.get("token")),
            "host": ui_config.get("host"),
        }
    except Exception as exc:  # noqa: BLE001
        ui_payload = {"enabled": False, "error": str(exc)}

    payload = {
        "project_root": str(PROJECT_ROOT),
        "edward_root": str(EDWARD_ROOT),
        "initialized": (EXECUTION_DIR / "goal.md").exists(),
        "scheduler": scheduler,
        "daemon": {**daemon, "pid": pid, "running": running},
        "pending_replies": pending,
        "report_count": len(list(REPORTS_DIR.glob("*.md"))),
        "reply_count": len(current_replies),
        "dashboard": {
            "path": str(DASHBOARD_FILE),
            "updated_at": dashboard_updated,
            "metric_count": len(dashboard_metrics),
            "health": dashboard_health,
        },
        "ui": ui_payload,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("Edward status")
    print(f"  project:   {PROJECT_ROOT}")
    print(f"  workspace: {EDWARD_ROOT}")
    print(f"  initialized: {'yes' if payload['initialized'] else 'no'}")
    print(f"  daemon:    {'running (pid ' + str(pid) + ')' if running else 'stopped'}")
    print(f"  paused:    {'yes' if scheduler.get('paused') else 'no'}")
    print(
        "  next wake: "
        f"{iso(next_wake) if next_wake else '未设置'}"
        f"（{human_delta(next_wake)}）"
        f" source={scheduler.get('schedule_source', 'unknown')}"
    )
    print(f"  wake on reply: {'yes' if scheduler.get('wake_on_reply', True) else 'no'}")
    print(f"  default interval: {scheduler.get('default_interval_minutes', DEFAULT_INTERVAL_MINUTES)}m")
    if retry_at and now() < retry_at:
        print(f"  retry in:  {human_delta(retry_at)}")
    print(f"  last run:  {scheduler.get('last_run_id') or '无'}")
    print(f"  reports:   {payload['report_count']}  replies: {payload['reply_count']}")
    print(
        "  dashboard: "
        f"{len(dashboard_metrics)} metrics, "
        f"failed={dashboard_health.get('failed', 0)}, "
        f"skipped={dashboard_health.get('skipped', 0)}, "
        f"updated={dashboard_updated or 'never'}"
    )
    if ui_payload.get("error"):
        print(f"  ui:        disabled ({ui_payload['error']})")
    elif ui_payload.get("enabled"):
        print(f"  ui:        {ui_payload['url']} {'(token required)' if ui_payload.get('token_required') else ''}".rstrip())
    else:
        print("  ui:        disabled")
    if pending:
        print(f"  pending report-reply: {len(pending)}")
        for name in pending:
            print(f"    - {name}")
    else:
        print("  pending report-reply: 0")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    config = load_metrics_config()
    metrics = [item for item in config.get("metrics", []) if isinstance(item, dict) and item.get("id")]
    if not metrics:
        print("尚未配置指标。请在 edward/execution/dashboard/metrics.json 中添加指标和 collector。")
        render_dashboard()
        return 0
    if args.metric and not any(str(item.get("id")) == args.metric for item in metrics):
        print(f"[edward] 找不到指标: {args.metric}", file=sys.stderr)
        return 2
    latest = collect_metrics(only=args.metric, quiet=args.quiet or args.json)
    if args.json:
        print(json.dumps(latest, ensure_ascii=False, indent=2))
    return 0 if latest["health"]["failed"] == 0 else 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    path = render_dashboard()
    latest = load_json(DASHBOARD_LATEST_FILE, {})
    if args.json:
        print(json.dumps(latest, ensure_ascii=False, indent=2))
    else:
        print(f"看板: {path}")
        metrics = latest.get("metrics", {}) if isinstance(latest, dict) else {}
        print(f"指标数: {len(metrics)}")
        if not metrics:
            print("尚未采集到数字。编辑 metrics.json 后运行: python3 .opencode/scripts/edward.py collect")
    return 0


def cmd_run_now(args: argparse.Namespace) -> int:
    if not (EXECUTION_DIR / "goal.md").exists():
        try:
            init_workspace(quiet=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[edward] 初始化失败: {exc}", file=sys.stderr)
            return 1

    scheduler = load_scheduler()
    seen = scheduler.get("seen_replies", {})
    unseen = [
        info
        for name, info in scan_replies().items()
        if seen.get(name) != info["fingerprint"]
    ]
    revision_before = int(scheduler.get("schedule_revision", 0))
    result = run_agent(
        "manual",
        reply_paths=[info["path"] for info in unseen],
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        if result.success:
            mark_replies_seen(unseen)
        apply_run_result(result, revision_before)
    return 0 if result.success else 1


def cmd_serve(args: argparse.Namespace) -> int:
    if not (EXECUTION_DIR / "goal.md").exists():
        try:
            init_workspace(quiet=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[edward] 初始化失败: {exc}", file=sys.stderr)
            return 1

    if args.daemon:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        output = (LOG_DIR / "daemon.out").open("ab", buffering=0)
        if hasattr(os, "fork"):
            sys.stdout.flush()
            sys.stderr.flush()
            pid = os.fork()
            if pid > 0:
                output.close()
                print(f"Edward 服务已在后台启动，pid={pid}")
                print(f"日志: {LOG_DIR / 'daemon.out'}")
                return 0
            os.setsid()
            devnull = open(os.devnull, "rb")
            os.dup2(devnull.fileno(), 0)
            os.dup2(output.fileno(), 1)
            os.dup2(output.fileno(), 2)
        else:
            spawn_command = [sys.executable, "serve"] if FROZEN else [sys.executable, str(Path(__file__).resolve()), "serve"]
            child_env = {key: value for key, value in os.environ.items() if not key.startswith("_PYI_")}
            process = subprocess.Popen(
                spawn_command,
                cwd=str(PROJECT_ROOT),
                stdout=output,
                stderr=output,
                start_new_session=True,
                env=child_env,
            )
            print(f"Edward 服务已在后台启动，pid={process.pid}")
            print(f"日志: {LOG_DIR / 'daemon.out'}")
            return 0

    try:
        daemon_lock = file_lock(DAEMON_LOCK_FILE, blocking=False)
        daemon_lock.__enter__()
    except LockBusy:
        print("Edward 服务已经在运行。", file=sys.stderr)
        return 1

    stopping = {"value": False}

    def handle_signal(signum, frame):  # noqa: ANN001
        stopping["value"] = True
        print(f"\n[edward] 收到信号 {signum}，准备退出 ...")

    previous_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.signal(sig, handle_signal)

    daemon_info = {
        "pid": os.getpid(),
        "started_at": iso(now()),
        "version": VERSION,
        "project_root": str(PROJECT_ROOT),
    }
    try:
        write_json_atomic(DAEMON_FILE, daemon_info)
    except Exception:
        daemon_lock.__exit__(None, None, None)
        raise
    log_event("serve_start", pid=os.getpid(), project_root=str(PROJECT_ROOT))
    print(f"Edward 服务已启动 (pid {os.getpid()})")
    print(f"调度状态: {SCHEDULER_FILE}")

    start_ui = bool(getattr(args, "ui", False)) or (
        not bool(getattr(args, "no_ui", False))
        and not bool(getattr(args, "once", False))
        and not bool(getattr(args, "dry_run", False))
    )
    if start_ui:
        try:
            ensure_ui_config()
            ui_config = load_ui_config(args)
            if getattr(args, "ui", False) or ui_config.get("enabled", True):
                _, _, ui_value = ui_start_background(ui_config)
                print(f"Edward UI: {ui_value}")
                if ui_config["token"]:
                    print("UI token: 已启用")
                log_event("ui_start", url=ui_value)
        except Exception as exc:  # noqa: BLE001 - UI failure must not stop the scheduler
            log_event("ui_error", error=str(exc))
            print(f"[edward] UI 启动失败，调度继续: {exc}", file=sys.stderr)

    watcher = ReplyWatcher()
    exit_code = 0
    try:
        while not stopping["value"]:
            scheduler = load_scheduler()

            if scheduler.get("paused"):
                if args.once:
                    break
                time.sleep(LOOP_TICK_SECONDS)
                continue

            retry_at = parse_datetime(scheduler.get("retry_at"))
            if retry_at and now() < retry_at:
                if args.once:
                    break
                time.sleep(LOOP_TICK_SECONDS)
                continue

            reply_ready: list[dict[str, Any]] = []
            if scheduler.get("wake_on_reply", True):
                reply_ready = watcher.poll(scheduler.get("seen_replies", {}))

            wake_replies = [info for info in reply_ready if reply_wants_wake(Path(info["path"]))]
            silent_replies = [info for info in reply_ready if info not in wake_replies]
            if silent_replies:
                mark_replies_seen(silent_replies)
                log_event("reply_silent", files=[info["name"] for info in silent_replies])

            if wake_replies:
                reason = "reply"
                reply_paths = [info["path"] for info in wake_replies]
            elif is_due(scheduler):
                reason = "interval"
                reply_paths = []
            else:
                if args.once:
                    break
                time.sleep(LOOP_TICK_SECONDS)
                continue

            revision_before = int(scheduler.get("schedule_revision", 0))
            result = run_agent(reason, reply_paths=reply_paths, dry_run=args.dry_run)

            if args.dry_run:
                if args.once:
                    break
                time.sleep(LOOP_TICK_SECONDS)
                continue

            if result.success:
                mark_replies_seen(wake_replies)
            apply_run_result(result, revision_before)

            if args.once:
                break

            # If a reply arrived while the shift was running, poll again soon.
            time.sleep(LOOP_TICK_SECONDS)
    except KeyboardInterrupt:
        stopping["value"] = True
    except Exception as exc:  # noqa: BLE001 - keep the scheduler observable
        log_event("serve_error", error=str(exc))
        print(f"[edward] 服务异常: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        daemon_info["stopped_at"] = iso(now())
        daemon_info["pid"] = None
        write_json_atomic(DAEMON_FILE, daemon_info)
        log_event("serve_stop", pid=os.getpid())
        print("Edward 服务已停止。")
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        daemon_lock.__exit__(None, None, None)

    return exit_code


# ---------------------------------------------------------------------------
# Local UI
# ---------------------------------------------------------------------------

UI_CSS = r"""
:root{
  --bg:#f4f6fb; --card:#ffffff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0;
  --blue:#2563eb; --blue-2:#1d4ed8; --blue-soft:#eff6ff;
  --green:#059669; --green-soft:#ecfdf5; --amber:#d97706; --amber-soft:#fffbeb;
  --rose:#e11d48; --rose-soft:#fff1f4; --radius:16px;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans SC","Microsoft YaHei",sans-serif;
  background:var(--bg); color:var(--ink); font-size:14px; line-height:1.65;
  -webkit-font-smoothing:antialiased;
}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
.top{background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);color:#fff}
.top-inner{max-width:1240px;margin:0 auto;padding:20px 24px 16px;display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;align-items:flex-start}
.brand{display:flex;gap:12px;align-items:center}
.logo{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,#60a5fa,#2563eb);display:grid;place-items:center;font-weight:800;font-size:18px;box-shadow:0 8px 20px -12px #60a5fa}
.brand h1{margin:0;font-size:18px;letter-spacing:-.01em}
.brand p{margin:0;color:#94a3b8;font-size:11px;letter-spacing:.08em;text-transform:uppercase}
.status-row{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.pill{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14);padding:5px 10px;border-radius:999px;font-size:11.5px;color:#cbd5e1;white-space:nowrap}
.pill b{color:#fff;font-weight:650}
.pill .dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#22c55e;margin-right:6px;box-shadow:0 0 0 3px rgba(34,197,94,.16)}
.pill.paused .dot{background:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.16)}
.nav{background:#0f172a;border-top:1px solid rgba(255,255,255,.08)}
.nav-inner{max-width:1240px;margin:0 auto;padding:0 24px;display:flex;gap:2px;overflow:auto}
.nav a{color:#94a3b8;padding:11px 14px;border-bottom:2px solid transparent;font-size:12.5px;font-weight:600;white-space:nowrap}
.nav a:hover{color:#fff;text-decoration:none}
.nav a.active{color:#fff;border-bottom-color:#60a5fa}
.container{max-width:1240px;margin:0 auto;padding:24px 24px 44px}
.page-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:0 0 16px}
.page-head h2{margin:0;font-size:22px;letter-spacing:-.02em}
.page-head .sub{color:var(--muted);font-size:12px;margin-top:3px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:18px;box-shadow:0 18px 40px -36px rgba(15,23,42,.35);margin-bottom:16px}
.card h3{margin:0 0 10px;font-size:15px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 15px}
.stat .label{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:700}
.stat .value{margin-top:6px;font-size:18px;font-weight:750;letter-spacing:-.01em}
.stat .hint{margin-top:3px;font-size:11px;color:var(--muted)}
.table-wrap{overflow:auto}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:9px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:750;background:#f8fafc}
tr:last-child td{border-bottom:0}
.badge{display:inline-flex;align-items:center;gap:4px;font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:999px;background:#f1f5f9;color:#475569;border:1px solid #e2e8f0}
.badge.green{background:var(--green-soft);color:var(--green);border-color:#a7f3d0}
.badge.amber{background:var(--amber-soft);color:var(--amber);border-color:#fde68a}
.badge.rose{background:var(--rose-soft);color:var(--rose);border-color:#fecdd3}
.badge.blue{background:var(--blue-soft);color:var(--blue-2);border-color:#bfdbfe}
.muted{color:var(--muted)}
.small{font-size:11.5px}
.empty{padding:28px;text-align:center;color:var(--muted);background:#f8fafc;border:1px dashed #cbd5e1;border-radius:12px}
.btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:#fff;color:var(--ink);padding:8px 13px;border-radius:9px;font-size:12px;font-weight:650;cursor:pointer;font-family:inherit}
.btn:hover{background:#f8fafc;text-decoration:none}
.btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}
.btn.primary:hover{background:var(--blue-2)}
.btn.ghost{background:#fff}
.btn.danger{color:var(--rose);border-color:#fecdd3}
.field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}
.field label{font-size:11.5px;font-weight:700;color:#334155}
input,textarea,select{width:100%;border:1px solid #cbd5e1;border-radius:9px;padding:9px 10px;font:inherit;font-size:12.5px;background:#fff;color:var(--ink);outline:none}
input:focus,textarea:focus,select:focus{border-color:#93c5fd;box-shadow:0 0 0 3px rgba(37,99,235,.1)}
textarea{min-height:108px;resize:vertical}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.reply-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.banner{border-radius:12px;padding:10px 13px;font-size:12px;margin-bottom:14px;border:1px solid}
.banner.ok{background:var(--green-soft);border-color:#a7f3d0;color:#047857}
.banner.warn{background:var(--amber-soft);border-color:#fde68a;color:#92400e}
.markdown{font-size:13px;line-height:1.72;color:#334155;overflow-wrap:anywhere}
.markdown h1,.markdown h2,.markdown h3,.markdown h4{margin:18px 0 7px;color:var(--ink);letter-spacing:-.01em;line-height:1.35}
.markdown h1{font-size:20px}.markdown h2{font-size:17px}.markdown h3{font-size:14.5px}.markdown h4{font-size:13px}
.markdown p{margin:7px 0}
.markdown ul,.markdown ol{margin:7px 0;padding-left:22px}
.markdown li{margin:3px 0}
.markdown code{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;font-size:.9em;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;padding:1px 4px;color:#9f1239}
.markdown pre{background:#0f172a;color:#e2e8f0;border-radius:12px;padding:13px 14px;overflow:auto;font-size:11.5px;line-height:1.6}
.markdown pre code{background:none;border:0;padding:0;color:inherit}
.markdown table{font-size:12px;margin:10px 0}
.markdown th,.markdown td{border:1px solid #e2e8f0;padding:7px 9px}
.markdown th{background:#f8fafc}
.markdown blockquote{margin:10px 0;padding:8px 13px;border-left:3px solid #bfdbfe;background:var(--blue-soft);color:#475569;border-radius:0 8px 8px 0}
.markdown hr{border:0;border-top:1px solid var(--line);margin:16px 0}
.markdown a{font-weight:600}
.iframe-wrap{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:8px;box-shadow:0 18px 40px -36px rgba(15,23,42,.35)}
.dash-frame{width:100%;height:calc(100vh - 250px);min-height:520px;border:0;border-radius:12px;background:#fff}
.split{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(320px,.85fr);gap:16px;align-items:start}
.sticky{position:sticky;top:16px}
.kv{display:grid;grid-template-columns:120px 1fr;gap:6px 12px;font-size:12.5px}
.kv dt{color:var(--muted)}
.kv dd{margin:0;font-weight:600;word-break:break-all}
.list{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.list li{display:flex;gap:8px;align-items:flex-start;font-size:12.5px;color:#475569}
.list li::before{content:"";width:6px;height:6px;border-radius:50%;background:#93c5fd;margin-top:7px;flex:0 0 auto}
.login{max-width:440px;margin:60px auto}
.doc-layout{display:grid;grid-template-columns:270px minmax(0,1fr);gap:16px;align-items:start}
.doc-nav{display:grid;gap:3px}
.doc-nav a{display:block;padding:7px 9px;border-radius:8px;color:#475569;font-size:12.5px;word-break:break-all}
.doc-nav a:hover{background:#f8fafc;text-decoration:none}
.doc-nav a.active{background:var(--blue-soft);color:var(--blue-2);font-weight:700}
.doc-group-label{margin:14px 0 6px;color:var(--muted);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;font-weight:750}
.doc-group-label:first-child{margin-top:0}
.doc-editor{min-height:420px;font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;font-size:12px;line-height:1.55;white-space:pre}
.footer{max-width:1240px;margin:0 auto;padding:0 24px 30px;color:#94a3b8;font-size:10.5px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
@media(max-width:900px){
  .grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .split{grid-template-columns:1fr}
  .doc-layout{grid-template-columns:1fr}
  .sticky{position:static}
  .form-grid{grid-template-columns:1fr}
}
@media(max-width:560px){
  .grid{grid-template-columns:1fr}
  .top-inner,.container,.nav-inner,.footer{padding-left:16px;padding-right:16px}
  .page-head{align-items:flex-start;flex-direction:column}
  .dash-frame{height:calc(100vh - 300px);min-height:420px}
}
"""


def default_ui_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "host": UI_DEFAULT_HOST,
        "port": UI_DEFAULT_PORT,
        "base_path": UI_DEFAULT_BASE_PATH,
        "document_root": UI_DEFAULT_DOCUMENT_ROOT,
        "token_env": "EDWARD_UI_TOKEN",
    }


def ensure_ui_config() -> None:
    if not UI_CONFIG_FILE.exists():
        write_json_atomic(UI_CONFIG_FILE, default_ui_config())


def normalize_base_path(value: str | None) -> str:
    text = (value or "/").strip()
    if not text.startswith("/"):
        text = "/" + text
    if not text.endswith("/"):
        text += "/"
    return text


def ui_url(base_path: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    if base_path == "/":
        return path
    return base_path.rstrip("/") + path


def is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


def load_ui_config(args: argparse.Namespace | None = None) -> dict[str, Any]:
    data = default_ui_config()
    file_data = load_json(UI_CONFIG_FILE, {})
    if isinstance(file_data, dict):
        for key in ("enabled", "host", "port", "base_path", "document_root", "token", "token_env"):
            if key in file_data and file_data[key] is not None:
                data[key] = file_data[key]

    if args is not None:
        for key, attr in (
            ("host", "ui_host"),
            ("port", "ui_port"),
            ("base_path", "ui_base_path"),
            ("document_root", "ui_document_root"),
            ("token", "ui_token"),
        ):
            value = getattr(args, attr, None)
            if value not in (None, ""):
                data[key] = value

    token_env = str(data.get("token_env") or "EDWARD_UI_TOKEN")
    env_token = os.environ.get(token_env, "")
    if env_token:
        data["token"] = env_token

    data["enabled"] = bool(data.get("enabled", True))
    data["host"] = str(data.get("host") or UI_DEFAULT_HOST)
    try:
        data["port"] = int(data.get("port") or UI_DEFAULT_PORT)
    except (TypeError, ValueError):
        raise ValueError("UI port 必须是整数")
    data["base_path"] = normalize_base_path(str(data.get("base_path") or "/"))
    document_root = Path(str(data.get("document_root") or UI_DEFAULT_DOCUMENT_ROOT)).expanduser()
    if not document_root.is_absolute():
        document_root = PROJECT_ROOT / document_root
    data["document_root"] = str(document_root.resolve())
    data["token"] = str(data.get("token") or "")
    data["token_env"] = token_env

    if not is_loopback(data["host"]) and not data["token"]:
        raise ValueError("非本机监听必须设置 UI token（EDWARD_UI_TOKEN 或 --ui-token）")
    return data


def ui_read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


def editable_doc_files() -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for domain, (label, root) in EDITABLE_DOC_ROOTS.items():
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            parts = relative.parts
            if any(part.startswith(".") or part == "__pycache__" for part in parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            files.append(
                {
                    "id": f"{domain}/{relative.as_posix()}",
                    "domain": domain,
                    "label": label,
                    "name": relative.as_posix(),
                    "path": path,
                }
            )
    return files


def resolve_editable_doc(rel_path: str) -> tuple[Path, str]:
    text = (rel_path or "").strip().replace("\\", "/")
    if not text or text.startswith("/"):
        raise ValueError("文件路径非法")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("文件路径非法")
    domain, relative = parts[0], "/".join(parts[1:])
    root_entry = EDITABLE_DOC_ROOTS.get(domain)
    if root_entry is None or not relative:
        raise ValueError("文件不在允许编辑的目录内")
    root = root_entry[1]
    candidate = root / relative
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        raise ValueError("文件不在允许编辑的目录内")
    if candidate.is_symlink():
        raise ValueError("不允许编辑符号链接")
    if not resolved.is_file() or resolved.suffix.lower() != ".md":
        raise ValueError("只能编辑已存在的 Markdown 文件")
    return resolved, f"{domain}/{relative}"


def save_editable_doc(rel_path: str, content: str) -> dict[str, Any]:
    target, normalized = resolve_editable_doc(rel_path)
    encoded = content.encode("utf-8")
    if len(encoded) > DOC_CONTENT_MAX_BYTES:
        raise ValueError(f"内容超过上限 {DOC_CONTENT_MAX_BYTES // 1024} KiB")
    with state_lock():
        try:
            before_bytes = target.stat().st_size
        except OSError:
            before_bytes = 0
        write_text_atomic(target, content)
        data = load_doc_edits()
        entry = append_doc_edit(data, normalized, before_bytes, len(encoded))
        save_doc_edits(data)
    log_event("doc_edit", path=normalized, before_bytes=before_bytes, after_bytes=len(encoded))
    return entry


def ui_phase() -> str:
    text = ui_read_text(EXECUTION_DIR / "current_state.md")
    match = re.search(r"项目阶段[：:]\s*(.+)", text)
    if not match:
        return "—"
    value = re.split(r"[（(]", match.group(1).strip())[0].strip()
    return value.rstrip("。.") or "—"


def ui_wake_marker(text: str) -> bool | None:
    markers = re.findall(r"<!--\s*edward-wake:\s*(true|false)\s*-->", text, re.IGNORECASE)
    if not markers:
        return None
    return markers[-1].lower() == "true"


def ui_status_payload() -> dict[str, Any]:
    scheduler = load_scheduler()
    daemon = load_json(DAEMON_FILE, {})
    pid = daemon.get("pid") if isinstance(daemon, dict) else None
    seen = scheduler.get("seen_replies", {})
    current_replies = scan_replies()
    pending = [
        name
        for name, info in current_replies.items()
        if seen.get(name) != info["fingerprint"]
    ]
    latest = load_json(DASHBOARD_LATEST_FILE, {})
    latest = latest if isinstance(latest, dict) else {}
    return {
        "phase": ui_phase(),
        "next_wake_at": scheduler.get("next_wake_at"),
        "last_run_id": scheduler.get("last_run_id"),
        "last_run_started_at": scheduler.get("last_run_started_at"),
        "last_run_finished_at": scheduler.get("last_run_finished_at"),
        "last_run_exit": scheduler.get("last_run_exit"),
        "last_wake_reason": scheduler.get("last_wake_reason"),
        "paused": bool(scheduler.get("paused")),
        "schedule_source": scheduler.get("schedule_source"),
        "default_interval_minutes": scheduler.get("default_interval_minutes", DEFAULT_INTERVAL_MINUTES),
        "wake_on_reply": bool(scheduler.get("wake_on_reply", True)),
        "retry_at": scheduler.get("retry_at"),
        "daemon": {"pid": pid, "running": pid_alive(pid)},
        "pending_replies": pending,
        "report_count": len(list(REPORTS_DIR.glob("*.md"))),
        "dashboard_updated_at": latest.get("updated_at"),
    }


def ui_reports_payload() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in REPORTS_DIR.glob("*.md"):
        try:
            stat = path.stat()
        except OSError:
            continue
        reply_path = REPLIES_DIR / path.name
        reply_text = ui_read_text(reply_path)
        wake = ui_wake_marker(reply_text)
        result.append(
            {
                "run_id": path.stem,
                "filename": path.name,
                "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                "size": stat.st_size,
                "reply_exists": reply_path.exists(),
                "wake": wake,
                "path": str(path),
            }
        )
    result.sort(key=lambda item: item["mtime"], reverse=True)
    return result


def ui_timeline_payload() -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    if EVENT_LOG.exists():
        for line in ui_read_text(EVENT_LOG).splitlines()[-500:]:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)

    runs: dict[str, dict[str, Any]] = {}
    for event in events:
        run_id = event.get("run_id")
        if not run_id:
            continue
        run = runs.setdefault(str(run_id), {"run_id": str(run_id)})
        if event.get("event") == "run_start":
            run["started_at"] = event.get("ts")
            run["reason"] = event.get("reason")
        elif event.get("event") == "run_end":
            run["finished_at"] = event.get("ts")
            run["exit_code"] = event.get("exit_code")
            run["reason"] = run.get("reason") or event.get("reason")

    for run in runs.values():
        try:
            start = parse_datetime(run.get("started_at"))
            end = parse_datetime(run.get("finished_at"))
            if start and end:
                run["duration_seconds"] = int((end - start).total_seconds())
        except ValueError:
            pass

    ordered_runs = sorted(
        runs.values(),
        key=lambda item: item.get("started_at") or "",
        reverse=True,
    )
    return {
        "scheduler": load_scheduler(),
        "runs": ordered_runs[:30],
        "events": list(reversed(events[-60:])),
    }


def ui_md_inline(text: str) -> str:
    value = html_lib.escape(text)
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    value = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", value)
    value = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", value)
    value = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" target="_blank" rel="noreferrer">\1</a>',
        value,
    )
    return value


def ui_split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def ui_is_table_sep(line: str) -> bool:
    cells = ui_split_table_row(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) for cell in cells)


def ui_is_block_start(line: str) -> bool:
    return bool(
        re.match(r"^(#{1,6}\s+|```|>\s?|[-*+]\s+|\d+\.\s+)", line)
        or re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line)
    )


def render_markdown(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    output: list[str] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        if line.startswith("```"):
            language = line[3:].strip()
            index += 1
            code: list[str] = []
            while index < total and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            cls = f' class="language-{html_lib.escape(language)}"' if language else ""
            output.append(f"<pre><code{cls}>{html_lib.escape(chr(10).join(code))}</code></pre>")
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            output.append(f"<h{level}>{ui_md_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            output.append("<hr>")
            index += 1
            continue

        if "|" in line and index + 1 < total and ui_is_table_sep(lines[index + 1]):
            headers = ui_split_table_row(line)
            index += 2
            rows: list[list[str]] = []
            while index < total and lines[index].strip() and "|" in lines[index]:
                rows.append(ui_split_table_row(lines[index]))
                index += 1
            output.append('<div class="table-wrap"><table><thead><tr>')
            output.extend(f"<th>{ui_md_inline(cell)}</th>" for cell in headers)
            output.append("</tr></thead><tbody>")
            for row in rows:
                output.append("<tr>")
                output.extend(f"<td>{ui_md_inline(cell)}</td>" for cell in row)
                output.append("</tr>")
            output.append("</tbody></table></div>")
            continue

        if re.match(r"^\s*>\s?", line):
            quote: list[str] = []
            while index < total and re.match(r"^\s*>\s?", lines[index]):
                quote.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            output.append(f"<blockquote>{render_markdown(chr(10).join(quote))}</blockquote>")
            continue

        unordered = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if unordered:
            items: list[str] = []
            while index < total:
                match = re.match(r"^\s*[-*+]\s+(.*)$", lines[index])
                if not match:
                    break
                items.append(f"<li>{ui_md_inline(match.group(1))}</li>")
                index += 1
            output.append("<ul>" + "".join(items) + "</ul>")
            continue

        ordered = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if ordered:
            items = []
            while index < total:
                match = re.match(r"^\s*\d+\.\s+(.*)$", lines[index])
                if not match:
                    break
                items.append(f"<li>{ui_md_inline(match.group(1))}</li>")
                index += 1
            output.append("<ol>" + "".join(items) + "</ol>")
            continue

        paragraph: list[str] = []
        while index < total:
            current = lines[index]
            if not current.strip() or ui_is_block_start(current):
                break
            if "|" in current and index + 1 < total and ui_is_table_sep(lines[index + 1]):
                break
            paragraph.append(current.strip())
            index += 1
        if paragraph:
            output.append(f"<p>{ui_md_inline(' '.join(paragraph))}</p>")

    return "\n".join(output)


def ui_metric_state(record: dict[str, Any]) -> tuple[str, str]:
    if record.get("status") == "error":
        return "异常", "rose"
    value = record.get("value")
    target = record.get("target")
    if value is None:
        return "无数据", "blue"
    if target is None:
        return "—", "blue"
    try:
        value_number = float(value)
        target_number = float(target)
    except (TypeError, ValueError):
        return "—", "blue"
    direction = str(record.get("direction") or "up").lower()
    achieved = value_number >= target_number if direction == "up" else value_number <= target_number
    return ("达标", "green") if achieved else ("进行中", "amber")


def ui_format_value(value: Any, unit: Any) -> str:
    text = format_number(value)
    unit_text = str(unit or "").strip()
    if value is None:
        return "—"
    if not unit_text:
        return text
    if unit_text == "%":
        return f"{text}%"
    return f"{text} {unit_text}"


def ui_format_duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        return "—"
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def ui_layout(config: dict[str, Any], title: str, active: str, content: str, status: dict[str, Any] | None = None) -> str:
    status = status or ui_status_payload()
    base_path = config["base_path"]
    nav_items = [
        ("/", "看板"),
        ("/timeline", "执行时间"),
        ("/plan", "计划"),
        ("/docs", "文档"),
        ("/knowledge", "知识库"),
        ("/reports", "汇报"),
    ]
    nav_html = "".join(
        f'<a class="{"active" if path == active else ""}" href="{ui_url(base_path, path)}">{label}</a>'
        for path, label in nav_items
    )
    next_wake = status.get("next_wake_at") or ""
    next_display = (
        f'<span data-countdown="{html_lib.escape(str(next_wake))}">{html_lib.escape(str(next_wake))}</span>'
        if next_wake
        else "未设置"
    )
    paused_class = " paused" if status.get("paused") else ""
    paused_text = "已暂停" if status.get("paused") else "运行中"
    last_run = html_lib.escape(str(status.get("last_run_id") or "无"))
    phase = html_lib.escape(str(status.get("phase") or "—"))
    daemon_running = status.get("daemon", {}).get("running")
    daemon_text = "服务在线" if daemon_running else "服务离线"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(title)} · Edward</title>
<style>{UI_CSS}</style>
</head>
<body>
<header class="top">
  <div class="top-inner">
    <div class="brand">
      <div class="logo">E</div>
      <div>
        <h1>Edward</h1>
        <p>Project Owner Workspace</p>
      </div>
    </div>
    <div class="status-row">
      <span class="pill">阶段 <b>{phase}</b></span>
      <span class="pill{paused_class}"><span class="dot"></span>{paused_text}</span>
      <span class="pill">下次唤醒 <b>{next_display}</b></span>
      <span class="pill">上次运行 <b>{last_run}</b></span>
      <span class="pill">{daemon_text}</span>
    </div>
  </div>
</header>
<nav class="nav"><div class="nav-inner">{nav_html}</div></nav>
<main class="container">{content}</main>
<footer class="footer">
  <span>Edward · {html_lib.escape(str(PROJECT_ROOT))}</span>
  <span>本机 UI · 回复写入 report-reply · <a href="{ui_url(base_path, '/timeline')}">执行时间</a></span>
</footer>
<script>
(function(){{
  function fmt(ms){{
    if(ms <= 0) return '已到期';
    var s=Math.floor(ms/1000), d=Math.floor(s/86400); s-=d*86400;
    var h=Math.floor(s/3600); s-=h*3600;
    var m=Math.floor(s/60); s-=m*60;
    if(d) return d+'天 '+h+'小时';
    if(h) return h+'小时 '+m+'分钟';
    if(m) return m+'分钟 '+s+'秒';
    return s+'秒';
  }}
  document.querySelectorAll('[data-countdown]').forEach(function(el){{
    var target=new Date(el.getAttribute('data-countdown')).getTime();
    if(isNaN(target)) return;
    function tick(){{ el.textContent=fmt(target-Date.now()); }}
    tick(); setInterval(tick,1000);
  }});
}})();
</script>
</body>
</html>"""


def ui_page_dashboard(config: dict[str, Any]) -> str:
    base_path = config["base_path"]
    custom_index = Path(config["document_root"]) / "index.html"
    if custom_index.exists():
        content = f"""
        <div class="page-head">
          <div><h2>看板</h2><div class="sub">自定义看板由 Edward 生成 · 数据来自 dashboard/data/latest.json</div></div>
          <a class="btn" href="{ui_url(base_path, '/custom/')}" target="_blank" rel="noreferrer">新窗口打开 ↗</a>
        </div>
        <div class="iframe-wrap"><iframe class="dash-frame" src="{ui_url(base_path, '/custom/')}" loading="lazy"></iframe></div>
        """
        return ui_layout(config, "看板", "/", content)

    dashboard_text = ui_read_text(DASHBOARD_FILE)
    latest = load_json(DASHBOARD_LATEST_FILE, {})
    metrics = latest.get("metrics", {}) if isinstance(latest, dict) else {}
    latest_html = ""
    if isinstance(metrics, dict) and metrics:
        rows = []
        for metric_id, record in metrics.items():
            if not isinstance(record, dict):
                continue
            value = record.get("value")
            target = record.get("target")
            unit = record.get("unit") or ""
            value_text = ui_format_value(value, unit)
            target_text = ui_format_value(target, unit)
            status_text, badge = ui_metric_state(record)
            rows.append(
                f"<tr><td>{html_lib.escape(str(record.get('name') or metric_id))}</td>"
                f"<td>{html_lib.escape(value_text)}</td><td>{html_lib.escape(target_text)}</td>"
                f"<td><span class='badge {badge}'>{html_lib.escape(status_text)}</span></td>"
                f"<td class='muted'>{html_lib.escape(str(record.get('observed_at') or '—'))}</td></tr>"
            )
        latest_html = (
            '<div class="card"><h3>最新指标</h3><div class="table-wrap"><table>'
            "<thead><tr><th>指标</th><th>当前值</th><th>目标</th><th>状态</th><th>观测时间</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div></div>"
        )

    if dashboard_text:
        body = f'<div class="card"><div class="markdown">{render_markdown(dashboard_text)}</div></div>'
    else:
        body = (
            '<div class="empty">还没有看板。Edward 会在目标确定后生成 '
            '<code>dashboard/index.html</code>，或写入 <code>dashboard.md</code>。</div>'
        )
    content = f"""
    <div class="page-head">
      <div><h2>看板</h2><div class="sub">当前为默认看板 · 自定义页面放在 dashboard/index.html</div></div>
      <a class="btn" href="{ui_url(base_path, '/timeline')}">查看执行时间</a>
    </div>
    {latest_html}
    {body}
    """
    return ui_layout(config, "看板", "/", content)


def ui_page_timeline(config: dict[str, Any]) -> str:
    status = ui_status_payload()
    timeline = ui_timeline_payload()
    scheduler = timeline["scheduler"]
    runs = timeline["runs"]
    events = timeline["events"]
    next_wake = status.get("next_wake_at") or "未设置"
    last_start = status.get("last_run_started_at") or "—"
    last_finish = status.get("last_run_finished_at") or "—"
    paused = "是" if status.get("paused") else "否"
    source = status.get("schedule_source") or "—"
    interval = status.get("default_interval_minutes", DEFAULT_INTERVAL_MINUTES)
    pending = len(status.get("pending_replies", []))

    run_rows = []
    for run in runs:
        exit_code = run.get("exit_code")
        badge = "green" if exit_code == 0 else ("rose" if exit_code is not None else "blue")
        run_rows.append(
            "<tr>"
            f"<td><code>{html_lib.escape(str(run.get('run_id')))}</code></td>"
            f"<td>{html_lib.escape(str(run.get('reason') or '—'))}</td>"
            f"<td>{html_lib.escape(str(run.get('started_at') or '—'))}</td>"
            f"<td>{html_lib.escape(ui_format_duration(run.get('duration_seconds')))}</td>"
            f"<td><span class='badge {badge}'>{html_lib.escape(str(exit_code if exit_code is not None else '运行中'))}</span></td>"
            "</tr>"
        )

    event_rows = []
    for event in events[:30]:
        detail = ", ".join(
            f"{key}={value}"
            for key, value in event.items()
            if key not in {"ts", "event"} and value not in (None, "", [])
        )
        event_rows.append(
            "<tr>"
            f"<td class='muted'>{html_lib.escape(str(event.get('ts') or '—'))}</td>"
            f"<td><code>{html_lib.escape(str(event.get('event') or '—'))}</code></td>"
            f"<td class='muted'>{html_lib.escape(detail or '—')}</td>"
            "</tr>"
        )

    content = f"""
    <div class="page-head">
      <div><h2>执行时间</h2><div class="sub">调度状态、下一次唤醒、最近班次与事件</div></div>
      <a class="btn" href="{ui_url(config['base_path'], '/reports')}">查看汇报</a>
    </div>
    <div class="grid">
      <div class="stat"><div class="label">下次唤醒</div><div class="value" data-countdown="{html_lib.escape(str(next_wake if next_wake != '未设置' else ''))}">{html_lib.escape(str(next_wake))}</div><div class="hint">source={html_lib.escape(str(source))}</div></div>
      <div class="stat"><div class="label">默认间隔</div><div class="value">{html_lib.escape(str(interval))}m</div><div class="hint">暂停：{html_lib.escape(paused)}</div></div>
      <div class="stat"><div class="label">上次运行</div><div class="value">{html_lib.escape(str(status.get('last_run_id') or '—'))}</div><div class="hint">exit={html_lib.escape(str(status.get('last_run_exit')))}</div></div>
      <div class="stat"><div class="label">待处理回复</div><div class="value">{pending}</div><div class="hint">报告总数：{status.get('report_count', 0)}</div></div>
    </div>
    <div class="card">
      <h3>调度详情</h3>
      <dl class="kv">
        <dt>上次开始</dt><dd>{html_lib.escape(str(last_start))}</dd>
        <dt>上次结束</dt><dd>{html_lib.escape(str(last_finish))}</dd>
        <dt>唤醒原因</dt><dd>{html_lib.escape(str(status.get('last_wake_reason') or '—'))}</dd>
        <dt>看板更新</dt><dd>{html_lib.escape(str(status.get('dashboard_updated_at') or '—'))}</dd>
        <dt>服务</dt><dd>{'在线' if status['daemon']['running'] else '离线'} · pid={html_lib.escape(str(status['daemon']['pid'] or '—'))}</dd>
        <dt>调度源</dt><dd>{html_lib.escape(str(SCHEDULER_FILE))}</dd>
      </dl>
    </div>
    <div class="card">
      <h3>最近班次</h3>
      <div class="table-wrap"><table><thead><tr><th>run-id</th><th>原因</th><th>开始</th><th>时长</th><th>退出</th></tr></thead>
      <tbody>{''.join(run_rows) if run_rows else '<tr><td colspan="5" class="muted">暂无运行记录</td></tr>'}</tbody></table></div>
    </div>
    <div class="card">
      <h3>最近事件</h3>
      <div class="table-wrap"><table><thead><tr><th>时间</th><th>事件</th><th>详情</th></tr></thead>
      <tbody>{''.join(event_rows) if event_rows else '<tr><td colspan="3" class="muted">暂无事件</td></tr>'}</tbody></table></div>
    </div>
    """
    return ui_layout(config, "执行时间", "/timeline", content, status)


def ui_page_plan(config: dict[str, Any]) -> str:
    sections = [
        ("目标", EXECUTION_DIR / "goal.md"),
        ("计划", EXECUTION_DIR / "plan.md"),
        ("当前状态", EXECUTION_DIR / "current_state.md"),
    ]
    cards = []
    for title, path in sections:
        text = ui_read_text(path)
        body = f'<div class="markdown">{render_markdown(text)}</div>' if text else '<div class="empty">文件为空或不存在</div>'
        cards.append(f'<div class="card"><h3>{html_lib.escape(title)} <span class="muted small">{html_lib.escape(str(path.relative_to(PROJECT_ROOT)))}</span></h3>{body}</div>')
    content = f"""
    <div class="page-head">
      <div><h2>计划</h2><div class="sub">目标、Plan Book 与当前自然语言状态</div></div>
      <a class="btn" href="{ui_url(config['base_path'], '/timeline')}">执行时间</a>
    </div>
    {''.join(cards)}
    """
    return ui_layout(config, "计划", "/plan", content)


def ui_page_docs(config: dict[str, Any], doc_id: str | None = None) -> str | None:
    base_path = config["base_path"]
    if not doc_id:
        doc_id = "notebook" if "notebook" in EXECUTION_DOCS else next(iter(EXECUTION_DOCS))
    if doc_id not in EXECUTION_DOCS:
        return None

    nav_rows = []
    for key, (label, path) in EXECUTION_DOCS.items():
        active = ' class="active"' if key == doc_id else ""
        relative = path.relative_to(PROJECT_ROOT)
        nav_rows.append(
            f'<a{active} href="{ui_url(base_path, "/docs")}?doc={quote(key)}">'
            f"<b>{html_lib.escape(label)}</b><br><span class=\"muted small\">{html_lib.escape(str(relative))}</span></a>"
        )

    label, path = EXECUTION_DOCS[doc_id]
    text = ui_read_text(path)
    body = (
        f'<div class="markdown">{render_markdown(text)}</div>'
        if text.strip()
        else '<div class="empty">文件为空或不存在</div>'
    )
    updated = "—"
    try:
        updated = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        pass

    content = f"""
    <div class="page-head">
      <div><h2>文档 · {html_lib.escape(label)}</h2><div class="sub">执行域固定文档 · 只读 · 最后修改 {html_lib.escape(updated)}</div></div>
      <a class="btn" href="{ui_url(base_path, '/knowledge')}">编辑知识库</a>
    </div>
    <div class="doc-layout">
      <div class="card"><div class="doc-group-label">执行域</div><div class="doc-nav">{''.join(nav_rows)}</div></div>
      <div class="card">{body}</div>
    </div>
    """
    return ui_layout(config, "文档", "/docs", content)


def ui_page_knowledge(config: dict[str, Any], selected: str | None = None, saved: bool = False) -> str | None:
    base_path = config["base_path"]
    files = editable_doc_files()
    ids = {item["id"] for item in files}
    if selected in (None, "") and files:
        selected = files[0]["id"]
    if selected and selected not in ids:
        return None

    current = next((item for item in files if item["id"] == selected), None)
    nav_html = ""
    for domain, (label, _root) in EDITABLE_DOC_ROOTS.items():
        nav_html += f'<div class="doc-group-label">{html_lib.escape(label)}</div>'
        linked = [item for item in files if item["domain"] == domain]
        if not linked:
            nav_html += '<div class="muted small" style="padding:4px 9px">暂无 Markdown 文件</div>'
            continue
        links = []
        for item in linked:
            active = ' class="active"' if item["id"] == selected else ""
            links.append(
                f'<a{active} href="{ui_url(base_path, "/knowledge")}?file={quote(item["id"], safe="")}">'
                f"{html_lib.escape(item['name'])}</a>"
            )
        nav_html += f'<div class="doc-nav">{"".join(links)}</div>'

    banner = ""
    if saved and current:
        banner = f'<div class="banner ok">已保存 {html_lib.escape(current["id"])}。下次唤醒 Edward 时会提醒先复核。</div>'

    editor_block = '<div class="empty">没有可编辑的 Markdown 文件。</div>'
    preview_block = ""
    if current:
        text = ui_read_text(current["path"])
        body = (
            f'<div class="markdown">{render_markdown(text)}</div>'
            if text.strip()
            else '<div class="empty">文件为空</div>'
        )
        preview_block = f'<div class="card"><h3>预览 <span class="muted small">{html_lib.escape(current["id"])}</span></h3>{body}</div>'
        editor_block = f"""
        <div class="card">
          <h3>编辑 <span class="muted small">{html_lib.escape(current["id"])}</span></h3>
          <form method="post" action="{ui_url(base_path, '/knowledge/save')}">
            <input type="hidden" name="csrf" value="{{CSRF}}">
            <input type="hidden" name="path" value="{html_lib.escape(current["id"])}">
            <div class="field"><label>Markdown 内容</label><textarea class="doc-editor" name="content">{html_lib.escape(text)}</textarea></div>
            <div class="reply-actions"><button class="btn primary" type="submit">保存文件</button></div>
          </form>
        </div>
        """

    entries = list(reversed(load_doc_edits().get("entries", [])[-10:]))
    edit_rows = []
    for entry in entries:
        consumed = entry.get("consumed_at")
        status = (
            f'<span class="badge green">已提醒 · {html_lib.escape(str(entry.get("consumed_run_id") or "—"))}</span>'
            if consumed
            else '<span class="badge amber">待提醒</span>'
        )
        edit_rows.append(
            "<tr>"
            f"<td class='muted'>{html_lib.escape(str(entry.get('ts') or '—'))}</td>"
            f"<td><code>{html_lib.escape(str(entry.get('path') or '—'))}</code></td>"
            f"<td>{entry.get('before_bytes')} → {entry.get('after_bytes')} B</td>"
            f"<td>{status}</td>"
            "</tr>"
        )
    edits_block = (
        '<div class="card"><h3>最近编辑</h3><div class="table-wrap"><table>'
        "<thead><tr><th>时间</th><th>文件</th><th>大小</th><th>提醒</th></tr></thead>"
        f"<tbody>{''.join(edit_rows)}</tbody></table></div></div>"
        if edit_rows
        else '<div class="card"><h3>最近编辑</h3><div class="empty">还没有编辑记录。</div></div>'
    )

    pending_count = len(pending_doc_edits())
    content = f"""
    <div class="page-head">
      <div><h2>知识库</h2><div class="sub">法典域只读遵守，知识域人 + Edward 共同维护 · 保存后提醒 Edward 复核</div></div>
      <span class="badge {'amber' if pending_count else ''}">待提醒 {pending_count} 条</span>
    </div>
    {banner}
    <div class="doc-layout">
      <div class="card">{nav_html}</div>
      <div>
        {preview_block}
        {editor_block}
        {edits_block}
      </div>
    </div>
    """
    return ui_layout(config, "知识库", "/knowledge", content)


def ui_page_reports(config: dict[str, Any]) -> str:
    reports = ui_reports_payload()
    rows = []
    for report in reports:
        reply = (
            '<span class="badge green">已回复</span>'
            if report["reply_exists"]
            else '<span class="badge amber">未回复</span>'
        )
        wake = report.get("wake")
        wake_badge = (
            '<span class="badge blue">会唤醒</span>'
            if wake is True
            else ('<span class="badge">不唤醒</span>' if wake is False else '<span class="muted">—</span>')
        )
        rows.append(
            "<tr>"
            f"<td><a href=\"{ui_url(config['base_path'], '/reports/' + quote(report['run_id']))}\"><code>{html_lib.escape(report['run_id'])}</code></a></td>"
            f"<td>{html_lib.escape(report['mtime'])}</td>"
            f"<td>{html_lib.escape(f'{report["size"] / 1024:.1f} KB')}</td>"
            f"<td>{reply}</td><td>{wake_badge}</td>"
            "</tr>"
        )
    content = f"""
    <div class="page-head">
      <div><h2>汇报</h2><div class="sub">每个班次一份报告 · 详情页可直接追加回复</div></div>
      <a class="btn" href="{ui_url(config['base_path'], '/timeline')}">执行时间</a>
    </div>
    <div class="card">
      <div class="table-wrap"><table><thead><tr><th>run-id</th><th>时间</th><th>大小</th><th>回复</th><th>唤醒</th></tr></thead>
      <tbody>{''.join(rows) if rows else '<tr><td colspan="5" class="muted">还没有 report。Edward 上班后会生成第一份。</td></tr>'}</tbody></table></div>
    </div>
    """
    return ui_layout(config, "汇报", "/reports", content)


def ui_page_report(config: dict[str, Any], run_id: str, saved: str | None, wake_saved: str | None) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", run_id or ""):
        return None
    report_path = REPORTS_DIR / f"{run_id}.md"
    if not report_path.exists():
        return None
    report_text = ui_read_text(report_path)
    reply_path = REPLIES_DIR / f"{run_id}.md"
    reply_text = ui_read_text(reply_path)
    wake = ui_wake_marker(reply_text)

    banner = ""
    if saved:
        if wake_saved == "1":
            banner = '<div class="banner ok">已保存回复。Edward 将在约 5 秒内被唤醒。</div>'
        else:
            banner = '<div class="banner warn">已保存回复，但不提前唤醒；Edward 会在下一次定时班次读取它。</div>'

    reply_block = ""
    if reply_text:
        display_reply = re.sub(
            r"<!--\s*edward-wake:\s*(true|false)\s*-->",
            "",
            reply_text,
            flags=re.IGNORECASE,
        ).strip()
        reply_block = f"""
        <div class="card">
          <h3>已有回复 <span class="badge {'blue' if wake is not False else ''}">{'会唤醒' if wake is True else ('不唤醒' if wake is False else '未标记')}</span></h3>
          <div class="markdown">{render_markdown(display_reply)}</div>
        </div>
        """

    reply_form = f"""
    <div class="card">
      <h3>{'追加回复' if reply_text else '回复这份报告'}</h3>
      <form method="post" action="{ui_url(config['base_path'], '/reports/' + quote(run_id) + '/reply')}">
        <input type="hidden" name="csrf" value="{{CSRF}}">
        <div class="field"><label>我的反馈</label><textarea name="feedback" placeholder="说明你的判断、纠偏或需要 Edward 知道的信息"></textarea></div>
        <div class="form-grid">
          <div class="field"><label>决策 / 拍板</label><input name="decision" placeholder="例如：批准方案 A"></div>
          <div class="field"><label>解阻 / 资源</label><input name="unblock" placeholder="例如：已开通权限"></div>
          <div class="field"><label>目标 / 优先级调整</label><input name="priority" placeholder="例如：先做转化率"></div>
          <div class="field"><label>对下次唤醒的要求</label><input name="next_wake" placeholder="例如：明天 9 点后再跑"></div>
        </div>
        <div class="reply-actions">
          <button class="btn primary" type="submit" name="wake" value="1">保存并唤醒 Edward</button>
          <button class="btn ghost" type="submit" name="wake" value="0">仅保存，不唤醒</button>
        </div>
        <p class="muted small" style="margin:10px 0 0">追加会写入 <code>{html_lib.escape(str(reply_path.relative_to(PROJECT_ROOT)))}</code>。最新一条 <code>&lt;!-- edward-wake: true|false --&gt;</code> 决定是否提前唤醒。</p>
      </form>
    </div>
    """

    content = f"""
    <div class="page-head">
      <div><h2>报告 · {html_lib.escape(run_id)}</h2><div class="sub">写入 <code>{html_lib.escape(str(report_path.relative_to(PROJECT_ROOT)))}</code></div></div>
      <a class="btn" href="{ui_url(config['base_path'], '/reports')}">返回列表</a>
    </div>
    {banner}
    <div class="split">
      <div>
        <div class="card"><div class="markdown">{render_markdown(report_text)}</div></div>
        {reply_block}
      </div>
      <div class="sticky">{reply_form}</div>
    </div>
    """
    return ui_layout(config, f"报告 {run_id}", "/reports", content)


def ui_page_login(config: dict[str, Any], next_path: str, csrf: str, error: bool = False) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edward · 登录</title><style>{UI_CSS}</style></head>
<body>
<main class="container"><div class="login card">
  <div class="brand" style="margin-bottom:18px"><div class="logo">E</div><div><h1>Edward</h1><p>Project Owner Workspace</p></div></div>
  <h2 style="margin:0 0 6px">需要访问 token</h2>
  <p class="muted small">这个 Edward UI 配置了局域网访问 token。输入后会在浏览器保存 cookie。</p>
  {'<div class="banner warn">token 不正确，请重试。</div>' if error else ''}
  <form method="post" action="{ui_url(config['base_path'], '/login')}">
    <input type="hidden" name="csrf" value="{html_lib.escape(csrf)}">
    <input type="hidden" name="next" value="{html_lib.escape(next_path)}">
    <div class="field"><label>访问 token</label><input type="password" name="token" autofocus></div>
    <button class="btn primary" type="submit">进入</button>
  </form>
</div></main>
</body></html>"""


def ui_cookie_header(config: dict[str, Any]) -> str:
    cookie = SimpleCookie()
    cookie[UI_COOKIE_NAME] = config.get("token", "")
    cookie[UI_COOKIE_NAME]["path"] = config["base_path"]
    cookie[UI_COOKIE_NAME]["httponly"] = True
    cookie[UI_COOKIE_NAME]["samesite"] = "Strict"
    return cookie.output(header="").strip()


def ui_config_url(config: dict[str, Any]) -> str:
    host = config["host"]
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    elif host == "::1":
        host = "[::1]"
    return f"http://{host}:{config['port']}{config['base_path']}"


def ui_public_url(config: dict[str, Any], server: ThreadingHTTPServer) -> str:
    host = config["host"]
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    elif host == "::1":
        host = "[::1]"
    port = server.server_address[1]
    return f"http://{host}:{port}{config['base_path']}"


def ui_make_handler(config: dict[str, Any], csrf: str):
    base_path = config["base_path"]
    token = config["token"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "EdwardUI/0.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _subpath(self) -> str | None:
            path = unquote(urlparse(self.path).path)
            if base_path != "/" and path == base_path.rstrip("/"):
                self._redirect(base_path)
                return None
            if not path.startswith(base_path):
                self._send_error_page(404, "页面不存在")
                return None
            return "/" + path[len(base_path):]

        def _url(self, path: str) -> str:
            return ui_url(base_path, path)

        def _send_html(self, body: str, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _send_json(self, payload: Any, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _redirect(self, location: str, cookie: bool = False, status: int = 302) -> None:
            self.send_response(status)
            self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", ui_cookie_header(config))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_error_page(self, status: int, message: str) -> None:
            body = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Edward · {status}</title><style>{UI_CSS}</style></head><body><main class="container"><div class="login card"><div class="brand" style="margin-bottom:18px"><div class="logo">E</div><div><h1>Edward</h1><p>Project Owner Workspace</p></div></div><h2>{status}</h2><p class="muted">{html_lib.escape(message)}</p><a class="btn" href="{self._url('/')}">返回看板</a></div></main></body></html>"""
            self._send_html(body, status)

        def _authorized(self) -> bool:
            if not token:
                return True
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get(UI_COOKIE_NAME)
            if morsel and secrets.compare_digest(morsel.value, token):
                return True
            query = parse_qs(urlparse(self.path).query)
            supplied = query.get("token", [None])[0]
            if supplied and secrets.compare_digest(supplied, token):
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], token):
                return True
            return False

        def _handle_token_query(self) -> bool:
            if not token:
                return False
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            supplied = query.get("token", [None])[0]
            if not supplied or not secrets.compare_digest(supplied, token):
                return False
            pairs = [
                (key, value)
                for key, values in query.items()
                if key != "token"
                for value in values
            ]
            location = parsed.path + (("?" + urlencode(pairs)) if pairs else "")
            self._redirect(location, cookie=True)
            return True

        def _safe_next(self, value: str | None) -> str:
            if value and value.startswith(base_path):
                return value
            return base_path

        def _read_form(self) -> dict[str, str]:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = 0
            if length > 2_000_000:
                raise ValueError("表单过大")
            body = self.rfile.read(length).decode("utf-8", "replace")
            parsed = parse_qs(body, keep_blank_values=True)
            return {key: values[-1] for key, values in parsed.items()}

        def _csrf_ok(self, form: dict[str, str]) -> bool:
            supplied = form.get("csrf", "")
            return bool(supplied) and secrets.compare_digest(supplied, csrf)

        def _serve_custom(self, relative: str) -> None:
            root = Path(config["document_root"]).resolve()
            cleaned = unquote(relative).lstrip("/")
            target = (root / cleaned).resolve() if cleaned else (root / "index.html").resolve()
            try:
                target.relative_to(root)
            except ValueError:
                self._send_error_page(404, "页面不存在")
                return
            if target.is_dir():
                target = target / "index.html"
            if not target.is_file():
                self._send_error_page(404, "自定义看板不存在")
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            try:
                data = target.read_bytes()
            except OSError as exc:
                self._send_error_page(500, f"读取静态文件失败: {exc}")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if content_type.startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self' 'unsafe-inline' data: https:; img-src 'self' data: https:;",
                )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_HEAD(self) -> None:  # noqa: N802
            self._send_error_page(405, "不支持 HEAD")

        def do_GET(self) -> None:  # noqa: N802
            subpath = self._subpath()
            if subpath is None:
                return
            if self._handle_token_query():
                return

            if subpath == "/login":
                if not token:
                    self._redirect(self._url("/"))
                    return
                query = parse_qs(urlparse(self.path).query)
                next_path = self._safe_next(query.get("next", [None])[0])
                error = query.get("error", ["0"])[0] == "1"
                self._send_html(ui_page_login(config, next_path, csrf, error))
                return

            if not self._authorized():
                if subpath.startswith("/api/"):
                    self._send_json({"error": "unauthorized"}, 401)
                else:
                    self._redirect(self._url("/login") + "?next=" + quote(self.path, safe=""))
                return

            if subpath == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return

            if subpath == "/healthz":
                self._send_json({"ok": True, "version": VERSION})
                return

            if subpath == "/":
                self._send_html(ui_page_dashboard(config))
                return

            if subpath == "/timeline":
                self._send_html(ui_page_timeline(config))
                return

            if subpath == "/plan":
                self._send_html(ui_page_plan(config))
                return

            if subpath == "/docs":
                query = parse_qs(urlparse(self.path).query)
                doc_id = query.get("doc", [None])[0]
                page = ui_page_docs(config, doc_id)
                if page is None:
                    self._send_error_page(404, "文档不存在")
                else:
                    self._send_html(page)
                return

            if subpath == "/knowledge":
                query = parse_qs(urlparse(self.path).query)
                selected = query.get("file", [None])[0]
                saved = query.get("saved", [None])[0] == "1"
                page = ui_page_knowledge(config, selected, saved)
                if page is None:
                    self._send_error_page(404, "知识文件不存在")
                else:
                    self._send_html(page.replace("{CSRF}", html_lib.escape(csrf)))
                return

            if subpath == "/reports":
                self._send_html(ui_page_reports(config))
                return

            report_match = re.fullmatch(r"/reports/([^/]+)", subpath)
            if report_match:
                run_id = unquote(report_match.group(1))
                query = parse_qs(urlparse(self.path).query)
                saved = query.get("saved", [None])[0]
                wake_saved = query.get("wake", [None])[0]
                page = ui_page_report(config, run_id, saved, wake_saved)
                if page is None:
                    self._send_error_page(404, "报告不存在")
                else:
                    self._send_html(page.replace("{CSRF}", html_lib.escape(csrf)))
                return

            if subpath == "/custom":
                self._redirect(self._url("/custom/"))
                return

            if subpath.startswith("/custom/"):
                self._serve_custom(subpath[len("/custom/"):])
                return

            if subpath == "/api/status":
                self._send_json(ui_status_payload())
                return

            if subpath == "/api/dashboard":
                self._send_json(
                    {
                        "metrics": load_metrics_config(),
                        "latest": load_json(DASHBOARD_LATEST_FILE, {}),
                    }
                )
                return

            if subpath == "/api/reports":
                self._send_json({"reports": ui_reports_payload()})
                return

            if subpath == "/api/timeline":
                self._send_json(ui_timeline_payload())
                return

            self._send_error_page(404, "页面不存在")

        def do_POST(self) -> None:  # noqa: N802
            subpath = self._subpath()
            if subpath is None:
                return

            try:
                form = self._read_form()
            except ValueError as exc:
                self._send_error_page(400, str(exc))
                return

            if not self._csrf_ok(form):
                self._send_error_page(403, "CSRF token 无效")
                return

            if subpath == "/login":
                if not token:
                    self._redirect(self._url("/"))
                    return
                supplied = form.get("token", "")
                next_path = self._safe_next(form.get("next"))
                if secrets.compare_digest(supplied, token):
                    self._redirect(next_path, cookie=True)
                else:
                    self._redirect(self._url("/login") + "?error=1&next=" + quote(next_path, safe=""))
                return

            if subpath == "/knowledge/save":
                if not self._authorized():
                    self._send_error_page(401, "请先登录")
                    return
                try:
                    entry = save_editable_doc(form.get("path", ""), form.get("content", ""))
                except ValueError as exc:
                    self._send_error_page(400, str(exc))
                    return
                except OSError as exc:
                    self._send_error_page(500, f"保存失败: {exc}")
                    return
                target = (
                    self._url("/knowledge")
                    + "?file="
                    + quote(str(entry.get("path") or ""), safe="")
                    + "&saved=1"
                )
                self._redirect(target, status=303)
                return

            reply_match = re.fullmatch(r"/reports/([^/]+)/reply", subpath)
            if reply_match:
                if not self._authorized():
                    self._send_error_page(401, "请先登录")
                    return
                run_id = unquote(reply_match.group(1))
                wake = form.get("wake", "0") == "1"
                try:
                    append_ui_reply(run_id, form, wake)
                except FileNotFoundError:
                    self._send_error_page(404, "报告不存在")
                    return
                except ValueError as exc:
                    self._send_error_page(400, str(exc))
                    return
                target = self._url(f"/reports/{quote(run_id)}") + f"?saved=1&wake={1 if wake else 0}"
                self._redirect(target)
                return

            self._send_error_page(405, "不支持 POST")

    return Handler


def ui_create_server(config: dict[str, Any], csrf: str | None = None) -> ThreadingHTTPServer:
    csrf_token = csrf or secrets.token_urlsafe(24)
    handler = ui_make_handler(config, csrf_token)
    server = ThreadingHTTPServer((config["host"], int(config["port"])), handler)
    server.daemon_threads = True
    setattr(server, "edward_csrf", csrf_token)
    setattr(server, "edward_config", config)
    return server


def ui_start_background(config: dict[str, Any]) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ui_create_server(config)
    thread = threading.Thread(target=server.serve_forever, name="edward-ui", daemon=True)
    thread.start()
    return server, thread, ui_public_url(config, server)


def append_ui_reply(run_id: str, form: dict[str, str], wake: bool) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", run_id or ""):
        raise ValueError("run-id 非法")
    report_path = REPORTS_DIR / f"{run_id}.md"
    if not report_path.exists():
        raise FileNotFoundError(run_id)

    fields = [
        ("我的反馈", form.get("feedback", "").strip()),
        ("决策 / 拍板", form.get("decision", "").strip()),
        ("解阻 / 资源", form.get("unblock", "").strip()),
        ("目标 / 优先级调整", form.get("priority", "").strip()),
        ("对下次唤醒的要求", form.get("next_wake", "").strip()),
    ]
    if not any(value for _, value in fields):
        raise ValueError("至少填写一项回复内容")

    reply_path = REPLIES_DIR / f"{run_id}.md"
    existing = ui_read_text(reply_path)
    timestamp = now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    if not existing:
        lines.append(f"# Report Reply · {run_id}\n")
    lines.append(f"\n## {timestamp} · 追加回复\n")
    for label, value in fields:
        if value:
            lines.append(f"### {label}\n\n{value}\n")
    lines.append(f"\n<!-- edward-wake: {'true' if wake else 'false'} -->\n")
    new_text = (existing.rstrip() + "\n" + "\n".join(lines)).lstrip() if existing else "\n".join(lines)
    write_text_atomic(reply_path, new_text)
    log_event("ui_reply", run_id=run_id, wake=wake, reply=str(reply_path))
    return reply_path


def reply_wants_wake(path: Path) -> bool:
    marker = ui_wake_marker(ui_read_text(path))
    return marker is not False


def cmd_ui(args: argparse.Namespace) -> int:
    ensure_ui_config()
    try:
        config = load_ui_config(args)
    except ValueError as exc:
        print(f"[edward] UI 配置错误: {exc}", file=sys.stderr)
        return 2

    try:
        server = ui_create_server(config)
    except OSError as exc:
        print(f"[edward] UI 启动失败: {exc}", file=sys.stderr)
        return 1

    url = ui_public_url(config, server)
    print(f"Edward UI: {url}")
    if config["token"]:
        print("访问需要 token（EDWARD_UI_TOKEN 或 --ui-token）")
    print("按 Ctrl+C 停止")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edward.py",
        description="Edward project-owner scheduler (stdlib only)",
    )
    parser.add_argument("--version", action="version", version=f"edward {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="创建 edward/ 三域工件目录")
    init_parser.set_defaults(func=cmd_init)

    serve_parser = subparsers.add_parser("serve", help="启动调度服务")
    serve_parser.add_argument("--daemon", action="store_true", help="后台运行")
    serve_parser.add_argument("--once", action="store_true", help="只检查/执行一次后退出")
    serve_parser.add_argument("--dry-run", action="store_true", help="只打印将执行的命令，不调用 opencode")
    serve_parser.add_argument("--ui", action="store_true", help="强制启动本地 UI")
    serve_parser.add_argument("--no-ui", action="store_true", help="不启动本地 UI")
    serve_parser.add_argument("--ui-host", dest="ui_host", help="UI 监听地址")
    serve_parser.add_argument("--ui-port", dest="ui_port", type=int, help="UI 端口")
    serve_parser.add_argument("--ui-token", dest="ui_token", help="UI 访问 token")
    serve_parser.add_argument("--ui-base-path", dest="ui_base_path", help="UI URL 前缀，如 /edward/")
    serve_parser.add_argument("--ui-document-root", dest="ui_document_root", help="自定义看板静态根目录")
    serve_parser.set_defaults(func=cmd_serve)

    ui_parser = subparsers.add_parser("ui", help="启动本地 Web UI")
    ui_parser.add_argument("--host", dest="ui_host", help="监听地址，默认 127.0.0.1")
    ui_parser.add_argument("--port", dest="ui_port", type=int, help="端口，默认 8765")
    ui_parser.add_argument("--token", dest="ui_token", help="访问 token；局域网访问必填")
    ui_parser.add_argument("--base-path", dest="ui_base_path", help="URL 前缀，如 /edward/")
    ui_parser.add_argument("--document-root", dest="ui_document_root", help="自定义看板静态根目录")
    ui_parser.set_defaults(func=cmd_ui)

    schedule_parser = subparsers.add_parser("schedule", help="声明下一次唤醒时间")
    group = schedule_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--in", dest="in_value", help="相对时间，如 6h、90m、2d")
    group.add_argument("--at", dest="at_value", help="ISO 时间，如 2026-10-18T09:00:00+08:00")
    schedule_parser.set_defaults(func=cmd_schedule)

    status_parser = subparsers.add_parser("status", help="查看调度与服务状态")
    status_parser.add_argument("--json", action="store_true", help="输出 JSON")
    status_parser.set_defaults(func=cmd_status)

    collect_parser = subparsers.add_parser("collect", help="运行固定采集脚本并刷新看板")
    collect_parser.add_argument("--metric", help="只采集指定指标 id")
    collect_parser.add_argument("--quiet", action="store_true", help="不打印摘要")
    collect_parser.add_argument("--json", action="store_true", help="输出 latest.json")
    collect_parser.set_defaults(func=cmd_collect)

    dashboard_parser = subparsers.add_parser("dashboard", help="从最新数据重新渲染看板")
    dashboard_parser.add_argument("--json", action="store_true", help="输出 latest.json")
    dashboard_parser.set_defaults(func=cmd_dashboard)

    run_parser = subparsers.add_parser("run-now", help="立即触发一个班次")
    run_parser.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    run_parser.set_defaults(func=cmd_run_now)

    pause_parser = subparsers.add_parser("pause", help="暂停调度")
    pause_parser.set_defaults(func=cmd_pause)

    resume_parser = subparsers.add_parser("resume", help="恢复调度")
    resume_parser.set_defaults(func=cmd_resume)

    stop_parser = subparsers.add_parser("stop", help="停止后台服务")
    stop_parser.set_defaults(func=cmd_stop)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ensure_dirs()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
