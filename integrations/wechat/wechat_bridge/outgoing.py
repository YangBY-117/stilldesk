"""Bounded local media preparation and conservative authenticated-row matching.

Preparation never sends or starts Weixin. Receipt matching consumes only raw
values already authenticated by the caller and does not expose transport XML.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from io import BytesIO
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid
import warnings
import xml.etree.ElementTree as ET

from PIL import Image, UnidentifiedImageError
import zstandard


MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_XML_BYTES = 2_000_000
_MD5 = re.compile(r"[0-9a-fA-F]{32}")
_REQUEST = re.compile(r"[A-Za-z0-9_-]{8,100}")
_RESERVED = re.compile(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?")


class OutgoingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(code="unsafe_outgoing_path", message="发送文件路径无效或包含链接、设备路径。"):
    raise OutgoingError(code, message)


def _filename(name: str) -> None:
    if (not name or len(name) > 240 or name in (".", "..")
            or name[-1:] in (" ", ".") or _RESERVED.fullmatch(name)
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in name)
            or re.search(r'[<>:"/\\|?*]', name)):
        _fail("invalid_outgoing_filename", "发送文件的名称不符合 Windows 普通文件规则。")


def _absolute(value: str | Path) -> Path:
    value = os.fspath(value)
    if (not isinstance(value, str) or not value or value.startswith(("\\\\", "//", "\\??\\"))
            or "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _fail()
    path = Path(value)
    if not path.is_absolute():
        _fail("absolute_outgoing_path_required", "发送文件必须使用本机绝对路径。")
    # A drive letter is the only permitted colon; reject alternate data streams.
    remainder = value[2:] if re.match(r"^[A-Za-z]:", value) else value
    if ":" in remainder:
        _fail()
    for part in path.parts[1:]:
        _filename(part)
    return path


def _is_reparse(path: Path) -> bool:
    details = path.lstat()
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, "st_file_attributes", 0) & 0x400)


def _check_components(path: Path) -> None:
    current = Path(path.anchor)
    try:
        for part in (None, *path.parts[1:]):
            if part is not None:
                current /= part
            if _is_reparse(current):
                _fail()
        if os.path.normcase(str(path.resolve(strict=True))) != os.path.normcase(str(path)):
            _fail()
    except (OSError, RuntimeError):
        _fail("outgoing_file_unavailable", "发送文件或上级目录不存在或不可读取。")


def _identity(details) -> tuple:
    return (details.st_dev, details.st_ino, details.st_size,
            details.st_mtime_ns, details.st_ctime_ns)


def _binding_identity(details) -> tuple:
    # CPython 3.13 on Windows exposes change time in fstat.st_ctime but
    # creation time in path.stat.st_ctime. Compare the common birth time when
    # binding a pathname; full handle change times still guard a read/copy.
    clock = (getattr(details, "st_birthtime_ns", 0) if os.name == "nt"
             else details.st_ctime_ns)
    return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, clock)


def _handle_path(descriptor: int) -> Path:
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
        _fail()
    final = buffer.value
    if final.startswith("\\\\?\\UNC\\"):
        _fail()
    if final.startswith("\\\\?\\"):
        final = final[4:]
    return Path(final)


def _windows_open(path: Path, sharing: int = 0x1 | 0x2 | 0x4) -> int:
    """Open the final entry without following a reparse point."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    # Ordinary source reads permit sharing; a staged-send lease passes READ
    # only, denying new write/delete opens until the request releases it.
    handle = create(str(path), 0x80000000, sharing, None, 3,
                    0x00200000 | 0x08000000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "Cannot open outgoing file")
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise


