"""Read-only Windows WCDB key discovery and user-bound DPAPI storage.

Config.Cipher layout adapted from wechatauto-replica (Apache-2.0),
commit d322c4a9b1124228820bab363e5676016f188fc6. See THIRD_PARTY.md.
No process writes, injected code, debugger attachment, or network access.
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

_NAME = b"com.Tencent.WCDB.Config.Cipher"
_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b450048844c2448488944254048584c24"
)
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
        ok = fn(ctypes.byref(inp), "WeChat Agent Bridge", None, None, None, 1, ctypes.byref(out))
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
        raise DiscoveryError("密钥缓存与所选微信账号不匹配。")
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
            raise DiscoveryError("无法只读访问微信进程；请确认微信与桥接程序以同一用户权限运行。")
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


def discover_keys(account: Path, paths: list[Path], executable: Path) -> tuple[dict[str, bytes], dict]:
    """Inspect only matching local Weixin processes; return verified per-database keys."""
    from .cipher import verify_key
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise DiscoveryError("需要 Windows 64 位 Python。")
    root = account / "db_storage"
    pages = {}
    for path in paths:
        with path.open("rb") as stream:
            pages[path.relative_to(root).as_posix()] = stream.read(4096)
    pids = []
    for proc in psutil.process_iter(["pid", "name", "exe", "memory_info"]):
        if (proc.info["name"] or "").lower() != "weixin.exe":
            continue
        if proc.info["exe"] and Path(proc.info["exe"]).resolve() == executable.resolve():
            pids.append((proc.info["memory_info"].rss if proc.info["memory_info"] else 0,
                         proc.info["pid"]))
    if not pids:
        raise DiscoveryError("没有找到所选安装路径的微信进程，请先登录微信。")
    keys: dict[str, bytes] = {}
    tested: set[bytes] = set()
    metrics = {"processes_examined": 0, "cipher_objects": 0, "verified_databases": 0,
               "read_bytes": 0, "mode": "PROCESS_VM_READ|PROCESS_QUERY_INFORMATION"}
    started = time.monotonic()
    for _, pid in sorted(pids, reverse=True):
        try:
            proc = _ReadProcess(pid)
        except DiscoveryError:
            continue
        metrics["processes_examined"] += 1
        try:
            anchors = set()
            for base, data in proc.blocks():
                pos = data.find(_NAME)
                while pos >= 0:
                    anchors.add(base + pos)
                    pos = data.find(_NAME, pos + 1)
            if not anchors:
                continue
            pairs = [struct.pack("<QQ", address, len(_NAME)) for address in anchors]
            pattern = re.compile(b"|".join(re.escape(pair) for pair in pairs))
            for base, data in proc.blocks():
                for match in pattern.finditer(data):
                    node = proc.read(base + match.start() - 16, 80)
                    if len(node) != 80:
                        continue
                    pointer = struct.unpack_from("<Q", node, 0x28)[0]
                    if not 0x10000 <= pointer < 0x800000000000:
                        continue
                    obj = proc.read(pointer + 0x88, 40)
                    if len(obj) < 24:
                        continue
                    data_pointer, length = struct.unpack_from("<QQ", obj, 8)
                    if not (0 < length <= 1024 and 0x10000 <= data_pointer < 0x800000000000):
                        continue
                    encoded = proc.read(data_pointer, length)
                    if len(encoded) != length:
                        continue
                    decoded = bytes(value ^ _MASK[i % len(_MASK)] for i, value in enumerate(encoded))
                    metrics["cipher_objects"] += 1
                    for literal in _HEX.finditer(decoded):
                        run = literal.group(1)
                        starts = {0}
                        if len(run) > 96:
                            starts.update(range(0, len(run) - 63, 32))
                            starts.add(len(run) - 64)
                        for start in starts:
                            candidate = bytes.fromhex(run[start:start + 64].decode())
                            variants = [candidate]
                            if start + 96 <= len(run):
                                variants.append(bytes.fromhex(run[start:start + 96].decode()))
                            for key in variants:
                                if key in tested:
                                    continue
                                tested.add(key)
                                for rel, page in pages.items():
                                    if rel not in keys and verify_key(key, page):
                                        keys[rel] = key
                if len(keys) == len(pages):
                    break
        finally:
            metrics["read_bytes"] += proc.bytes_read
            proc.close()
        if len(keys) == len(pages):
            break
    metrics["verified_databases"] = len(keys)
    metrics["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return keys, metrics
