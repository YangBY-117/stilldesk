"""Account-bound file-card parsing and verified local attachment export.

CDN identifiers are opaque credentials, not HTTP URLs. This module performs no
network requests and never changes a Weixin source file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid
import xml.etree.ElementTree as ET

import zstandard

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_XML_BYTES = 2_000_000
MAX_SCAN_ENTRIES = 50_000
MAX_CANDIDATES = 128
_MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")
_MD5 = re.compile(r"[0-9a-fA-F]{32}")
_PRIVATE_FIELDS = (
    "aeskey", "cdnattachurl", "attachid", "fileuploadtoken", "filekey",
    "cdnthumbaeskey", "cdnthumburl",
)


class AttachmentUnavailable(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _filename(value: str) -> str:
    """Accept one Windows-compatible filename, never a path."""
    if (not isinstance(value, str) or not value or len(value) > 240
            or value in (".", "..") or value[-1:] in (" ", ".")
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value)):
        raise AttachmentUnavailable("invalid_attachment_metadata", "文件卡片中的文件名无效。")
    return value


@dataclass(frozen=True)
class AttachmentInfo:
    name: str
    size: int | None
    extension: str
    md5: str | None
    private_fields: dict[str, str] = field(default_factory=dict, repr=False)
    is_large_file: bool = False

    def public(self) -> dict:
        return {
            "name": self.name, "filename": self.name, "size": self.size,
            "extension": self.extension, "md5": self.md5,
            "transport_available": bool(
                self.private_fields.get("cdnattachurl") or self.private_fields.get("attachid")
            ),
            "is_large_file": self.is_large_file,
        }


@dataclass(frozen=True)
class AttachmentCandidate:
    """A classification, not permission to operate a matching UI bubble.

    Reasons are fixed codes. The attachment remains internal; only its public()
    projection can be returned to clients without private transport fields.
    """
    kind: str
    attachment: AttachmentInfo | None = field(default=None, repr=False)
    subtype: int | None = None
    reason: str = ""


def _card_envelope(text: str) -> str:
    text = text.split("\x01\x00", 1)[0].replace("\x00", "").strip().lstrip("\ufeff")
    # Strip one bounded group sender envelope, not arbitrary preceding prose.
    return re.sub(r"^[A-Za-z0-9_.@-]{1,256}:\r?\n[ \t]{0,64}(?=<)", "", text, count=1)


def _xml_shaped_bytes(raw: bytes) -> bool:
    """Recognize damaged XML without treating incidental packed '<' as XML."""
    text = _card_envelope(raw.decode("utf-8", errors="replace"))
    if text.startswith("<"):
        return True
    marker = re.search(r"<(?:msg|appmsg)(?=[\s/>])|<\?xml(?=[\s?])|<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I)
    if marker is None:
        return False
    prefix = text[:marker.start()]
    # Human prose before an actual card is a malformed envelope. Binary/control
    # prefixes, including decoding replacements, are opaque packed metadata.
    return "\ufffd" not in prefix and all(ch.isprintable() or ch in "\t\r\n" for ch in prefix)


def _outer_appmsg(raw) -> tuple[ET.Element | None, int | None, str]:
    """Decode one bounded representation and identify only its outer card.

    Opaque packed-info bytes and ordinary text are auxiliary fields. A damaged
    XML-shaped or compressed representation instead remains an explicit unknown.
    """
    compressed = False
    if raw is None or raw == "" or raw == b"":
        return None, None, "absent"
    if isinstance(raw, bytes):
        if len(raw) > MAX_XML_BYTES:
            return None, None, "representation_too_large"
        compressed = raw.startswith(b"\x28\xb5\x2f\xfd")
        if compressed:
            try:
                frame = zstandard.get_frame_parameters(raw)
                if (frame.window_size > MAX_XML_BYTES
                        or (frame.content_size != zstandard.CONTENTSIZE_UNKNOWN
                            and frame.content_size > MAX_XML_BYTES)):
                    return None, None, "representation_too_large"
                # Check the complete frame; stream_reader can return an empty
                # or partial prefix at EOF without rejecting a truncated frame.
                raw = zstandard.ZstdDecompressor().decompress(
                    raw, max_output_size=MAX_XML_BYTES + 1, allow_extra_data=False)
            except zstandard.ZstdError:
                return None, None, "compressed_representation_invalid"
            if len(raw) > MAX_XML_BYTES:
                return None, None, "representation_too_large"
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            # XML-shaped invalid UTF-8 cannot be mistaken for packed metadata.
            reason = ("xml_encoding_invalid" if compressed or _xml_shaped_bytes(raw)
                      else "opaque_auxiliary")
            return None, None, reason
    if not isinstance(raw, str):
        return None, None, "representation_type_unknown"
    try:
        if len(raw) > MAX_XML_BYTES or len(raw.encode("utf-8")) > MAX_XML_BYTES:
            return None, None, "representation_too_large"
    except UnicodeError:
        return None, None, "xml_encoding_invalid"
    text = _card_envelope(raw)
    if not text:
        return None, None, "absent"
    if not compressed and (ord(text[0]) < 0x20 or ord(text[0]) == 0x7f):
        # A protobuf/control prefix can precede a literal 0x3c byte. It is not
        # XML merely because that byte decodes to '<'.
        return None, None, "opaque_auxiliary"
    if not text.startswith("<"):
        return None, None, ("xml_envelope_invalid" if compressed or "<" in text else "opaque_auxiliary")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        return None, None, "xml_declaration_rejected"
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, UnicodeError, ValueError):
        return None, None, "xml_invalid"
    if root.tag == "appmsg":
        app = root
    elif root.tag == "msg":
        apps = root.findall("appmsg")
        if len(apps) != 1:
            return None, None, "outer_appmsg_not_unique"
        app = apps[0]
    else:
        return None, None, "outer_appmsg_unknown"
    types = app.findall("type")
    if len(types) != 1 or list(types[0]):
        return None, None, "outer_type_not_unique"
    subtype = (types[0].text or "").strip()
    if not re.fullmatch(r"[0-9]{1,10}", subtype):
        return None, None, "outer_type_invalid"
    return app, int(subtype), "outer_appmsg_valid"


def _attachment_from_app(app: ET.Element) -> AttachmentInfo:
    attachment = app.find("appattach")
    if attachment is None:
        raise AttachmentUnavailable("invalid_attachment_metadata", "文件卡片缺少附件信息。")
    name = _filename((app.findtext("title") or attachment.findtext("filename") or "").strip())
    size_text = (attachment.findtext("totallen") or "").strip()
    size = None
    if size_text:
        if not re.fullmatch(r"[0-9]{1,18}", size_text):
            raise AttachmentUnavailable("invalid_attachment_metadata", "文件卡片中的大小无效。")
        size = int(size_text)
        if size > MAX_FILE_BYTES:
            raise AttachmentUnavailable("attachment_too_large", "附件超过 512 MiB 的处理上限。")
    digest = (app.findtext("md5") or attachment.findtext("md5") or "").strip()
    if digest and not _MD5.fullmatch(digest):
        raise AttachmentUnavailable("invalid_attachment_metadata", "文件卡片中的摘要无效。")
    extension = (attachment.findtext("fileext") or Path(name).suffix.lstrip(".")).strip()
    if extension and not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", extension):
        raise AttachmentUnavailable("invalid_attachment_metadata", "文件卡片中的扩展名无效。")
    private = {key: value for key in _PRIVATE_FIELDS
               if (value := (attachment.findtext(key) or "").strip())}
    return AttachmentInfo(
        name=name, size=size, extension=extension.lower(), md5=digest.lower() or None,
        private_fields=private,
        is_large_file=(attachment.findtext("islargefilemsg") or "").strip() == "1",
    )


def parse_attachment(*raw_values) -> AttachmentInfo | None:
    """Read a type-6 outer card, retaining the existing fallback contract.

    None does not distinguish a known non-file from unreadable input. Candidate
    exclusion must use classify_attachment_candidate instead of skipping None.
    """
    for raw in raw_values:
        app, subtype, _ = _outer_appmsg(raw)
        if app is not None and subtype == 6:
            return _attachment_from_app(app)
    return None


def classify_attachment_candidate(*raw_values) -> AttachmentCandidate:
    """Exclude only consistently parsed, explicitly known non-file cards.

    Link (5) and quoted reply (57) are the supported non-file types. A quote's
    nested appmsg is not its own card type. Unknown types, unreadable structured
    alternatives and contradictory representations remain fail-closed.
    """
    apps = []
    for raw in raw_values:
        app, subtype, reason = _outer_appmsg(raw)
        if app is not None:
            apps.append((app, subtype))
        elif reason not in {"absent", "opaque_auxiliary"}:
            return AttachmentCandidate("unknown", reason=reason)
    if not apps:
        return AttachmentCandidate("unknown", reason="outer_appmsg_unavailable")
    subtypes = {subtype for _, subtype in apps}
    if len(subtypes) != 1:
        return AttachmentCandidate("unknown", reason="conflicting_outer_types")
    subtype = next(iter(subtypes))
    if subtype == 6:
        try:
            infos = [_attachment_from_app(app) for app, _ in apps]
        except AttachmentUnavailable as error:
            return AttachmentCandidate("unknown", subtype=6, reason=error.code)
        if any(info != infos[0] for info in infos[1:]):
            return AttachmentCandidate("unknown", subtype=6, reason="conflicting_file_metadata")
        return AttachmentCandidate("file", infos[0], 6, "validated_file_metadata")
    if subtype in {5, 57}:
        return AttachmentCandidate("known_non_file", subtype=subtype, reason="known_outer_type")
    return AttachmentCandidate("unknown", subtype=subtype, reason="unsupported_outer_type")


def _is_reparse(path: Path) -> bool:
    attributes = path.lstat()
    return stat.S_ISLNK(attributes.st_mode) or bool(
        getattr(attributes, "st_file_attributes", 0) & 0x400
    )


def _check_path(path: Path, boundary: Path) -> None:
    """Reject link/reparse components, including the scope directory itself."""
    try:
        relative = path.relative_to(boundary)
    except ValueError:
        raise AttachmentUnavailable("unsafe_attachment_path", "附件路径不在允许目录中。") from None
    current = boundary
    for component in (None, *relative.parts):
        if component is not None:
            current = current / component
        if _is_reparse(current):
            raise AttachmentUnavailable("unsafe_attachment_path", "附件路径含链接或重解析点。")
    if not path.resolve().is_relative_to(boundary.resolve()):
        raise AttachmentUnavailable("unsafe_attachment_path", "附件路径不在允许目录中。")


def _open_read(path: Path, boundary: Path):
    _check_path(path, boundary)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise AttachmentUnavailable("unsafe_attachment_path", "附件不是普通文件。")
        if details.st_size > MAX_FILE_BYTES:
            raise AttachmentUnavailable("attachment_too_large", "附件超过 512 MiB 的处理上限。")
        if os.name == "nt":
            # Check the opened handle, not just the name checked before open.
            import ctypes
            from ctypes import wintypes
            import msvcrt
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            get_name = kernel.GetFinalPathNameByHandleW
            get_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            get_name.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = get_name(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise AttachmentUnavailable("unsafe_attachment_path", "无法核实附件句柄路径。")
            final = buffer.value
            if final.startswith("\\\\?\\UNC\\"):
                final = "\\\\" + final[8:]
            elif final.startswith("\\\\?\\"):
                final = final[4:]
            if Path(final).resolve() != path.resolve():
                raise AttachmentUnavailable("unsafe_attachment_path", "附件路径在读取期间变化。")
            if not Path(final).resolve().is_relative_to(boundary.resolve()):
                raise AttachmentUnavailable("unsafe_attachment_path", "附件句柄不在允许目录中。")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _digest(path: Path, boundary: Path, info: AttachmentInfo) -> tuple[int, str, str] | None:
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    count = 0
    with _open_read(path, boundary) as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            count += len(block)
            if count > MAX_FILE_BYTES:
                raise AttachmentUnavailable("attachment_too_large", "附件超过 512 MiB 的处理上限。")
            sha.update(block)
            md5.update(block)
    if info.size is not None and count != info.size:
        return None
    if info.md5 is not None and md5.hexdigest() != info.md5:
        return None
    return count, sha.hexdigest(), md5.hexdigest()


class LocalAttachments:
    def __init__(self, account: str | Path, state: str | Path):
        self.account = Path(os.path.abspath(account))
        self.state = Path(os.path.abspath(state))
        if self.state == self.account or self.state.is_relative_to(self.account):
            raise ValueError("附件导出缓存必须位于微信账号目录之外。")
        self.root = self.account / "msg" / "file"
        self.downloads = self.state / "downloads"

    def _locate_verified(self, info: AttachmentInfo, message: dict | None = None) -> tuple[Path, tuple[int, str, str]]:
        _filename(info.name)
        if info.size is None and info.md5 is None:
            raise AttachmentUnavailable(
                "attachment_identity_unavailable", "卡片没有可核验的大小或摘要，无法仅按文件名选择附件。"
            )
        # A previous verified export survives Weixin's cleanup of its cache.
        # Only this exact message's private export folder is eligible.
        if message is not None and info.md5 is not None:
            identifier = self._message_id(message)
            cached = self.downloads / hashlib.sha256(identifier.encode("utf-8")).hexdigest() / info.name
            if cached.exists():
                _check_path(cached, self.state)
                details = _digest(cached, self.state, info)
                if details is None:
                    raise AttachmentUnavailable("attachment_export_conflict", "私有导出目录已有与当前卡片不符的文件，已保留原文件。")
                return cached, details
        original_name = Path(info.name)
        # Weixin may preserve an existing download by inserting "(1)", "(2)",
        # etc. Only an expected content digest can authorize these variants.
        numbered_name = (
            re.compile(
                re.escape(original_name.stem) + r"\([1-9][0-9]*\)"
                + re.escape(original_name.suffix), re.IGNORECASE,
            )
            if info.md5 is not None else None
        )
        candidates = []
        inspected = 0
        folders = []
        if self.root.exists():
            _check_path(self.root, self.account)
            for month in sorted(self.root.iterdir()):
                if not _MONTH.fullmatch(month.name):
                    continue
                _check_path(month, self.account)
                if month.is_dir():
                    folders.append(month)
        # Files opened in Weixin are also cached under RWTemp/<month>/<chat
        # MD5>. Do not search another chat's temporary folder by filename.
        temporary = self.account / "temp" / "RWTemp"
        chat = (message or {}).get("chat")
        if isinstance(chat, str) and chat and temporary.exists():
            digest = (chat[5:] if re.fullmatch(r"hash:[0-9a-f]{32}", chat)
                      else hashlib.md5(chat.encode("utf-8")).hexdigest())
            _check_path(temporary, self.account)
            for month in sorted(temporary.iterdir()):
                if not _MONTH.fullmatch(month.name):
                    continue
                _check_path(month, self.account)
                if not month.is_dir():
                    continue
                folder = month / digest
                if folder.exists():
                    _check_path(folder, self.account)
                    if folder.is_dir():
                        folders.append(folder)
        for folder in folders:
            for path in folder.iterdir():
                inspected += 1
                if inspected > MAX_SCAN_ENTRIES:
                    raise AttachmentUnavailable("attachment_scan_limit", "附件目录过大，无法在本次限额内完成核验。")
                if (path.name.casefold() != info.name.casefold()
                        and (numbered_name is None or not numbered_name.fullmatch(path.name))):
                    continue
                _check_path(path, self.account)
                if path.is_file():
                    candidates.append(path)
                    if len(candidates) > MAX_CANDIDATES:
                        raise AttachmentUnavailable("attachment_scan_limit", "同名附件过多，无法在本次限额内完成核验。")
        verified = []
        for path in candidates:
            if info.size is not None and path.stat().st_size != info.size:
                continue
            details = _digest(path, self.account, info)
            if details:
                verified.append((path, details))
        if not verified:
            raise AttachmentUnavailable(
                "attachment_not_cached",
                "未找到与该消息大小及摘要相符的本地附件；同名文件不能作为下载凭据。",
            )
        if len({details[1] for _, details in verified}) > 1:
            raise AttachmentUnavailable("attachment_ambiguous", "存在内容不同的同名附件，当前卡片无法唯一确定文件。")
        return verified[0]

    def locate(self, message: dict, info: AttachmentInfo) -> Path:
        return self._locate_verified(info, message)[0]

    def get(self, message: dict, info: AttachmentInfo) -> dict:
        self._message_id(message)
        source_path, original = self._locate_verified(info, message)
        boundary = self.account if source_path.is_relative_to(self.account) else self.state
        result = self._export(message, info, source_path, boundary, original)
        if boundary == self.state:
            result["source"] = "verified_bridge_export"
        elif source_path.is_relative_to(self.account / "temp" / "RWTemp"):
            result["source"] = "verified_local_weixin_temporary_file"
        return result

    def import_download(self, message: dict, info: AttachmentInfo, path: str | Path) -> dict:
        """Validate a client Save As result staged inside this bridge's cache.

