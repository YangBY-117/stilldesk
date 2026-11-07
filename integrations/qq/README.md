# StillDesk QQ

Account-scoped, background-only QQNT history, search, incremental observations and local image pixels. It reads native local data; it does not substitute a QQ bot account for a person's account.

## Setup

Windows and Python 3.11+ are required for native source access. Select the exact account; the tool does not log in or choose another account automatically.

```powershell
python -m pip install -e .
python -m qq_bridge.cli capabilities
python -m qq_bridge.cli status
python -m qq_bridge.cli --config '<private-state>/config.json' init --account-dir '<selected-nt_qq-directory>' --executable '<QQ.exe>' --state-dir '<private-state>'
python -m qq_bridge.cli --config '<private-state>/config.json' sync
python -m qq_bridge.server --config '<private-state>/config.json'
```

The 0.2.0 upgrade adds history continuation with only a cursor, paginated literal search, explicit cached reads, compact previews and `qq_get_message` with same-chat context and long-text pagination. Incremental long-poll waits release the global synchronization lock, allowing status and cached reads to remain responsive.

The MCP surface has nine tools. `qq_capabilities` reports background execution paths and coverage. Personal-account sending, remote media download, login, recall and deletion are not implemented. No desktop, pointer, global keyboard or debugger fallback is provided.

Coverage remains the locally retained ordinary private/group message tables; it is not all cloud/mobile history. See [AUDIT.md](AUDIT.md), [VERIFICATION.md](VERIFICATION.md) and [THIRD_PARTY.md](THIRD_PARTY.md).
