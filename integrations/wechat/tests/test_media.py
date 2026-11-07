"""Synthetic images exercise decryption, corruption and account boundaries."""
from io import BytesIO
import hashlib
import os
from pathlib import Path
import struct
import subprocess

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
import pytest
import zstandard

from wechat_bridge.media import (
    ImageUnavailable, LocalImages, V1_MAGIC, V2_MAGIC, decrypt_image, image_hashes,
    render_image, _config_image_key,
)

KEY = b"SyntheticKey1234"


def picture(kind="JPEG", size=(120, 80)):
    stream = BytesIO()
    Image.new("RGB", size, "cornflowerblue").save(stream, format=kind)
    return stream.getvalue()


def v2(plain, *, key=KEY, xor_key=0x39, aes_size=128, xor_size=64):
    aes_size = min(aes_size, len(plain) - xor_size)
    padding = 16 - aes_size % 16
    encoder = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    first = encoder.update(plain[:aes_size] + bytes([padding]) * padding) + encoder.finalize()
    tail = bytes(value ^ xor_key for value in plain[-xor_size:]) if xor_size else b""
    middle = plain[aes_size:-xor_size] if xor_size else plain[aes_size:]
    return V2_MAGIC + struct.pack("<II", aes_size, xor_size) + b"\1" + first + middle + tail


@pytest.mark.parametrize("kind", ["JPEG", "PNG"])
@pytest.mark.parametrize("footer", [b"", b"container-footer"])
def test_v2_roundtrip_and_render(kind, footer):
    plain = picture(kind)
    encrypted = v2(plain + footer)
    assert decrypt_image(encrypted, KEY) == plain
    rendered, meta = render_image(plain, 512)
    with Image.open(BytesIO(rendered)) as image:
        image.load()
        assert image.size == (120, 80)
    assert meta["resized"] is False
    assert meta["mime_type"] == ("image/png" if kind == "PNG" else "image/jpeg")


@pytest.mark.parametrize("xor_size", [0, 17, 64])
def test_v2_padded_block_and_zero_tail(xor_size):
    plain = picture()
    assert decrypt_image(v2(plain, aes_size=128, xor_size=xor_size), KEY) == plain


@pytest.mark.parametrize("key", [None, b"WrongImageKey123"])
def test_v2_wrong_or_missing_key(key):
    with pytest.raises(ImageUnavailable, match="密钥"):
        decrypt_image(v2(picture()), key)


@pytest.mark.parametrize("data", [V2_MAGIC, V2_MAGIC + struct.pack("<II", 2**31, 12) + b"\0" * 20])
def test_reject_invalid_lengths(data):
    with pytest.raises(ImageUnavailable):
        decrypt_image(data, KEY)


@pytest.mark.parametrize("prefix", [b"", V1_MAGIC + b"unused-key-field"])
def test_legacy_xor_and_plain(prefix):
    plain = picture()
    encrypted = prefix + bytes(value ^ 0xA7 for value in plain)
    assert decrypt_image(encrypted) == plain
    assert decrypt_image(plain) == plain


def test_image_id_extraction_excludes_credentials_and_decompresses():
    identity = "a1" * 16
    blob = zstandard.ZstdCompressor().compress(f'<img md5="{identity}" aeskey="{"b" * 64}"/>'.encode())
    assert image_hashes(blob, identity) == [identity]


def test_render_rejects_truncated_jpeg_and_bounds_size():
    with pytest.raises(ImageUnavailable):
        render_image(picture()[:-100], 512)
    _, meta = render_image(picture(size=(1600, 800)), 512)
    assert (meta["width"], meta["height"]) == (512, 256)
    assert meta["resized"]


def test_prefer_original_fallback_to_thumbnail_without_touching_source(tmp_path):
    account, state = tmp_path / "account", tmp_path / "private"
    state.mkdir()
    images = LocalImages(account, state, tmp_path / "Weixin.exe")
    chat, identity = "synthetic@chatroom", "ab" * 16
    directory = account / "msg/attach" / hashlib.md5(chat.encode()).hexdigest() / "2026-10/Img"
    directory.mkdir(parents=True)
    original = directory / (identity + "_h.dat")
    thumbnail = directory / (identity + "_t.dat")
    original.write_bytes(b"broken")
    thumbnail.write_bytes(picture())
    before = {p: p.read_bytes() for p in (original, thumbnail)}
    message = {"id": "test-message", "chat": chat, "created": 1792350000}
    result = images.get(message, [identity])
    assert result.metadata["thumbnail_only"] is True
    assert result.metadata["tier"] == "thumbnail"
    assert Path(result.metadata["local_path"]).is_relative_to(state)
    assert before == {p: p.read_bytes() for p in (original, thumbnail)}
    original.write_bytes(picture(size=(300, 200)))
    assert images.get(message, [identity]).metadata["tier"] == "original"
    with pytest.raises(ValueError):
        images.get(message, [identity], True)
    with pytest.raises(ImageUnavailable) as failure:
        images.get(message, ["cd" * 16])
    assert failure.value.code == "image_not_cached"


