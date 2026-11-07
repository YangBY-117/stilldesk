"""Reader integration checks using synthetic SQLite sources, never live Weixin data."""

from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from wechat_bridge import cipher
from wechat_bridge import reader as reader_module
from wechat_bridge.reader import Reader
from wechat_bridge.media import ImageResult


def test_group_file_card_does_not_leak_internal_transport_fields():
    value = ('wxid_example:\n<?xml version="1.0"?><msg><appmsg>'
             '<type>6</type><title>Homework.docx</title><appattach>'
             '<aeskey>PRIVATE_TRANSPORT</aeskey><cdnattachurl>PRIVATE_TOKEN</cdnattachurl>'
             '</appattach></appmsg></msg>')
    text, status = reader_module.decode_content(value, (6 << 32) | 49)
    assert text == "[链接/文件/引用] Homework.docx"
    assert status == "card_fields"
    assert "PRIVATE" not in text


def test_image_message_returns_pixels_and_provenance_without_transport_credentials(make_scenario):
    from io import BytesIO
    from PIL import Image
    scenario = make_scenario()
    identity = "c7" * 16
    private_token = "PRIVATE_CDN_TOKEN_MUST_NOT_RETURN"
    scenario.add_message(0, 2, f'<msg><img md5="{identity}" cdnaeskey="{private_token}"/></msg>', seq=20)
    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute(f'UPDATE "{TABLE}" SET local_type=3 WHERE local_id=2')
    directory = (scenario.reader.account / "msg/attach" / hashlib.md5(CHAT.encode()).hexdigest()
                 / "2023-11/Img")
    directory.mkdir(parents=True)
    stream = BytesIO()
    Image.new("RGB", (200, 100), "red").save(stream, format="JPEG")
    path = directory / (identity + ".dat")
    path.write_bytes(stream.getvalue())
    messages = scenario.reader.get_messages(CHAT)["messages"]
    image_message = next(message for message in messages if message["local_id"] == 2)
    assert image_message["image"]["message_id"] == image_message["id"]
    assert image_message["content"] == "[图片]"
    scenario.failed_snapshots.add(scenario.shards[1])
    result = scenario.reader.get_image(image_message["id"])
    assert isinstance(result, ImageResult)
    assert result.metadata["ok"] is True
    assert result.metadata["source"]["sender_name"] == "小艾"
    assert result.metadata["source"]["id"] == image_message["id"]
    assert result.data
    assert private_token not in json.dumps(result.metadata)
    assert path.read_bytes() == stream.getvalue()
    assert scenario.reader.get_image("../../other-account/secret").metadata["error"]["code"] == "message_not_found"
    text_message = next(message for message in messages if message["local_id"] == 1)
    assert scenario.reader.get_image(text_message["id"]).metadata["error"]["code"] == "not_image_message"
    path.unlink()
    assert scenario.reader.get_image(image_message["id"]).metadata["error"]["code"] == "image_not_cached"
    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute(f'UPDATE "{TABLE}" SET local_type=1 WHERE local_id=2')
    assert scenario.reader.get_image(image_message["id"]).metadata["error"]["code"] == "message_source_changed"


CHAT = "synthetic-study@chatroom"
TABLE = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()
FIRST_TIME = 1_700_000_000


def test_history_cursor_keeps_all_messages_in_same_second_and_binds_filters(make_scenario):
    scenario = make_scenario()
    for local_id in range(2,12):
        scenario.add_message(0,local_id,"同一秒消息",seq=20)
    with sqlite3.connect(scenario.shards[0]) as connection:
        connection.execute(f'UPDATE "{TABLE}" SET create_time=?',(FIRST_TIME,))
    seen=[]
    cursor=None
    while True:
        page=scenario.reader.get_messages(CHAT,limit=2,cursor=cursor)
        seen.extend(m['id'] for m in page['messages'])
        cursor=page['next_cursor']
        if not cursor:break
    expected=len(scenario.reader.get_messages(CHAT,limit=500)['messages'])
    assert len(seen)==len(set(seen))==expected
    first=scenario.reader.get_messages(CHAT,limit=2)
    with pytest.raises(ValueError):
        scenario.reader.get_messages(CHAT,limit=2,after='2020-01-01T00:00:00+08:00',cursor=first['next_cursor'])


def test_explicit_cache_pages_do_not_recapture_source(make_scenario):
    scenario=make_scenario()
    scenario.reader.sync()
    scenario.failed_snapshots.update(scenario.shards.values())
    assert scenario.reader.get_messages(CHAT,refresh=False)['messages']
    assert scenario.reader.list_chats(refresh=False)['chats']
    assert scenario.reader.search_messages("项目",refresh=False)['messages']
    assert scenario.reader.get_changes(refresh=False)['initialized_at_current_state']
    with pytest.raises(Exception):
        scenario.reader.get_messages(CHAT)


