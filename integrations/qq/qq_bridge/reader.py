"""Account-bound QQ history index and observation ledger over verified snapshots."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from . import __version__
from .media import ImageResult, LocalImages
from .protocol import decode_message

PROJECT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
TABLES = {"c2c_msg_table": "c2c", "group_msg_table": "group"}
SCOPE = "nt_msg.db: c2c_msg_table + group_msg_table; locally retained rows only"
EXCLUDED = ["c2c_temp_msg_table", "dataline_msg_table", "discuss_msg_table",
            "service_assistant_msg_table", "other message tables", "server-only history"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _integer(value, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid " + label)
    return value


def _time(value: str | None, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("invalid time")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=TZ)
    return int(result.timestamp())


def _scalar(value, limit: int = 256) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return "binary-" + hashlib.sha256(value).hexdigest()[:16]
    text = str(value)
    if len(text) > limit or any(ord(char) < 32 for char in text):
        return "value-" + hashlib.sha256(text.encode()).hexdigest()[:16]
    return text


def _safe_metadata(value, depth: int = 0):
    """Return bounded source coverage without keys, paths, runtime dumps, or SQL."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {str(key): _safe_metadata(item, depth + 1) for key, item in list(value.items())[:50]
                if not any(word in str(key).lower() for word in
                           ("key", "path", "token", "secret", "runtime", "config", "executable"))}
    if isinstance(value, list):
        return [_safe_metadata(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        if len(value) > 240 or "\\" in value or ":/" in value or value.startswith("/"):
            return "redacted_local_detail"
        return value
    if value is None or type(value) in (bool, int, float):
        return value
    return None


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _locked(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with self._lock, self._process_lock():
            return method(self, *args, **kwargs)
    return call


from .reading import ReadingMixin


class Reader(ReadingMixin):
    def __init__(self, config_path: str | Path | None = None, *, source=None):
        if source is None:
            from .source import LocalSource
            source = LocalSource(config_path)
        self.source = source
        self.config_path = Path(source.config_path)
        self.config = source.config
        self.account = Path(source.account).resolve()
        self.state = Path(source.state).resolve()
        if self.state == self.account or self.state.is_relative_to(self.account):
            raise ValueError("state must be outside QQ account")
        if not self.state.is_dir():
            raise ValueError("private state directory does not exist")
        self.account_tag = str(source.account_tag)
        if not self.account_tag or len(self.account_tag) > 64:
            raise ValueError("invalid account tag")
        self.index_path = self.state / "index.db"
        self._lock = threading.RLock()
        self._images = LocalImages(self.account)
        with self._process_lock(), self._index() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(
                    storage_id INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    chat TEXT NOT NULL,sender TEXT NOT NULL,created INTEGER NOT NULL,
                    source_table TEXT NOT NULL,native_id TEXT NOT NULL,peer TEXT NOT NULL,
                    sender_native TEXT NOT NULL,type TEXT NOT NULL,content TEXT NOT NULL,
                    decode_status TEXT NOT NULL,unsupported_json TEXT NOT NULL,
                    images_json TEXT NOT NULL,digest TEXT NOT NULL,observed_at TEXT NOT NULL,
                    present INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX IF NOT EXISTS msg_chat_time ON messages(chat,created DESC,id DESC);
                CREATE INDEX IF NOT EXISTS msg_time ON messages(created DESC,id DESC);
                CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,name TEXT NOT NULL,
                    native_id TEXT NOT NULL,is_group INTEGER NOT NULL,last_time INTEGER NOT NULL,
                    message_count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS entities(kind TEXT NOT NULL,id TEXT NOT NULL,
                    name TEXT NOT NULL,origin TEXT NOT NULL,PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,kind TEXT NOT NULL,observed_at TEXT NOT NULL,
                    version_json TEXT NOT NULL);
            """)
            existing = self._meta(conn, "account")
            if existing is not None and existing != self.account_tag:
                raise ValueError("index is bound to another account")
            self._setmeta(conn, "account", self.account_tag)
            if self._meta(conn, "epoch") is None:
                self._setmeta(conn, "epoch", uuid.uuid4().hex)
            try:
                conn.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
                        content,content='messages',content_rowid='storage_id',tokenize='trigram');
                    CREATE TRIGGER IF NOT EXISTS msg_ai AFTER INSERT ON messages BEGIN
                        INSERT INTO message_fts(rowid,content) VALUES(new.storage_id,new.content); END;
                    CREATE TRIGGER IF NOT EXISTS msg_au AFTER UPDATE OF content ON messages BEGIN
                        INSERT INTO message_fts(message_fts,rowid,content)
                        VALUES('delete',old.storage_id,old.content);
                        INSERT INTO message_fts(rowid,content) VALUES(new.storage_id,new.content); END;
                """)
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False

    @contextmanager
    def _process_lock(self):
        with (self.state / "reader.lock").open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + 30
            if os.name == "nt":
                import msvcrt
                while True:
                    stream.seek(0)
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("reader lock busy")
                        time.sleep(0.1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                while True:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("reader lock busy")
                        time.sleep(0.1)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    @contextmanager
    def _index(self):
        conn = sqlite3.connect(self.index_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _meta(conn, key, default=None):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _setmeta(conn, key, value):
        conn.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))

    def _freshness(self, conn, result: dict | None = None):
        error = (result or {}).get("error")
        if error is None:
            raw = self._meta(conn, "last_error")
            error = json.loads(raw) if raw else None
        coverage = json.loads(self._meta(conn, "coverage", "{}"))
        return {"fresh": bool(self._meta(conn, "last_sync")) and error is None,
                "last_successful_sync": self._meta(conn, "last_sync"),
                "sync_error": error, "coverage": coverage,
                "account_tag": self.account_tag, "platform": "qq"}

    def _cursor(self, conn, kind: str, **values) -> str:
        payload = {"v": 1, "kind": kind, "account": self.account_tag,
                   "epoch": self._meta(conn, "epoch"), **values}
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"),
                                               ensure_ascii=True).encode()).decode().rstrip("=")

    def _decode_cursor(self, conn, cursor: str, kind: str) -> dict:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
            raise ValueError("invalid cursor")
        try:
            payload = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4),
                                                 altchars=b"-_", validate=True))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid cursor") from error
        if (not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("kind") != kind
                or payload.get("account") != self.account_tag
                or payload.get("epoch") != self._meta(conn, "epoch")):
            raise ValueError("cursor account or epoch mismatch")
        return payload

    def _public(self, row, conn=None, *, compact=False, max_content_chars=None) -> dict:
        result = {"id": row["id"], "platform": "qq", "account_tag": self.account_tag,
                  "chat": row["chat"], "sender": row["sender"],
                  "time": datetime.fromtimestamp(row["created"], TZ).isoformat() if row["created"] else None,
                  "timestamp": row["created"] or None,
                  "timestamp_status": "available" if row["created"] else "unavailable",
                  "content": row["content"], "type": row["type"],
                  "decode_status": row["decode_status"],
                  "source": {"database": "nt_msg.db", "table": row["source_table"],
                             "native_message_id": row["native_id"],
                             "observed_at": row["observed_at"], "read_only": True,
                             "present_in_last_snapshot": bool(row["present"])}}
        if conn is not None:
            person = conn.execute("SELECT name,origin FROM entities WHERE kind='user' AND id=?",
                                  (row["sender_native"],)).fetchone()
            chat_kind = "group" if row["source_table"] == "group_msg_table" else "user"
            room = conn.execute("SELECT name,origin FROM entities WHERE kind=? AND id=?",
                                (chat_kind, row["peer"])).fetchone()
            if person:
                result["sender_name"] = person["name"]
            if room:
                result["chat_name"] = room["name"]
        unsupported = json.loads(row["unsupported_json"])
        if unsupported:
            result["unsupported_fields"] = unsupported
        if row["type"] in ("image", "mixed") and json.loads(row["images_json"]):
            result["image"] = {"message_id": row["id"], "tool": "qq_get_image",
                               "availability": "unverified_local_cache", "originality": "unknown"}
        elif row["type"] == "image":
            result["image"] = {"message_id": row["id"], "tool": "qq_get_image",
                               "availability": "image_not_cached"}
        cap = max_content_chars if max_content_chars is not None else (1000 if compact else None)
        if cap is not None:
            if type(cap) is not int or not 1 <= cap <= 6000:
                raise ValueError("max_content_chars must be 1–6000")
            result["content_length"] = len(result["content"])
            result["content_truncated"] = len(result["content"]) > cap
            result["content"] = result["content"][:cap]
            if result["content_truncated"]:
                result["full_content_tool"] = "qq_get_message"
        return result

    def _load_entities(self, conn, capture: dict) -> dict:
        coverage: dict = {}
        specs = [("group_info.db", "group_list", ["60001", "60007"]),
                 ("profile_info.db", "profile_info_v6", ["1000", "1002", "20002"])]
        for database, table, names in specs:
            path = capture.get("snapshots", {}).get(database)
            if path is None:
                coverage[database] = "unavailable; previous labels retained"
                continue
            connection = None
            try:
                path = Path(path).resolve()
                fingerprint = _file_hash(path)
                hash_key = "entity_hash_" + database
                if self._meta(conn, hash_key) == fingerprint:
                    coverage[database] = "unchanged_authenticated_snapshot"
                    continue
                connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                if not set(names).issubset(columns):
                    coverage[database] = "unsupported_schema; previous labels retained"
                    continue
                select = ",".join('"' + name + '"' for name in names)
                labels = []
                for row in connection.execute(f'SELECT {select} FROM "{table}"'):
                    name = _scalar(row[-1])
                    if not name:
                        continue
                    kind = "group" if database == "group_info.db" else "user"
                    for native_id in row[:-1]:
                        identifier = _scalar(native_id)
                        if identifier and identifier != "0":
                            labels.append((kind, identifier, name, database + ":" + names[-1]))
                conn.executemany("""INSERT INTO entities VALUES(?,?,?,?) ON CONFLICT(kind,id)
                    DO UPDATE SET name=excluded.name,origin=excluded.origin""", labels)
                self._setmeta(conn, hash_key, fingerprint)
                coverage[database] = {"status": "snapshot_loaded", "labels": len(labels)}
            except (OSError, sqlite3.Error, ValueError):
                coverage[database] = "metadata_read_failed; previous labels retained"
            finally:
                if connection is not None:
                    connection.close()
        return coverage

    def _sync(self) -> dict:
        """Atomic full-scope observation; failed capture never advances ledger state."""
        try:
            capture = self.source.capture()
            main = capture.get("snapshots", {}).get("nt_msg.db")
            if main is None or not Path(main).is_file():
                raise FileNotFoundError("verified main snapshot missing")
            path = Path(main).resolve()
            fingerprint = _file_hash(path)
            captured_at = str(capture.get("completed_at") or capture.get("captured_at") or _now())
            with self._index() as conn:
                metadata_coverage = self._load_entities(conn, capture)
                previous = self._meta(conn, "source_hash")
                added = updated = scanned = 0
                table_counts: dict[str, int] = {}
                if fingerprint != previous:
                    source = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
                    source.row_factory = sqlite3.Row
                    try:
                        source.execute("PRAGMA query_only=ON")
                        source.execute("PRAGMA trusted_schema=OFF")
                        available = {row[0] for row in source.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'")}
                        selected = [table for table in TABLES if table in available]
                        if not selected:
                            raise ValueError("no supported message tables")
                        # Complete fingerprints avoid missing late historical rows and updates.
                        known = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT id,digest,present FROM messages")}
                        seen: set[str] = set()
                        for table in selected:
                            columns = {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}
                            if not {"40001", "40021", "40050", "40800"}.issubset(columns):
                                raise ValueError("unsupported message schema")
                            names = [name for name in ("40001", "40020", "40021", "40030", "40033",
                                                      "40050", "40800") if name in columns]
                            select = ",".join('"' + name + '"' for name in names)
                            rows = source.execute(f'SELECT {select} FROM "{table}"')
                            count = 0
                            while batch := rows.fetchmany(512):
                                for raw in batch:
                                    count += 1
                                    scanned += 1
                                    peer = _scalar(raw["40021"]) or (_scalar(raw["40030"]) if "40030" in names else "")
                                    native = _scalar(raw["40001"])
                                    if not peer or not native:
                                        raise ValueError("message identity unavailable")
                                    kind = TABLES[table]
                                    identity = hashlib.sha256((table + "\0" + peer + "\0" + native).encode()).hexdigest()[:32]
                                    message_id = f"qq:{self.account_tag}:{kind}:{identity}"
                                    if message_id in seen:
                                        raise ValueError("duplicate message identity")
                                    seen.add(message_id)
                                    digest = hashlib.sha256()
                                    for name in names:
                                        value = raw[name]
                                        data = bytes(value) if isinstance(value, (bytes, bytearray, memoryview)) else str(value).encode()
                                        digest.update(len(data).to_bytes(8, "little"))
                                        digest.update(data)
                                    digest_hex = digest.hexdigest()
                                    if known.get(message_id) == (digest_hex, 1):
                                        continue
                                    sender = _scalar(raw["40020"]) if "40020" in names else ""
                                    if not sender and "40033" in names:
                                        sender = _scalar(raw["40033"])
                                    created = raw["40050"]
                                    if type(created) is not int or not 0 <= created <= 253402214399:
                                        raise ValueError("unsupported message timestamp")
                                    decoded = decode_message(raw["40800"])
                                    chat = f"qq:{self.account_tag}:{kind}:{peer}"
                                    sender_id = f"qq:{self.account_tag}:user:{sender or 'unknown'}"
                                    values = (message_id, chat, sender_id, created, table, native, peer, sender,
                                              decoded.type, decoded.content, decoded.decode_status,
                                              json.dumps(decoded.unsupported_fields), json.dumps(decoded.image_paths),
                                              digest_hex, captured_at)
                                    conn.execute("""INSERT INTO messages(id,chat,sender,created,source_table,
                                        native_id,peer,sender_native,type,content,decode_status,unsupported_json,
                                        images_json,digest,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                        ON CONFLICT(id) DO UPDATE SET chat=excluded.chat,sender=excluded.sender,
                                        created=excluded.created,type=excluded.type,content=excluded.content,
                                        decode_status=excluded.decode_status,unsupported_json=excluded.unsupported_json,
                                        images_json=excluded.images_json,digest=excluded.digest,
                                        observed_at=excluded.observed_at,present=1""", values)
                                    event_kind = "updated" if message_id in known else "new"
                                    row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
                                    conn.execute("INSERT INTO events(message_id,kind,observed_at,version_json) VALUES(?,?,?,?)",
                                                 (message_id, event_kind, captured_at,
                                                  json.dumps(self._public(row, conn), ensure_ascii=False)))
                                    added += event_kind == "new"
                                    updated += event_kind == "updated"
                            table_counts[table] = count
                        conn.executemany("UPDATE messages SET present=0 WHERE id=? AND present<>0",
                                         ((identity,) for identity in set(known) - seen))
                    finally:
                        source.close()
                    for chat in conn.execute("""SELECT chat,peer,source_table,MAX(created) last_time,
                                               COUNT(*) message_count FROM messages GROUP BY chat""").fetchall():
                        entity_kind = "group" if chat["source_table"] == "group_msg_table" else "user"
                        entity = conn.execute("SELECT name FROM entities WHERE kind=? AND id=?",
                                              (entity_kind, chat["peer"])).fetchone()
                        label = entity[0] if entity else ("QQ 群 " if entity_kind == "group" else "QQ 会话 ") + chat["peer"]
                        conn.execute("""INSERT INTO chats VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                                        name=excluded.name,last_time=excluded.last_time,message_count=excluded.message_count""",
                                     (chat["chat"], label, chat["peer"], int(chat["source_table"] == "group_msg_table"),
                                      chat["last_time"], chat["message_count"]))
                    self._setmeta(conn, "source_hash", fingerprint)
                    self._setmeta(conn, "table_counts", json.dumps(table_counts))
                else:
                    table_counts = json.loads(self._meta(conn, "table_counts", "{}"))
                # Metadata snapshots may change while the message snapshot stays identical.
                conn.execute("""UPDATE chats SET name=COALESCE((SELECT name FROM entities
                    WHERE entities.id=chats.native_id AND entities.kind=
                    CASE chats.is_group WHEN 1 THEN 'group' ELSE 'user' END),name)""")
                indexed = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                coverage = {"scope": SCOPE, "history_complete": False,
                            "tables": table_counts, "excluded": EXCLUDED,
                            "indexed_messages": indexed, "rows_scanned_this_sync": scanned,
                            "snapshot_unchanged": fingerprint == previous,
                            "missing_supported_tables": [table for table in TABLES if table not in table_counts],
                            "removal_policy": "previously observed rows retained; source removals are not inferred",
                            "metadata": metadata_coverage,
                            "source": _safe_metadata(capture.get("coverage", {})),
                            "source_data_acquired_at": capture.get("captured_at")}
                self._setmeta(conn, "coverage", json.dumps(coverage, ensure_ascii=False))
                self._setmeta(conn, "last_sync", captured_at)
                conn.execute("DELETE FROM meta WHERE key='last_error'")
                return {"ok": True, "read_only": True, "added": added, "updated": updated,
                        **self._freshness(conn)}
        except Exception as error:
            code = ("source_unavailable" if isinstance(error, FileNotFoundError) else
                    "access_denied" if isinstance(error, PermissionError) else "sync_failed")
            safe_error = {"code": code, "error_type": type(error).__name__,
                          "message": "QQ 本地同步失败；旧缓存不代表当前完整消息，请检查本机诊断。"}
            with self._index() as conn:
                self._setmeta(conn, "last_error", json.dumps(safe_error, ensure_ascii=False))
                return {"ok": False, "read_only": True, "error": safe_error,
                        "cache_available": self._meta(conn, "last_sync") is not None,
                        **self._freshness(conn, {"error": safe_error})}

    @_locked
    def status(self) -> dict:
        try:
            source_status = _safe_metadata(self.source.status())
        except Exception:
            source_status = {"ok": False, "error": "source_status_unavailable"}
        with self._index() as conn:
            return {"ok": True, "version": __version__, "mode": "read_only",
                    "source": source_status,
                    "capabilities": {**__import__("qq_bridge.capabilities", fromlist=["capabilities"]).capabilities(), "read_only": True, "account_locked": True,
                        "history_cursor": "keyset_timestamp_and_stable_id",
                        "change_cursor": "observed_ledger_seq_account_epoch",
                        "initial_changes": "current_baseline; import history separately",
                        "late_history": True, "parsed_updates": True,
                        "message_tables": list(TABLES), "scope": SCOPE,
                        "excluded": EXCLUDED, "history_complete": False,
                        "images": {"local_pixels": True, "network_fetch": False,
                                   "arbitrary_file_paths": False, "originality": "unknown",
                                   "formats": ["JPEG", "PNG", "WebP", "GIF first frame"],
                                   "unsupported_media": "explicit_placeholder"}},
                    "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    "chats": conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0],
                    "search": "fts5_trigram" if self._fts else "literal_substring",
                    **self._freshness(conn)}

    @_locked
    def sync(self) -> dict:
        return self._sync()

    def _read_refresh(self, refresh):
        if type(refresh) is not bool:
            raise ValueError("refresh must be boolean")
        if refresh:
            return self._sync()
        with self._index() as conn:
            state = self._freshness(conn)
            return {"ok": state["fresh"], "error": state["sync_error"] or
                    (None if state["fresh"] else {"code":"not_synced","message":"先同步QQ缓存。"})}

    @_locked
    def list_chats(self, query: str = "", limit: int = 30, offset: int = 0, refresh: bool = True) -> dict:
        _integer(limit, 1, 200, "limit")
        _integer(offset, 0, 1_000_000, "offset")
        if not isinstance(query, str) or len(query) > 512:
            raise ValueError("invalid chat query")
        refresh = self._read_refresh(refresh)
        with self._index() as conn:
            clause = " WHERE instr(name,?)>0 OR instr(id,?)>0" if query else ""
            args = [query, query] if query else []
            total = conn.execute("SELECT COUNT(*) FROM chats" + clause, args).fetchone()[0]
            rows = conn.execute("SELECT * FROM chats" + clause + " ORDER BY last_time DESC,id LIMIT ? OFFSET ?",
                                [*args, limit, offset]).fetchall()
            chats = [{"id": row["id"], "platform": "qq", "account_tag": self.account_tag,
                      "name": row["name"], "native_id": row["native_id"],
                      "is_group": bool(row["is_group"]), "message_count": row["message_count"],
                      "last_timestamp": row["last_time"], "name_source": "local_profile_or_native_identifier"}
                     for row in rows]
            return {"ok": refresh["ok"] or self._meta(conn, "last_sync") is not None,
                    "chats": chats, "total": total, "offset": offset,
                    "has_more": offset + len(chats) < total, **self._freshness(conn, refresh)}

    def _check_chat(self, chat):
        if not isinstance(chat, str) or not 1 <= len(chat) <= 512:
            raise ValueError("invalid chat")
        if not chat.startswith(f"qq:{self.account_tag}:"):
            raise ValueError("chat belongs to another account")



    def get_changes(self, cursor: str | None = None, limit: int = 100,
                    wait_seconds: int = 0, refresh: bool = True) -> dict:
        _integer(limit, 1, 200, "limit")
        _integer(wait_seconds, 0, 30, "wait")
        with self._index() as conn:
            decoded = self._decode_cursor(conn, cursor, "changes") if cursor else None
            seq = decoded.get("seq") if decoded else None
            if decoded is not None:
                _integer(seq, 0, 2**63 - 1, "cursor sequence")
                if seq > conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]:
                    raise ValueError("cursor is ahead of this ledger")
        refresh_source = refresh
        deadline = time.monotonic() + wait_seconds
        while True:
            with self._lock, self._process_lock():
                refresh = self._read_refresh(refresh_source)
            with self._index() as conn:
                conn.execute("BEGIN")
                if cursor:
                    self._decode_cursor(conn, cursor, "changes")
                if not refresh["ok"]:
                    # Never create/advance a baseline or claim no changes after failed capture.
                    return {"ok": False, "cursor": cursor, "cursor_advanced": False,
                            "error": refresh["error"], **self._freshness(conn, refresh)}
                if seq is None:
                    maximum = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                    return {"ok": True, "changes": [], "cursor": self._cursor(conn, "changes", seq=maximum),
                            "baseline": True, "has_more": False,
                            "semantics": "newly observed local rows; use history for initial import",
                            **self._freshness(conn, refresh)}
                rows = conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                                    (seq, limit + 1)).fetchall()
                if rows or time.monotonic() >= deadline:
                    more = len(rows) > limit
                    rows = rows[:limit]
                    next_seq = rows[-1]["seq"] if rows else seq
                    changes = []
                    for row in rows:
                        message = json.loads(row["version_json"])
                        # A cache generated by an earlier reader revision may have rendered
                        # source timestamp 0 as 1970; normalize the public ledger view too.
                        if not message.get("timestamp"):
                            message.update(time=None, timestamp=None, timestamp_status="unavailable")
                        else:
                            message.setdefault("timestamp_status", "available")
                        changes.append({"seq": row["seq"], "kind": row["kind"],
                                        "observed_at": row["observed_at"], "message": message})
                    return {"ok": True, "changes": changes,
                            "cursor": self._cursor(conn, "changes", seq=next_seq), "has_more": more,
                            "baseline": False, "update_semantics": "local parsing view changed; no edit/revoke claim",
                            **self._freshness(conn, refresh)}
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    @_locked
    def get_image(self, message_id: str, max_edge: int = 2048) -> ImageResult:
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
            raise ValueError("invalid message id")
        _integer(max_edge, 512, 4096, "image edge")
        with self._index() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                return ImageResult({"ok": False, "platform": "qq", "error": {"code": "message_not_found"}})
        refresh = self._sync()
        if not refresh["ok"]:
            return ImageResult({"ok": False, "platform": "qq", "fresh": False,
                                "error": refresh["error"]})
        with self._index() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            source = self._public(row, conn)
            if not row["present"]:
                return ImageResult({"ok": False, "source": source, "error": {"code": "message_source_changed"}})
            if row["type"] not in ("image", "mixed"):
                return ImageResult({"ok": False, "source": source, "error": {"code": "not_image_message"}})
            return self._images.read(json.loads(row["images_json"]), source, max_edge)
