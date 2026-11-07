"""Selected-account QQNT source acquisition, with private immutable snapshots."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import traceback

from .cipher import snapshot, verify_key, QQ_HEADER_SIZE, PAGE_SIZE
from .keys import discover_keys, load_keys, save_keys, restrict_directory, DiscoveryError

PROJECT = Path(__file__).resolve().parents[1]
NAMES = ("nt_msg.db", "group_info.db", "profile_info.db")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _configuration_path(config_path=None):
    selected = config_path or os.environ.get("STILLDESK_QQ_CONFIG") or os.environ.get("QQ_BRIDGE_CONFIG")
    if not selected:
        raise ValueError("An explicit --config or STILLDESK_QQ_CONFIG is required")
    return Path(selected).resolve()


def _check_private_paths(account: Path, state: Path, config_path: Path):
    if (state == account or state.is_relative_to(account)
            or account.is_relative_to(state)):
        raise ValueError("QQ bridge 缓存与源账号目录不得包含或重叠。")
    if not config_path.is_relative_to(state) or config_path == state:
        raise ValueError("QQ bridge 配置必须保存在明确的私有缓存目录内。")


def initialize(account_dir, executable, state_dir=None, config_path=None):
    account = Path(account_dir).resolve(strict=True)
    executable = Path(executable).resolve(strict=True)
    target = _configuration_path(config_path)
    state = Path(state_dir or target.parent).resolve()
    if not (account / "nt_db" / "nt_msg.db").is_file():
        raise ValueError("account_dir 必须是含 nt_db/nt_msg.db 的明确 QQNT 账号目录。")
    # Validate before mkdir/ACL/config writes. A parent-of-account cache would
    # otherwise change source directory permissions through inheritance.
    _check_private_paths(account, state, target)
    restrict_directory(state)
    restrict_directory(target.parent)
    config = {"version": 1, "platform": "qq", "account_dir": str(account),
              "state_dir": str(state), "executable": str(executable)}
    if target.exists():
        old = json.loads(target.read_text(encoding="utf-8"))
        if Path(old["account_dir"]).resolve() != account:
            raise ValueError("现有配置绑定另一账号；请选择独立配置及缓存。")
        if old != config:
            raise ValueError("现有配置与请求不同；请保留现有配置并显式迁移。")
    else:
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)
    return {"initialized": True, "config_path": str(target),
            "account_tag": hashlib.sha256(str(account).casefold().encode()).hexdigest()[:16]}


class LocalSource:
    def __init__(self, config_path=None):
        self.config_path = _configuration_path(config_path)
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        if self.config.get("platform") != "qq" or self.config.get("version") != 1:
            raise ValueError("不是支持的 QQ bridge 配置。")
        self.account = Path(self.config["account_dir"]).resolve(strict=True)
        self.state = Path(self.config["state_dir"]).resolve(strict=True)
        self.executable = Path(self.config["executable"]).resolve(strict=True)
        _check_private_paths(self.account, self.state, self.config_path)
        self.root = (self.account / "nt_db").resolve(strict=True)
        if not self.root.is_relative_to(self.account):
            raise ValueError("QQNT 数据目录不能重定向到绑定账号之外。")
        self.account_tag = hashlib.sha256(str(self.account).casefold().encode()).hexdigest()[:16]
        self.key_path = self.state / "keys.dpapi"
        self.last_capture = None

    @contextmanager
    def _source_lock(self):
        with (self.state / "source.lock").open("a+b") as stream:
            if stream.seek(0, 2) == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + 90
            if os.name == "nt":
                import msvcrt
                while True:
                    stream.seek(0)
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("QQ bridge 另一个来源捕获正在运行。")
                        time.sleep(0.1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def status(self):
        from .session import inspect_session
        version = None
        try:
            current = json.loads((self.executable.parent / "versions" / "config.json").read_text(encoding="utf-8"))
            version = current.get("curVersion")
        except (OSError, ValueError):
            pass
        return {"platform": "qq", "account_tag": self.account_tag,
                "source_available": (self.root / "nt_msg.db").is_file(),
                "key_cached": self.key_path.is_file(), "client_version": version,
                "client_session": inspect_session(self.executable, self.account),
                "coverage": {"mode": "local_account_cache", "complete_history": False,
                             "source_read_only": True, "cross_database_atomic": False}}

    def capture(self):
        with self._source_lock():
            try:
                return self._capture()
            except Exception as error:
                # Keep content-free local diagnostics. MCP receives only the
                # classified failure; exception text may contain source paths.
                try:
                    frames = traceback.extract_tb(error.__traceback__)
                    reason = "source_capture_failed"
                    cause = error
                    for _ in range(6):
                        if type(cause).__name__ == "_SourceChanged":
                            reason = "source_changed_during_capture"
                            break
                        cause = cause.__cause__
                        if cause is None:
                            break
                    diagnostic = {"at": _now(), "error_type": type(error).__name__,
                        "reason": reason, "frames": [{"file": Path(f.filename).name,
                            "line": f.lineno, "function": f.name} for f in frames]}
                    temporary = self.state / "last_source_error.json.tmp"
                    temporary.write_text(json.dumps(diagnostic), encoding="utf-8")
                    os.replace(temporary, self.state / "last_source_error.json")
                except OSError:
                    pass
                raise

    def _capture(self):
        paths = []
        pages = {}
        unavailable = []
        for name in NAMES:
            path = self.root / name
            try:
                if path.resolve(strict=True).parent != self.root:
                    raise ValueError("QQNT 数据库不能重定向到账号数据库目录之外。")
                if path.stat().st_size < QQ_HEADER_SIZE + PAGE_SIZE:
                    raise FileNotFoundError("QQNT 数据库没有可验证页面。")
                with path.open("rb") as stream:
                    stream.seek(QQ_HEADER_SIZE)
                    pages[name] = stream.read(PAGE_SIZE)
                if len(pages[name]) != PAGE_SIZE:
                    raise FileNotFoundError("QQNT 数据库页面不完整。")
                paths.append(path)
            except (OSError, ValueError):
                if name == "nt_msg.db":
                    raise
                unavailable.append({"database": name, "status": "unavailable"})
        keys = load_keys(self.key_path, self.account)
        missing = []
        for path in paths:
            page = pages[path.name]
            if path.name not in keys or not verify_key(keys[path.name], page):
                missing.append(path)
        discovery = None
        if missing:
            try:
                recovered, discovery = discover_keys(self.account, missing, self.executable)
            except DiscoveryError:
                if any(path.name == "nt_msg.db" for path in missing):
                    raise
                # Name enrichment is optional. Its inability to recover a key
                # must not prevent authenticating the main message database.
                recovered = {}
                discovery = {"optional_key_discovery": "unavailable"}
            if recovered:
                keys.update(recovered)
                save_keys(self.key_path, self.account, keys)
        snapshots = {}
        records = []
        for path in paths:
            try:
                if path.name not in keys:
                    raise DiscoveryError("没有通过验证的可用密钥。")
                destination = self.state / "snapshots" / path.name
                record = snapshot(path, destination, keys[path.name])
                snapshots[path.name] = destination
                records.append({"database": path.name, **record})
            except Exception:
                if path.name == "nt_msg.db":
                    raise
                unavailable.append({"database": path.name, "status": "unavailable"})
        result = {"snapshots": snapshots, "records": records,
                  "captured_at": min(record["captured_at"] for record in records),
                  "completed_at": _now(),
                  "coverage": {**self.status()["coverage"], "databases": list(snapshots),
                               "optional_unavailable": unavailable,
                               "capture_validation": "per-page-hmac-and-sqlite-quick-check",
                               "wal_applied": True}}
        if discovery:
            result["key_discovery"] = discovery
        self.last_capture = result
        return result