def test_download_wrapper_preserves_default_non_desktop_and_explicit_opt_in(scenario, monkeypatch):
    from types import SimpleNamespace
    from wechat_bridge import actions
    calls = []
    def download(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True}
    monkeypatch.setattr(actions, "BridgeActions", lambda reader: SimpleNamespace(download_attachment=download))
    scenario.reader.download_attachment("message-id", dry_run=False)
    assert calls[-1] == (("message-id", False, 30), {"allow_desktop": False})
    scenario.reader.download_attachment("message-id", allow_desktop=True)
    assert calls[-1] == (("message-id", True, 30), {"allow_desktop": True})


def test_history_cursor_inherits_time_and_chat_but_rejects_explicit_conflict(scenario):
    for local_id in range(2, 7):
        scenario.add_message(0, local_id, "分页消息", seq=local_id)
    start = scenario.reader._time(FIRST_TIME)
    end = scenario.reader._time(FIRST_TIME+200)
    first = scenario.reader.get_messages(CHAT, limit=2, after=start, before=end)
    second = scenario.reader.get_messages(limit=2, cursor=first["next_cursor"], refresh=False)
    assert second["chat"] == CHAT
    assert second["effective_after"] == start
    assert second["effective_before"] == end
    assert not ({x["id"] for x in first["messages"]} & {x["id"] for x in second["messages"]})
    from wechat_bridge.errors import CursorError
    with pytest.raises(CursorError):
        scenario.reader.get_messages(CHAT, after=scenario.reader._time(FIRST_TIME+1),
                                     cursor=first["next_cursor"], refresh=False)
    alias = scenario.reader.get_messages(chat_id=CHAT, refresh=False)
    assert alias["chat"] == CHAT
    with pytest.raises(ValueError):
        scenario.reader.get_messages(CHAT, chat_id="other", refresh=False)


def test_search_keyset_keeps_equal_time_sequence_and_binds_query_filters_and_account(scenario, make_scenario):
    for local_id in range(2, 10):
        scenario.add_message(0, local_id, "唯一关键字", seq=20)
    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute(f'UPDATE "{TABLE}" SET create_time=?,sort_seq=20 WHERE local_id>=2', (FIRST_TIME+100,))
    reader = scenario.reader
    first = reader.search_messages("唯一关键字", CHAT, limit=2,
                                   after=reader._time(FIRST_TIME), before=reader._time(FIRST_TIME+200))
    seen, cursor = [], first["next_cursor"]
    seen.extend(x["id"] for x in first["messages"])
    while cursor:
        page = reader.search_messages("唯一关键字", limit=2, cursor=cursor, refresh=False)
        seen.extend(x["id"] for x in page["messages"])
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 8
    for kwargs in ({"query": "别的关键字"}, {"after": reader._time(FIRST_TIME+1)},
                   {"chat": "wxid_alice"}, {"before": reader._time(FIRST_TIME+199)}):
        arguments = {"query": "唯一关键字", "cursor": first["next_cursor"], "refresh": False, **kwargs}
        with pytest.raises(ValueError):
            reader.search_messages(**arguments)
    foreign = make_scenario("wxid_foreign_b2c3").reader
    foreign.sync()
    with pytest.raises(ValueError):
        foreign.search_messages("唯一关键字", cursor=first["next_cursor"], refresh=False)
    with reader._index() as conn:
        conn.execute("UPDATE meta SET value='rebuilt-index' WHERE key='epoch'")
    with pytest.raises(ValueError):
        reader.search_messages("唯一关键字", cursor=first["next_cursor"], refresh=False)


def test_single_message_exposes_long_text_and_only_same_chat_context(scenario):
    long_text = "正文" * 7000
    for local_id in range(2, 7):
        scenario.add_message(0, local_id, long_text if local_id == 4 else f"消息{local_id}", seq=local_id)
    reader = scenario.reader
    history = reader.get_messages(CHAT)
    target = next(x for x in history["messages"] if x["local_id"] == 4)
    assert len(target["content"]) == 6000
    assert target["read_full"]["tool"] == "wechat_get_message"
    single = reader.get_message(target["id"], context_before=1, context_after=1, text_limit=20000)
    assert single["message"]["content"] == long_text
    assert single["message"]["text_truncated"] is False
    assert single["message"]["source_text_truncated"] is False
    assert [x["content"] for x in single["context"]["before"]] == ["消息3"]
    assert [x["content"] for x in single["context"]["after"]] == ["消息5"]
    assert single["context"]["has_more_before"] is True
    assert single["context"]["has_more_after"] is True
    assert single["cache_only"] is True
    assert single["cache_synced_at"]
    assert reader.get_message("not-an-indexed-id")["error"]["code"] == "message_not_found"