@contextmanager
def _open_read(path: Path, *, sharing: int = 0x1 | 0x2 | 0x4):
    _check_components(path)
    descriptor = None
    try:
        descriptor = (_windows_open(path, sharing) if os.name == "nt" else
                      os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)))
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode)
                or getattr(details, "st_file_attributes", 0) & 0x400):
            _fail()
        if os.name == "nt" and os.path.normcase(str(_handle_path(descriptor))) != os.path.normcase(str(path)):
            _fail("outgoing_source_changed", "发送文件的打开路径在读取期间发生变化。")
        _check_components(path)
        if _binding_identity(path.lstat()) != _binding_identity(details):
            _fail("outgoing_source_changed", "发送文件在打开期间发生变化。")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        with stream:
            yield stream
    except OSError:
        _fail("outgoing_file_unavailable", "发送文件无法作为本机普通文件读取。")
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _open_new(path: Path):
    _check_components(path.parent)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            _fail()
        if os.name == "nt" and os.path.normcase(str(_handle_path(descriptor))) != os.path.normcase(str(path)):
            _fail()
        _check_components(path)
        if _binding_identity(path.lstat()) != _binding_identity(details):
            _fail()
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fingerprint(path: Path, cap: int, *, collect=False, output=None):
    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
    data = bytearray()
    with _open_read(path) as source:
        before = os.fstat(source.fileno())
        if before.st_size <= 0:
            _fail("empty_outgoing_file", "不能发送空文件。")
        if before.st_size > cap:
            _fail("outgoing_file_too_large", "发送文件超过此媒体类型的大小上限。")
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            if size > cap:
                _fail("outgoing_file_too_large", "发送文件超过此媒体类型的大小上限。")
            sha.update(block)
            md5.update(block)
            if collect:
                data.extend(block)
            elif len(data) < 4096:
                data.extend(block[:4096 - len(data)])
            if output is not None:
                output.write(block)
        after = os.fstat(source.fileno())
        _check_components(path)
        if (_identity(before) != _identity(after) or size != before.st_size
                or _binding_identity(path.lstat()) != _binding_identity(after)):
            _fail("outgoing_source_changed", "发送文件在读取或复制期间发生变化。")
    return size, sha.hexdigest(), md5.hexdigest(), _identity(after), bytes(data)


