from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time

import pytest

from qq_bridge.reader import Reader
from conftest import FakeSource


def chat_for(reader, source):
    return "qq:" + source.account_tag + ":group:synthetic-group"


def test_explicit_cached_paging_requires_successful_sync_and_does_not_capture(scenario):
    reader,source=scenario
    reader.sync()
    before=source.captures
    source.fail=True
    assert reader.get_messages(chat_for(reader,source),refresh=False)['messages']
    assert reader.get_changes(refresh=False)['baseline']
    assert reader.list_chats(refresh=False)['chats']
    assert source.captures==before
    assert not reader.sync()['ok']
    assert not reader.get_changes(refresh=False)['ok']


def test_status_is_scoped_and_never_returns_local_keys_paths_or_runtime(scenario):
    reader, source = scenario
    result = reader.status()
    serialized = json.dumps(result)
    assert "PRIVATE_KEY" not in serialized and "PRIVATE_PATH" not in serialized
    assert source.captures == 0
    assert result["capabilities"]["history_cursor"] == "keyset_timestamp_and_stable_id"
    assert result["capabilities"]["history_complete"] is False


def test_sync_is_idempotent_and_source_is_unchanged(scenario):
    reader, source = scenario
    original = source.main.read_bytes()
    assert reader.sync()["added"] == 1
    again = reader.sync()
    assert again["added"] == again["updated"] == 0
    assert again["coverage"]["snapshot_unchanged"] is True
    assert source.main.read_bytes() == original
    assert "PRIVATE_KEY" not in json.dumps(again)
    with sqlite3.connect(reader.index_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_history_keyset_does_not_lose_same_second_messages(scenario):
    reader, source = scenario
    for native in range(11, 19):
        source.add(native, "同一秒消息 " + str(native))
    messages = []
    cursor = None
    while True:
        page = reader.get_messages(chat_for(reader, source), limit=2, cursor=cursor)
        messages.extend(page["messages"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(messages) == len({message["id"] for message in messages}) == 9
    assert [message["id"] for message in messages] == sorted((m["id"] for m in messages), reverse=True)
    assert all(message["platform"] == "qq" for message in messages)
    assert all(message["id"].startswith("qq:") for message in messages)
    assert all(message["time"].endswith("+08:00") for message in messages)


def test_late_history_and_updates_are_observed_without_max_native_id_assumption(scenario):
    reader, source = scenario
    baseline = reader.get_changes()
    assert baseline["baseline"] and baseline["changes"] == []
    source.add(1, "迟到的很旧历史", timestamp=1_600_000_000)
    result = reader.get_changes(baseline["cursor"], limit=1)
    assert result["changes"][0]["kind"] == "new"
    assert result["changes"][0]["message"]["content"] == "迟到的很旧历史"
    source.change(10, "修改后的本地正文")
    result = reader.get_changes(result["cursor"])
    assert [change["kind"] for change in result["changes"]] == ["updated"]
    assert result["changes"][0]["message"]["content"] == "修改后的本地正文"
    assert reader.get_changes(result["cursor"])["changes"] == []


def test_event_ledger_keeps_each_observed_version_and_pagination(scenario):
    reader, source = scenario
    baseline = reader.get_changes()["cursor"]
    source.change(10, "版本一")
    reader.sync()
    source.change(10, "版本二")
    reader.sync()
    first = reader.get_changes(baseline, limit=1)
    assert first["has_more"] is True
    assert first["changes"][0]["message"]["content"] == "版本一"
    second = reader.get_changes(first["cursor"], limit=1)
    assert second["changes"][0]["message"]["content"] == "版本二"


def test_failed_sync_retains_stale_cache_and_never_advances_cursor(scenario):
    reader, source = scenario
    baseline = reader.get_changes()["cursor"]
    source.fail = True
    sync = reader.sync()
    assert sync["ok"] is False and sync["fresh"] is False
    page = reader.get_messages(chat_for(reader, source))
    assert page["ok"] is True and page["fresh"] is False
    assert page["messages"][0]["content"] == "明天下午交材料"
    changes = reader.get_changes(baseline)
    assert changes["ok"] is False and changes["cursor"] == baseline
    assert changes["cursor_advanced"] is False and "changes" not in changes
    assert "PRIVATE_KEY" not in json.dumps([sync, page, changes])
    source.fail = False
    assert reader.sync()["fresh"] is True


def test_bad_row_rolls_back_all_new_messages_events_and_source_progress(scenario):
    reader, source = scenario
    baseline = reader.get_changes()["cursor"]
    source.add(11, "应回滚的行")
    source.add(12, "坏时间", timestamp=-1)
    assert reader.sync()["ok"] is False
    with sqlite3.connect(reader.index_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    with sqlite3.connect(source.main) as conn:
        conn.execute('DELETE FROM group_msg_table WHERE "40001"=12')
    result = reader.get_changes(baseline)
    assert [change["message"]["content"] for change in result["changes"]] == ["应回滚的行"]


def test_cursor_rejects_foreign_account_reset_epoch_query_and_future_sequence(scenario, tmp_path):
    reader, source = scenario
    source.add(11)
    history = reader.get_messages(chat_for(reader, source), limit=1)["next_cursor"]
    changes = reader.get_changes()["cursor"]
    other = FakeSource(tmp_path / "other-account", tmp_path / "other-state", tmp_path / "other.db")
    other.add(10)
    other_reader = Reader(source=other)
    with pytest.raises(ValueError):
        other_reader.get_changes(changes)
    with pytest.raises(ValueError):
        reader.get_messages(chat_for(reader, source), cursor=history, before="2026-10-20T00:00:00+08:00")
    with reader._index() as conn:
        future = reader._cursor(conn, "changes", seq=9999)
    with pytest.raises(ValueError):
        reader.get_changes(future)
    with reader._index() as conn:
        reader._setmeta(conn, "epoch", "reset-epoch")
    for cursor in (future, changes):
        with pytest.raises(ValueError):
            reader.get_changes(cursor)
    with pytest.raises(ValueError):
        reader.get_messages(chat_for(reader, source), cursor=history)


def test_zero_source_time_is_unknown_not_a_1970_date(scenario):
    reader, source = scenario
    source.add(11, "缺少时间的消息", timestamp=0)
    result = reader.get_messages(chat_for(reader, source))
    message = next(message for message in result["messages"] if message["content"] == "缺少时间的消息")
    assert message["time"] is None and message["timestamp"] is None
    assert message["timestamp_status"] == "unavailable"
    with reader._index() as conn:
        assert conn.execute("SELECT created FROM messages WHERE id=?", (message["id"],)).fetchone()[0] == 0


def test_change_cursor_cannot_omit_sequence_to_silently_rebaseline(scenario):
    reader, _ = scenario
    reader.sync()
    with reader._index() as conn:
        invalid = reader._cursor(conn, "changes")
    with pytest.raises(ValueError):
        reader.get_changes(invalid)


def test_old_zero_timestamp_ledger_versions_are_normalized(scenario):
    reader, source = scenario
    baseline = reader.get_changes()["cursor"]
    source.add(11, "没有时间", timestamp=0)
    reader.sync()
    with reader._index() as conn:
        row = conn.execute("SELECT seq,version_json FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        version = json.loads(row["version_json"])
        version.update(time="1970-01-01T08:00:00+08:00", timestamp=0)
        version.pop("timestamp_status", None)
        conn.execute("UPDATE events SET version_json=? WHERE seq=?", (json.dumps(version), row["seq"]))
    message = reader.get_changes(baseline)["changes"][0]["message"]
    assert message["time"] is None and message["timestamp"] is None
    assert message["timestamp_status"] == "unavailable"


@pytest.mark.parametrize("invalid", ["not-base64!", "e30", "W10", "null", "x" * 2049])
def test_malformed_cursors_are_rejected(scenario, invalid):
    reader, _ = scenario
    with pytest.raises(ValueError):
        reader.get_changes(invalid)


def test_existing_index_cannot_be_rebound_to_different_account(scenario, tmp_path):
    reader, source = scenario
    other = FakeSource(tmp_path / "foreign", tmp_path / "foreign-private", tmp_path / "foreign.db")
    other.state = source.state
    with pytest.raises(ValueError):
        Reader(source=other)


def test_search_is_literal_with_chinese_and_operator_sql_characters(scenario):
    reader, source = scenario
    source.add(11, '日程三个字，还有 OR 以及 "引号" 和 100% 字样')
    assert reader.search_messages("交材料")["messages"]
    assert reader.search_messages("材料")["messages"]
    assert len(reader.search_messages("日程三个字")["messages"]) == 1
    assert len(reader.search_messages("100%")["messages"]) == 1
    assert reader.search_messages("nothing OR something")["messages"] == []
    assert reader.search_messages("' OR 1=1 --")["messages"] == []
    assert reader.search_messages('"引号"')["messages"]


def test_optional_metadata_maps_names_and_missing_metadata_does_not_hide_messages(scenario, tmp_path):
    reader, source = scenario
    group = tmp_path / "group.db"
    profile = tmp_path / "profile.db"
    with sqlite3.connect(group) as conn:
        conn.execute('CREATE TABLE group_list("60001" TEXT,"60007" TEXT)')
        conn.execute("INSERT INTO group_list VALUES(?,?)", ("synthetic-group", "项目群"))
    with sqlite3.connect(profile) as conn:
        conn.execute('CREATE TABLE profile_info_v6("1000" TEXT,"1002" INTEGER,"20002" TEXT)')
        conn.execute("INSERT INTO profile_info_v6 VALUES(?,?,?)", ("synthetic-sender", 654321, "张同学"))
    source.optional = {"group_info.db": group, "profile_info.db": profile}
    assert reader.list_chats()["chats"][0]["name"] == "项目群"
    message = reader.get_messages(chat_for(reader, source))["messages"][0]
    assert message["sender_name"] == "张同学" and message["chat_name"] == "项目群"
    with sqlite3.connect(group) as conn:
        conn.execute('UPDATE group_list SET "60007"=?', ("新项目群",))
    assert reader.list_chats()["chats"][0]["name"] == "新项目群"
    source.optional = {}
    assert reader.get_messages(chat_for(reader, source))["messages"]


def test_two_reader_instances_serialize_capture_and_do_not_duplicate_events(scenario):
    reader, source = scenario
    other = Reader(source=source)
    original = source.capture
    active = 0
    maximum = 0

    def slow_capture():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        time.sleep(0.05)
        try:
            return original()
        finally:
            active -= 1

    source.capture = slow_capture
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: item.sync(), [reader, other]))
    assert all(result["ok"] for result in results)
    assert maximum == 1
    with sqlite3.connect(reader.index_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
