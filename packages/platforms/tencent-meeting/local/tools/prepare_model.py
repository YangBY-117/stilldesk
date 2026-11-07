"""Download a versioned CPU speech model into this application's own directory."""
from pathlib import Path
import hashlib
import json
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{NAME}.tar.bz2"
dest = ROOT / "models" / NAME
dest.mkdir(parents=True, exist_ok=True)
archive = dest.parent / (NAME + ".tar.bz2")
if not archive.exists():
    tmp = archive.with_suffix(".download")
    request = urllib.request.Request(URL, headers={"User-Agent": "MeetingAssistant-Local/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response, tmp.open("wb") as output:
        count = 0
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > 400 * 1024 * 1024:
                raise RuntimeError("Model archive exceeded the declared 400 MiB download budget.")
            output.write(chunk)
            if count % (20 * 1024 * 1024) == 0:
                print(f"Downloaded {count // (1024 * 1024)} MiB", flush=True)
    tmp.replace(archive)
with tarfile.open(archive, "r:bz2") as source:
    for member in source.getmembers():
        if member.isfile() and Path(member.name).name in {"model.int8.onnx", "tokens.txt", "LICENSE", "README.md"}:
            target = dest / Path(member.name).name
            if not target.exists():
                reader = source.extractfile(member)
                if reader is None:
                    raise RuntimeError("Missing archive payload")
                target.write_bytes(reader.read())
required = [dest / "model.int8.onnx", dest / "tokens.txt"]
if not all(f.exists() for f in required):
    raise RuntimeError("The pinned model archive did not contain the expected files.")
receipt = {"url": URL, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "files": [{"name": f.name, "bytes": f.stat().st_size, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()} for f in required]}
(dest / "download_receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
print(json.dumps({"ready": True, "directory": str(dest)}, ensure_ascii=False), flush=True)