Both expected length and MD5 are required for a downloaded file; a partial
download or an unrelated same-name file must not be published. Staging files
are preserved, including incomplete or mismatching candidates.
        """
        self._message_id(message)
        _filename(info.name)
        if info.size is None or info.md5 is None:
            raise AttachmentUnavailable(
                "attachment_identity_unavailable", "下载导入需要卡片同时提供文件大小和摘要。"
            )
        staging = self.state / "download-staging"
        source_path = Path(os.path.abspath(path))
        if not source_path.is_relative_to(staging) or source_path == staging:
            raise AttachmentUnavailable(
                "unsafe_attachment_path", "下载文件不在当前桥接器的私有暂存目录中。"
            )
        if not source_path.exists():
            raise AttachmentUnavailable("attachment_download_unverified", "下载文件尚未完整出现。")
        _check_path(source_path, self.state)
        original = _digest(source_path, staging, info)
        if original is None:
            raise AttachmentUnavailable(
                "attachment_download_unverified", "下载文件尚未通过卡片大小与摘要核验，未发布副本。"
            )
        return self._export(message, info, source_path, staging, original)

    @staticmethod
    def _message_id(message: dict) -> str:
        identifier = message.get("id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 512:
            raise ValueError("消息编号无效。")
        return identifier

    def _export(self, message: dict, info: AttachmentInfo, source_path: Path,
                boundary: Path, original: tuple[int, str, str]) -> dict:
        identifier = self._message_id(message)
        _check_path(self.state, self.state)
        self.downloads.mkdir(exist_ok=True)
        _check_path(self.downloads, self.state)
        folder = self.downloads / hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        folder.mkdir(exist_ok=True)
        _check_path(folder, self.state)
        destination = folder / _filename(info.name)
        temporary = folder / (".part-" + uuid.uuid4().hex)
        count = 0
        sha, md5 = hashlib.sha256(), hashlib.md5()
        try:
            with _open_read(source_path, boundary) as source, temporary.open("xb") as output:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    count += len(block)
                    if count > MAX_FILE_BYTES:
                        raise AttachmentUnavailable("attachment_too_large", "附件超过 512 MiB 的处理上限。")
                    output.write(block)
                    sha.update(block)
                    md5.update(block)
                output.flush()
                os.fsync(output.fileno())
            if ((info.size is not None and count != info.size)
                    or (info.md5 is not None and md5.hexdigest() != info.md5)
                    or sha.hexdigest() != original[1]):
                raise AttachmentUnavailable("attachment_source_changed", "附件内容在导出期间发生变化，未发布副本。")
            existing = destination.exists()
            if existing:
                previous = _digest(destination, self.state, info)
                if previous is None or previous[1] != sha.hexdigest():
                    raise AttachmentUnavailable("attachment_export_conflict", "私有导出目录已有不同内容的文件，已保留原文件。")
            else:
                _check_path(folder, self.state)
                # Publish without replacing an existing artifact, even if one
                # was created between the existence check and this operation.
                if os.name == "nt":
                    os.rename(temporary, destination)
                else:
                    os.link(temporary, destination)
            return {
                "ok": True, "message_id": identifier, "attachment": info.public(),
                "path": str(destination), "size": count, "sha256": sha.hexdigest(),
                "md5": md5.hexdigest(), "already_exported": existing,
                "source": ("verified_client_download" if boundary != self.account
                           else "verified_local_weixin_file"),
                "untrusted_data": True,
            }
        finally:
            if temporary.exists():
                temporary.unlink()
