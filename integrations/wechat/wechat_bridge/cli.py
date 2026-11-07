"""Local operator CLI; private content is printed only by explicit read commands."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .keys import restrict_directory
from .reader import PROJECT, Reader




def initialize(args) -> dict:
    if not args.config or not args.account_dir:
        raise ValueError("init requires explicit --config and --account-dir; no account is auto-selected")
    config_path = Path(args.config).resolve()
    if config_path.exists():
        raise ValueError("配置已存在；直接运行 sync。切换账号须使用独立目录和独立配置。")
    if args.account_dir:
        account = Path(args.account_dir).resolve()
    if not (account / "db_storage/message").is_dir():
        raise ValueError("账号目录中没有 db_storage/message。")
    executable = Path(args.executable).resolve() if args.executable else None
    if executable is None:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin") as key:
            install, _ = winreg.QueryValueEx(key, "InstallPath")
        executable = Path(install) / "Weixin.exe"
    if not executable.is_file():
        raise ValueError("没有找到指定的微信可执行文件。")
    state = Path(args.state_dir).resolve() if args.state_dir else config_path.parent
    if state == account or state.is_relative_to(account) or account.is_relative_to(state):
        raise ValueError("缓存目录必须位于微信数据目录之外。")
    restrict_directory(state)
    if config_path.parent != state:
        restrict_directory(config_path.parent)
    config = {"account_dir": str(account), "state_dir": str(state),
              "executable": str(executable.resolve()),
              "client_version_at_setup": args.client_version,
              "mode": "background_only",
              "timezone": "Asia/Shanghai"}
    with config_path.open("x", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
    return {"configured": True, "config": str(config_path), "mode": config["mode"],
            "next": "sync"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="微信本地只读消息桥")
    parser.add_argument("--config", default=None, help="本地配置的绝对路径")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="固定账号并创建私有本地目录")
    init.add_argument("--account-dir")
    init.add_argument("--executable")
    init.add_argument("--state-dir")
    init.add_argument("--client-version", default=None)
    commands.add_parser("capabilities")
    commands.add_parser("status")
    commands.add_parser("sync")
    chats = commands.add_parser("chats")
    chats.add_argument("--query", default="")
    chats.add_argument("--limit", type=int, default=30)
    chats.add_argument("--offset", type=int, default=0)
    chats.add_argument("--cache-only", action="store_true", help="复用已有缓存，不重新采集数据库")
    history = commands.add_parser("messages")
    history.add_argument("chat", nargs="?", help="会话 ID 或名称；续页可省略")
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--after")
    history.add_argument("--before")
    history.add_argument("--cursor")
    history.add_argument("--cache-only", action="store_true")
    history.add_argument("--compact", action="store_true")
    history.add_argument("--max-content-chars", type=int)
    message = commands.add_parser("message", help="读取准确消息 ID 的完整正文与同会话上下文")
    message.add_argument("message_id")
    message.add_argument("--context-before", type=int, default=3)
    message.add_argument("--context-after", type=int, default=3)
    message.add_argument("--refresh", action="store_true", help="读取前刷新本地索引")
    message.add_argument("--text-offset", type=int, default=0)
    message.add_argument("--text-limit", type=int, default=10000)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--chat")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--after")
    search.add_argument("--before")
    search.add_argument("--cursor")
    search.add_argument("--cache-only", action="store_true")
    search.add_argument("--compact", action="store_true")
    search.add_argument("--max-content-chars", type=int)
    changes = commands.add_parser("changes")
    changes.add_argument("--cursor")
    changes.add_argument("--limit", type=int, default=100)
    changes.add_argument("--wait-seconds", type=int, default=0)
    changes.add_argument("--cache-only", action="store_true")
    image = commands.add_parser("image", help="按消息 ID 读取本机已缓存图片")
    image.add_argument("message_id")
    image.add_argument("--max-edge", type=int, default=2048)
    commands.add_parser("desktop-status", help="只读检查客户端账号绑定与界面能力")
    attachment = commands.add_parser("attachment", help="检查文件卡片和本地副本")
    attachment.add_argument("message_id")
    download = commands.add_parser("download", help="获取文件；默认预览，--execute 执行下载")
    download.add_argument("message_id")
    download.add_argument("--execute", action="store_true")
    download.add_argument("--timeout-seconds", type=int, default=30)
    download.add_argument("--allow-desktop", action="store_true", help="明确允许旧客户端界面下载；默认仅非界面路径")
    send = commands.add_parser("send", help="发送文字；默认预览，--execute 执行")
    send.add_argument("chat", help="准确会话 ID")
    send.add_argument("text")
    send.add_argument("--request-id")
    send.add_argument("--execute", action="store_true")
    send.add_argument("--timeout-seconds", type=int, default=15)
    for kind, label in (("file", "文件"), ("image", "照片"), ("video", "视频")):
        media_send = commands.add_parser(f"send-{kind}", help=f"发送{label}；默认预览，--execute 执行")
        media_send.add_argument("chat", help="准确会话 ID")
        media_send.add_argument("path", help="用户明确指定的本机文件绝对路径")
        media_send.add_argument("--request-id")
        media_send.add_argument("--execute", action="store_true")
        media_send.add_argument("--timeout-seconds", type=int, choices=range(1, 61), default=30)
    args = parser.parse_args(argv)
    try:
        if args.command == "capabilities":
            from .capabilities import capabilities
            result = {"ok": True, **capabilities()}
        elif args.command == "status" and not (args.config or os.environ.get("STILLDESK_WECHAT_CONFIG") or os.environ.get("WECHAT_BRIDGE_CONFIG")):
            from .capabilities import unconfigured_status
            result = unconfigured_status()
        elif args.command == "init":
            result = initialize(args)
        else:
            reader = Reader(args.config)
            if args.command == "status":
                result = reader.status()
            elif args.command == "sync":
                result = reader.sync()
            elif args.command == "chats":
                result = reader.list_chats(args.query, args.limit, args.offset, refresh=not args.cache_only)
            elif args.command == "messages":
                result = reader.get_messages(args.chat, args.limit, args.before, args.after,
                                             cursor=args.cursor, refresh=not args.cache_only,
                                             compact=args.compact, max_content_chars=args.max_content_chars)
            elif args.command == "message":
                result = reader.get_message(args.message_id, args.context_before, args.context_after,
                                            refresh=args.refresh, text_offset=args.text_offset, text_limit=args.text_limit)
            elif args.command == "search":
                result = reader.search_messages(args.query, args.chat, args.limit, args.after, args.before,
                                                cursor=args.cursor, refresh=not args.cache_only,
                                                compact=args.compact, max_content_chars=args.max_content_chars)
            elif args.command == "image":
                result = reader.get_image(args.message_id, args.max_edge).metadata
            elif args.command == "desktop-status":
                result = reader.desktop_status()
            elif args.command == "attachment":
                result = reader.get_attachment(args.message_id)
            elif args.command == "download":
                result = reader.download_attachment(args.message_id, not args.execute, args.timeout_seconds,
                                                     allow_desktop=args.allow_desktop)
            elif args.command == "send":
                result = reader.send_text(args.chat, args.text, args.request_id,
                                          not args.execute, args.timeout_seconds)
            elif args.command in ("send-file", "send-image", "send-video"):
                method = args.command.replace("-", "_")
                result = getattr(reader, method)(chat=args.chat, path=args.path,
                                               request_id=args.request_id, dry_run=not args.execute,
                                               timeout_seconds=args.timeout_seconds)
            else:
                result = reader.get_changes(args.cursor, args.limit, args.wait_seconds, refresh=not args.cache_only)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        # No traceback or key material. Technical snapshot errors contain source metadata only.
        print(json.dumps({"error": type(error).__name__, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