def test_account_boundary_rejects_external_attachment_link(tmp_path):
    account, state, outside = (tmp_path / name for name in ("account", "private", "other-account"))
    account.mkdir()
    state.mkdir()
    outside.mkdir()
    (account / "msg").mkdir()
    try:
        (account / "msg/attach").symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(account / "msg/attach"))
        else:
            pytest.skip("Creating directory links is unavailable for this user")
    images = LocalImages(account, state, tmp_path / "Weixin.exe")
    with pytest.raises(PermissionError):
        list(images.candidates("chat", 1792350000, ["ab" * 16]))


@pytest.mark.parametrize("matching_account", [True, False])
def test_config_derivation_verifies_account_and_real_image(monkeypatch, tmp_path, matching_account):
    import wechat_bridge.media as media
    base, owner, config = 0x100000, 0x200000, 0x300000
    name = b"wxid_owner" if matching_account else b"wxid_other"
    dword = 123456789
    key = hashlib.md5(str(dword).encode() + b"wxid_owner").hexdigest()[:16].encode()
    module = bytearray(1024)
    anchor = 0x200
    module[anchor:anchor + 13] = b"global_config"
    struct.pack_into("<QQ", module, anchor + 16, 13, 15)
    struct.pack_into("<Q", module, anchor + 16 - 0x138, owner)
    info = bytearray(40)
    struct.pack_into("<I", info, 0, dword)
    info[8:8 + len(name)] = name
    struct.pack_into("<QQ", info, 24, len(name), 15)

    class Process:
        def read(self, address, length):
            for start, value in ((base, module), (owner + 0x68, struct.pack("<Q", config)),
                                 (config + 0x40, info)):
                if start <= address and address + length <= start + len(value):
                    return bytes(value[address - start:address - start + length])
            return b""

    monkeypatch.setattr(media, "_weixin_module", lambda *args: (base, len(module)))
    found = _config_image_key(Process(), 7, tmp_path / "Weixin.exe",
                              tmp_path / "wxid_owner_a1b2", v2(picture(), key=key))
    assert found == ((key, dword & 0xFF) if matching_account else None)


def test_config_discovery_checks_main_process_before_scanning_renderer(monkeypatch, tmp_path):
    import wechat_bridge.media as media
    from types import SimpleNamespace
    images = LocalImages(tmp_path / "account", tmp_path / "state", tmp_path / "Weixin.exe")
    processes = [SimpleNamespace(info={"pid": pid, "name": "Weixin.exe",
                 "exe": str(images.executable), "memory_info": SimpleNamespace(rss=rss)})
                 for pid, rss in ((1, 10000), (2, 100))]
    monkeypatch.setattr(media.psutil, "process_iter", lambda *args: processes)

    class Process:
        def __init__(self, pid): self.pid = pid
        def close(self): pass
        def blocks(self): raise AssertionError("Renderer scanned before checking main process")

    monkeypatch.setattr(media, "_ReadProcess", Process)
    monkeypatch.setattr(media, "_config_image_key", lambda process, *args: (KEY, 0x39) if process.pid == 2 else None)
    assert images._discover_key(v2(picture())) == KEY


def test_wxgf_hevc_pixels_from_encrypted_container():
    import imageio_ffmpeg
    encoded = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-f", "lavfi",
         "-i", "color=c=red:s=64x32:d=1", "-frames:v", "1", "-c:v", "libx265",
         "-x265-params", "log-level=error:pools=1:frame-threads=1", "-f", "hevc", "pipe:1"],
        capture_output=True, timeout=15, creationflags=0x08000000 if os.name == "nt" else 0,
    )
    assert encoded.returncode == 0
    plain = b"wxgf" + b"\0" * 20 + encoded.stdout
    decrypted = decrypt_image(v2(plain), KEY, xor_key=0x39)
    assert decrypted == plain
    pixels, metadata = render_image(decrypted, 512)
    assert metadata["source_encoding"] == "wxgf"
    assert (metadata["width"], metadata["height"]) == (64, 32)
    with Image.open(BytesIO(pixels)) as image:
        assert image.getpixel((32, 16))[0] > 200


def test_wxgf_missing_stream_is_explicitly_unavailable():
    with pytest.raises(ImageUnavailable) as error:
        render_image(b"wxgf-not-a-video-stream", 512)
    assert error.value.code == "invalid_image_file"
