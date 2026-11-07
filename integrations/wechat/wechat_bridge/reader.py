"""Account-locked, read-only Weixin history with a private searchable local index."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import functools
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET

import zstandard
import apsw

from .keys import discover_keys, load_keys, save_keys
from .media import ImageResult, ImageUnavailable, LocalImages, image_hashes
from . import __version__

PROJECT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


def _locked(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with self._lock, self._process_lock():
            return method(self, *args, **kwargs)
    return call


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: str | None, default: int) -> int:
    if value is None:
        return default
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.timestamp())


def decode_content(value, message_type: int) -> tuple[str, str]:
    """Decode text and card fields; binary media remain explicit placeholders."""
    names = {1: "文本", 3: "图片", 34: "语音", 43: "视频", 47: "表情",
             48: "位置", 49: "链接/文件/引用", 50: "通话", 10000: "系统消息"}
    kind = names.get(message_type & 0xFFFFFFFF, f"类型{message_type}")
    if message_type & 0xFFFFFFFF in (3, 34, 43, 47, 50):
        # Media XML can contain CDN credentials; this text-only bridge returns
        # a visible placeholder rather than exposing an internal transport blob.
        return f"[{kind}]", "media_placeholder"
    if isinstance(value, bytes):
        if value.startswith(b"\x28\xb5\x2f\xfd"):
            try:
                value = zstandard.ZstdDecompressor().decompress(value, max_output_size=2_000_000)
            except zstandard.ZstdError:
                return f"[{kind}：正文无法解码]", "unavailable"
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return f"[{kind}：非文本内容]", "media_or_unknown"
    text = str(value or "").split("\x01\x00", 1)[0].replace("\x00", "").strip().lstrip("\ufeff")
    if message_type & 0xFFFFFFFF == 49:
        # Group cards can have an account sender prefix before the XML.
        # Strip only the bounded envelope so transport credentials never fall
        # through as ordinary text when the card is otherwise well formed.
        text = re.sub(r"^[^\s<>:]{1,256}:\r?\n(?=<)", "", text, count=1)
    if not text:
        return f"[{kind}]", "empty_or_media"
    if len(text) > 200_000:
        if message_type & 0xFFFFFFFF == 49:
            return "[链接/文件/引用：卡片过长未解析]", "card_unparsed"
        return text[:200_000], "truncated_at_200000_characters"
    if text.startswith(("<msg", "<?xml", "<appmsg")) or (message_type & 0xFFFFFFFF == 49 and text.startswith("<")):
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, flags=re.IGNORECASE):
            return f"[{kind}：卡片声明未解析]", "card_unparsed"
        try:
            node = ET.fromstring(text)
            parts = [node.findtext(path) for path in
                     (".//title", ".//des", ".//url", ".//refermsg/displayname", ".//refermsg/content")]
            fields = list(dict.fromkeys(p.strip() for p in parts if p and p.strip()))
            if fields:
                return f"[{kind}] " + "\n".join(fields), "card_fields"
        except ET.ParseError:
            pass
        if message_type & 0xFFFFFFFF == 49:
            return "[链接/文件/引用：未解析卡片]", "card_unparsed"
    if message_type & 0xFFFFFFFF == 49 and "<appmsg" in text:
        return "[链接/文件/引用：未解析卡片]", "card_unparsed"
    return text, "decoded"


def table_fingerprints(path: str | Path, tables: list[str]) -> dict[str, str]:
    """Hash complete B-trees, including overflow pages, to skip unchanged chats.

    This reads an already authenticated, immutable private snapshot. No heuristic
    recent-message window is used: old edits and removals change the table hash.
    """
    path = Path(path).resolve()
    hashes = {name: hashlib.blake2b(digest_size=32) for name in tables}
    conn = apsw.Connection(path.as_uri() + "?mode=ro&immutable=1",
                           flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI)
    try:
        conn.execute("PRAGMA query_only=ON")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        senders = list(conn.execute("SELECT rowid,user_name FROM Name2Id ORDER BY rowid"))
        sender_digest = hashlib.sha256(json.dumps(senders, ensure_ascii=False).encode()).hexdigest()
        with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as image:
            for name, page in conn.execute("SELECT name,pageno FROM dbstat WHERE name GLOB 'Msg_*' ORDER BY pageno"):
                if name in hashes:
                    hashes[name].update(page.to_bytes(8, "little"))
                    hashes[name].update(image[(page - 1) * page_size:page * page_size])
    finally:
        conn.close()
    return {name: digest.hexdigest() + ":" + sender_digest for name, digest in hashes.items()}


class Reader:
    def __init__(self, config_path: str | Path | None = None):
        selected = config_path or os.environ.get("STILLDESK_WECHAT_CONFIG") or os.environ.get("WECHAT_BRIDGE_CONFIG")
        if not selected:
            raise ValueError("An explicit --config or STILLDESK_WECHAT_CONFIG is required")
        self.config_path = Path(selected).resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.account = Path(self.config["account_dir"]).resolve()
        self.root = self.account / "db_storage"
        self.state = Path(self.config["state_dir"]).resolve()
        if (self.state == self.account or self.state.is_relative_to(self.account)
                or self.account.is_relative_to(self.state)):
            raise ValueError("缓存必须位于微信数据目录之外。")
        if not self.state.is_dir() or not self.root.is_dir():
            raise ValueError("账号目录或已初始化的私有缓存不存在。")
        self.index_path = self.state / "index.db"
        self.key_path = self.state / "keys.dpapi"
        self.account_tag = hashlib.sha256(str(self.account).encode()).hexdigest()[:16]
        self.self_id = re.sub(r"_\w{4}$", "", self.account.name)
        self._lock = threading.RLock()
        self._last_sync_completed = 0.0
        self._keys: dict[str, bytes] | None = None
        self._images = LocalImages(self.account, self.state, Path(self.config["executable"]))
        with self._process_lock(), self._index() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(rel TEXT PRIMARY KEY, signature TEXT NOT NULL,
                    captured_at TEXT NOT NULL, rows INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS source_tables(shard TEXT NOT NULL, name TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, PRIMARY KEY(shard,name));
                CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    is_group INTEGER NOT NULL DEFAULT 0, unread INTEGER NOT NULL DEFAULT 0,
                    last_time INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS messages(
                    ingest_id INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    chat TEXT NOT NULL, shard TEXT NOT NULL, source_table TEXT NOT NULL,
                    local_id INTEGER NOT NULL, server_id TEXT, seq INTEGER NOT NULL,
                    created INTEGER NOT NULL, sender TEXT, type INTEGER NOT NULL,
                    content TEXT NOT NULL, decode_status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS msg_chat_time ON messages(chat, created, seq, id);
                CREATE INDEX IF NOT EXISTS msg_time ON messages(created);
                CREATE INDEX IF NOT EXISTS msg_source_table ON messages(shard,source_table);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL, kind TEXT NOT NULL, observed_at TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
                    content, content='messages', content_rowid='ingest_id', tokenize='trigram');
                CREATE TRIGGER IF NOT EXISTS msg_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO message_fts(rowid,content) VALUES(new.ingest_id,new.content); END;
                CREATE TRIGGER IF NOT EXISTS msg_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO message_fts(message_fts,rowid,content)
                    VALUES('delete',old.ingest_id,old.content); END;
                CREATE TRIGGER IF NOT EXISTS msg_au AFTER UPDATE ON messages BEGIN
                    INSERT INTO message_fts(message_fts,rowid,content)
                    VALUES('delete',old.ingest_id,old.content);
                    INSERT INTO message_fts(rowid,content) VALUES(new.ingest_id,new.content); END;
            """)
            conn.execute("INSERT OR IGNORE INTO meta VALUES('epoch',?)", (uuid.uuid4().hex,))
            existing = conn.execute("SELECT value FROM meta WHERE key='account'").fetchone()
            if existing and existing[0] != self.account_tag:
                raise ValueError("本地索引绑定的是另一账号，拒绝切换。")
            conn.execute("INSERT OR IGNORE INTO meta VALUES('account',?)", (self.account_tag,))

    @contextmanager
    def _process_lock(self):
        path = self.state / "bridge.lock"
        with path.open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + 120
            if os.name == "nt":
                import msvcrt
                while True:
                    stream.seek(0)
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("另一个本地同步仍在运行。")
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

    @contextmanager
    def _index(self):
        conn = sqlite3.connect(self.index_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _paths(self) -> list[Path]:
        required = [self.root / "contact/contact.db", self.root / "session/session.db"]
        messages = sorted((self.root / "message").glob("message_[0-9]*.db"))
        paths = [path for path in required + messages if path.is_file()]
        if not messages:
            raise RuntimeError("所选账号没有可读取的本地消息分片。")
        return paths

    def _ensure_keys(self, paths: list[Path]):
        from .cipher import verify_key
        if self._keys is None:
            self._keys = load_keys(self.key_path, self.account)
        missing = []
        for path in paths:
            rel = path.relative_to(self.root).as_posix()
            with path.open("rb") as stream:
                first = stream.read(4096)
            if rel not in self._keys or not verify_key(self._keys[rel], first):
                missing.append(path)
        if missing:
            found, metrics = discover_keys(self.account, missing, Path(self.config["executable"]))
            self._keys.update(found)
            if found:
                save_keys(self.key_path, self.account, self._keys)
            (self.state / "last_discovery.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
            if len(found) != len(missing):
                raise RuntimeError(f"有 {len(missing) - len(found)} 个必需数据库未找到有效读取密钥；保持微信登录后重试。")

    @staticmethod
    def _source_conn(path: str):
        conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        conn.text_factory = lambda value: value.decode("utf-8", errors="replace")
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _sync(self) -> dict:
        from .cipher import snapshot
        started = _now()
        paths = self._paths()
        self._ensure_keys(paths)
        snapshots = []
        for path in paths:
            rel = path.relative_to(self.root).as_posix()
            destination = self.state / "snapshots" / rel.replace("/", "__")
            record = snapshot(path, destination, self._keys[rel])
            record["rel"] = rel
            snapshots.append(record)
        with self._index() as index:
            bootstrapped = index.execute("SELECT 1 FROM meta WHERE key='bootstrapped'").fetchone() is not None
            names = {}
            session_rows = []
            for record in snapshots:
                if record["rel"] == "contact/contact.db":
                    conn = self._source_conn(record["path"])
                    try:
                        for row in conn.execute("SELECT username,nick_name,remark FROM contact"):
                            names[row["username"]] = row["remark"] or row["nick_name"] or row["username"]
                    finally:
                        conn.close()
                elif record["rel"] == "session/session.db":
                    conn = self._source_conn(record["path"])
                    try:
                        session_rows = conn.execute("SELECT username,unread_count,last_timestamp FROM SessionTable").fetchall()
                    finally:
                        conn.close()
            for chat, name in names.items():
                if chat:
                    index.execute("INSERT INTO chats(id,name,is_group) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                                  (chat, name, int(chat.endswith("@chatroom"))))
            for row in session_rows:
                chat = row["username"]
                if chat:
                    index.execute("INSERT INTO chats(id,name,is_group,unread,last_time) VALUES(?,?,?,?,?) "
                                  "ON CONFLICT(id) DO UPDATE SET unread=excluded.unread,last_time=excluded.last_time",
                                  (chat, names.get(chat, chat), int(chat.endswith("@chatroom")), row["unread_count"] or 0,
                                   row["last_timestamp"] or 0))
            # A previously resolved account-local chat ID remains a valid MD5
            # mapping if the contact/session source temporarily disappears.
            known_chats = {row[0] for row in index.execute("SELECT id FROM chats")
                           if not re.fullmatch(r"hash:[0-9a-f]{32}", row[0])}
            changed = []
            messages_added = messages_updated = 0
            for record in snapshots:
                rel = record["rel"]
                if not rel.startswith("message/"):
                    continue
                source = self._source_conn(record["path"])
                try:
                    sender_map = {row[0]: row[1] for row in source.execute("SELECT rowid,user_name FROM Name2Id")}
                    hash_map = {hashlib.md5(name.encode()).hexdigest(): name
                                for name in known_chats | set(sender_map.values()) if name}
                    # The parsed view depends on metadata outside the message
                    # B-tree. A recovered contact/session can resolve a hash-only
                    # chat even when the entire message shard is unchanged.
                    mapping = {"chats": sorted(hash_map.items()),
                               "senders": sorted(sender_map.items())}
                    mapping_digest = hashlib.sha256(json.dumps(mapping, ensure_ascii=False).encode()).hexdigest()
                    signature = json.dumps({"source": record["source_signature"],
                                            "mapping_sha256": mapping_digest}, sort_keys=True)
                    previous = index.execute("SELECT signature FROM sources WHERE rel=?", (rel,)).fetchone()
                    if previous and previous[0] == signature:
                        continue
                    tables = [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
                              if re.fullmatch(r"Msg_[0-9a-fA-F]{32}", row[0])]
                    fingerprints = table_fingerprints(record["path"], tables)
                    old_fingerprints = {row[0]: row[1] for row in index.execute(
                        "SELECT name,fingerprint FROM source_tables WHERE shard=?", (rel,))}
                    index.execute("CREATE TEMP TABLE IF NOT EXISTS seen_ids(id TEXT PRIMARY KEY)")
                    index.execute("DELETE FROM seen_ids")
                    count = 0
                    for table in tables:
                        chat = hash_map.get(table[4:].lower(), "hash:" + table[4:].lower())
                        # Unchanged message bytes do not imply an unchanged
                        # resolved chat. Check the cached attribution before
                        # taking the table fast path; unrelated new contacts
                        # still avoid decoding existing message content.
                        cached_chats = [row[0] for row in index.execute(
                            "SELECT DISTINCT chat FROM messages WHERE shard=? AND source_table=? LIMIT 2",
                            (rel, table))]
                        if (old_fingerprints.get(table) == fingerprints[table]
                                and (not cached_chats or cached_chats == [chat])):
                            # Preserve these rows in deletion reconciliation below.
                            index.execute("INSERT OR IGNORE INTO seen_ids SELECT id FROM messages WHERE shard=? AND source_table=?",
                                          (rel, table))
                            count += index.execute("SELECT count(*) FROM messages WHERE shard=? AND source_table=?", (rel, table)).fetchone()[0]
                            continue
                        index.execute("INSERT OR IGNORE INTO chats(id,name,is_group) VALUES(?,?,?)",
                                      (chat, names.get(chat, chat), int(chat.endswith("@chatroom"))))
                        for row in source.execute(f'SELECT local_id,server_id,sort_seq,create_time,real_sender_id,'
                                                  f'local_type,message_content,compress_content FROM "{table}"'):
                            ref = f"{rel}/{table}/{row['local_id']}"
                            text, decoding = decode_content(row["message_content"], row["local_type"])
                            if decoding in ("unavailable", "empty_or_media", "media_or_unknown") and row["compress_content"]:
                                alternative, alt_status = decode_content(row["compress_content"], row["local_type"])
                                if alt_status in ("decoded", "card_fields"):
                                    text, decoding = alternative, alt_status
                            sender = sender_map.get(row["real_sender_id"], "")
                            # Sender ids are resolved using this exact shard's Name2Id.
                            values = (chat, rel, table, row["local_id"], str(row["server_id"] or ""),
                                      row["sort_seq"] or 0, row["create_time"] or 0, sender,
                                      row["local_type"], text, decoding)
                            old = index.execute("SELECT chat,shard,source_table,local_id,server_id,seq,created,sender,type,content,decode_status "
                                                "FROM messages WHERE id=?", (ref,)).fetchone()
                            if not old:
                                index.execute("INSERT INTO messages(id,chat,shard,source_table,local_id,server_id,seq,created,sender,type,content,decode_status) "
                                              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (ref,) + values)
                                messages_added += 1
                            elif tuple(old) != values:
                                index.execute("UPDATE messages SET chat=?,shard=?,source_table=?,local_id=?,server_id=?,seq=?,created=?,sender=?,type=?,content=?,decode_status=? WHERE id=?",
                                              values + (ref,))
                                messages_updated += 1
                            if bootstrapped and (not old or tuple(old) != values):
                                index.execute("INSERT INTO events(message_id,kind,observed_at) VALUES(?,?,?)",
                                              (ref, "newly_observed" if not old else "updated", _now()))
                            index.execute("INSERT OR IGNORE INTO seen_ids VALUES(?)", (ref,))
                            count += 1
                        index.execute("INSERT INTO source_tables VALUES(?,?,?) ON CONFLICT(shard,name) DO UPDATE SET fingerprint=excluded.fingerprint",
                                      (rel, table, fingerprints[table]))
                    removed = index.execute("SELECT id FROM messages WHERE shard=? AND id NOT IN (SELECT id FROM seen_ids)", (rel,)).fetchall()
                    for row in removed:
                        if bootstrapped:
                            index.execute("INSERT INTO events(message_id,kind,observed_at) VALUES(?,?,?)", (row[0], "removed_from_local_source", _now()))
                        index.execute("DELETE FROM messages WHERE id=?", (row[0],))
                    for table in set(old_fingerprints) - set(tables):
                        index.execute("DELETE FROM source_tables WHERE shard=? AND name=?", (rel, table))
                    index.execute("INSERT INTO sources VALUES(?,?,?,?) ON CONFLICT(rel) DO UPDATE SET "
                                  "signature=excluded.signature,captured_at=excluded.captured_at,rows=excluded.rows",
                                  (rel, signature, record["captured_at"], count))
                    changed.append(rel)
                finally:
                    source.close()
            index.execute("UPDATE chats SET last_time=MAX(last_time,COALESCE((SELECT MAX(created) FROM messages WHERE chat=chats.id),0))")
            index.execute("INSERT OR REPLACE INTO meta VALUES('bootstrapped','1')")
            finished = _now()
            index.execute("INSERT OR REPLACE INTO meta VALUES('last_sync',?)", (finished,))
            index.execute("INSERT OR REPLACE INTO meta VALUES('snapshot_details',?)", (json.dumps(snapshots),))
        return {"started_at": started, "finished_at": finished, "changed_shards": changed,
                "messages_added": messages_added, "messages_updated": messages_updated,
                "databases": len(snapshots), "coverage": self._coverage()}

    def _coverage(self):
        # A disappeared shard is not evidence that its messages were deleted.
        # Retain those rows, but make their unavailable source explicit even on
        # cache-only queries and status checks that do not run a fresh sync.
        with self._index() as conn:
            shards = {row[0] for row in conn.execute("SELECT rel FROM sources")}
            shards = {shard for shard in shards if re.fullmatch(r"message/message_[0-9]+\.db", shard)}
            missing = sorted(shard for shard in shards if not (self.root / shard).is_file())
            cached_count = sum(conn.execute("SELECT COUNT(*) FROM messages WHERE shard=?", (shard,)).fetchone()[0]
                               for shard in missing)
        availability = {"status": "partial" if missing else ("available" if shards else "not_indexed"),
                        "checked_at": _now(), "indexed_message_shards": len(shards),
                        "missing_message_shards": missing,
                        "cached_messages_in_missing_shards": cached_count,
                        "missing_source_policy": "retain_cached_messages"}
        missing_metadata = [rel for rel in ("contact/contact.db", "session/session.db")
                            if not (self.root / rel).is_file()]
        availability["missing_metadata_sources"] = missing_metadata
        if missing_metadata:
            availability["status"] = "partial"
        return {"source": "selected_local_weixin_account", "read_only": True,
                "phone_or_cloud_complete": False, "cross_database_atomic": False,
                "text_and_card_fields": True,
                "local_image_contents": True, "image_tool": "wechat_get_image",
                "image_requires_local_cache": True, "image_text_searchable": False,
                "voice_video_contents": False,
                "indexed_source_availability": availability,
                "untrusted_message_content": True}

    @_locked
    def sync(self) -> dict:
        result = self._sync()
        self._last_sync_completed = time.monotonic()
        return result

    def _refresh(self):
        """Keep source mutations locked, coalescing overlapping refresh requests."""
        requested_at = time.monotonic()
        with self._lock, self._process_lock():
            if self._last_sync_completed < requested_at:
                self._sync()
                self._last_sync_completed = time.monotonic()

    def _cache_at(self, conn):
        row = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
        return row[0] if row else None

    def status(self) -> dict:
        from .session import inspect_session
        with self._index() as conn:
            conn.execute("BEGIN")
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            chats = conn.execute("SELECT COUNT(DISTINCT chat) FROM messages").fetchone()[0]
            last = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
            span = conn.execute("SELECT MIN(created),MAX(created) FROM messages").fetchone()
        return {"version": __version__, "account_tag": self.account_tag, "initialized": self.key_path.exists(),
                "desktop_actions_enabled": False,
                "capabilities": __import__("wechat_bridge.capabilities", fromlist=["capabilities"]).capabilities(),
                "messages": count, "chats_with_messages": chats,
                "last_sync": last[0] if last else None,
                "oldest_message": self._time(span[0]), "newest_message": self._time(span[1]),
                "client_version_at_setup": self.config.get("client_version_at_setup"),
                "client_session": inspect_session(self.config["executable"], self.account),
                "coverage": self._coverage()}

    def _resolve_chat(self, conn, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError("会话查询需要非空字符串。")
        exact = conn.execute("SELECT id FROM chats WHERE id=?", (value,)).fetchone()
        if exact:
            return exact[0]
        rows = conn.execute("SELECT id,name FROM chats WHERE name=? ORDER BY id LIMIT 11", (value,)).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        if not rows:
            rows = conn.execute("SELECT id,name FROM chats WHERE instr(name,?)>0 OR instr(id,?)>0 "
                                "ORDER BY last_time DESC,id LIMIT 11", (value, value)).fetchall()
        if len(rows) != 1:
            from .errors import ChatResolutionError
            raise ChatResolutionError(rows[:10])
        return rows[0]["id"]

    @staticmethod
    def _time(value):
        return datetime.fromtimestamp(value, TZ).isoformat() if value else None

    def list_chats(self, query: str = "", limit: int = 30, offset: int = 0, refresh: bool = True) -> dict:
        if type(refresh) is not bool:
            raise ValueError("refresh 需要布尔值。")
        if refresh:
            self._refresh()
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("分页参数无效。")
        with self._index() as conn:
            conn.execute("BEGIN")
            rows = conn.execute("SELECT c.*,COUNT(m.id) AS message_count FROM chats c JOIN messages m ON m.chat=c.id "
                                "WHERE instr(c.name,?)>0 OR instr(c.id,?)>0 GROUP BY c.id "
                                "ORDER BY c.last_time DESC,c.id LIMIT ? OFFSET ?", (query, query, limit + 1, offset)).fetchall()
            items = [dict(row) for row in rows[:limit]]
            for item in items:
                item["last_time"] = self._time(item["last_time"])
            cache_at = self._cache_at(conn)
        return {"chats": items, "has_more": len(rows) > limit, "next_offset": offset + len(items),
                "cache_only": not refresh, "cache_synced_at": cache_at, "coverage": self._coverage()}

    def _format(self, row, conn, *, content_limit=6000, compact=False) -> dict:
        result = dict(row)
        for field, key in (("chat", "chat_name"), ("sender", "sender_name")):
            found = conn.execute("SELECT name FROM chats WHERE id=?", (result.get(field, ""),)).fetchone()
            result[key] = found[0] if found else result.get(field) or "[发送者未解析]"
        result["time"] = self._time(result["created"])
        result["is_self"] = result["sender"] == self.self_id
        if result["type"] & 0xFFFFFFFF in (3, 34, 43, 47, 49, 50):
            result["content"], result["decode_status"] = decode_content(result["content"], result["type"])
        result["text_truncated"] = len(result["content"]) > content_limit
        result["content"] = result["content"][:content_limit]
        if result["text_truncated"]:
            result["read_full"] = {"tool": "wechat_get_message", "message_id": result["id"]}
        if result["type"] & 0xFFFFFFFF == 3:
            result["image"] = {"tool": "wechat_get_image", "message_id": result["id"],
                               "availability": "not_checked", "requires_local_cache": True}
        if result["type"] & 0xFFFFFFFF == 49:
            subtype = result["type"] >> 32
            result["card"] = {"subtype": subtype or None,
                              "kind": {5: "link", 6: "file", 19: "forwarded_history", 57: "reference"}.get(subtype, "unknown")}
            if subtype == 6:
                result["attachment"] = {"tool": "wechat_get_attachment", "message_id": result["id"],
                                        "availability": "not_checked"}
            elif subtype == 0:
                result["card"]["note"] = "卡片类型尚未从索引确认；提到文件名不代表该消息是文件附件。"
        result.pop("ingest_id", None)
        if compact:
            keep = {"id", "chat", "chat_name", "sender", "sender_name", "time", "is_self", "type",
                    "content", "text_truncated", "read_full", "image", "attachment", "card", "decode_status"}
            result = {key: value for key, value in result.items() if key in keep}
        return result

    @staticmethod
    def _read_options(compact, max_content_chars):
        if type(compact) is not bool or (max_content_chars is not None and
                (type(max_content_chars) is not int or not 1 <= max_content_chars <= 6000)):
            raise ValueError("compact 需要布尔值，max_content_chars 需要 1–6000 的整数。")
        return max_content_chars if max_content_chars is not None else (1000 if compact else 6000)

    @_locked
    def desktop_status(self) -> dict:
        from .actions import BridgeActions
        return BridgeActions(self).desktop_status()

    @_locked
    def get_attachment(self, message_id: str) -> dict:
        from .actions import BridgeActions
        return BridgeActions(self).get_attachment(message_id)

    @_locked
    def download_attachment(self, message_id: str, dry_run: bool = True,
                            timeout_seconds: int = 30, allow_desktop: bool = False) -> dict:
        from .actions import BridgeActions
        return BridgeActions(self).download_attachment(message_id, dry_run, timeout_seconds,
                                                       allow_desktop=allow_desktop)

    @_locked
    def send_text(self, chat: str, text: str, request_id: str | None = None,
                  dry_run: bool = True, timeout_seconds: int = 15) -> dict:
        from .actions import BridgeActions
        return BridgeActions(self).send_text(chat, text, request_id, dry_run, timeout_seconds)

    @_locked
    def send_file(self, chat: str, path: str, request_id: str | None = None,
                  dry_run: bool = True, timeout_seconds: int = 30) -> dict:
        from .actions import BridgeActions
        return BridgeActions(self).send_media(chat, path, "file", request_id, dry_run, timeout_seconds)

    @_locked
    def send_image(self, chat: str, path: str, request_id: str | None = None,
                   dry_run: bool = True, timeout_seconds: int = 30) -> dict:
        from .actions import BridgeActions
        return BridgeActions(self).send_media(chat, path, "image", request_id, dry_run, timeout_seconds)

    @_locked
    def send_video(self, chat: str, path: str, request_id: str | None = None,
                   dry_run: bool = True, timeout_seconds: int = 30) -> dict:
        from .actions import BridgeActions
        return BridgeActions(self).send_media(chat, path, "video", request_id, dry_run, timeout_seconds)

    @_locked
    def get_image(self, message_id: str, max_edge: int = 2048) -> ImageResult:
        """Resolve a known message, not a caller-supplied path or CDN address."""
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
            raise ValueError("图片消息编号无效。")
        if type(max_edge) is not int or not 512 <= max_edge <= 4096:
            raise ValueError("max_edge 必须为 512 到 4096 的整数。")
        with self._index() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                return ImageResult({"ok": False, "error": {"code": "message_not_found",
                                    "message": "当前账号索引中没有这条消息。"}})
            message = dict(row)
            formatted = self._format(row, conn)
        if message["type"] & 0xFFFFFFFF != 3:
            return ImageResult({"ok": False, "error": {"code": "not_image_message",
                                "message": "指定消息不是图片。"}})
        if (not re.fullmatch(r"message/message_[0-9]+\.db", message["shard"])
                or not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", message["source_table"])):
            raise ValueError("图片消息来源无效。")
        path = self.state / "snapshots" / message["shard"].replace("/", "__")
        if not path.resolve().is_relative_to(self.state):
            raise PermissionError("Snapshot escapes private cache")
        # Message IDs come from the indexed history. Refresh and authenticate
        # only their own shard; unrelated full-history sync is not needed to
        # read one image and can fail while another shard is being written.
        from .cipher import snapshot
        source_path = self.root / message["shard"]
        self._ensure_keys([source_path])
        capture = snapshot(source_path, path, self._keys[message["shard"]])
        source = self._source_conn(str(path))
        try:
            table = message["source_table"]
            columns = {r[1] for r in source.execute(f'PRAGMA table_info("{table}")')}
            fields = [name for name in ("packed_info_data", "message_content", "compress_content") if name in columns]
            select = ",".join(f"substr({field},1,2000001)" for field in fields)
            raw = source.execute(f'SELECT {select},local_type,create_time,server_id,real_sender_id '
                                 f'FROM "{table}" WHERE local_id=?',
                                 (message["local_id"],)).fetchone()
            if (raw is None or raw["local_type"] != message["type"]
                    or raw["create_time"] != message["created"]
                    or str(raw["server_id"] or "") != message["server_id"]):
                return ImageResult({"ok": False, "error": {"code": "message_source_changed",
                                    "message": "本地消息来源已变化；请重新同步并查询消息。"}})
            sender = source.execute("SELECT user_name FROM Name2Id WHERE rowid=?", (raw["real_sender_id"],)).fetchone()
            if (sender[0] if sender else "") != message["sender"]:
                return ImageResult({"ok": False, "error": {"code": "message_source_changed",
                                    "message": "本地发送者映射已变化；请重新同步并查询消息。"}})
            hashes = image_hashes(*tuple(raw)[:len(fields)])
        finally:
            source.close()
        try:
            result = self._images.get(message, hashes, max_edge)
            result.metadata["source"] = {key: formatted[key] for key in
                                         ("id", "chat", "chat_name", "sender", "sender_name", "time")}
            result.metadata["message_snapshot_at"] = capture["captured_at"]
            return result
        except ImageUnavailable as error:
            return ImageResult({"ok": False, "message_id": message_id,
                                "error": {"code": error.code, "message": str(error)},
                                "untrusted_data": True})

    def _read_page_cursor(self, conn, cursor, kind):
        from .errors import CursorError
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 8192:
                raise ValueError()
            page = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
            epoch = conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
            if (not isinstance(page, dict) or page.get("kind") != kind
                    or type(page.get("v")) is not int or page["v"] != 1
                    or page.get("account") != self.account_tag or page.get("epoch") != epoch
                    or type(page.get("start")) is not int or type(page.get("end")) is not int
                    or not -2**62 <= page["start"] < page["end"] <= 2**62
                    or type(page.get("created")) is not int or type(page.get("seq")) is not int
                    or not isinstance(page.get("id"), str) or not 1 <= len(page["id"]) <= 512):
                raise ValueError()
            chat = page.get("chat")
            if ((kind == "history" or chat is not None)
                    and (not isinstance(chat, str) or not 1 <= len(chat) <= 512)):
                raise ValueError()
            if kind == "search" and (not isinstance(page.get("query"), str)
                                      or not page["query"].strip() or len(page["query"]) > 300):
                raise ValueError()
            # Reject unusable forged bounds here, instead of later leaking an
            # unrelated timestamp/platform failure as a reader/config problem.
            datetime.fromtimestamp(page["start"], TZ)
            datetime.fromtimestamp(page["created"], TZ)
            if page["end"] != 2**62:
                datetime.fromtimestamp(page["end"], TZ)
            return page
        except Exception:
            raise CursorError(kind) from None

    @staticmethod
    def _encode_page_cursor(payload):
        return base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()

    def get_message(self, message_id: str, context_before: int = 3, context_after: int = 3,
                    refresh: bool = False, text_offset: int = 0, text_limit: int = 10000) -> dict:
        """Read one exact indexed message in full, with bounded same-chat context."""
        if (not isinstance(message_id, str) or not 1 <= len(message_id) <= 512
                or type(context_before) is not int or not 0 <= context_before <= 20
                or type(context_after) is not int or not 0 <= context_after <= 20
                or type(text_offset) is not int or not 0 <= text_offset <= 200_000
                or type(text_limit) is not int or not 1 <= text_limit <= 200_000
                or type(refresh) is not bool):
            raise ValueError("消息 ID 或上下文参数无效。")
        if refresh:
            self._refresh()
        with self._index() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": {"code": "message_not_found",
                        "message": "当前账号索引中没有这条消息；需要最新消息时先同步，再使用返回的准确消息 ID。"}}
            key = (row["created"], row["seq"], row["id"])
            older = conn.execute("SELECT * FROM messages WHERE chat=? AND (created,seq,id)<(?,?,?) "
                                 "ORDER BY created DESC,seq DESC,id DESC LIMIT ?",
                                 (row["chat"], *key, context_before+1)).fetchall()
            newer = conn.execute("SELECT * FROM messages WHERE chat=? AND (created,seq,id)>(?,?,?) "
                                 "ORDER BY created,seq,id LIMIT ?",
                                 (row["chat"], *key, context_after+1)).fetchall()
            message = self._format(row, conn, content_limit=200_000)
            full_content = message["content"]
            message["content"] = full_content[text_offset:text_offset+text_limit]
            more_text = text_offset + len(message["content"]) < len(full_content)
            message["text_truncated"] = text_offset > 0 or more_text
            message["source_text_truncated"] = row["decode_status"] == "truncated_at_200000_characters"
            result = {"ok": True, "message": message,
                      "text_page": {"offset": text_offset, "returned_characters": len(message["content"]),
                                    "total_characters": len(full_content), "has_more": more_text,
                                    "next_offset": text_offset + len(message["content"]) if more_text else None},
                      "context": {"before": [self._format(x, conn, content_limit=1000, compact=True) for x in reversed(older[:context_before])],
                                  "after": [self._format(x, conn, content_limit=1000, compact=True) for x in newer[:context_after]],
                                  "has_more_before": len(older) > context_before,
                                  "has_more_after": len(newer) > context_after},
                      "cache_only": not refresh, "cache_synced_at": self._cache_at(conn),
                      "content_scope": "decoded_index_text_up_to_200000_characters", "untrusted_data": True}
        return {**result, "coverage": self._coverage()}

    def get_messages(self, chat: str | None = None, limit: int = 100, before: str | None = None,
                     after: str | None = None, cursor: str | None = None, refresh: bool = True,
                     chat_id: str | None = None, compact: bool = False, max_content_chars: int | None = None) -> dict:
        content_limit = self._read_options(compact, max_content_chars)
        if type(refresh) is not bool:
            raise ValueError("refresh 需要布尔值。")
        if refresh:
            self._refresh()
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("消息数量需要为 1 到 500。")
        with self._index() as conn:
            conn.execute("BEGIN")
            epoch = conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
            page = self._read_page_cursor(conn, cursor, "history") if cursor is not None else None
            if chat is not None and chat_id is not None and chat != chat_id:
                raise ValueError("chat 和 chat_id 不能指定不同会话。")
            chat_id = self._resolve_chat(conn, chat if chat is not None else chat_id
                                         if chat_id is not None else page.get("chat") if page else None)
            start = _timestamp(after, page["start"] if page else 0)
            end = _timestamp(before, page["end"] if page else 2**62)
            if start >= end:
                raise ValueError("after 必须早于 before。")
            condition, values = "chat=? AND created>=? AND created<?", [chat_id,start,end]
            if page is not None:
                if page.get("chat") != chat_id or page["start"] != start or page["end"] != end:
                    from .errors import CursorError
                    raise CursorError("history") from None
                condition += " AND (created<? OR (created=? AND seq<?) OR (created=? AND seq=? AND id<?))"
                values.extend([page['created'],page['created'],page['seq'],page['created'],page['seq'],page['id']])
            rows = conn.execute("SELECT * FROM messages WHERE "+condition+" ORDER BY created DESC,seq DESC,id DESC LIMIT ?",
                                (*values, limit+1)).fetchall()
            items = [self._format(row, conn, content_limit=content_limit, compact=compact) for row in reversed(rows[:limit])]
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit-1]
                payload = {'v':1,'kind':'history','account':self.account_tag,'epoch':epoch,'chat':chat_id,
                           'start':start,'end':end,'created':last['created'],'seq':last['seq'],'id':last['id']}
                next_cursor = self._encode_page_cursor(payload)
            cache_at = self._cache_at(conn)
        return {"chat": chat_id, "messages": items, "has_more_older": len(rows) > limit,
                "has_more": len(rows) > limit, "next_cursor":next_cursor, "cache_only":not refresh,
                "requested_after": after, "requested_before": before,
                "effective_after": self._time(start), "effective_before": self._time(end) if end != 2**62 else None,
                "cache_synced_at": cache_at,
                "coverage": self._coverage(), "untrusted_data": True}

    def search_messages(self, query: str, chat: str | None = None, limit: int = 50,
                        after: str | None = None, before: str | None = None,
                        cursor: str | None = None, refresh: bool = True, chat_id: str | None = None,
                        compact: bool = False, max_content_chars: int | None = None) -> dict:
        content_limit = self._read_options(compact, max_content_chars)
        if type(refresh) is not bool:
            raise ValueError("refresh 需要布尔值。")
        if (not isinstance(query, str) or not query.strip() or len(query) > 300
                or type(limit) is not int or not 1 <= limit <= 200):
            raise ValueError("搜索词或数量无效。")
        if refresh:
            self._refresh()
        with self._index() as conn:
            conn.execute("BEGIN")
            page = self._read_page_cursor(conn, cursor, "search") if cursor is not None else None
            start = _timestamp(after, page["start"] if page else 0)
            end = _timestamp(before, page["end"] if page else 2**62)
            if start >= end:
                raise ValueError("after 必须早于 before。")
            if chat is not None and chat_id is not None and chat != chat_id:
                raise ValueError("chat 和 chat_id 不能指定不同会话。")
            chat = chat if chat is not None else chat_id
            chat_id = self._resolve_chat(conn, chat) if chat is not None else page.get("chat") if page else None
            if page is not None and (page.get("query") != query or page.get("chat") != chat_id
                                     or page["start"] != start or page["end"] != end):
                from .errors import CursorError
                raise CursorError("search")
            params = [start, end]
            condition = "m.created>=? AND m.created<? AND (m.type & 4294967295) NOT IN (3,34,43,47,50)"
            if chat_id:
                condition += " AND m.chat=?"
                params.append(chat_id)
            if page is not None:
                condition += " AND (m.created,m.seq,m.id)<(?,?,?)"
                params.extend((page["created"], page["seq"], page["id"]))
            if len(query) >= 3:
                sql = "SELECT m.* FROM message_fts JOIN messages m ON message_fts.rowid=m.ingest_id WHERE message_fts MATCH ? AND "
                params.insert(0, '"' + query.replace('"', '""') + '"')
            else:
                sql = "SELECT m.* FROM messages m WHERE instr(m.content,?)>0 AND "
                params.insert(0, query)
            rows = conn.execute(sql + condition + " ORDER BY m.created DESC,m.seq DESC,m.id DESC LIMIT ?", params + [limit + 1]).fetchall()
            items = [self._format(row, conn, content_limit=content_limit, compact=compact) for row in rows[:limit]]
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit-1]
                epoch = conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
                next_cursor = self._encode_page_cursor({"v": 1, "kind": "search", "account": self.account_tag,
                    "epoch": epoch, "query": query, "chat": chat_id, "start": start, "end": end,
                    "created": last["created"], "seq": last["seq"], "id": last["id"]})
            cache_at = self._cache_at(conn)
        return {"query": query, "messages": items, "has_more": len(rows) > limit,
                "next_cursor": next_cursor, "chat": chat_id, "cache_only": not refresh,
                "cache_synced_at": cache_at,
                "effective_after": self._time(start), "effective_before": self._time(end) if end != 2**62 else None,
                "coverage": self._coverage(), "untrusted_data": True}

    def _cursor(self, conn, seq: int) -> str:
        epoch = conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
        data = {"v": 1, "account": self.account_tag, "epoch": epoch, "seq": seq}
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()

    def get_changes(self, cursor: str | None = None, limit: int = 100, wait_seconds: int = 0, refresh: bool = True) -> dict:
        if type(refresh) is not bool:
            raise ValueError("refresh 需要布尔值。")
        if not 1 <= limit <= 200 or not 0 <= wait_seconds <= 30:
            raise ValueError("增量参数无效。")
        deadline = time.monotonic() + wait_seconds
        seq = None
        validated_epoch = None
        while True:
            if refresh:
                self._refresh()
            response = None
            with self._index() as conn:
                # Keep event metadata and its joined message body in one short
                # SQLite read snapshot. Release this transaction before sleep.
                conn.execute("BEGIN")
                if seq is None:
                    if cursor is None:
                        latest = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                        response = {"changes": [], "cursor": self._cursor(conn, latest), "initialized_at_current_state": True,
                                    "note": "保存游标，下次传入以读取之后新观察到的消息。"}
                    else:
                        try:
                            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
                                raise ValueError
                            obj = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
                            epoch = conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
                            latest = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                            if (type(obj["v"]) is not int or obj["v"] != 1
                                    or obj["account"] != self.account_tag or obj["epoch"] != epoch
                                    or type(obj["seq"]) is not int):
                                raise ValueError
                            seq = int(obj["seq"])
                            if not 0 <= seq <= latest:
                                raise ValueError
                            validated_epoch = epoch
                        except Exception:
                            from .errors import CursorError
                            raise CursorError("changes") from None
                else:
                    epoch = conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
                    latest = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                    if epoch != validated_epoch or seq > latest:
                        from .errors import CursorError
                        raise CursorError("changes")
                if response is None:
                    events = conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (seq, limit + 1)).fetchall()
                    if events or time.monotonic() >= deadline:
                        changes = []
                        for event in events[:limit]:
                            row = conn.execute("SELECT * FROM messages WHERE id=?", (event["message_id"],)).fetchone()
                            changes.append({"event": event["kind"], "observed_at": event["observed_at"],
                                            "message_id": event["message_id"], "message": self._format(row, conn) if row else None})
                        last = events[min(len(events), limit) - 1]["seq"] if events else seq
                        response = {"changes": changes, "cursor": self._cursor(conn, last), "has_more": len(events) > limit,
                                    "untrusted_data": True}
            # _coverage opens another connection. The short read transaction
            # must be released first, so a pending writer can finish COMMIT.
            if response is not None:
                return {**response, "coverage": self._coverage()}
            time.sleep(min(1, max(0, deadline - time.monotonic())))
