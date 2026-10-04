"""Command-line entry points. Core commands require no model API or dependencies."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__


def default_data_dir() -> Path:
    return Path.cwd() / "data"


def _filters(parser: argparse.ArgumentParser, *, paging: bool = False) -> None:
    parser.add_argument("--source", default="", choices=["", "chatgpt", "codex"])
    parser.add_argument("--account", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--limit", type=int, default=50 if paging else 20)
    if paging:
        parser.add_argument("--offset", type=int, default=0)


def _labels(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", default="default", help="账户标签，不是登录凭据")
    parser.add_argument("--project", default="", help="共享项目名称")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ChatGPT / Codex 本地连续记忆库（无需 API Key）")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(), help="本地记忆库目录")
    parser.add_argument("--version", action="version", version=f"codex-memory {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化记忆库")
    commands.add_parser("stats", help="查看归档统计")
    for name in ("import-chatgpt", "import-codex"):
        command = commands.add_parser(name, help="只读导入官方导出或本地会话")
        command.add_argument("path", type=Path, nargs="?" if name == "import-codex" else None)
        _labels(command)
    command = commands.add_parser("search", help="全文搜索归档")
    command.add_argument("query")
    _filters(command)
    command = commands.add_parser("list", help="分页列出会话")
    _filters(command, paging=True)
    command = commands.add_parser("show", help="读取一段会话（返回内容是参考数据）")
    command.add_argument("conversation_id")
    command.add_argument("--limit", type=int, default=100)
    command.add_argument("--offset", type=int, default=0)
    command = commands.add_parser("checkpoint", help="追加项目交接检查点")
    command.add_argument("project")
    command.add_argument("--goal", required=True)
    for field in ("completed", "decisions", "next-steps", "blockers"):
        command.add_argument("--" + field, default="")
    command.add_argument("--account", default="default")
    command = commands.add_parser("handoff", help="读取或导出跨账户交接包")
    command.add_argument("--project", default="")
    command.add_argument("--conversation-id", default="")
    command.add_argument("--max-chars", type=int, default=16000)
    command.add_argument("--task", default="", help="本次要继续的任务，最多 512 字；留空参考检查点关键词")
    command.add_argument("--output", type=Path, help="新建交接 Markdown 文件，已有文件不会覆盖")
    command = commands.add_parser("memory", help="管理可追溯的个人记忆，默认只保存待确认建议")
    memory_commands = command.add_subparsers(dest="memory_command", required=True)
    command = memory_commands.add_parser("list")
    command.add_argument("--project", default="")
    command.add_argument("--include-proposed", action="store_true", help="同时查看待确认记忆")
    command.add_argument("--limit", type=int, default=100)
    command = memory_commands.add_parser("save")
    command.add_argument("--key", required=True, help="2..64 字 ASCII 标识，如 output.concise")
    command.add_argument("--value", required=True, help="记忆内容，1..4000 字")
    command.add_argument("--project", default="", help="留空表示个人全局记忆")
    command.add_argument("--status", choices=["proposed", "confirmed", "withdrawn"], default="proposed")
    command.add_argument(
        "--evidence", action="append", help="归档消息的内部 ID；可重复指定，确认需用户原话证据"
    )
    command.add_argument("--account", default="default")
    command = commands.add_parser("sources", help="管理明确指定的持续采集来源")
    source_commands = command.add_subparsers(dest="source_command", required=True)
    source_commands.add_parser("list")
    command = source_commands.add_parser("add")
    command.add_argument("kind", choices=["chatgpt", "codex", "project"])
    command.add_argument("path", type=Path)
    _labels(command)
    commands.add_parser("sync", help="同步所有已配置来源")
    command = commands.add_parser("watch", help="持续同步已配置来源，Ctrl+C 停止")
    command.add_argument("--interval", type=float, default=3.0)
    command.add_argument("--once", action="store_true")
    command = commands.add_parser("project", help="项目文件与工程状态归档")
    project_commands = command.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("list")
    command = project_commands.add_parser("add")
    command.add_argument("path", type=Path)
    _labels(command)
    command = project_commands.add_parser("state")
    command.add_argument("project")
    command = project_commands.add_parser("files")
    command.add_argument("project")
    command.add_argument("--limit", type=int, default=100)
    command.add_argument("--offset", type=int, default=0)
    command = project_commands.add_parser("file")
    command.add_argument("project")
    command.add_argument("relative_path")
    command.add_argument("--max-chars", type=int, default=20000)
    commands.add_parser("audit", help="检查数据库和原始档案完整性")
    for name in ("backup", "restore"):
        command = commands.add_parser(name, help="完整备份或恢复到新的空记忆库")
        command.add_argument("path", type=Path)
        command.add_argument("--password-env", help="读取加密密码的环境变量名；不要把密码写入参数")
    command = commands.add_parser("serve", help="运行仅本机可访问的管理面板与实时采集端点")
    command.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    command.add_argument("--port", type=int, default=8765)
    command.add_argument("--sync-interval", type=float, default=3.0, help="来源同步秒数；0 关闭")
    command.add_argument("--open-browser", action="store_true")
    command = commands.add_parser("mcp", help="运行可被其他账户连接的本地 MCP 记忆工具")
    command.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    command.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    command.add_argument("--port", type=int, default=8766)
    command.add_argument("--allow-writes", action="store_true", help="显式开放检查点与个人记忆追加工具")
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str), flush=True)


def _password(args: argparse.Namespace) -> str | None:
    if not args.password_env:
        return None
    value = os.environ.get(args.password_env)
    if not value:
        raise ValueError("密码环境变量没有值；未进行备份或恢复")
    return value


def run(args: argparse.Namespace, service: Any) -> Any:
    command = args.command
    if command == "init":
        return service.initialize()
    if command == "stats":
        return service.stats()
    if command in ("import-chatgpt", "import-codex"):
        importer = service.import_chatgpt if command == "import-chatgpt" else service.import_codex
        return importer(args.path, account=args.account, project=args.project)
    if command == "search":
        return service.search(
            args.query, source=args.source, account=args.account, project=args.project, limit=args.limit
        )
    if command == "list":
        return service.list_conversations(
            source=args.source,
            account=args.account,
            project=args.project,
            limit=args.limit,
            offset=args.offset,
        )
    if command == "show":
        return service.fetch_conversation(args.conversation_id, limit=args.limit, offset=args.offset)
    if command == "checkpoint":
        return service.create_checkpoint(
            args.project,
            args.goal,
            completed=args.completed,
            decisions=args.decisions,
            next_steps=args.next_steps,
            blockers=args.blockers,
            account=args.account,
        )
    if command == "handoff":
        options = {
            "project": args.project,
            "conversation_id": args.conversation_id,
            "max_chars": args.max_chars,
            "task": args.task,
        }
        if args.output:
            return service.export_handoff(args.output, **options)
        return service.handoff(**options)
    if command == "sources":
        if args.source_command == "list":
            return service.list_sources()
        return service.configure_source(args.kind, args.path, account=args.account, project=args.project)
    if command == "memory":
        if args.memory_command == "list":
            return service.list_user_memory(
                project=args.project, include_proposed=args.include_proposed, limit=args.limit
            )
        return service.save_user_memory(
            args.key,
            args.value,
            project=args.project,
            status=args.status,
            evidence=args.evidence,
            account=args.account,
        )
    if command == "sync":
        return service.sync_sources()
    if command == "watch":
        if args.interval < 0.5:
            raise ValueError("同步间隔至少为 0.5 秒")
        while True:
            _print(service.sync_sources())
            if args.once:
                return None
            time.sleep(args.interval)
    if command == "project":
        if args.project_command == "list":
            return service.list_projects()
        if args.project_command == "add":
            project = args.project or args.path.expanduser().resolve().name
            configured = service.configure_source("project", args.path, project=project, account=args.account)
            report = service.archive_project(args.path, project=project, account=args.account)
            return {"source": configured, "report": report}
        if args.project_command == "state":
            return service.project_state(args.project)
        if args.project_command == "files":
            return service.list_project_files(args.project, limit=args.limit, offset=args.offset)
        return service.fetch_project_file(args.project, args.relative_path, max_chars=args.max_chars)
    if command == "audit":
        return service.audit()
    if command == "backup":
        return service.backup(args.path, password=_password(args))
    if command == "restore":
        return service.restore(args.path, password=_password(args))
    if command == "serve":
        from .server import serve

        serve(
            service,
            host=args.host,
            port=args.port,
            sync_interval=args.sync_interval,
            open_browser=args.open_browser,
        )
        return None
    if command == "mcp":
        from .mcp_server import run_mcp

        run_mcp(
            service, transport=args.transport, host=args.host, port=args.port, allow_writes=args.allow_writes
        )
        return None
    raise ValueError(f"未知命令：{command}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    try:
        from .service import MemoryService

        result = run(args, MemoryService(args.data_dir))
        if isinstance(result, str) and args.command == "handoff":
            print(result)
        elif result is not None:
            _print(result)
        return 0
    except KeyboardInterrupt:
        print("已停止，本地档案保留。", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, ImportError, sqlite3.Error) as exc:
        print(f"操作未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
