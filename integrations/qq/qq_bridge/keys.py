"""Read-only account-bound QQNT keyspec recovery and current-user DPAPI.

Shared Windows primitives adapted from this workspace own WeChat bridge.
QQ candidates must match the selected DB salt and page authentication.
No process writes, debugger, injection, credential output or network access.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time

import psutil

_HEX = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")


class DiscoveryError(RuntimeError):
    pass


def restrict_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
        return
    who = subprocess.check_output(["whoami.exe"], text=True, creationflags=0x08000000).strip()
    result = subprocess.run(
        ["icacls.exe", str(path.resolve()), "/inheritance:r", "/grant:r",
         f"{who}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
        capture_output=True, creationflags=0x08000000,
    )
    if result.returncode:
        raise DiscoveryError("无法限制本地缓存目录权限，停止保存消息和密钥。")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise DiscoveryError("密钥保存仅支持 Windows 当前用户 DPAPI。")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    inp_buf = ctypes.create_string_buffer(data)
    inp = _Blob(len(data), ctypes.cast(inp_buf, ctypes.POINTER(ctypes.c_ubyte)))
    out = _Blob()
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        ok = fn(ctypes.byref(inp), None, None, None, None, 1, ctypes.byref(out))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        ok = fn(ctypes.byref(inp), "QQ Agent Bridge", None, None, None, 1, ctypes.byref(out))
    if not ok:
        raise DiscoveryError("Windows 当前用户密钥保护失败。")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel.LocalFree(out.pbData)


def load_keys(path: Path, account: Path) -> dict[str, bytes]:
    if not path.exists():
        return {}
    obj = json.loads(_dpapi(path.read_bytes(), decrypt=True))
    if obj.get("account") != str(account.resolve()):
        raise DiscoveryError("密钥缓存与所选QQ账号不匹配。")
    return {rel: bytes.fromhex(value) for rel, value in obj["keys"].items()}


def save_keys(path: Path, account: Path, keys: dict[str, bytes]) -> None:
    if not keys:
        raise DiscoveryError("没有已验证的密钥，拒绝覆盖缓存。")
    raw = json.dumps({"account": str(account.resolve()),
                      "keys": {rel: key.hex() for rel, key in keys.items()}}).encode()
    encrypted = _dpapi(raw)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(encrypted)
    os.replace(tmp, path)


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("padding1", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("padding2", wintypes.DWORD)]


class _ReadProcess:
    def __init__(self, pid: int):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                    ctypes.POINTER(_MBI), ctypes.c_size_t]
        k.VirtualQueryEx.restype = ctypes.c_size_t
        k.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.handle = k.OpenProcess(0x0010 | 0x0400, False, pid)
        if not self.handle:
            raise DiscoveryError("无法只读访问QQ进程；请确认QQ与桥接程序以同一用户权限运行。")
        self.bytes_read = 0

    def close(self):
        self.kernel.CloseHandle(self.handle)

    def read(self, address: int, size: int) -> bytes:
        if not 0 < size <= 8 * 1024 * 1024:
            return b""
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        self.kernel.ReadProcessMemory(self.handle, address, buf, size, ctypes.byref(got))
        self.bytes_read += got.value
        return buf.raw[:got.value]

    def blocks(self):
        address = 0
        while address < 0x800000000000:
            mbi = _MBI()
            if not self.kernel.VirtualQueryEx(self.handle, address, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base, size = mbi.BaseAddress or 0, mbi.RegionSize
            if not size or base + size <= address:
                break
            if mbi.State == 0x1000 and mbi.Protect & 0xE6 and not mbi.Protect & 0x100:
                for offset in range(0, size, 4 * 1024 * 1024):
                    # Overlap permits a string or pointer pair to straddle a read boundary.
                    start = max(0, offset - 192)
                    data = self.read(base + start, min(size - start, 4 * 1024 * 1024 + 192))
                    if data:
                        yield base + start, data
            address = base + size


def discover_keys(account: Path, paths: list[Path], executable: Path,
                  *, timeout: float = 45, max_read_bytes: int = 2_147_483_648) -> tuple[dict[str, bytes], dict]:
    """Recover only salts authenticated against selected account DB pages.

    We inspect the selected application's currently open account, with VM_READ
    and QUERY_INFORMATION. No debugger, WriteProcessMemory or target code is used.
    Candidate bytes are never returned in diagnostic metrics.
    """
    from .cipher import verify_key, QQ_HEADER_SIZE, PAGE_SIZE
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise DiscoveryError("QQ 密钥恢复需要 Windows x64 Python。")
    account = account.resolve(strict=True)
    executable = executable.resolve(strict=True)
    root = account / "nt_db"
    pages: dict[str, bytes] = {}
    for path in paths:
        path = path.resolve(strict=True)
        if path.parent != root:
            raise DiscoveryError("数据库不属于已绑定的 QQ 账号。")
        with path.open("rb") as stream:
            stream.seek(QQ_HEADER_SIZE)
            page = stream.read(PAGE_SIZE)
        if len(page) == PAGE_SIZE:
            pages[path.name] = page
    if not pages:
        raise DiscoveryError("所选账号没有可验证的 QQNT 消息数据库。")
    candidates = []
    for proc in psutil.process_iter(["pid", "name", "exe", "memory_info"]):
        if (proc.info["name"] or "").lower() != "qq.exe":
            continue
        try:
            if not proc.info["exe"] or Path(proc.info["exe"]).resolve() != executable:
                continue
            # Do not recover another account's process merely because it shares
            # the installed executable. Open-file identity binds the selection.
            if not any(Path(item.path).resolve().parent == root for item in proc.open_files()):
                continue
            candidates.append((proc.info["memory_info"].rss, proc.pid))
        except (OSError, psutil.Error):
            continue
    if not candidates:
        raise DiscoveryError("未找到打开所选 QQ 账号数据库的进程；请在本机登录该账号。")
    keys: dict[str, bytes] = {}
    tested: set[bytes] = set()
    metrics = {"processes_examined": 0, "keyspec_candidates": 0,
               "verified_databases": 0, "read_bytes": 0,
               "mode": "PROCESS_VM_READ|PROCESS_QUERY_INFORMATION", "bounded": True}
    started = time.monotonic()
    for _, pid in sorted(candidates, reverse=True):
        proc = _ReadProcess(pid)
        metrics["processes_examined"] += 1
        try:
            for base, data in proc.blocks():
                for match in _HEX.finditer(data):
                    if len(match[1]) != 96:
                        continue
                    value = bytes.fromhex(match[1].decode("ascii"))
                    if len(value) != 48 or value in tested:
                        continue
                    tested.add(value)
                    metrics["keyspec_candidates"] += 1
                    for name, page in pages.items():
                        if name not in keys and value[-16:] == page[:16] and verify_key(value, page):
                            keys[name] = value
                if len(keys) == len(pages):
                    break
                if (time.monotonic() - started >= timeout
                        or metrics["read_bytes"] + proc.bytes_read >= max_read_bytes):
                    break
        finally:
            metrics["read_bytes"] += proc.bytes_read
            proc.close()
        if (len(keys) == len(pages) or time.monotonic() - started >= timeout
                or metrics["read_bytes"] >= max_read_bytes):
            break
    metrics["verified_databases"] = len(keys)
    metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if not keys:
        raise DiscoveryError("未获得通过所选数据库页面校验的密钥；保留已有缓存。")
    return keys, metrics


