from io import BytesIO
import json
import sqlite3
import struct
import zlib

from PIL import Image

from qq_bridge.media import LocalImages
from conftest import field


def png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = BytesIO()
    Image.new("RGB", (200, 100), "red").save(stream, format="PNG")
    path.write_bytes(stream.getvalue())
    return stream.getvalue()


def image_body(path):
    return field(40800, field(45002, 2) + field(45812, str(path))
                 + field(45802, "PRIVATE_CDN_TOKEN"))


def test_indexed_image_returns_pixels_and_provenance_without_paths_or_tokens(scenario):
    reader, source = scenario
    path = source.account / "cache" / "test.png"
    original = png(path)
    source.add(11, payload=image_body(path))
    messages = reader.get_messages(f"qq:{source.account_tag}:group:synthetic-group")["messages"]
    image = next(message for message in messages if message["type"] == "image")
    result = reader.get_image(image["id"])
    assert result.metadata["ok"] is True and result.data
    assert result.metadata["originality"] == "unknown"
    assert result.metadata["source"]["id"] == image["id"]
    assert result.metadata["width"] == 200
    assert path.read_bytes() == original
    rendered = json.dumps(result.metadata)
    assert "PRIVATE_CDN_TOKEN" not in rendered and str(path) not in rendered
    with Image.open(BytesIO(result.data)) as picture:
        assert picture.size == (200, 100)
    path.unlink()
    assert reader.get_image(image["id"]).metadata["error"]["code"] == "image_not_cached"


def test_arbitrary_message_path_and_outside_account_image_are_rejected(scenario, tmp_path):
    reader, source = scenario
    outside = tmp_path / "private.png"
    png(outside)
    source.add(11, payload=image_body(outside))
    image = next(message for message in reader.get_messages(
        f"qq:{source.account_tag}:group:synthetic-group")["messages"] if message["type"] == "image")
    assert reader.get_image(image["id"]).metadata["ok"] is False
    captures = source.captures
    assert reader.get_image(str(outside)).metadata["error"]["code"] == "message_not_found"
    assert source.captures == captures
    assert LocalImages(source.account).read(["../private.png"], {}).metadata["ok"] is False


def test_bad_signature_is_not_returned(scenario):
    _, source = scenario
    bad = source.account / "cache" / "not-image.png"
    bad.parent.mkdir()
    bad.write_bytes(b"PRIVATE_TOKEN_NOT_IMAGE" * 100)
    result = LocalImages(source.account).read([str(bad)], {})
    assert result.metadata["ok"] is False and result.data is None
    assert "PRIVATE_TOKEN" not in json.dumps(result.metadata)


def test_oversized_pixel_header_is_rejected_before_loading_pixels(scenario):
    _, source = scenario
    path = source.account / "huge.png"
    data = bytearray(png(path))
    data[16:24] = struct.pack(">II", 100_000, 100_000)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]) & 0xFFFFFFFF)
    path.write_bytes(data)
    result = LocalImages(source.account).read([str(path)], {})
    assert result.metadata["ok"] is False and result.data is None


def test_file_byte_limit_and_opened_handle_account_boundary(scenario, tmp_path, monkeypatch):
    from qq_bridge import media
    _, source = scenario
    path = source.account / "small.png"
    png(path)
    monkeypatch.setattr(media, "MAX_FILE_BYTES", 32)
    assert LocalImages(source.account).read([str(path)], {}).metadata["ok"] is False
    monkeypatch.setattr(media, "MAX_FILE_BYTES", 32 * 1024 * 1024)
    outside = tmp_path / "outside.png"
    png(outside)
    images = LocalImages(source.account)
    # Simulate substitution after path validation: the opened handle still fails.
    monkeypatch.setattr(images, "_path", lambda raw: outside)
    result = images.read(["previously-validated-cache-path"], {})
    assert result.metadata["ok"] is False and result.data is None


def test_source_removed_or_changed_message_does_not_serve_stale_pixels(scenario):
    reader, source = scenario
    path = source.account / "image.png"
    png(path)
    source.add(11, payload=image_body(path))
    image = next(message for message in reader.get_messages(
        f"qq:{source.account_tag}:group:synthetic-group")["messages"] if message["type"] == "image")
    with sqlite3.connect(source.main) as conn:
        conn.execute('DELETE FROM group_msg_table WHERE "40001"=11')
    result = reader.get_image(image["id"])
    assert result.metadata["error"]["code"] == "message_source_changed"
    assert result.data is None


def test_source_failure_does_not_serve_unvalidated_old_pixels(scenario):
    reader, source = scenario
    path = source.account / "image.png"
    png(path)
    source.add(11, payload=image_body(path))
    image = next(message for message in reader.get_messages(
        f"qq:{source.account_tag}:group:synthetic-group")["messages"] if message["type"] == "image")
    source.fail = True
    result = reader.get_image(image["id"])
    assert result.metadata["ok"] is False and result.data is None