def _image_metadata(data: bytes, suffix: str) -> dict:
    expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}.get(suffix)
    if expected is None:
        _fail("unsupported_outgoing_image", "照片发送仅支持 JPEG 或 PNG 文件。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != expected:
                    _fail("outgoing_format_mismatch", "图片内容与文件扩展名不一致。")
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    _fail("outgoing_image_too_large", "照片超过 4000 万像素的处理上限。")
                metadata = {"format": expected, "width": width, "height": height,
                            "animated": bool(getattr(image, "is_animated", False))}
                image.verify()
            # JPEG verify() alone does not detect every truncated pixel stream.
            with Image.open(BytesIO(data)) as image:
                image.load()
        return metadata
    except OutgoingError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        _fail("invalid_outgoing_image", "照片不是完整可解码的 JPEG 或 PNG 文件。")


def _video_metadata(path: Path, prefix: bytes) -> dict:
    if (path.suffix.lower() != ".mp4" or len(prefix) < 16 or prefix[4:8] != b"ftyp"
            or not 16 <= int.from_bytes(prefix[:4], "big") <= 4096):
        _fail("unsupported_outgoing_video", "视频发送仅支持具有 MP4 文件标识的 .mp4 文件。")
    box_size = int.from_bytes(prefix[:4], "big")
    brands = [prefix[8:12], *(prefix[i:i + 4] for i in range(16, box_size, 4))]
    mp4_brands = {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"iso8", b"iso9",
                  b"mp41", b"mp42", b"avc1", b"hvc1", b"hev1", b"M4V ", b"MSNV", b"dash"}
    if box_size > len(prefix) or not any(brand in mp4_brands for brand in brands):
        _fail("unsupported_outgoing_video", "视频的文件标识不属于支持的 MP4 格式。")
    try:
        import imageio_ffmpeg
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-xerror",
             "-max_alloc", str(64 * 1024 * 1024), "-threads", "1",
             "-protocol_whitelist", "file,pipe", "-enable_drefs", "0", "-use_absolute_path", "0",
             "-f", "mov", "-i", str(path),
             "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
             "-vf", "scale=1:1", "-threads", "1", "-pix_fmt", "rgb24",
             "-f", "rawvideo", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except (ImportError, RuntimeError, OSError):
        _fail("outgoing_video_decoder_unavailable", "本机 MP4 视频校验器不可用。")
    except subprocess.TimeoutExpired:
        _fail("outgoing_video_probe_timeout", "视频首帧校验超时，未准备发送。")
    if result.returncode != 0 or len(result.stdout) != 3:
        _fail("invalid_outgoing_video", "MP4 文件没有可完整解码的视频首帧。")
    return {"format": "MP4", "first_frame_decoded": True}


@dataclass(frozen=True)
class PreparedMedia:
    source_path: Path
    path: Path
    name: str
    kind: str
    size: int
    sha256: str
    md5: str
    metadata: dict
    _identity: tuple = field(default=(), repr=False, compare=False)
    _staged_root: Path | None = field(default=None, repr=False, compare=False)

    def public(self) -> dict:
        return {"kind": self.kind, "name": self.name, "size": self.size,
                "sha256": self.sha256, "md5": self.md5, "metadata": dict(self.metadata)}

    def _check_fingerprint(self, details, *, staged=False):
        size, sha, md5, identity, _ = details
        if ((size, sha, md5) != (self.size, self.sha256, self.md5)
                or not self._identity or identity != self._identity):
            _fail("outgoing_staging_changed" if staged else "outgoing_source_changed",
                  "待发送副本已发生变化。" if staged else "发送源文件在预检后发生变化。")

    def stage(self, state: Path, request_id: str) -> PreparedMedia:
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            _fail("invalid_outgoing_request_id", "实际发送必须提供有效且可复用的 request_id。")
        if self._staged_root is not None or self.path != self.source_path:
            _fail("outgoing_already_staged", "该发送副本已经暂存。")
        _filename(self.name)
        state = _absolute(state)
        _check_components(state)
        if not state.is_dir():
            _fail()
        cap = MAX_IMAGE_BYTES if self.kind == "image" else MAX_FILE_BYTES
        self._check_fingerprint(_fingerprint(self.source_path, cap))
        try:
            outgoing = state / "outgoing"
            outgoing.mkdir(mode=0o700, exist_ok=True)
            _check_components(outgoing)
            staging = outgoing / uuid.uuid4().hex
            staging.mkdir(mode=0o700)
            _check_components(staging)
            destination = staging / self.name
            # Exclusive creation cannot overwrite a pre-existing file or symlink.
            with _open_new(destination) as output:
                copied = _fingerprint(self.source_path, cap, output=output)
                self._check_fingerprint(copied)
                output.flush()
                os.fsync(output.fileno())
            staged = _fingerprint(destination, cap)
            if staged[:3] != (self.size, self.sha256, self.md5):
                _fail("outgoing_staging_changed", "待发送副本未通过大小与摘要校验。")
            return replace(self, path=destination, _identity=staged[3], _staged_root=staging)
        except OSError:
            _fail("outgoing_staging_failed", "无法创建可核验的私有发送副本。")

    def validate_staged(self) -> bool:
        if self._staged_root is None or self.path.parent != self._staged_root:
            _fail("outgoing_not_staged", "尚未创建独立的待发送副本。")
        _check_components(self._staged_root)
        cap = MAX_IMAGE_BYTES if self.kind == "image" else MAX_FILE_BYTES
        self._check_fingerprint(_fingerprint(self.path, cap), staged=True)
        return True

    @contextmanager
    def lease_staged(self):
        """Pin the staged file throughout a request, yielding this media object.

        Windows denies write/delete sharing for the held file handle. Other
        platforms retain a checked read handle and verify bytes/identity, but
        do not provide mandatory writer exclusion. No guarantee extends beyond
        the context's lifetime or to a client's later asynchronous reopening.
        """
        if self._staged_root is None or self.path.parent != self._staged_root:
            _fail("outgoing_not_staged", "尚未创建独立的待发送副本。")
        _check_components(self._staged_root)
        # Enter/close explicitly so caller OSErrors are not translated by the
        # checked-read context's own path/open error handling.
        lease = _open_read(self.path, sharing=0x1)
        stream = lease.__enter__()
        try:
            details = os.fstat(stream.fileno())
            if _identity(details) != self._identity:
                _fail("outgoing_staging_changed", "待发送副本在获取读取租约前发生变化。")
            self.validate_staged()
            yield self
            self.validate_staged()
            if _identity(os.fstat(stream.fileno())) != self._identity:
                _fail("outgoing_staging_changed", "待发送副本在读取租约期间发生变化。")
        finally:
            lease.__exit__(None, None, None)


def inspect_media(path: str, kind: str) -> PreparedMedia:
    if kind not in ("file", "image", "video"):
        _fail("invalid_outgoing_kind", "发送类型必须为 file、image 或 video。")
    if not isinstance(path, str):
        _fail()
    source = _absolute(path)
    cap = MAX_IMAGE_BYTES if kind == "image" else MAX_FILE_BYTES
    details = _fingerprint(source, cap, collect=kind == "image")
    size, sha, md5, identity, data = details
    metadata = (_image_metadata(data, source.suffix.lower()) if kind == "image" else
                _video_metadata(source, data) if kind == "video" else {})
    # The decoder opens a pathname too. Rebind the resulting metadata to the
    # exact file identity and bytes inspected before the decoder ran.
    if kind == "video" and _fingerprint(source, cap)[:4] != details[:4]:
        _fail("outgoing_source_changed", "发送视频在首帧校验期间发生变化。")
    return PreparedMedia(source, source, source.name, kind, size, sha, md5, metadata, identity)


def _xml(raw):
    """The bounded XML/Zstandard envelope used by attachment parsing, without keys."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_XML_BYTES:
            return None
        if raw.startswith(b"\x28\xb5\x2f\xfd"):
            try:
                with zstandard.ZstdDecompressor().stream_reader(BytesIO(raw)) as stream:
                    raw = stream.read(MAX_XML_BYTES + 1)
            except zstandard.ZstdError:
                return None
            if len(raw) > MAX_XML_BYTES:
                return None
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str) or len(raw) > MAX_XML_BYTES:
        return None
    text = raw.split("\x01\x00", 1)[0].replace("\x00", "").strip().lstrip("\ufeff")
    text = re.sub(r"^[A-Za-z0-9_.@-]{1,256}:\r?\n[ \t]{0,64}(?=<)", "", text, count=1)
    if not text.startswith("<") or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def receipt_matches(kind: str, raw: tuple, prepared: PreparedMedia) -> bool:
    """Match semantic original-byte hashes only; client conversion stays unknown."""
    if kind != prepared.kind or not _MD5.fullmatch(prepared.md5):
        return False
    if kind == "file":
        from .attachments import AttachmentUnavailable, parse_attachment
        try:
            info = parse_attachment(*raw)
            return bool(info and info.name == prepared.name and info.size == prepared.size
                        and info.md5 == prepared.md5.lower())
        except AttachmentUnavailable:
            return False
    if kind not in ("image", "video"):
        return False
    tag, fields = (("img", ("md5", "origimgmd5")) if kind == "image" else
                   ("videomsg", ("md5", "newmd5")))
    for value in raw:
        root = _xml(value)
        if root is None:
            continue
        node = root if root.tag == tag else root.find(tag) if root.tag == "msg" else None
        if node is None:
            continue
        for field_name in fields:
            digest = (node.get(field_name) or "").strip()
            if _MD5.fullmatch(digest) and digest.lower() == prepared.md5.lower():
                return True
    return False
