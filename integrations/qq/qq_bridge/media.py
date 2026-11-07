"""Account-root-confined local image reads, without network or executable calls."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import hashlib
from pathlib import Path
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ImageResult:
    metadata: dict
    data: bytes | None = None


def _signature(data: bytes) -> bool:
    return (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith((b"GIF87a", b"GIF89a"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP"))


class LocalImages:
    def __init__(self, account: Path):
        self.account = Path(account).resolve()

    def _path(self, raw: str) -> Path:
        if not isinstance(raw, str) or not raw or len(raw) > 4096 or "\x00" in raw:
            raise ValueError("invalid cache path")
        candidate = Path(raw)
        if ".." in candidate.parts:
            raise ValueError("cache traversal path")
        if not candidate.is_absolute():
            candidate = self.account / candidate
        resolved = candidate.resolve(strict=True)
        if resolved == self.account or not resolved.is_relative_to(self.account):
            raise ValueError("cache path outside account")
        # Reject junctions/reparse points, including in an otherwise in-root path.
        current = self.account
        for part in candidate.absolute().relative_to(self.account).parts:
            current = current / part
            info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("cache path has reparse component")
        return resolved

    def read(self, paths: list[str], source: dict, max_edge: int = 2048) -> ImageResult:
        if type(max_edge) is not int or not 512 <= max_edge <= 4096:
            raise ValueError("invalid image edge")
        base = {"platform": "qq", "source": source, "local_only": True,
                "originality": "unknown"}
        saw_unsupported = False
        for raw in paths[:16]:
            try:
                path = self._path(raw)
                size = path.stat().st_size
                if not 0 < size <= MAX_FILE_BYTES:
                    saw_unsupported = True
                    continue
                with path.open("rb") as stream:
                    self._check_open_file(stream)
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES or not _signature(data):
                    saw_unsupported = True
                    continue
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(BytesIO(data)) as picture:
                        if picture.width * picture.height > MAX_PIXELS:
                            saw_unsupported = True
                            continue
                        width, height = picture.size
                        picture.seek(0)
                        frame = ImageOps.exif_transpose(picture).convert("RGB")
                        frame.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                        output = BytesIO()
                        # Re-encoding strips metadata and returns only image pixels.
                        frame.save(output, format="JPEG", quality=88)
                        pixels = output.getvalue()
                        if len(pixels) > MAX_OUTPUT_BYTES:
                            saw_unsupported = True
                            continue
                        return ImageResult({**base, "ok": True, "status": "local_cache",
                            "mime_type": "image/jpeg", "width": frame.width, "height": frame.height,
                            "source_width": width, "source_height": height,
                            "sha256": hashlib.sha256(pixels).hexdigest(),
                            "first_frame_only": getattr(picture, "n_frames", 1) > 1}, pixels)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
                    Image.DecompressionBombWarning):
                saw_unsupported = True
        code = "unsupported_image_cache" if saw_unsupported else "image_not_cached"
        return ImageResult({**base, "ok": False, "error": {"code": code,
            "message": "本机图片缓存不可用或格式未支持；未联网获取图片。"}})

    def _check_open_file(self, stream):
        """Check the opened handle too, closing the path/symlink substitution race."""
        import os
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt
            function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
            function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            function.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            size = function(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
            if not 0 < size < len(buffer):
                raise OSError("cannot verify image handle")
            raw = buffer.value
            if raw.startswith("\\\\?\\UNC\\"):
                raw = "\\\\" + raw[8:]
            elif raw.startswith("\\\\?\\"):
                raw = raw[4:]
            opened = Path(raw).resolve()
        else:
            descriptor = Path("/proc/self/fd") / str(stream.fileno())
            if not descriptor.exists():
                raise OSError("cannot verify image handle")
            opened = descriptor.resolve()
        if opened == self.account or not opened.is_relative_to(self.account):
            raise ValueError("opened image outside account")
