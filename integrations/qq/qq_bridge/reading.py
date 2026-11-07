"""Convenient reads over the same authenticated local index; no client UI."""
from functools import wraps

def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock, self._process_lock():
            return method(self, *args, **kwargs)
    return call

def preview(compact, max_content_chars):
    if type(compact) is not bool:
        raise ValueError("compact must be boolean")
    if max_content_chars is not None and (type(max_content_chars) is not int or not 1 <= max_content_chars <= 6000):
        raise ValueError("max_content_chars must be 1–6000")
    return {"compact": compact, "max_content_chars": max_content_chars}

class ReadingMixin:
    @locked
    def get_messages(self, chat=None, limit=100, before=None, after=None, cursor=None,
                     refresh=True, compact=False, max_content_chars=None):
        from .reader import _time, _integer
        _integer(limit, 1, 500, "limit")
        options = preview(compact, max_content_chars)
        with self._index() as conn:
            page = self._decode_cursor(conn, cursor, "history") if cursor else None
            if page:
                if chat is None:
                    chat = page.get("chat")
                if before is None:
                    end = page.get("before")
                else:
                    end = _time(before, 2**63 - 1)
                if after is None:
                    start = page.get("after")
                else:
                    start = _time(after, -1)
            else:
                start, end = _time(after, -1), _time(before, 2**63 - 1)
            self._check_chat(chat)
            if type(start) is not int or type(end) is not int or start >= end:
                raise ValueError("invalid time range")
            if page and (page.get("chat") != chat or page.get("before") != end or page.get("after") != start):
                raise ValueError("cursor query mismatch")
            if page:
                _integer(page.get("created"), 0, 253402214399, "cursor timestamp")
                if not isinstance(page.get("id"), str) or len(page["id"]) > 256:
                    raise ValueError("invalid cursor message")
        refreshed = self._read_refresh(refresh)
        with self._index() as conn:
            conn.execute("BEGIN")
            conditions, arguments = ["chat=?", "created>?", "created<?"], [chat, start, end]
            if page:
                # Revalidate after a refresh that might rebuild the account epoch.
                self._decode_cursor(conn, cursor, "history")
                conditions.append("(created<? OR (created=? AND id<?))")
                arguments += [page["created"], page["created"], page["id"]]
            rows = conn.execute("SELECT * FROM messages WHERE " + " AND ".join(conditions) +
                                " ORDER BY created DESC,id DESC LIMIT ?", [*arguments, limit + 1]).fetchall()
            more, rows = len(rows) > limit, rows[:limit]
            next_cursor = self._cursor(conn, "history", chat=chat, before=end, after=start,
                                       created=rows[-1]["created"], id=rows[-1]["id"]) if more else None
            return {"ok": refreshed["ok"] or self._meta(conn, "last_sync") is not None,
                    "chat": chat, "messages": [self._public(row, conn, **options) for row in rows],
                    "has_more": more, "next_cursor": next_cursor, "order": "newest_first",
                    **self._freshness(conn, refreshed)}

    @locked
    def search_messages(self, query, chat=None, limit=50, after=None, before=None,
                        cursor=None, refresh=True, compact=False, max_content_chars=None):
        from .reader import _time, _integer
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise ValueError("invalid search query")
        _integer(limit, 1, 200, "limit")
        options = preview(compact, max_content_chars)
        with self._index() as conn:
            page = self._decode_cursor(conn, cursor, "search") if cursor else None
            if page:
                chat = page.get("chat") if chat is None else chat
                start = page.get("after") if after is None else _time(after, -1)
                end = page.get("before") if before is None else _time(before, 2**63 - 1)
            else:
                start, end = _time(after, -1), _time(before, 2**63 - 1)
            if chat:
                self._check_chat(chat)
            if type(start) is not int or type(end) is not int or start >= end:
                raise ValueError("invalid time range")
            if page and (page.get("query") != query or page.get("chat") != chat or page.get("after") != start or page.get("before") != end):
                raise ValueError("cursor query mismatch")
            if page:
                _integer(page.get("created"), 0, 253402214399, "cursor timestamp")
                if not isinstance(page.get("id"), str) or len(page["id"]) > 256:
                    raise ValueError("invalid cursor message")
        refreshed = self._read_refresh(refresh)
        with self._index() as conn:
            conn.execute("BEGIN")
            if page:
                self._decode_cursor(conn, cursor, "search")
            conditions, arguments = ["m.created>?", "m.created<?", "instr(m.content,?)>0"], [start, end, query]
            if chat:
                conditions.append("m.chat=?")
                arguments.append(chat)
            if page:
                conditions.append("(m.created<? OR (m.created=? AND m.id<?))")
                arguments += [page["created"], page["created"], page["id"]]
            rows = conn.execute("SELECT m.* FROM messages m WHERE " + " AND ".join(conditions) +
                                " ORDER BY m.created DESC,m.id DESC LIMIT ?", [*arguments, limit + 1]).fetchall()
            more, rows = len(rows) > limit, rows[:limit]
            next_cursor = self._cursor(conn, "search", query=query, chat=chat, before=end, after=start,
                                       created=rows[-1]["created"], id=rows[-1]["id"]) if more else None
            return {"ok": refreshed["ok"] or self._meta(conn, "last_sync") is not None,
                    "messages": [self._public(row, conn, **options) for row in rows],
                    "truncated": more, "has_more": more, "next_cursor": next_cursor,
                    "query_mode": "literal_substring", **self._freshness(conn, refreshed)}

    @locked
    def get_message(self, message_id, context_before=3, context_after=3, refresh=False,
                    text_offset=0, text_limit=10000):
        from .reader import _integer
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
            raise ValueError("invalid message ID")
        for number, high, name in [(context_before, 20, "context_before"), (context_after, 20, "context_after"),
                                   (text_offset, 2_000_000, "text_offset")]:
            _integer(number, 0, high, name)
        _integer(text_limit, 1, 200000, "text_limit")
        refreshed = self._read_refresh(refresh)
        with self._index() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                return {"ok": False, "error": {"code": "message_not_found", "message": "Message ID is not in this account index"},
                        **self._freshness(conn, refreshed)}
            older = conn.execute("SELECT * FROM messages WHERE chat=? AND (created,id)<(?,?) ORDER BY created DESC,id DESC LIMIT ?",
                                 (row["chat"], row["created"], row["id"], context_before)).fetchall()
            newer = conn.execute("SELECT * FROM messages WHERE chat=? AND (created,id)>(?,?) ORDER BY created,id LIMIT ?",
                                 (row["chat"], row["created"], row["id"], context_after)).fetchall()
            message = self._public(row, conn)
            content = message["content"]
            message["content"] = content[text_offset:text_offset + text_limit]
            end = min(len(content), text_offset + text_limit)
            return {"ok": True, "message": message,
                    "context_before": [self._public(item, conn, compact=True) for item in reversed(older)],
                    "context_after": [self._public(item, conn, compact=True) for item in newer],
                    "text_page": {"offset": text_offset, "total_chars": len(content), "has_more": end < len(content),
                                  "next_offset": end if end < len(content) else None},
                    **self._freshness(conn, refreshed)}
