"""Read-only client identity, independent of windows and accessibility.

Open database handles establish which account a process is using. They do not
prove connectivity, remote authentication, or whether a window is in the tray.
No operation in this module starts a process, activates a window, or logs in.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import os
from pathlib import Path

import psutil


def file_version(executable: str | Path) -> str | None:
    """Read the PE version resource with Windows APIs, without loading its code."""
    if os.name != "nt":
        return None
    try:
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                               wintypes.DWORD, ctypes.c_void_p]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                           ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
        version.VerQueryValueW.restype = wintypes.BOOL
        unused = wintypes.DWORD()
        path = str(executable)
        size = version.GetFileVersionInfoSizeW(path, ctypes.byref(unused))
        if not size or size > 16 * 1024 * 1024:
            return None
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, data):
            return None
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None
        if not pointer.value or length.value < 13 * ctypes.sizeof(wintypes.DWORD):
            return None
        fixed = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
        if fixed[0] != 0xFEEF04BD:
            return None
        high, low = fixed[2], fixed[3]
        return f"{high >> 16}.{high & 65535}.{low >> 16}.{low & 65535}"
    except (OSError, ValueError, AttributeError):
        return None


def inspect_session(executable: str | Path, account: str | Path) -> dict:
    """Return content-free facts about existing processes for one exact account."""
    expected, selected = Path(executable).resolve(), Path(account).resolve()
    matching, bound, mixed = [], [], []
    unreadable = 0
    unverified_name_candidates = 0
    complete = True
    try:
        processes = psutil.process_iter(["pid", "name", "exe"])
        for process in processes:
            try:
                actual = process.info.get("exe")
                if not actual:
                    if str(process.info.get("name") or "").casefold() == expected.name.casefold():
                        unverified_name_candidates += 1
                        complete = False
                    continue
                if Path(actual).resolve() != expected:
                    continue
                matching.append(process.pid)
                accounts = set()
                for handle in process.open_files():
                    for parent in Path(handle.path).parents:
                        if parent.name.casefold() == "db_storage":
                            accounts.add(parent.parent.resolve())
                            break
                if selected in accounts:
                    if accounts == {selected}:
                        bound.append(process.pid)
                    else:
                        mixed.append(process.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                unreadable += 1
                complete = False
    except (psutil.Error, OSError):
        complete = False
        unreadable += 1
    account_bound = len(bound) == 1 and not mixed
    if mixed:
        state, reason = "ambiguous_account", "multiple_accounts_open"
    elif len(bound) > 1:
        state, reason = "ambiguous_account", "multiple_account_processes"
    elif account_bound:
        state, reason = "account_open", None
    elif matching or unverified_name_candidates:
        state, reason = "account_binding_unavailable", "account_binding_unavailable"
    elif not complete:
        state, reason = "inspection_unavailable", "process_inspection_unavailable"
    else:
        state, reason = "client_not_observed", "client_process_not_observed"
    current_version = file_version(expected)
    from .capabilities import capabilities
    capability = {"status": "foreground_adapter_not_shipped", "foregroundRequired": False,
                  "background": capabilities()}
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "state": state, "reason": reason,
        "running": bool(matching or unverified_name_candidates) if complete or matching or unverified_name_candidates else None,
        "configured_executable_exists": expected.is_file(),
        "executable_version": current_version,
        "account_bound": account_bound, "pid": bound[0] if account_bound else None,
        "matching_executable_process_count": len(matching),
        "binding_observation": {
            "basis": "configured_executable_and_open_account_database_handles",
            "complete": complete, "bound_process_count": len(bound),
            "mixed_account_process_count": len(mixed),
            "unreadable_process_count": unreadable,
            "unverified_name_candidates": unverified_name_candidates,
        },
        "login_state": "unknown",
        "login_state_basis": "account_database_handles_do_not_prove_remote_authentication_or_connectivity",
        "reuse_existing_client": bool(matching or unverified_name_candidates),
        "relogin_recommended": False,
        "recommended_action": "reuse_existing_client" if account_bound else "inspect_existing_client",
        "guidance": (
            "已找到使用当前账号数据库的现有微信进程；直接复用。托盘、隐藏窗口或不可读控件不要求重新登录。"
            if account_bound else
            "本次未能唯一核实账号进程；先检查现有微信，不据此重新登录或启动另一个微信。"
        ),
        "desktop_capability": capability,
        "read_only": True,
    }
