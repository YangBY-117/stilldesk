"""Filesystem-only tests. No audio engine, microphone or meeting is started."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
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


def windows_acl_entries(descriptor):
    """Read binary ACE SIDs; SDDL display aliases are not principal identities."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_dacl = advapi.GetSecurityDescriptorDacl
    get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    get_dacl.restype = wintypes.BOOL
    get_ace = advapi.GetAce
    get_ace.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    get_ace.restype = wintypes.BOOL
    valid_sid = advapi.IsValidSid
    valid_sid.argtypes = [ctypes.c_void_p]
    valid_sid.restype = wintypes.BOOL
    sid_string = advapi.ConvertSidToStringSidW
    sid_string.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    sid_string.restype = wintypes.BOOL
    control = advapi.GetSecurityDescriptorControl
    control.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    control.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]

    class AclHeader(ctypes.Structure):
        _fields_ = [("revision", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte),
                    ("size", wintypes.WORD), ("ace_count", wintypes.WORD), ("reserved2", wintypes.WORD)]

    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    dacl = ctypes.c_void_p()
    if not get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not present.value or not dacl.value:
        raise AssertionError("Private state must have a non-null DACL")
    entries = []
    for index in range(AclHeader.from_address(dacl.value).ace_count):
        ace = ctypes.c_void_p()
        if not get_ace(dacl, index, ctypes.byref(ace)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Standard ACCESS_ALLOWED_ACE/ACCESS_DENIED_ACE: 4-byte header, mask, SID.
        # Reject other ACE layouts rather than reading their fields incorrectly.
        ace_type = ctypes.c_ubyte.from_address(ace.value).value
        ace_size = wintypes.WORD.from_address(ace.value + 2).value
        if ace_type not in (0, 1) or ace_size < 16:
            raise AssertionError("Unexpected ACE layout in private state DACL")
        sid = ctypes.c_void_p(ace.value + 8)
        if not valid_sid(sid):
            raise AssertionError("Invalid binary SID in private state DACL")
        output = ctypes.c_void_p()
        if not sid_string(sid, ctypes.byref(output)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entries.append({"sid": ctypes.wstring_at(output), "type": ace_type,
                            "mask": wintypes.DWORD.from_address(ace.value + 4).value})
        finally:
            kernel.LocalFree(output)
    flags, revision = wintypes.WORD(), wintypes.DWORD()
    if not control(descriptor, ctypes.byref(flags), ctypes.byref(revision)):
        raise ctypes.WinError(ctypes.get_last_error())
    return entries, bool(flags.value & 0x1000)  # SE_DACL_PROTECTED


def windows_private_acl(path):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get = advapi.GetNamedSecurityInfoW
    get.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    error = get(str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        return windows_acl_entries(descriptor)
    finally:
        kernel.LocalFree(descriptor)


def acl_fixture_from_sddl(sddl):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return windows_acl_entries(descriptor)
    finally:
        kernel.LocalFree(descriptor)


def assert_private(test, path, directory):
    if os.name == "nt":
        entries, protected = windows_private_acl(path)
        test.assertEqual(len(entries), 2, "Private DACL must contain exactly two ACEs")
        test.assertEqual({entry["sid"] for entry in entries}, {"S-1-5-18", state.current_user_sid()},
                         "Unexpected canonical SID identities in private DACL")
        test.assertTrue(all(entry["type"] == 0 and entry["mask"] == 0x1F01FF for entry in entries),
                        "Private DACL must have exactly full-control allow entries")
        test.assertTrue(protected, "DACL must be protected")
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

    @unittest.skipUnless(os.name == "nt", "Windows binary SID normalization fixture")
    def test_system_and_local_admin_aliases_are_canonicalized_by_windows(self):
        # Pure descriptor fixtures: no filesystem ACL changes, account lookup,
        # client launch or access to any real user's account data.
        entries, protected = acl_fixture_from_sddl("D:P(A;;FA;;;SY)(A;;FA;;;LA)")
        self.assertTrue(protected)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], {"sid": "S-1-5-18", "type": 0, "mask": 0x1F01FF})
        admin_sid = entries[1]["sid"]
        self.assertRegex(admin_sid, r"^S-1-5-21-(?:[0-9]+-){3}500$")
        self.assertNotEqual(admin_sid, "LA")
        raw_entries, raw_protected = acl_fixture_from_sddl(
            f"D:P(A;;FA;;;S-1-5-18)(A;;FA;;;{admin_sid})")
        self.assertEqual(raw_entries, entries)
        self.assertEqual(raw_protected, protected)


if __name__ == "__main__":
    unittest.main()
