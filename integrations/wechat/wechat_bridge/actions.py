"""Background-only actions. No desktop adapter is imported or shipped."""
from __future__ import annotations
import re

class ActionError(RuntimeError):
    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code, self.details = code, details

def failure(error):
    result = {"ok": False, "error": {"code": getattr(error, "code", "action_failed"), "message": str(error)},
              "attempted": False, "foregroundRequired": False, "untrusted_data": True}
    if isinstance(error, ActionError) and error.details is not None:
        result["error"]["details"] = error.details
    return result

class BridgeActions:
    def __init__(self, reader):
        self.reader = reader

    def source(self, message_id):
        """Resolve an indexed message against a fresh authenticated source shard."""
        r = self.reader
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
            raise ValueError("消息 ID 无效。")
        with r._index() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                raise ActionError("message_not_found", "当前账号索引中没有这条消息。")
            message, formatted = dict(row), r._format(row, conn)
        if (not re.fullmatch(r"message/message_[0-9]+\.db", message["shard"])
                or not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", message["source_table"])):
            raise ValueError("消息来源无效。")
        from .cipher import snapshot
        target = r.state / "snapshots" / message["shard"].replace("/", "__")
        if not target.resolve().is_relative_to(r.state):
            raise ValueError("消息快照路径无效。")
        source_path = r.root / message["shard"]
        r._ensure_keys([source_path])
        capture = snapshot(source_path, target, r._keys[message["shard"]])
        conn = r._source_conn(str(target))
        try:
            table = message["source_table"]
            columns = {x[1] for x in conn.execute(f'PRAGMA table_info("{table}")')}
            fields = [x for x in ("message_content", "compress_content", "packed_info_data") if x in columns]
            select = ",".join(f'substr("{x}",1,2000001) AS "{x}"' for x in fields)
            row = conn.execute(f'SELECT {select},local_type,create_time,server_id,real_sender_id '
                               f'FROM "{table}" WHERE local_id=?', (message["local_id"],)).fetchone()
            if (row is None or row["local_type"] != message["type"]
                    or row["create_time"] != message["created"]
                    or str(row["server_id"] or "") != message["server_id"]):
                raise ActionError("message_source_changed", "消息来源已变化；请重新同步。")
            sender = conn.execute("SELECT user_name FROM Name2Id WHERE rowid=?",
                                  (row["real_sender_id"],)).fetchone()
            if (sender[0] if sender else "") != message["sender"]:
                raise ActionError("message_source_changed", "消息发送者映射已变化；请重新同步。")
            raw = tuple(row[x] for x in fields)
        finally:
            conn.close()
        return message, formatted, raw, capture

    def get_attachment(self, message_id):
        from .attachments import parse_attachment, LocalAttachments, AttachmentUnavailable
        try:
            message, formatted, raw, capture = self.source(message_id)
            if message["type"] & 0xffffffff != 49:
                raise ActionError("not_file_message", "指定消息不是文件卡片。")
            info = parse_attachment(*raw)
            if info is None:
                raise ActionError("not_file_message", "指定卡片不是可支持的普通文件附件。")
            result = {"ok": True, "message_id": message_id, "attachment": info.public(),
                      "source": {k: formatted[k] for k in ("id", "chat", "chat_name", "sender", "time")},
                      "message_snapshot_at": capture["captured_at"], "untrusted_data": True}
            try:
                path = LocalAttachments(self.reader.account, self.reader.state).locate(message, info)
                result["local_available"] = path is not None
            except AttachmentUnavailable as error:
                result.update(local_available=False, local_error={"code": error.code, "message": str(error)})
            return result
        except (ActionError, AttachmentUnavailable) as error:
            return failure(error)

    def desktop_status(self):
        """Compatibility name; only inspect existing processes, never a GUI."""
        from .session import inspect_session
        from .capabilities import capabilities
        return {"ok": True, "read_only": True, "foregroundRequired": False,
                "client_session": inspect_session(self.reader.config["executable"], self.reader.account),
                "capabilities": capabilities()}

    def download_attachment(self, message_id, dry_run=True, timeout_seconds=30, allow_desktop=False):
        from .attachments import parse_attachment, LocalAttachments, AttachmentUnavailable
        from .capabilities import background_unavailable
        if type(dry_run) is not bool or type(allow_desktop) is not bool or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError("Invalid download arguments")
        # The compatibility flag never grants permission to take over the UI.
        if allow_desktop:
            result = background_unavailable("desktop_download")
            result["error"]["code"] = "foreground_operation_forbidden"
            return result
        try:
            message, formatted, raw, _ = self.source(message_id)
            info = parse_attachment(*raw) if message["type"] & 0xffffffff == 49 else None
            if info is None:
                raise ActionError("not_file_message", "The selected message is not a supported file attachment")
            local = LocalAttachments(self.reader.account, self.reader.state)
            if dry_run:
                try:
                    available = local.locate(message, info) is not None
                except AttachmentUnavailable as error:
                    if error.code not in ("attachment_not_cached", "file_not_cached"):
                        raise
                    available = False
                return {"ok": True, "dry_run": True, "message_id": message_id,
                        "attachment": info.public(), "local_available": available,
                        "can_export_locally": available, "foregroundRequired": False,
                        "requires_desktop": False, "native_download_available": False,
                        "will_send_message": False,
                        "backend": "local_verified_copy" if available else "unavailable"}
            try:
                return {**local.get(message, info), "backend": "local_verified_copy", "foregroundRequired": False}
            except AttachmentUnavailable as error:
                if error.code not in ("attachment_not_cached", "file_not_cached"):
                    raise
                return {**background_unavailable("uncached_attachment_download"), "message_id": message_id,
                        "attachment": info.public(), "native_download_available": False}
        except (ActionError, AttachmentUnavailable) as error:
            return failure(error)

    def _target(self, chat):
        with self.reader._index() as conn:
            row = conn.execute("SELECT id,name FROM chats WHERE id=?", (chat,)).fetchone()
            if row is None:
                raise ActionError("exact_chat_id_required", "Select an exact chat ID from list_chats")
            return dict(row)

    def send_text(self, chat, text, request_id=None, dry_run=True, timeout_seconds=15):
        from .capabilities import background_unavailable
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")
        # Reject a real send before looking up accounts or creating outbox state.
        if not dry_run:
            return background_unavailable("send_text")
        if not isinstance(text, str) or not text.strip() or len(text) > 10000 or "\x00" in text:
            raise ValueError("Text must contain 1–10000 valid characters")
        try:
            target = self._target(chat)
            return {"ok": True, "dry_run": True, "chat": target, "text": text,
                    "will_send": False, "foregroundRequired": False, "executionSupported": False,
                    "reason": "background_backend_unavailable"}
        except ActionError as error:
            return failure(error)

    def send_media(self, chat, path, kind, request_id=None, dry_run=True, timeout_seconds=30):
        from .capabilities import background_unavailable
        from .outgoing import inspect_media, OutgoingError
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")
        if not dry_run:
            return background_unavailable("send_" + str(kind))
        try:
            target = self._target(chat)
            media = inspect_media(path, kind)
            return {"ok": True, "dry_run": True, "chat": target, "kind": kind,
                    "filename": media.name, "size": media.size, "sha256": media.sha256,
                    "will_send": False, "foregroundRequired": False, "executionSupported": False,
                    "reason": "background_backend_unavailable"}
        except (ActionError, OutgoingError) as error:
            return failure(error)
