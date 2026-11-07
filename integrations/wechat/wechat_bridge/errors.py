"""Public error categories with fixed, non-private recovery instructions."""


class CursorError(ValueError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__("查询游标无效或来自另一账号、会话或索引。" if kind in ("history", "search")
                         else "游标无效或来自另一账号/重建索引。")


class ChatResolutionError(ValueError):
    """A read-only lookup failed; candidate names remain untrusted data."""

    def __init__(self, candidates=()):
        self.candidates = [{"id": row["id"], "name": row["name"]} for row in candidates]
        self.code = "ambiguous_chat" if self.candidates else "chat_not_found"
        super().__init__("会话名称不唯一。" if self.candidates else "本地索引中没有匹配会话。")
