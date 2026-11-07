"""Filesystem-only tests. No audio engine, microphone or meeting is started."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

LOCAL = Path(__file__).resolve().parents[1] / "tencent-meeting" / "local"
sys.path.insert(0, str(LOCAL))
from meeting_agent import state
from meeting_agent.store import Session


def windows_sddl(path):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get = advapi.GetNamedSecurityInfoW
    get.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get.restype = wintypes.DWORD
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor, output = ctypes.c_void_p(), ctypes.c_void_p()
    error = get(str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        if not convert(descriptor, 1, 4, ctypes.byref(output), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.wstring_at(output)
    finally:
        kernel.LocalFree(output)
        kernel.LocalFree(descriptor)


def assert_private(test, path, directory):
    if os.name == "nt":
        descriptor = windows_sddl(path)
        trustees = re.findall(r";;;([^\)]+)\)", descriptor)
        test.assertEqual(set(trustees), {"SY", state.current_user_sid()}, "Unexpected trustees in private DACL")
        test.assertIn("D:P", descriptor, "DACL must be protected")
    else:
        test.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if directory else 0o600)


class PrivateMeetingState(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stilldesk-state-fixture-")
        self.home = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"STILLDESK_HOME": str(self.home)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_state_and_runtime_are_outside_source(self):
        root = state.state_root()
        runtime = state.runtime_directory()
        sessions = state.sessions_directory()
        self.assertEqual(root, self.home / "meeting")
        self.assertEqual(runtime, root / "runtime")
        self.assertEqual(sessions, root / "sessions")
        self.assertFalse(root.is_relative_to(LOCAL))
        assert_private(self, root, True)
        assert_private(self, runtime, True)
        assert_private(self, sessions, True)

    def test_existing_shared_directory_is_restricted_without_residual_everyone(self):
        folder = self.home / "reused-shared"
        folder.mkdir()
        if os.name == "nt":
            subprocess.run(["icacls.exe", str(folder), "/grant", "*S-1-1-0:(OI)(CI)F"], check=True,
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertIn("WD", windows_sddl(folder))
        else:
            folder.chmod(0o777)
        state.private_directory(folder)
        assert_private(self, folder, True)

    def test_connection_is_atomic_private_json(self):
        runtime = state.runtime_directory()
        target = runtime / "connection.json"
        state.atomic_json(target, {"port": 12345, "token": "synthetic-fixture-token"})
        self.assertEqual(state.read_private_json(target)["port"], 12345)
        assert_private(self, target, False)
        state.atomic_json(target, {"port": 12346, "token": "synthetic-new-token"})
        self.assertEqual(json.loads(target.read_text())["port"], 12346)
        self.assertEqual(list(runtime.glob("*.tmp")), [])

    def test_loading_a_shared_session_restricts_existing_journals(self):
        folder = self.home / "legacy-fixture-session"
        folder.mkdir()
        (folder / "session.json").write_text(json.dumps({"session_id": "20261107-000000-00000000", "state": "finished"}))
        (folder / "transcript.jsonl").write_text('{"seq":1,"text":"synthetic only"}\n')
        (folder / "events.jsonl").write_text('')
        if os.name == "nt":
            subprocess.run(["icacls.exe", str(folder), "/grant", "*S-1-1-0:(OI)(CI)F", "/T"], check=True,
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            folder.chmod(0o777)
            for child in folder.iterdir():
                child.chmod(0o666)
        session = Session(folder)
        self.assertEqual(session.segments[0]["text"], "synthetic only")
        assert_private(self, folder, True)
        for child in folder.iterdir():
            assert_private(self, child, False)

    def test_relative_state_root_is_rejected(self):
        with patch.dict(os.environ, {"STILLDESK_HOME": "relative"}):
            with self.assertRaises(ValueError):
                state.state_root()


if __name__ == "__main__":
    unittest.main()