def test_compact_preview_and_text_pages_preserve_exact_message_and_unicode(scenario):
    text = ("段落🙂\n" * 4500).rstrip("\n")
    scenario.add_message(0, 2, text, seq=20)
    reader = scenario.reader
    full = reader.get_messages(CHAT)
    compact = reader.get_messages(CHAT, refresh=False, compact=True, max_content_chars=80)
    target_id = next(x["id"] for x in full["messages"] if x["local_id"] == 2)
    target = next(x for x in compact["messages"] if x["id"] == target_id)
    assert len(target["content"]) == 80
    assert not {"shard", "source_table", "local_id", "server_id", "created", "seq"} & target.keys()
    assert {"id", "chat", "sender", "time", "content", "read_full"} <= target.keys()
    assert len(json.dumps(compact, ensure_ascii=False)) < len(json.dumps(full, ensure_ascii=False))
    offset, pieces = 0, []
    while True:
        page = reader.get_message(target["id"], context_before=0, context_after=0,
                                  text_offset=offset, text_limit=7000)
        pieces.append(page["message"]["content"])
        if not page["text_page"]["has_more"]:
            break
        offset = page["text_page"]["next_offset"]
    assert "".join(pieces) == text
    search = reader.search_messages("段落", refresh=False, compact=True, max_content_chars=60)
    assert search["messages"][0]["id"] == target["id"]
    assert len(search["messages"][0]["content"]) == 60


def test_chat_lookup_failures_offer_bounded_candidates(scenario):
    from wechat_bridge.errors import ChatResolutionError
    scenario.reader.sync()
    with scenario.reader._index() as conn:
        conn.execute("INSERT INTO chats(id,name) VALUES('duplicate-one','同名群')")
        conn.execute("INSERT INTO chats(id,name) VALUES('duplicate-two','同名群')")
    with pytest.raises(ChatResolutionError) as ambiguous:
        scenario.reader.get_messages("同名群", refresh=False)
    assert ambiguous.value.code == "ambiguous_chat"
    assert len(ambiguous.value.candidates) == 2
    with pytest.raises(ChatResolutionError) as missing:
        scenario.reader.get_messages("完全不存在的会话", refresh=False)
    assert missing.value.code == "chat_not_found"
    assert missing.value.candidates == []


@pytest.mark.parametrize(("subtype", "kind"), ((5, "link"), (6, "file"), (57, "reference")))
def test_card_subtype_does_not_advertise_links_or_quotes_as_attachments(scenario, subtype, kind):
    raw = f'<msg><appmsg><type>{subtype}</type><title>说明.docx</title></appmsg></msg>'
    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute(f'UPDATE "{TABLE}" SET local_type=?,message_content=? WHERE local_id=1', ((subtype<<32)|49, raw))
    message = next(x for x in scenario.reader.get_messages(CHAT)["messages"] if x["shard"].endswith('0.db'))
    assert message["card"] == {"subtype": subtype, "kind": kind}
    assert ("attachment" in message) is (subtype == 6)


