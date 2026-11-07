from concurrent.futures import ThreadPoolExecutor
import json
import time

import pytest

def chat(reader, source):
    return "qq:" + source.account_tag + ":group:synthetic-group"

def test_cursor_only_history_inherits_filters_and_rejects_conflicts(scenario):
    reader, source = scenario
    for i in range(11, 18):
        source.add(i, "page needle " + str(i))
    first = reader.get_messages(chat(reader, source), limit=2, after="2020-01-01T00:00:00+00:00")
    second = reader.get_messages(cursor=first["next_cursor"], limit=2, refresh=False)
    assert set(m["id"] for m in first["messages"]).isdisjoint(m["id"] for m in second["messages"])
    with pytest.raises(ValueError, match="mismatch"):
        reader.get_messages(cursor=first["next_cursor"], before="2021-01-01T00:00:00+00:00", refresh=False)

def test_search_paging_is_complete_and_cached(scenario):
    reader, source = scenario
    for i in range(11, 20):
        source.add(i, "a needle " + str(i))
    first = reader.search_messages("needle", chat(reader, source), limit=2)
    count = source.captures
    records = list(first["messages"])
    cursor = first["next_cursor"]
    while cursor:
        page = reader.search_messages("needle", limit=2, cursor=cursor, refresh=False)
        records.extend(page["messages"])
        cursor = page["next_cursor"]
    assert len(records) == len({m["id"] for m in records}) == 9
    assert source.captures == count
    with pytest.raises(ValueError, match="mismatch"):
        reader.search_messages("different", cursor=first["next_cursor"], refresh=False)

def test_compact_preview_and_full_text_pages_preserve_message_identity(scenario):
    reader, source = scenario
    source.add(11, "L" * 12000, timestamp=1_700_000_001)
    source.add(12, "later", timestamp=1_700_000_002)
    page = reader.get_messages(chat(reader, source), compact=True, max_content_chars=100)
    long = next(m for m in page["messages"] if m.get("content_length") == 12000)
    assert len(long["content"]) == 100 and long["content_truncated"]
    first = reader.get_message(long["id"], text_limit=7000)
    second = reader.get_message(long["id"], text_offset=first["text_page"]["next_offset"])
    assert first["message"]["content"] + second["message"]["content"] == "L" * 12000
    assert first["context_before"] and first["context_after"]
    assert first["message"]["id"] == second["message"]["id"]

def test_waiting_for_changes_does_not_hold_status_or_cached_reads(scenario):
    reader, source = scenario
    baseline = reader.get_changes()["cursor"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(reader.get_changes, baseline, 10, 2, False)
        time.sleep(0.2)
        start = time.monotonic()
        assert reader.status()["capabilities"]["foregroundRequired"] is False
        assert reader.get_messages(chat(reader, source), refresh=False)["messages"]
        assert time.monotonic() - start < 0.8
        assert pending.result()["changes"] == []

@pytest.mark.anyio
async def test_mcp_reports_capabilities_and_new_read_tools_without_account():
    from qq_bridge.server import create_server
    app = create_server()
    names = {tool.name for tool in await app.list_tools()}
    assert {"qq_get_message", "qq_capabilities", "qq_search_messages"} <= names
    content, structured = await app.call_tool("qq_status", {})
    assert structured["configured"] is False
    content, structured = await app.call_tool("qq_capabilities", {})
    assert structured["foregroundRequired"] is False
