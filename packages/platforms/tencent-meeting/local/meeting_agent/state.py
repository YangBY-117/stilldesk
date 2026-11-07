"""Private mutable state, separate from source/model installation paths."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def current_user_sid() -> str:
    result = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"], check=True,
                            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    match = re.search(rb"S-1-5-[0-9-]+", result.stdout)
    if not match:
        raise RuntimeError("Cannot resolve the current user for private meeting storage")
    return match.group().decode("ascii")


def _windows_private(path: Path, directory: bool) -> None:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    get_dacl = advapi.GetSecurityDescriptorDacl
    get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    get_dacl.restype = wintypes.BOOL
    get_owner = advapi.GetSecurityDescriptorOwner
    get_owner.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    get_owner.restype = wintypes.BOOL
    set_security = advapi.SetNamedSecurityInfoW
    set_security.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    set_security.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    flags = "OICI" if directory else ""
    descriptor = ctypes.c_void_p()
    sid = current_user_sid()
    sddl = f"O:{sid}D:P(A;{flags};FA;;;SY)(A;{flags};FA;;;{sid})"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present.value:
            raise RuntimeError("Could not construct a private meeting DACL")
        owner = ctypes.c_void_p()
        if not get_owner(descriptor, ctypes.byref(owner), ctypes.byref(defaulted)):
            raise RuntimeError("Could not construct a private meeting owner")
        # Set OWNER and protected DACL in one call; a former owner must not keep WRITE_DAC.
        error = set_security(str(path), 1, 0x00000001 | 0x00000004 | 0x80000000, owner, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(descriptor)


def private_directory(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("Private meeting directories must not be symbolic links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("Private meeting storage is not a regular directory")
    if os.name == "nt":
        _windows_private(path, True)
    else:
        if path.stat().st_uid != os.getuid():
            raise ValueError("Private meeting directory is owned by another user")
        os.chmod(path, 0o700)
    return path


def private_file(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Private meeting state must be a regular file")
    if os.name == "nt":
        _windows_private(path, False)
    else:
        if path.stat().st_uid != os.getuid():
            raise ValueError("Private meeting file is owned by another user")
        os.chmod(path, 0o600)
    return path


def state_root() -> Path:
    configured = os.environ.get("STILLDESK_HOME")
    home = Path(configured) if configured else Path.home() / ".stilldesk"
    if not home.is_absolute():
        raise ValueError("STILLDESK_HOME must be an absolute directory")
    return private_directory(home / "meeting")


def runtime_directory() -> Path:
    return private_directory(state_root() / "runtime")


def sessions_directory() -> Path:
    return private_directory(state_root() / "sessions")


def atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    private_directory(path.parent)
    handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary)
    try:
        # The temp inherits the private parent DACL, and has mode 0600 on POSIX.
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        private_file(temporary)
        os.replace(temporary, path)
        private_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_private_json(path: Path) -> dict:
    path = private_file(Path(path))
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid private meeting state")
    return value
