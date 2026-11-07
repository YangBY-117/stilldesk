# Verification

2026-11-07. Public fork version 0.2.0.

**144 passed, 2 skipped** in the integration's fixture suite. The two inherited Windows tests require symbolic-link capabilities unavailable to that run. Run from this directory:

```powershell
python -m pytest -q
```

The inherited suite covers QQ encrypted-page and committed-WAL validation, account/source/path restrictions, bounded Protobuf decoding, image scope, ledger semantics, same-second keyset pagination and unchanged-source reuse. New tests cover cursor-only history with inherited filters, conflict rejection, exhaustive paginated cached search, compact previews with intact message identity, long-text reconstruction with same-chat context, and status/cached reads during a two-second incremental long-poll. A real stdio MCP server initializes and discovers nine tools without any private configuration.

A separate **live read-only identity** probe found no matching configured QQ process at observation time. The installed version manifest was 9.9.31-49738. Remote login state remained unknown and the tool did not start QQ or ask for another login. Foreground-window and pointer-position values stayed unchanged. `LIVE_IDENTITY_VERIFICATION.json` retains only content-free metadata.

This is a fixture-verified local reader, not a claim that the currently closed app passed a live message read, or that all QQNT versions are compatible. Personal-account sending, cloud-only history, uncached media fetches, login, recall and deletion remain unsupported. No live-account mutation was attempted.

The source manifest records the original logical files and SHA-256 values. A final comparison found no source drift during this fork. Original client/source files and configurations were not modified.