def test_cache_reads_and_status_do_not_wait_for_long_poll_sleep(scenario, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from wechat_bridge import session
    monkeypatch.setattr(session, "inspect_session", lambda *args: {"state": "test"})
    reader = scenario.reader
    baseline = reader.get_changes()["cursor"]
    sleeping, release = threading.Event(), threading.Event()
    def pause(seconds):
        sleeping.set()
        release.wait(timeout=3)
    monkeypatch.setattr(reader_module.time, "sleep", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        poll = pool.submit(reader.get_changes, baseline, wait_seconds=1, refresh=False)
        assert sleeping.wait(timeout=1)
        try:
            def read():
                assert reader.get_messages(CHAT, refresh=False)["messages"]
                assert reader.search_messages("项目", refresh=False)["messages"]
                assert reader.list_chats(refresh=False)["chats"]
                return reader.status()["messages"]
            assert pool.submit(read).result(timeout=1) == 2
        finally:
            release.set()
        assert poll.result(timeout=3)["changes"] == []


def test_waiting_changes_rejects_index_rebuild_between_poll_snapshots(scenario, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from wechat_bridge.errors import CursorError
    reader = scenario.reader
    baseline = reader.get_changes()["cursor"]
    sleeping, release = threading.Event(), threading.Event()
    def pause(seconds):
        sleeping.set()
        release.wait(timeout=3)
    monkeypatch.setattr(reader_module.time, "sleep", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        poll = pool.submit(reader.get_changes, baseline, wait_seconds=1, refresh=False)
        assert sleeping.wait(timeout=1)
        try:
            with reader._index() as conn:
                conn.execute("UPDATE meta SET value='different-index-generation' WHERE key='epoch'")
        finally:
            release.set()
        with pytest.raises(CursorError):
            poll.result(timeout=3)


@pytest.mark.parametrize("initialize_baseline", (False, True))
def test_changes_releases_read_snapshot_before_coverage_when_writer_commit_is_pending(
        scenario, monkeypatch, initialize_baseline):
    import threading
    import time
    reader = scenario.reader
    baseline = reader.get_changes()["cursor"]
    if not initialize_baseline:
        scenario.add_message(0, 2, "并发到达的消息", seq=30)
        reader.sync()
    with sqlite3.connect(reader.index_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    committing, finished = threading.Event(), threading.Event()
    failures = []
    def write():
        conn = sqlite3.connect(reader.index_path, timeout=2)
        try:
            conn.execute("INSERT OR REPLACE INTO meta VALUES('pending_writer_test','committed')")
            committing.set()
            conn.commit()
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            conn.close()
            finished.set()
    writer = threading.Thread(target=write)
    hook_name = "_cursor" if initialize_baseline else "_format"
    original = getattr(reader, hook_name)
    launched, pending_observed = False, False
    def start_pending_commit(*args, **kwargs):
        nonlocal launched, pending_observed
        value = original(*args, **kwargs)
        if launched:
            return value
        launched = True
        writer.start()
        assert committing.wait(timeout=1)
        # An independent reader is denied only once COMMIT has moved from
        # RESERVED to PENDING, while get_changes still holds its shared lock.
        deadline = time.monotonic()+1
        while time.monotonic() < deadline:
            probe = sqlite3.connect(reader.index_path, timeout=0)
            try:
                probe.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
            except sqlite3.OperationalError:
                pending_observed = True
                break
            finally:
                probe.close()
            threading.Event().wait(0.002)
        assert pending_observed
        return value
    monkeypatch.setattr(reader, hook_name, start_pending_commit)
    try:
        result = reader.get_changes(None if initialize_baseline else baseline, refresh=False)
    finally:
        if launched:
            writer.join(timeout=3)
    assert finished.is_set()
    assert failures == []
    assert pending_observed
    assert result["coverage"]["indexed_source_availability"]["status"] == "available"
    assert len(result["changes"]) == (0 if initialize_baseline else 1)


def create_database(path: Path, schema: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
    conn.close()


@dataclass
class Scenario:
    reader: Reader
    shards: dict[int, Path]
    failed_snapshots: set[Path]

    def add_message(self, shard: int, local_id: int, text: str, *, seq: int, chat: str = CHAT) -> None:
        table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
        with sqlite3.connect(self.shards[shard]) as conn:
            conn.execute(
                f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?,?)',
                (local_id, 900_000_000_000_000_000 + shard * 100 + local_id,
                 seq, FIRST_TIME + 100 + local_id, 7, 1, text, None),
            )
        conn.close()

    def index_state(self) -> dict:
        """Capture durable state so failed transactions cannot masquerade as progress."""
        with closing(sqlite3.connect(self.reader.index_path)) as conn:
            return {
                "events": conn.execute("SELECT * FROM events ORDER BY seq").fetchall(),
                "messages": conn.execute("SELECT id,content FROM messages ORDER BY id").fetchall(),
                "sources": conn.execute("SELECT * FROM sources ORDER BY rel").fetchall(),
                "source_tables": conn.execute("SELECT * FROM source_tables ORDER BY shard,name").fetchall(),
                "last_sync": conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone(),
            }


@pytest.fixture
def make_scenario(tmp_path, monkeypatch):
    failed_snapshots: set[Path] = set()

    def synthetic_keys(self, paths):
        self._keys = {path.relative_to(self.root).as_posix(): b"synthetic-key".ljust(32, b"\0")
                      for path in paths}

    def synthetic_snapshot(source, destination, key):
        source, destination = Path(source), Path(destination)
        if source in failed_snapshots:
            raise cipher.SnapshotError("synthetic snapshot failure")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return {
            "path": str(destination),
            "captured_at": "2026-10-19T00:00:00+00:00",
            # Match the real capture's stat/header signature so production fast
            # paths still notice same-size updates to a synthetic database.
            "source_signature": cipher._source_signature(source),
        }

    monkeypatch.setattr(Reader, "_ensure_keys", synthetic_keys)
    monkeypatch.setattr(cipher, "snapshot", synthetic_snapshot)

    def build(account_name="wxid_owner_a1b2"):
        account = tmp_path / account_name
        root = account / "db_storage"
        state = tmp_path / ("state-" + account_name)
        state.mkdir()
        create_database(root / "contact/contact.db", """
            CREATE TABLE contact(username TEXT PRIMARY KEY,nick_name TEXT,remark TEXT);
        """)
        with sqlite3.connect(root / "contact/contact.db") as conn:
            conn.executemany("INSERT INTO contact VALUES(?,?,?)", [
                (CHAT, "合成读书群", ""),
                ("wxid_alice", "艾丽丝", "小艾"),
                ("wxid_bob", "鲍勃", "小鲍"),
            ])
        conn.close()
        create_database(root / "session/session.db", """
            CREATE TABLE SessionTable(username TEXT PRIMARY KEY,unread_count INTEGER,last_timestamp INTEGER);
        """)
        with sqlite3.connect(root / "session/session.db") as conn:
            conn.execute("INSERT INTO SessionTable VALUES(?,?,?)", (CHAT, 2, FIRST_TIME + 1))
        conn.close()

        shards = {}
        for shard, sender in enumerate(("wxid_alice", "wxid_bob")):
            path = root / "message" / f"message_{shard}.db"
            create_database(path, f"""
                CREATE TABLE Name2Id(user_name TEXT);
                CREATE TABLE "{TABLE}"(
                    local_id INTEGER PRIMARY KEY,server_id INTEGER,sort_seq INTEGER,
                    create_time INTEGER,real_sender_id INTEGER,local_type INTEGER,
                    message_content,compress_content BLOB);
            """)
            text = ("项目安排：周五提交方案 📝 café" if shard == 0 else "晚饭见，こんにちは！")
            with sqlite3.connect(path) as conn:
                conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(7,?)", (sender,))
                conn.execute(f'INSERT INTO "{TABLE}" VALUES(?,?,?,?,?,?,?,?)',
                             (1, 900_000_000_000_000_001 + shard, 10 + shard,
                              FIRST_TIME + shard, 7, 1, text, None))
            conn.close()
            shards[shard] = path

        config_path = tmp_path / (account_name + ".json")
        config_path.write_text(json.dumps({
            "account_dir": str(account), "state_dir": str(state),
            "executable": str(tmp_path / "synthetic-Weixin.exe"),
        }), encoding="utf-8")
        return Scenario(Reader(config_path), shards, failed_snapshots)

    return build


@pytest.fixture
def scenario(make_scenario):
    return make_scenario()


@pytest.fixture
def decoded_values(monkeypatch):
    values = []
    original = reader_module.decode_content

    def observe(value, message_type):
        values.append(value)
        return original(value, message_type)

    monkeypatch.setattr(reader_module, "decode_content", observe)
    return values


def test_sender_numbers_are_resolved_within_their_own_shard(scenario):
    messages = scenario.reader.get_messages(CHAT)["messages"]

    assert len(messages) == 2
    assert {message["local_id"] for message in messages} == {1}
    assert len({message["id"] for message in messages}) == 2
    assert {(message["shard"], message["sender"], message["sender_name"])
            for message in messages} == {
                ("message/message_0.db", "wxid_alice", "小艾"),
                ("message/message_1.db", "wxid_bob", "小鲍"),
            }
    assert all(isinstance(message["server_id"], str) for message in messages)


@pytest.mark.parametrize("query,sender", [
    ("安排", "wxid_alice"),
    ("项目安排", "wxid_alice"),
    ("café", "wxid_alice"),
    ("こんにちは", "wxid_bob"),
    ("📝", "wxid_alice"),
])
def test_unicode_search_handles_short_terms_and_trigram_terms(scenario, query, sender):
    result = scenario.reader.search_messages(query, chat="合成读书群")

    assert len(result["messages"]) == 1
    assert result["messages"][0]["sender"] == sender
    assert query in result["messages"][0]["content"]
    assert result["has_more"] is False


def test_new_message_pagination_does_not_drop_equal_source_sequences(scenario):
    baseline = scenario.reader.get_changes()
    assert baseline["changes"] == []
    assert baseline["initialized_at_current_state"] is True
    assert scenario.index_state()["events"] == []
    assert scenario.reader.status()["messages"] == 2

    scenario.add_message(0, 2, "第一条同序号消息", seq=500)
    scenario.add_message(1, 2, "第二条同序号消息", seq=500)

    first = scenario.reader.get_changes(baseline["cursor"], limit=1)
    second = scenario.reader.get_changes(first["cursor"], limit=1)
    empty = scenario.reader.get_changes(second["cursor"], limit=1)

    assert len(first["changes"]) == len(second["changes"]) == 1
    assert first["has_more"] is True
    assert second["has_more"] is False
    changes = first["changes"] + second["changes"]
    assert {change["message"]["content"] for change in changes} == {
        "第一条同序号消息", "第二条同序号消息",
    }
    assert {change["message"]["seq"] for change in changes} == {500}
    assert len({change["message_id"] for change in changes}) == 2
    assert {change["event"] for change in changes} == {"newly_observed"}
    assert empty["changes"] == []
    assert empty["cursor"] == second["cursor"]


@pytest.mark.parametrize("mutation", ["invalid-base64", "unknown-epoch", "future-sequence", "unknown-version"])
def test_unknown_cursors_are_rejected(scenario, mutation):
    cursor = scenario.reader.get_changes()["cursor"]
    if mutation == "invalid-base64":
        cursor = "this-is-not-a-cursor"
    else:
        payload = json.loads(base64.urlsafe_b64decode(cursor))
        if mutation == "unknown-epoch":
            payload["epoch"] = "unknown-index-epoch"
        elif mutation == "future-sequence":
            payload["seq"] += 1_000_000
        else:
            payload["v"] += 1
        cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

    with pytest.raises(ValueError, match="游标无效"):
        scenario.reader.get_changes(cursor)


def test_cursor_from_another_account_is_rejected(scenario, make_scenario):
    foreign = make_scenario("wxid_other_b2c3")
    foreign_cursor = foreign.reader.get_changes()["cursor"]

    with pytest.raises(ValueError, match="另一账号"):
        scenario.reader.get_changes(foreign_cursor)


def test_snapshot_failure_does_not_advance_durable_events(scenario):
    baseline = scenario.reader.get_changes()["cursor"]
    before = scenario.index_state()
    scenario.add_message(0, 2, "失败后仍需补拉的消息", seq=500)
    scenario.failed_snapshots.add(scenario.shards[1])

    with pytest.raises(cipher.SnapshotError, match="synthetic snapshot failure"):
        scenario.reader.get_changes(baseline)
    assert scenario.index_state() == before

    scenario.failed_snapshots.clear()
    recovered = scenario.reader.get_changes(baseline)
    assert [change["message"]["content"] for change in recovered["changes"]] == ["失败后仍需补拉的消息"]


def test_later_shard_query_failure_rolls_back_earlier_shard_and_events(scenario):
    baseline = scenario.reader.get_changes()["cursor"]
    before = scenario.index_state()
    scenario.add_message(0, 2, "整轮成功后才应被观察到", seq=500)
    with sqlite3.connect(scenario.shards[1]) as conn:
        conn.execute(f'ALTER TABLE "{TABLE}" RENAME TO saved_messages')
        conn.execute(f'CREATE TABLE "{TABLE}"(local_id INTEGER)')

    with pytest.raises(sqlite3.OperationalError):
        scenario.reader.get_changes(baseline)
    assert scenario.index_state() == before

    with sqlite3.connect(scenario.shards[1]) as conn:
        conn.execute(f'DROP TABLE "{TABLE}"')
        conn.execute(f'ALTER TABLE saved_messages RENAME TO "{TABLE}"')
    recovered = scenario.reader.get_changes(baseline)
    assert len(recovered["changes"]) == 1
    assert recovered["changes"][0]["message"]["content"] == "整轮成功后才应被观察到"
    assert scenario.reader.get_changes(recovered["cursor"])["changes"] == []


def test_reader_queries_preserve_source_database_bytes(scenario):
    paths = list(scenario.reader.root.rglob("*.db"))
    before = {path: hashlib.sha256(path.read_bytes()).digest() for path in paths}

    scenario.reader.list_chats()
    scenario.reader.get_messages(CHAT)
    scenario.reader.search_messages("项目安排")
    scenario.reader.get_changes()

    assert {path: hashlib.sha256(path.read_bytes()).digest() for path in paths} == before


def test_old_message_edit_in_overflow_pages_refreshes_search_and_emits_update(scenario):
    # Equal-size replacement deep inside a long record exercises overflow pages,
    # not just the message B-tree's root/leaf page or its newest local_id.
    old_text = "甲" * 25_000 + "旧版检索词" + "乙" * 25_000
    new_text = old_text.replace("旧版检索词", "新版检索词")
    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute(f'UPDATE "{TABLE}" SET message_content=? WHERE local_id=1', (old_text,))
    scenario.add_message(0, 99, "最新消息保持不变", seq=999)
    cursor = scenario.reader.get_changes()["cursor"]
    original = scenario.reader.search_messages("旧版检索词")["messages"][0]

    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute(f'UPDATE "{TABLE}" SET message_content=? WHERE local_id=1', (new_text,))
    changes = scenario.reader.get_changes(cursor)["changes"]

    assert len(changes) == 1
    assert changes[0]["event"] == "updated"
    updated = changes[0]["message"]
    assert updated["id"] == original["id"]
    assert updated["local_id"] == 1
    assert updated["seq"] == 10
    assert updated["created"] == FIRST_TIME
    assert scenario.reader.search_messages("旧版检索词")["messages"] == []
    found = scenario.reader.search_messages("新版检索词")["messages"]
    assert len(found) == 1
    assert found[0]["id"] == original["id"]
    assert scenario.reader.status()["messages"] == 3


def test_deleting_old_message_removes_only_its_shard_row_and_search_hit(scenario):
    scenario.add_message(0, 99, "删除测试的新消息", seq=999)
    cursor = scenario.reader.get_changes()["cursor"]
    original = scenario.reader.search_messages("项目安排")["messages"][0]

    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute(f'DELETE FROM "{TABLE}" WHERE local_id=1')
    changes = scenario.reader.get_changes(cursor)["changes"]

    assert len(changes) == 1
    assert changes[0]["event"] == "removed_from_local_source"
    assert changes[0]["message_id"] == original["id"]
    assert changes[0]["message"] is None
    assert scenario.reader.search_messages("项目安排")["messages"] == []
    remaining = scenario.reader.get_messages(CHAT)["messages"]
    assert {(message["local_id"], message["sender"]) for message in remaining} == {
        (1, "wxid_bob"), (99, "wxid_alice"),
    }


@pytest.mark.parametrize("table_already_exists", [True, False])
def test_other_chat_updates_preserve_history_without_redecoding_unchanged_tables(
        scenario, decoded_values, table_already_exists):
    other_chat = "synthetic-side@chatroom"
    other_table = "Msg_" + hashlib.md5(other_chat.encode()).hexdigest()

    def create_other_table():
        with sqlite3.connect(scenario.shards[0]) as conn:
            conn.execute(f'''CREATE TABLE "{other_table}"(
                local_id INTEGER PRIMARY KEY,server_id INTEGER,sort_seq INTEGER,
                create_time INTEGER,real_sender_id INTEGER,local_type INTEGER,
                message_content,compress_content BLOB)''')

    # Keep the sender namespace stable when the extra table arrives. A changed
    # Name2Id map should deliberately invalidate affected sender attributions.
    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(8,?)", (other_chat,))
    if table_already_exists:
        create_other_table()
        scenario.add_message(0, 1, "旁支会话的既有消息", seq=20, chat=other_chat)
    cursor = scenario.reader.get_changes()["cursor"]
    original_history = scenario.reader.get_messages(CHAT)["messages"]
    decoded_values.clear()

    if not table_already_exists:
        create_other_table()
    scenario.add_message(0, 2, "旁支会话的新消息", seq=500, chat=other_chat)
    result = scenario.reader.get_changes(cursor)

    expected_decoded = (["旁支会话的既有消息", "旁支会话的新消息"]
                        if table_already_exists else ["旁支会话的新消息"])
    assert decoded_values == expected_decoded
    assert len(result["changes"]) == 1
    assert result["changes"][0]["event"] == "newly_observed"
    assert result["changes"][0]["message"]["chat"] == other_chat
    assert scenario.reader.get_messages(CHAT)["messages"] == original_history
    assert decoded_values == expected_decoded
    assert scenario.reader.get_changes(result["cursor"])["changes"] == []


def test_sender_namespace_change_refreshes_existing_message_attribution(scenario):
    cursor = scenario.reader.get_changes()["cursor"]
    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute("UPDATE Name2Id SET user_name=? WHERE rowid=7", ("wxid_bob",))

    changes = scenario.reader.get_changes(cursor)["changes"]

    assert len(changes) == 1
    assert changes[0]["event"] == "updated"
    assert changes[0]["message"]["shard"] == "message/message_0.db"
    assert changes[0]["message"]["sender"] == "wxid_bob"
    assert changes[0]["message"]["sender_name"] == "小鲍"


@pytest.mark.parametrize("mapping_source", ["contact", "session"])
def test_recovered_chat_mapping_refreshes_unchanged_message_shards(scenario, mapping_source):
    contact = scenario.reader.root / "contact/contact.db"
    session = scenario.reader.root / "session/session.db"
    for path, sql in ((contact, "DELETE FROM contact WHERE username=?"),
                      (session, "DELETE FROM SessionTable WHERE username=?")):
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(sql, (CHAT,))
        conn.close()
    cursor = scenario.reader.get_changes()["cursor"]
    unresolved = "hash:" + hashlib.md5(CHAT.encode()).hexdigest()
    assert len(scenario.reader.get_messages(unresolved, refresh=False)["messages"]) == 2
    original = {path: cipher._source_signature(path) for path in scenario.shards.values()}

    path = contact if mapping_source == "contact" else session
    conn = sqlite3.connect(path)
    with conn:
        if mapping_source == "contact":
            conn.execute("INSERT INTO contact VALUES(?,?,?)", (CHAT, "Recovered chat", ""))
        else:
            conn.execute("INSERT INTO SessionTable VALUES(?,?,?)", (CHAT, 0, FIRST_TIME))
    conn.close()
    result = scenario.reader.get_changes(cursor)

    assert original == {path: cipher._source_signature(path) for path in scenario.shards.values()}
    assert len(result["changes"]) == 2
    assert {change["event"] for change in result["changes"]} == {"updated"}
    assert {change["message"]["chat"] for change in result["changes"]} == {CHAT}
    assert len(scenario.reader.get_messages(CHAT, refresh=False)["messages"]) == 2
    assert scenario.reader.get_messages(unresolved, refresh=False)["messages"] == []
    assert scenario.reader.get_changes(result["cursor"])["changes"] == []


def test_unrelated_contact_recovery_does_not_redecode_message_tables(scenario, decoded_values):
    scenario.reader.sync()
    decoded_values.clear()
    conn = sqlite3.connect(scenario.reader.root / "contact/contact.db")
    with conn:
        conn.execute("INSERT INTO contact VALUES(?,?,?)", ("synthetic-unrelated", "New contact", ""))
    conn.close()

    result = scenario.reader.sync()

    assert result["messages_added"] == result["messages_updated"] == 0
    assert decoded_values == []
    assert len(scenario.reader.get_messages(CHAT, refresh=False)["messages"]) == 2


def test_sender_mapping_is_part_of_view_identity_even_with_same_source_signature(scenario, monkeypatch):
    original_snapshot = cipher.snapshot

    def fixed_signature(source, destination, key):
        result = original_snapshot(source, destination, key)
        # Model a retained source signature that alone cannot distinguish the
        # metadata update. The actual snapshot still contains the new Name2Id.
        result["source_signature"] = {"synthetic_identity": str(source)}
        return result

    monkeypatch.setattr(cipher, "snapshot", fixed_signature)
    cursor = scenario.reader.get_changes()["cursor"]
    conn = sqlite3.connect(scenario.shards[0])
    with conn:
        conn.execute("UPDATE Name2Id SET user_name=? WHERE rowid=7", ("wxid_bob",))
    conn.close()

    result = scenario.reader.get_changes(cursor)

    assert len(result["changes"]) == 1
    assert result["changes"][0]["event"] == "updated"
    assert result["changes"][0]["message"]["sender"] == "wxid_bob"


def test_missing_historical_shard_reports_partial_coverage_and_preserves_rows(scenario):
    cursor = scenario.reader.get_changes()["cursor"]
    shard = scenario.shards[1]
    original = shard.read_bytes()
    shard.unlink()
    # Availability must also be correct before another sync and for cache-only
    # readers; a missing source is not a locally observed message deletion.
    cached = scenario.reader.get_messages(CHAT, refresh=False)
    availability = cached["coverage"]["indexed_source_availability"]
    assert len(cached["messages"]) == 2
    assert availability["status"] == "partial"
    assert availability["missing_message_shards"] == ["message/message_1.db"]
    assert availability["cached_messages_in_missing_shards"] == 1
    assert availability["missing_source_policy"] == "retain_cached_messages"

    result = scenario.reader.get_changes(cursor)
    assert result["changes"] == []
    assert result["coverage"]["indexed_source_availability"]["status"] == "partial"
    assert scenario.reader.status()["messages"] == 2

    shard.write_bytes(original)
    scenario.add_message(1, 2, "Restored source message", seq=12)
    restored = scenario.reader.get_changes(result["cursor"])
    assert len(restored["changes"]) == 1
    assert restored["changes"][0]["event"] == "newly_observed"
    assert restored["coverage"]["indexed_source_availability"]["status"] == "available"
    assert restored["coverage"]["indexed_source_availability"]["missing_message_shards"] == []


def test_missing_contact_metadata_retains_resolved_chats_and_marks_partial(scenario):
    cursor = scenario.reader.get_changes()["cursor"]
    contact = scenario.reader.root / "contact/contact.db"
    contact.unlink()
    # Remove the other current source of the chat ID too; only its previously
    # resolved index identity remains available for the MD5 table mapping.
    conn = sqlite3.connect(scenario.reader.root / "session/session.db")
    with conn:
        conn.execute("DELETE FROM SessionTable WHERE username=?", (CHAT,))
    conn.close()

    result = scenario.reader.get_changes(cursor)

    assert result["changes"] == []
    assert len(scenario.reader.get_messages(CHAT, refresh=False)["messages"]) == 2
    coverage = scenario.reader.status()["coverage"]["indexed_source_availability"]
    assert coverage["status"] == "partial"
    assert coverage["missing_metadata_sources"] == ["contact/contact.db"]
    assert coverage["missing_message_shards"] == []


def test_legacy_image_xml_credentials_never_reach_history_or_search(scenario):
    aes_token = "FAKE_AES_CREDENTIAL_731"
    cdn_token = "FAKE_CDN_CREDENTIAL_284"
    raw_xml = f'<msg><img aeskey="{aes_token}" cdnkey="{cdn_token}" /></msg>'
    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute(f'UPDATE "{TABLE}" SET local_type=3,message_content=? WHERE local_id=1', (raw_xml,))

    # Verify new snapshots first, then simulate an index written by an older
    # bridge. Its unchanged source must not accidentally mask the output guard.
    fresh = scenario.reader.get_messages(CHAT)
    assert next(message for message in fresh["messages"] if message["type"] == 3)["content"] == "[图片]"
    with sqlite3.connect(scenario.reader.index_path) as conn:
        conn.execute("UPDATE messages SET content=?,decode_status='decoded' WHERE shard=? AND local_id=1",
                     (raw_xml, "message/message_0.db"))
    legacy = scenario.reader.get_messages(CHAT)

    assert next(message for message in legacy["messages"] if message["type"] == 3)["content"] == "[图片]"
    serialized = json.dumps(legacy, ensure_ascii=False)
    assert aes_token not in serialized
    assert cdn_token not in serialized
    assert "aeskey" not in serialized
    assert "cdnkey" not in serialized
    assert scenario.reader.search_messages(aes_token)["messages"] == []
    assert scenario.reader.search_messages("73")["messages"] == []
    with sqlite3.connect(scenario.reader.index_path) as conn:
        raw_stored = conn.execute("SELECT content FROM messages WHERE shard=? AND local_id=1",
                                  ("message/message_0.db",)).fetchone()[0]
    assert raw_stored == raw_xml


@pytest.mark.parametrize("title_xml", ["合成课程安排", "<![CDATA[合成课程安排]]>"], ids=["plain", "cdata"])
def test_card_only_returns_visible_title_and_url_without_attachment_keys(scenario, title_xml):
    aes_token = "FAKE_CARD_AES_CREDENTIAL_812"
    cdn_token = "FAKE_CARD_CDN_CREDENTIAL_496"
    url = "https://example.invalid/synthetic-agenda"
    raw_xml = (f"<msg><appmsg><title>{title_xml}</title><url>{url}</url>"
               f"<appattach><aeskey>{aes_token}</aeskey><cdnattachurl>{cdn_token}</cdnattachurl>"
               "</appattach></appmsg></msg>")
    with sqlite3.connect(scenario.shards[0]) as conn:
        conn.execute(f'UPDATE "{TABLE}" SET local_type=49,message_content=? WHERE local_id=1', (raw_xml,))

    result = scenario.reader.get_messages(CHAT)
    card = next(message for message in result["messages"] if message["type"] == 49)

    assert card["content"] == "[链接/文件/引用] 合成课程安排\n" + url
    serialized = json.dumps(result, ensure_ascii=False)
    assert aes_token not in serialized
    assert cdn_token not in serialized
    assert "appattach" not in serialized
    assert "aeskey" not in serialized
    assert scenario.reader.search_messages("合成课程安排")["messages"][0]["id"] == card["id"]
    assert scenario.reader.search_messages(aes_token)["messages"] == []
