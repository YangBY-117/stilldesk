from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hmac
import json
import msvcrt
import os
import secrets
import threading

from .engine import Engine
from .store import write_json
from .state import runtime_directory, read_private_json, private_directory

RUNTIME = runtime_directory()
CONNECTION = RUNTIME / "connection.json"


def serve() -> None:
    private_directory(RUNTIME)
    lock = (RUNTIME / "service.lock").open("a+b")
    lock.seek(0, 2)
    if lock.tell() == 0:
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        return
    token = secrets.token_urlsafe(32)
    # PortAudio initialization belongs on the daemon's main thread on Windows.
    # It enumerates endpoints but does not open a recording stream.
    import sounddevice
    engine = Engine()
    shutdown_requested = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, code: int, data: dict) -> None:
            content = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            try:
                self.end_headers()
                self.wfile.write(content)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                # A caller can cancel a long poll without causing a second response.
                return

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_json(401, {"ok": False, "error": {"type": "unauthorized", "message": "Local service authorization required"}})
                return
            if self.path != "/rpc":
                self.send_json(404, {"ok": False, "error": {"type": "not_found", "message": "Unknown path"}})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2 * 1024 * 1024:
                    raise ValueError("Invalid request size")
                request = json.loads(self.rfile.read(size))
                method = request.get("method")
                params = request.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                actions = {"devices": engine.devices, "sessions": engine.list_sessions, "status": engine.status,
                           "start": engine.start, "pause": engine.pause, "resume": engine.resume, "stop": engine.stop,
                           "import_audio": engine.import_audio}
                if method == "ping":
                    result = {"service": "meeting-agent-local", "version": "0.1.0", "pid": os.getpid()}
                elif method in actions:
                    result = actions[method](**params)
                elif method in {"transcript", "events", "save_notes", "export"}:
                    sid = params.pop("session_id")
                    session = engine.session(sid)
                    if method == "transcript":
                        result = session.transcript(**params)
                    elif method == "events":
                        after = params.get("after_event_seq", 0)
                        if not isinstance(after, int) or after < 0:
                            raise ValueError("after_event_seq must be nonnegative")
                        with session.condition:
                            result = {"events": session.events[after:after+500], "next_event_seq": min(len(session.events), after+500),
                                      "has_more": len(session.events) > after+500}
                    elif method == "save_notes":
                        result = session.save_notes(**params)
                    else:
                        result = session.export(**params)
                elif method == "shutdown":
                    if engine.active:
                        raise ValueError("尚有会议在采集或处理中，请先结束会议。")
                    shutdown_requested.set()
                    result = {"stopping": True}
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    raise ValueError("Unknown method")
                self.send_json(200, {"ok": True, "result": result})
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                self.send_json(400, {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}})
            except Exception as exc:
                self.send_json(500, {"ok": False, "error": {"type": type(exc).__name__, "message": "服务内部错误，请查看会话事件与本机日志。"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    write_json(CONNECTION, {"port": server.server_address[1], "token": token, "pid": os.getpid()})
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        engine.close()
        server.server_close()
        if CONNECTION.exists():
            saved = read_private_json(CONNECTION)
            if saved.get("pid") == os.getpid():
                CONNECTION.unlink()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()
