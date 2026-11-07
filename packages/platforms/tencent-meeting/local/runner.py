"""Agent entry point. Machine-readable commands write exactly one JSON result."""
from __future__ import annotations

from pathlib import Path
import argparse
import importlib.metadata
import json
import os
import sys

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="本机腾讯会议旁听 Agent 工具")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["devices", "sessions", "doctor", "serve", "shutdown"]:
        commands.add_parser(name)
    start = commands.add_parser("start")
    start.add_argument("--title", required=True)
    microphone = start.add_mutually_exclusive_group()
    microphone.add_argument("--microphone", dest="no_microphone", action="store_false")
    microphone.add_argument("--no-microphone", dest="no_microphone", action="store_true")
    start.set_defaults(no_microphone=True)
    start.add_argument("--microphone-id", type=int)
    start.add_argument("--target-pid", type=int)
    start.add_argument("--save-audio", action="store_true")
    for name in ["status", "pause", "resume", "stop", "transcript", "events", "export", "save-notes"]:
        p = commands.add_parser(name)
        p.add_argument("session_id")
        if name == "transcript":
            p.add_argument("--after-seq", type=int, default=0)
            p.add_argument("--limit", type=int, default=100)
            p.add_argument("--wait-seconds", type=float, default=0)
        if name == "events":
            p.add_argument("--after-event-seq", type=int, default=0)
        if name == "export":
            p.add_argument("--format", choices=["markdown", "json", "text", "srt"], default="markdown")
        if name == "save-notes":
            p.add_argument("--file", required=True)
            p.add_argument("--evidence-seqs", default="[]", help="JSON array of transcript sequence numbers")
            p.add_argument("--kind", choices=["live", "final"], default="final")
    imp = commands.add_parser("import-audio")
    imp.add_argument("path")
    imp.add_argument("--title", required=True)
    imp.add_argument("--save-audio", action="store_true")
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "serve":
        from meeting_agent.service import serve
        serve()
        return 0
    try:
        if command == "doctor":
            from meeting_agent.asr import MODEL_DIR
            packages = {}
            for name in ["numpy", "sherpa-onnx", "sounddevice", "av", "mcp"]:
                try:
                    packages[name] = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    packages[name] = None
            result = {"platform": sys.platform, "python": sys.version.split()[0], "packages": packages,
                      "helper_ready": (ROOT / "native/MeetingAudio/bin/Release/net9.0-windows/MeetingAudio.exe").exists(),
                      "model_ready": (MODEL_DIR / "model.int8.onnx").exists() and (MODEL_DIR / "tokens.txt").exists(),
                      "requires_windows_build": 20348, "default_audio_upload": False, "default_audio_retention": False}
        else:
            from meeting_agent.client import call
            if command == "start":
                args["include_microphone"] = not args.pop("no_microphone")
            if command == "save-notes":
                args["markdown"] = Path(args.pop("file")).read_text(encoding="utf-8")
                args["evidence_seqs"] = json.loads(args["evidence_seqs"])
            if "session_id" in args and command in {"status", "pause", "resume", "stop"}:
                args["sid"] = args.pop("session_id")
            result = call(command.replace("-", "_"), **args)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
