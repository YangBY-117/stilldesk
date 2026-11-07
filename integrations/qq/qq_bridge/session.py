"""Read-only identity hints for existing QQ processes, independent of windows."""
from pathlib import Path
import psutil

def inspect_session(executable, account):
    expected, selected = Path(executable).resolve(), Path(account).resolve()
    matching, bound = [], []
    complete = True
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if not process.info.get("exe") or Path(process.info["exe"]).resolve() != expected:
                continue
            matching.append(process.pid)
            if any(Path(item.path).resolve().is_relative_to(selected / "nt_db") for item in process.open_files()):
                bound.append(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            complete = False
    return {"state": "account_open" if len(bound) == 1 else "account_binding_unavailable" if matching else "client_not_observed" if complete else "inspection_unavailable",
            "running": bool(matching) if complete or matching else None,
            "account_bound": len(bound) == 1, "matching_process_count": len(matching),
            "bound_process_count": len(bound), "inspection_complete": complete,
            "reuse_existing_client": bool(matching), "relogin_recommended": False,
            "login_state": "unknown", "foregroundRequired": False,
            "basis": "configured_executable_and_open_account_database_handles"}
