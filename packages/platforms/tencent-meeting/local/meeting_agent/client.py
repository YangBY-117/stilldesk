from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from .state import runtime_directory, read_private_json, private_directory

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = runtime_directory()
CONNECTION = RUNTIME / "connection.json"
_startup_lock = threading.Lock()


def _request(method: str, params: dict, timeout: float) -> dict:
    connection = read_private_json(CONNECTION)
    port = connection["port"]
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeError("Invalid local service endpoint")
    payload = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/rpc", data=payload,
               headers={"Authorization": "Bearer " + connection["token"], "Content-Type": "application/json"}, method="POST")
    # This is strictly a loopback channel; never route local transcript data through an HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        result = json.load(exc)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", {}).get("message", "Local service error"))
    return result["result"]


def ensure_service() -> None:
    with _startup_lock:
        try:
            _request("ping", {}, 1)
            return
        except (OSError, ValueError, KeyError, RuntimeError):
            pass
        private_directory(RUNTIME)
        python = ROOT / ".venv/Scripts/python.exe"
        if not python.exists():
            python = Path(sys.executable)
        with (RUNTIME / "service.log").open("ab") as log:
            process = subprocess.Popen([str(python), "-X", "utf8", str(ROOT / "runner.py"), "serve"], cwd=str(ROOT),
                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                       creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                _request("ping", {}, 1)
                return
            except (OSError, ValueError, KeyError, RuntimeError):
                if process.poll() not in {None, 0}:
                    raise RuntimeError("本地服务启动失败，请查看 .runtime/service.log。")
                time.sleep(0.15)
        raise RuntimeError("本地服务启动超时，请查看 .runtime/service.log。")


def call(method: str, **params) -> dict:
    ensure_service()
    return _request(method, params, max(30.0, float(params.get("wait_seconds", 0)) + 5))
