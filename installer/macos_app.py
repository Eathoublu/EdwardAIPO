#!/usr/bin/env python3
"""Edward macOS installer.

Double-click the built .app to choose a project folder, install .opencode
resources, and optionally create the edward/ workspace and start the service.
It can also run headless with --cli --target <dir> --init --yes.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


def _version_from_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    value = ""
    if path.suffix in {".yaml", ".yml"}:
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line.startswith("version:"):
                value = line.split(":", 1)[1].strip().strip("'\"")
                break
    elif text.strip():
        value = text.strip().splitlines()[0].strip()
    if value.startswith("v"):
        value = value[1:]
    return value if VERSION_PATTERN.fullmatch(value) else None


def app_version() -> str:
    candidates: list[Path] = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(Path(bundle_dir) / "EDWARD_VERSION")
    if getattr(sys, "frozen", False):
        candidates.append(resource_root() / "EDWARD_VERSION")
    else:
        candidates.append(Path(__file__).resolve().parent.parent / "release.yaml")
    for candidate in candidates:
        value = _version_from_file(candidate)
        if value:
            return value
    return "0.0.0"


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if len(executable.parents) >= 2:
            resources = executable.parents[1] / "Resources"
            if (resources / "payload").exists():
                return resources
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


VERSION = app_version()


def payload_root() -> Path:
    return resource_root() / "payload"


def app_bundle_path() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    if len(executable.parents) >= 3:
        return executable.parents[2]
    return None


def default_target() -> Path:
    bundle = app_bundle_path()
    if bundle is not None:
        candidate = bundle.parent
        if candidate != Path("/") and candidate.name != "Applications":
            return candidate
    return Path.home()


def run_edward(target: Path, *args: str) -> str:
    bin_path = target / ".opencode" / "bin" / "edward"
    script_path = target / ".opencode" / "scripts" / "edward.py"
    if bin_path.exists():
        command = [str(bin_path), *args]
    elif script_path.exists():
        command = [sys.executable, str(script_path), *args]
    else:
        raise FileNotFoundError(f"找不到 edward CLI: {bin_path}")
    completed = subprocess.run(
        command,
        cwd=str(target),
        capture_output=True,
        text=True,
        env=None,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"命令失败，exit={completed.returncode}")
    return (completed.stdout or "").strip()


def install(
    target: Path,
    force: bool = False,
    do_init: bool = True,
    do_serve: bool = False,
) -> str:
    target = target.expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"目标目录不存在: {target}")

    source = payload_root() / ".opencode"
    if not source.exists():
        raise FileNotFoundError(f"安装包缺少 payload: {source}")

    agent_file = target / ".opencode" / "agents" / "edward.md"
    if agent_file.exists() and not force:
        raise FileExistsError(f"{agent_file} 已存在")

    for root, _dirs, files in os.walk(source, followlinks=True):
        for name in files:
            source_file = Path(root) / name
            relative = source_file.relative_to(source)
            destination = target / ".opencode" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not force:
                continue
            shutil.copy2(source_file, destination)

    for relative in (".opencode/bin/edward", ".opencode/scripts/edward.py"):
        path = target / relative
        if path.exists():
            path.chmod(0o755)

    output = [f"已安装到: {target / '.opencode'}"]
    if do_init:
        output.append(run_edward(target, "init"))
    if do_serve:
        output.append(run_edward(target, "serve", "--daemon"))
    output.append(f"状态: {target / '.opencode/bin/edward'} status")
    return "\n".join(part for part in output if part)


def cli_main(args: argparse.Namespace) -> int:
    target = Path(args.target or ".").expanduser().resolve()
    if not args.yes:
        print(f"即将安装到: {target}")
        print("确认请加 --yes")
        return 2
    try:
        print(install(target, force=args.force, do_init=args.init, do_serve=args.serve))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"安装失败: {exc}", file=sys.stderr)
        return 1


def gui_main() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Edward Installer")
    root.geometry("720x390")
    root.minsize(620, 360)

    target_var = tk.StringVar(value=str(default_target()))
    init_var = tk.BooleanVar(value=True)
    serve_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="选择一个项目目录，安装 .opencode 资源。")

    style = ttk.Style(root)
    try:
        style.theme_use("aqua")
    except tk.TclError:
        pass

    frame = ttk.Frame(root, padding=22)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text="Edward Installer", font=("Helvetica", 20, "bold")).pack(anchor="w")
    ttk.Label(
        frame,
        text="安装 agent、调度器、模板和看板采集框架。不会复制当前项目的 edward/ 实例。",
        wraplength=650,
    ).pack(anchor="w", pady=(4, 18))

    ttk.Label(frame, text="目标项目").pack(anchor="w")
    target_row = ttk.Frame(frame)
    target_row.pack(fill="x", pady=(4, 12))
    entry = ttk.Entry(target_row, textvariable=target_var)
    entry.pack(side="left", fill="x", expand=True)

    def choose_folder() -> None:
        selected = filedialog.askdirectory(initialdir=target_var.get() or str(Path.home()))
        if selected:
            target_var.set(selected)

    ttk.Button(target_row, text="选择…", command=choose_folder).pack(side="left", padx=(8, 0))

    ttk.Checkbutton(frame, text="安装后创建 edward/ 工作区（init）", variable=init_var).pack(anchor="w")
    ttk.Checkbutton(frame, text="安装后启动后台调度服务（serve --daemon）", variable=serve_var).pack(anchor="w", pady=(4, 12))

    status_label = ttk.Label(frame, textvariable=status_var, foreground="#475569", wraplength=650)
    status_label.pack(anchor="w", pady=(4, 12))

    button_row = ttk.Frame(frame)
    button_row.pack(fill="x")

    install_button = ttk.Button(button_row, text="安装 Edward")
    install_button.pack(side="left")

    def do_install(force: bool = False) -> None:
        target = Path(target_var.get()).expanduser().resolve()
        install_button.configure(state="disabled")
        status_var.set("正在安装…")
        root.update_idletasks()
        try:
            message = install(
                target,
                force=force,
                do_init=init_var.get(),
                do_serve=serve_var.get(),
            )
            status_var.set("安装完成。")
            messagebox.showinfo(
                "安装完成",
                message
                + "\n\n常用命令：\n"
                + f"{target}/.opencode/bin/edward status\n"
                + f"{target}/.opencode/bin/edward serve --daemon\n"
                + f"{target}/.opencode/bin/edward collect\n"
                + "UI: http://127.0.0.1:8765/",
            )
        except FileExistsError:
            if messagebox.askyesno(
                "已安装",
                "目标项目已有 Edward 资源。要覆盖资源吗？\n（不会删除 edward/ 工件）",
            ):
                do_install(force=True)
            else:
                status_var.set("已取消。")
        except Exception as exc:  # noqa: BLE001
            status_var.set("安装失败。")
            messagebox.showerror("安装失败", str(exc))
        finally:
            install_button.configure(state="normal")

    install_button.configure(command=lambda: do_install(False))
    ttk.Button(button_row, text="退出", command=root.destroy).pack(side="left", padx=(8, 0))

    ttk.Label(
        frame,
        text="安装后，人类仍然是唯一持续交互入口：写 edward/execution/report-reply/<run-id>.md。",
        foreground="#94a3b8",
        wraplength=650,
    ).pack(anchor="w", pady=(18, 0))

    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="Edward Installer")
    parser.add_argument("--target", help="目标项目目录")
    parser.add_argument("--force", action="store_true", help="覆盖已安装资源")
    parser.add_argument("--init", action="store_true", help="安装后创建 edward/ 工作区")
    parser.add_argument("--serve", action="store_true", help="安装后启动后台服务")
    parser.add_argument("--cli", action="store_true", help="无 GUI 模式")
    parser.add_argument("--yes", action="store_true", help="CLI 模式确认安装")
    parser.add_argument("--version", action="version", version=f"Edward Installer {VERSION}")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.cli or args.yes:
        return cli_main(args)
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
