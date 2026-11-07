"""Content-free process identity and private failure diagnostics.

The manifest is captured when the server starts. It describes the source tree
at startup, not proof that every lazily imported module has executed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import traceback

from . import __version__

_PACKAGE = Path(__file__).resolve().parent
_STARTED_AT = datetime.now(timezone.utc).isoformat()


def source_identity(package: Path) -> dict:
    """Hash code only; never read config, keys, messages, or local state."""
    try:
        digest = hashlib.sha256()
        files = sorted(package.glob("*.py"))
        if not files:
            return {"sha256": None, "version": None, "available": False}
        version = None
        for path in files:
            data = path.read_bytes()
            digest.update(path.name.encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(data).digest())
            if path.name == "__init__.py":
                match = re.search(rb'^__version__\s*=\s*[\'"]([^\'"\r\n]+)[\'"]', data, re.M)
                version = match.group(1).decode("ascii") if match else None
        return {"sha256": digest.hexdigest(), "version": version, "available": True}
    except (OSError, UnicodeError):
        return {"sha256": None, "version": None, "available": False}


_STARTUP_SOURCE = source_identity(_PACKAGE)


def status() -> dict:
    installed = source_identity(_PACKAGE)
    comparable = _STARTUP_SOURCE["available"] and installed["available"]
    return {
        "pid": os.getpid(), "started_at": _STARTED_AT,
        "loaded_version": __version__, "installed_version": installed["version"],
        "startup_source_sha256": _STARTUP_SOURCE["sha256"],
        "installed_source_sha256": installed["sha256"],
        "source_identity_available": bool(comparable),
        "restart_required": (_STARTUP_SOURCE["sha256"] != installed["sha256"]
                             or __version__ != installed["version"]) if comparable else None,
        "identity_scope": "python_source_tree_at_server_import",
    }


def record_error(state: Path | None, method: str, error: Exception,
                 *, code: str, stage: str | None = None, retryable: bool = False) -> None:
    if not isinstance(state, Path):
        return
    # Exception messages and source lines can contain private SQL, paths or keys.
    diagnostic = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "method": method, "error_type": type(error).__name__, "code": code,
        "stage": stage, "retryable": retryable, "runtime": status(),
        "frames": [{"file": Path(frame.filename).name, "line": frame.lineno,
                    "function": frame.name}
                   for frame in traceback.extract_tb(error.__traceback__)],
    }
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".mcp-error-", suffix=".tmp", dir=state)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(diagnostic, stream, ensure_ascii=False)
        os.replace(temporary, state / "last_mcp_error.json")
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
