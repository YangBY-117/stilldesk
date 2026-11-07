from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3

import pytest

from qq_bridge.reader import Reader


def varint(number: int) -> bytes:
    data = bytearray()
    while number >= 128:
        data.append((number & 127) | 128)
        number >>= 7
    data.append(number)
    return bytes(data)


def field(number: int, value: int | str | bytes) -> bytes:
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    value = value.encode() if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(value)) + value


def body(text: str) -> bytes:
    return field(40800, field(45002, 1) + field(45101, text))


@dataclass
class FakeSource:
    account: Path
    state: Path
    main: Path
    fail: bool = False
    captures: int = 0

    def __post_init__(self):
        self.account.mkdir(parents=True)
        self.state.mkdir(parents=True)
        self.config_path = self.state / "config.json"
        self.config = {"account_dir": str(self.account), "state_dir": str(self.state)}
        self.account_tag = hashlib.sha256(str(self.account).encode()).hexdigest()[:16]
        self.optional = {}
        with sqlite3.connect(self.main) as conn:
            for table in ("c2c_msg_table", "group_msg_table"):
                conn.execute(f'''CREATE TABLE "{table}"("40001" INTEGER PRIMARY KEY,
                    "40020" TEXT,"40021" TEXT,"40030" INTEGER,"40033" INTEGER,
                    "40050" INTEGER,"40800" BLOB)''')

    def capture(self):
        self.captures += 1
        if self.fail:
            raise RuntimeError("PRIVATE_KEY_SENTINEL C:\\private\\source SQL secret")
        return {"snapshots": {"nt_msg.db": self.main, **self.optional},
                "records": [{"path": "PRIVATE_PATH", "authenticated": True}],
                "captured_at": "2026-10-20T04:00:00+00:00",
                "coverage": {"authenticated": True, "complete_history": False,
                             "key": "PRIVATE_KEY_SENTINEL", "source_path": "PRIVATE_PATH"}}

    def status(self):
        return {"source_available": True, "account_tag": self.account_tag,
                "private_path": "PRIVATE_PATH", "key": "PRIVATE_KEY_SENTINEL"}

    def add(self, native: int, text: str = "明天下午交材料", *, timestamp=1_700_000_000,
            table="group_msg_table", peer="synthetic-group", payload=None):
        with sqlite3.connect(self.main) as conn:
            conn.execute(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)',
                         (native, "synthetic-sender", peer, 123456, 654321, timestamp,
                          body(text) if payload is None else payload))

    def change(self, native: int, text: str):
        with sqlite3.connect(self.main) as conn:
            conn.execute('UPDATE group_msg_table SET "40800"=? WHERE "40001"=?', (body(text), native))


@pytest.fixture
def scenario(tmp_path):
    source = FakeSource(tmp_path / "account" / "nt_qq", tmp_path / "private", tmp_path / "main.db")
    source.add(10)
    reader = Reader(source=source)
    return reader, source


@pytest.fixture
def anyio_backend():
    return "asyncio"
