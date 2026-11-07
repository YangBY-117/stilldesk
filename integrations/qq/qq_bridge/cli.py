"""Local operator CLI; only explicit read commands print selected private content."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .reader import Reader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机 QQ 只读消息桥")
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="显式固定 QQ 账号，并初始化私有缓存")
    init.add_argument("--account-dir", type=Path, required=True)
    init.add_argument("--executable", type=Path, required=True)
    init.add_argument("--state-dir", type=Path)
    commands.add_parser("capabilities")
    commands.add_parser("status")
    commands.add_parser("sync")
    chats = commands.add_parser("chats")
    chats.add_argument("--query", default="")
    chats.add_argument("--limit", type=int, default=30)
    chats.add_argument("--offset", type=int, default=0)
    messages = commands.add_parser("messages")
    messages.add_argument("chat", nargs="?")
    messages.add_argument("--limit", type=int, default=100)
    messages.add_argument("--after")
    messages.add_argument("--before")
    messages.add_argument("--cursor")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--chat")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--after")
    search.add_argument("--before")
    changes = commands.add_parser("changes")
    changes.add_argument("--cursor")
    changes.add_argument("--limit", type=int, default=100)
    changes.add_argument("--wait-seconds", type=int, default=0)
    image = commands.add_parser("image")
    image.add_argument("message_id")
    image.add_argument("--max-edge", type=int, default=2048)
    one = commands.add_parser("message")
    one.add_argument("message_id")
    one.add_argument("--context-before", type=int, default=3)
    one.add_argument("--context-after", type=int, default=3)
    one.add_argument("--text-offset", type=int, default=0)
    one.add_argument("--text-limit", type=int, default=10000)
    one.add_argument("--refresh", action="store_true")
    for command in (chats, messages, search, changes):
        command.add_argument("--cache-only", action="store_true")
    for command in (messages, search):
        command.add_argument("--compact", action="store_true")
        command.add_argument("--max-content-chars", type=int)
    search.add_argument("--cursor")
    args = parser.parse_args(argv)
    try:
        if args.command == "capabilities":
            from .capabilities import capabilities
            result = {"ok": True, **capabilities()}
        elif args.command == "status" and not (args.config or os.environ.get("STILLDESK_QQ_CONFIG") or os.environ.get("QQ_BRIDGE_CONFIG")):
            from .capabilities import unconfigured_status
            result = unconfigured_status()
        elif args.command == "init":
            from .source import initialize
            result = initialize(args.account_dir, args.executable, args.state_dir, args.config)
        else:
            reader = Reader(args.config)
            if args.command == "status":
                result = reader.status()
            elif args.command == "sync":
                result = reader.sync()
            elif args.command == "chats":
                result = reader.list_chats(args.query, args.limit, args.offset, refresh=not args.cache_only)
            elif args.command == "messages":
                result = reader.get_messages(args.chat, args.limit, args.before, args.after, args.cursor, refresh=not args.cache_only, compact=args.compact, max_content_chars=args.max_content_chars)
            elif args.command == "search":
                result = reader.search_messages(args.query, args.chat, args.limit, args.after, args.before, cursor=args.cursor, refresh=not args.cache_only, compact=args.compact, max_content_chars=args.max_content_chars)
            elif args.command == "changes":
                result = reader.get_changes(args.cursor, args.limit, args.wait_seconds, refresh=not args.cache_only)
            elif args.command == "message":
                result = reader.get_message(args.message_id, args.context_before, args.context_after,
                                            args.refresh, args.text_offset, args.text_limit)
            else:
                result = reader.get_image(args.message_id, args.max_edge).metadata
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", result.get("configured", True)) else 1
    except Exception as error:
        # CLI errors also avoid accidental key/SQL/runtime dumps.
        print(json.dumps({"ok": False, "error_type": type(error).__name__,
                          "message": "读取失败；请检查本地配置、账号登录状态、权限和参数。"},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
