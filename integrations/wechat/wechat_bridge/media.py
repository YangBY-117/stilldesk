"""Account-scoped local images; no CDN requests or Weixin UI automation.

DAT layout and AES candidate discovery reference: wechatauto-replica,
d322c4a9b1124228820bab363e5676016f188fc6 (Apache-2.0). See THIRD_PARTY.md.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import hashlib
from io import BytesIO
import os
from pathlib import Path
import re
import struct
import subprocess
import time
import warnings

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, ImageOps, UnidentifiedImageError
import psutil
import zstandard

from .keys import DiscoveryError, _ReadProcess, load_keys, save_keys

V1_MAGIC = b"\x07\x08\x05\x56\x02\x05"
V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 40_000_000
_HASH = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_ASCII_KEY = re.compile(rb"(?<![0-9A-Za-z])[0-9A-Za-z]{16,32}(?![0-9A-Za-z])")
_UTF16_KEY = re.compile(rb"(?:[0-9A-Za-z]\x00){16,32}")


class _ModuleEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("module_id", wintypes.DWORD),
                ("pid", wintypes.DWORD), ("global_usage", wintypes.DWORD),
                ("process_usage", wintypes.DWORD), ("base", ctypes.c_void_p),
                ("bytes", wintypes.DWORD), ("module", wintypes.HMODULE),
                ("name", wintypes.WCHAR * 256), ("path", wintypes.WCHAR * 260)]


def _weixin_module(process: _ReadProcess, pid: int, executable: Path):
    kernel = process.kernel
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Module32FirstW", "Module32NextW"):
        getattr(kernel, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(_ModuleEntry)]
    handle = kernel.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    try:
        module = _ModuleEntry()
        module.size = ctypes.sizeof(module)
        more = kernel.Module32FirstW(handle, ctypes.byref(module))
        while more:
            if (module.name.lower() == "weixin.dll"
                    and Path(module.path).resolve().is_relative_to(executable.parent)):
                return module.base, module.bytes
            more = kernel.Module32NextW(handle, ctypes.byref(module))
    finally:
        kernel.CloseHandle(handle)
    return None


def _config_image_key(process: _ReadProcess, pid: int, executable: Path,
                      account: Path, probe: bytes) -> tuple[bytes, int] | None:
    """Try version-sensitive pointers only when account AND real image agree."""
    module = _weixin_module(process, pid, executable)
    if not module:
        return None
    base, size = module
    if not 0 < size < 512 * 1024 * 1024:
        return None
    self_id = re.sub(r"_\w{4}$", "", account.name).encode("utf-8")

    def pointer(address):
        value = process.read(address, 8)
        if len(value) == 8:
            result = struct.unpack("<Q", value)[0]
            if 0x10000 <= result < 0x800000000000:
                return result
        return None

    back_offsets = list(dict.fromkeys([0x138, 0x130] + list(range(0x100, 0x181, 8))))
    for offset in range(0, size, 4 * 1024 * 1024):
        start = max(0, offset - 64)
        block = process.read(base + start, min(size - start, 4 * 1024 * 1024 + 64))
        for match in re.finditer(b"global_config", block):
            address = base + start + match.start()
            header = process.read(address, 32)
            if len(header) != 32 or struct.unpack_from("<QQ", header, 16) != (13, 15):
                continue
            for back in back_offsets:
                owner = pointer(address + 16 - back)
                if owner is None:
                    continue
                for field in (0x68, 0x60, 0x70, 0x58, 0x78):
                    config = pointer(owner + field)
                    if config is None:
                        continue
                    info = process.read(config + 0x40, 40)
                    if len(info) != 40:
                        continue
                    dword = struct.unpack_from("<I", info)[0]
                    length = struct.unpack_from("<Q", info, 24)[0]
                    if length != len(self_id):
                        continue
                    name = info[8:8 + length] if length <= 15 else process.read(struct.unpack_from("<Q", info, 8)[0], length)
                    if name != self_id:
                        continue
                    key = hashlib.md5(str(dword).encode("ascii") + name).hexdigest()[:16].encode("ascii")
                    if _valid_key(key, probe):
                        return key, dword & 0xFF
    return None


class ImageUnavailable(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ImageResult:
    metadata: dict
    data: bytes = b""


def image_hashes(*values) -> list[str]:
    """Extract only file identities, never return the private media XML/blob."""
    found = []
    for value in values:
        if isinstance(value, str):
            value = value.encode("utf-8")
        if not isinstance(value, bytes) or len(value) > 2_000_000:
            continue
        if value.startswith(b"\x28\xb5\x2f\xfd"):
            try:
                value = zstandard.ZstdDecompressor().decompress(value, max_output_size=2_000_000)
            except zstandard.ZstdError:
                continue
        for match in _HASH.finditer(value):
            digest = match.group().decode("ascii").lower()
            if digest not in found:
                found.append(digest)
    return found[:32]


def image_kind(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"wxgf"):
        return "wxgf"
    return None


def _aes(data: bytes, key: bytes) -> bytes:
    decoder = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decoder.update(data) + decoder.finalize()


def _valid_key(key: bytes, data: bytes) -> bool:
    if len(key) != 16 or len(data) < 31 or not data.startswith(V2_MAGIC):
        return False
    return image_kind(_aes(data[15:31], key)) is not None


def _xor(data: bytes, key: int) -> bytes:
    return data.translate(bytes(value ^ key for value in range(256)))


def _legacy_body(data: bytes) -> bytes:
    for magic in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF89a", b"GIF87a", b"RIFF", b"wxgf"):
        key = data[0] ^ magic[0]
        if _xor(data[:len(magic)], key) == magic:
            plain = _xor(data, key)
            if image_kind(plain):
                return plain
    raise ImageUnavailable("unsupported_image_format", "本地图片格式尚不支持。")


def decrypt_image(data: bytes, key: bytes | None = None, xor_key: int | None = None) -> bytes:
    if not data or len(data) > MAX_FILE_BYTES:
        raise ImageUnavailable("invalid_image_file", "图片为空或超过 32 MiB 读取上限。")
    if image_kind(data):
        return data
    if data.startswith(V1_MAGIC):
        if len(data) <= 22:
            raise ImageUnavailable("incomplete_image", "本地图片文件不完整。")
        return _legacy_body(data[22:])
    if not data.startswith(V2_MAGIC):
        return _legacy_body(data)
    if len(data) < 31:
        raise ImageUnavailable("incomplete_image", "本地图片文件不完整。")
    aes_size, xor_size = struct.unpack_from("<II", data, 6)
    encrypted_size = (aes_size // 16 + 1) * 16
    split = 15 + encrypted_size
    if split + xor_size > len(data):
        raise ImageUnavailable("incomplete_image", "本地图片文件尚未完整缓存。")
    if key is None or not _valid_key(key, data):
        raise ImageUnavailable("image_key_unavailable", "图片读取密钥暂不可用；保持微信登录后重试。")
    prefix = _aes(data[15:split], key)
    padding = encrypted_size - aes_size
    if prefix[-padding:] != bytes([padding]) * padding:
        raise ImageUnavailable("invalid_image_padding", "本地图片解密校验失败。")
    prefix = prefix[:aes_size]
    middle = data[split:len(data) - xor_size] if xor_size else data[split:]
    if not xor_size:
        return prefix + middle
    tail = data[-xor_size:]
    kind = image_kind(prefix + middle)
    marker = {"jpeg": b"\xff\xd9", "png": b"IEND\xaeB`\x82"}.get(kind)
    if xor_key is not None:
        plain = prefix + middle + _xor(tail, xor_key)
        if marker and not plain.endswith(marker):
            end = plain.rfind(marker)
            if end >= 0 and len(plain) - end - len(marker) <= 32:
                plain = plain[:end + len(marker)]
        return plain
    if marker:
        # A small container footer can follow the image terminator.
        for footer in range(min(32, len(tail) - len(marker)) + 1):
            end = len(tail) - footer
            candidate = tail[end - len(marker)] ^ marker[0]
            if _xor(tail[end - len(marker):end], candidate) == marker:
                return prefix + middle + _xor(tail[:end], candidate)
    raise ImageUnavailable("unsupported_image_format", "该图片容器暂无法完整解码；尝试本地其他副本。")


def _wxgf_pixels(data: bytes) -> bytes:
    start = data.find(b"\0\0\0\1")
    if start < 0:
        raise ImageUnavailable("invalid_image_file", "wxgf 副本中没有可识别的图片编码流。")
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run(
            [executable, "-nostdin", "-v", "error", "-xerror", "-threads", "1",
             "-max_pixels", str(MAX_PIXELS), "-protocol_whitelist", "pipe",
             "-f", "hevc", "-i", "pipe:0", "-frames:v", "1", "-an", "-sn",
             "-threads", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"],
            input=data[start:], capture_output=True, timeout=15,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if result.returncode or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ImageUnavailable("invalid_image_file", "wxgf 图片编码流损坏或不完整。")
        return result.stdout
    except (ImportError, RuntimeError, FileNotFoundError) as error:
        raise ImageUnavailable("image_decoder_unavailable", "本机 wxgf 图片解码器不可用；尝试本地其他副本。") from error
    except subprocess.TimeoutExpired as error:
        raise ImageUnavailable("image_decode_timeout", "wxgf 图片解码超时；尝试本地其他副本。") from error


def render_image(data: bytes, max_edge: int) -> tuple[bytes, dict]:
    """Decode completely before returning pixels; strip transport/EXIF metadata."""
    source_encoding = image_kind(data)
    if source_encoding == "wxgf":
        data = _wxgf_pixels(data)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > MAX_PIXELS:
                    raise ImageUnavailable("image_too_large", "图片超过 4000 万像素读取上限。")
                source_width, source_height = image.size
                source_format = image.format
                animated = getattr(image, "n_frames", 1) > 1
                image.load()  # Truncated/corrupt files must not pass as readable.
                image = ImageOps.exif_transpose(image)
                oriented_size = image.size
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                output = BytesIO()
                if source_format == "PNG" or image.mode in ("RGBA", "LA") or "transparency" in image.info:
                    image.convert("RGBA").save(output, format="PNG")
                    mime = "image/png"
                else:
                    image.convert("RGB").save(output, format="JPEG", quality=92)
                    mime = "image/jpeg"
                encoded = output.getvalue()
                if len(encoded) > 10 * 1024 * 1024:
                    raise ImageUnavailable("image_too_large", "图片输出超过 10 MiB；请降低 max_edge。")
                return encoded, {
                    "mime_type": mime, "width": image.width, "height": image.height,
                    "source_width": source_width, "source_height": source_height,
                    "resized": image.size != oriented_size,
                    "first_frame_only": animated or source_encoding == "wxgf", "bytes": len(encoded),
                    "source_encoding": source_encoding,
                }
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise ImageUnavailable("invalid_image_file", "本地图片损坏、不完整或解码超出限制。") from error


class LocalImages:
    def __init__(self, account: Path, state: Path, executable: Path):
        self.account = account.resolve()
        self.state = state.resolve()
        self.executable = executable.resolve()
        self._key: bytes | None = None
        self._xor_key: int | None = None

    def _key_for(self, data: bytes, require_xor: bool = False) -> bytes:
        if self._key and _valid_key(self._key, data) and (not require_xor or self._xor_key is not None):
            return self._key
        store = self.state / "image_keys.dpapi"
        if store.exists():
            saved = load_keys(store, self.account)
            for key in saved.values():
                if _valid_key(key, data):
                    self._key = key
                    xor_bytes = saved.get("xor:" + hashlib.sha256(key).hexdigest()[:16])
                    self._xor_key = xor_bytes[0] if xor_bytes is not None and len(xor_bytes) == 1 else None
                    if not require_xor or self._xor_key is not None:
                        return key
        key = self._discover_key(data)
        previous = load_keys(store, self.account) if store.exists() else {}
        identity = hashlib.sha256(key).hexdigest()[:16]
        previous[identity] = key
        if self._xor_key is not None:
            previous["xor:" + identity] = bytes([self._xor_key])
        save_keys(store, self.account, previous)
        self._key = key
        if require_xor and self._xor_key is None:
            raise ImageUnavailable("image_xor_key_unavailable", "该图片的完整读取密钥暂不可用；尝试本地其他副本。")
        return key

    def _discover_key(self, probe: bytes) -> bytes:
        if os.name != "nt":
            raise ImageUnavailable("image_key_unavailable", "加密图片的首次读取需要 Windows 微信进程。")
        deadline = time.monotonic() + 30
        tested = set()
        processes = []
        for process in psutil.process_iter(["pid", "name", "exe", "memory_info"]):
            if ((process.info["name"] or "").lower() == "weixin.exe"
                    and process.info["exe"]
                    and Path(process.info["exe"]).resolve() == self.executable):
                processes.append((process.info["memory_info"].rss, process.info["pid"]))
        # Try every matching process's account-bound config before a costly
        # generic scan: a large renderer process may precede the main process.
        for _, pid in sorted(processes, reverse=True):
            try:
                process = _ReadProcess(pid)
            except DiscoveryError:
                continue
            try:
                derived = _config_image_key(process, pid, self.executable, self.account, probe)
                if derived:
                    self._xor_key = derived[1]
                    return derived[0]
            finally:
                process.close()
        for _, pid in sorted(processes, reverse=True):
            if time.monotonic() > deadline:
                break
            try:
                process = _ReadProcess(pid)
            except DiscoveryError:
                continue
            try:
                for _, block in process.blocks():
                    if time.monotonic() > deadline:
                        break
                    for pattern in (_ASCII_KEY, _UTF16_KEY):
                        for match in pattern.finditer(block):
                            candidate = match.group()[:16] if pattern is _ASCII_KEY else match.group()[::2][:16]
                            if candidate not in tested:
                                tested.add(candidate)
                                if _valid_key(candidate, probe):
                                    self._xor_key = None
                                    return candidate
                            if time.monotonic() > deadline:
                                break
            finally:
                process.close()
            if time.monotonic() > deadline:
                break
        raise ImageUnavailable("image_key_unavailable", "未找到有效图片读取密钥；保持微信登录，在微信中查看一张图片后重试。")

    def candidates(self, chat: str, created: int, hashes: list[str]):
        chat_hash = chat[5:] if re.fullmatch(r"hash:[0-9a-f]{32}", chat) else hashlib.md5(chat.encode()).hexdigest()
        attach = self.account / "msg" / "attach"
        base = attach / chat_hash
        if not base.resolve().is_relative_to(attach) or not attach.resolve().is_relative_to(self.account):
            raise PermissionError("Image directory escapes selected account")
        if not base.is_dir():
            return
        from datetime import datetime, timezone, timedelta
        month = datetime.fromtimestamp(created, timezone(timedelta(hours=8))).strftime("%Y-%m")
        months = sorted(p for p in base.iterdir() if re.fullmatch(r"\d{4}-\d{2}", p.name))
        months.sort(key=lambda p: p.name != month)
        # Prefer the original tier globally; never silently label thumbnails original.
        for suffix, tier in (("_h.dat", "original"), (".dat", "local"), ("_t.dat", "thumbnail")):
            for digest in hashes:
                for directory in months:
                    path = directory / "Img" / (digest + suffix)
                    resolved = path.resolve()
                    if not resolved.is_relative_to(base):
                        raise PermissionError("Image file escapes selected conversation")
                    if resolved.is_file():
                        yield resolved, tier

    def get(self, message: dict, hashes: list[str], max_edge: int = 2048) -> ImageResult:
        if type(max_edge) is not int or not 512 <= max_edge <= 4096:
            raise ValueError("max_edge 必须为 512 到 4096 的整数。")
        if not hashes:
            raise ImageUnavailable("image_reference_missing", "这条消息没有可识别的本地图片引用。")
        failure = None
        for path, tier in self.candidates(message["chat"], message["created"], hashes):
            try:
                with path.open("rb") as stream:
                    before = os.fstat(stream.fileno())
                    data = stream.read(MAX_FILE_BYTES + 1)
                    after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ImageUnavailable("image_changing", "图片仍在写入缓存，请稍后重试。")
                if not data or len(data) > MAX_FILE_BYTES:
                    raise ImageUnavailable("invalid_image_file", "图片为空或超过 32 MiB 读取上限。")
                key = self._key_for(data) if data.startswith(V2_MAGIC) else None
                if key and image_kind(_aes(data[15:31], key)) in ("wxgf", "gif", "webp"):
                    key = self._key_for(data, require_xor=True)
                plain = decrypt_image(data, key, self._xor_key)
                encoded, metadata = render_image(plain, max_edge)
                output_dir = self.state / "media"
                if not output_dir.resolve().is_relative_to(self.state):
                    raise PermissionError("Image output escapes private cache")
                output_dir.mkdir(exist_ok=True)
                digest = hashlib.sha256(encoded).hexdigest()
                output = output_dir / (digest + (".png" if metadata["mime_type"] == "image/png" else ".jpg"))
                if not output.resolve().is_relative_to(output_dir):
                    raise PermissionError("Image output escapes private cache")
                temp = output.with_suffix(output.suffix + ".tmp")
                temp.write_bytes(encoded)
                os.replace(temp, output)
                metadata.update({"ok": True, "message_id": message["id"], "tier": tier,
                                 "thumbnail_only": tier == "thumbnail", "sha256": digest,
                                 "local_path": str(output), "untrusted_data": True,
                                 "text_searchable": False})
                return ImageResult(metadata, encoded)
            except ImageUnavailable as error:
                failure = error
        if failure:
            raise failure
        raise ImageUnavailable("image_not_cached", "这张图片未缓存在当前电脑；请在微信中打开该图片后重试。")
