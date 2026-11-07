# Verification

2026-11-07. Public fork version 0.5.1.

**268 passed, 1 skipped** in the integration's fixture suite. The skipped test requires a real Windows symbolic-link privilege; it is not counted as passed. Run from this directory:

```powershell
python -m pytest -q
```

The inherited tests exercise authenticated snapshot/WAL validation, bounded text/card/media parsing, attachment cache locations and digest checks, stable history/search cursors, single-message context, cached reads and concurrency. New tests prove that real-send requests fail before account/file/UI access, the legacy desktop flag cannot enable a fallback, unsupported UI modules are absent, and a tray/hidden process is reused without UIA. The actual stdio MCP server initializes and discovers 16 tools with no private configuration; capability and unconfigured-status calls work.

A separate **live, read-only process identity** probe observed executable version 4.2.0.11 and a uniquely bound existing account process. It recommended reuse and did not recommend logging in again. Foreground-window and pointer-position values were unchanged across that probe. Only booleans/version/count metadata are retained in `LIVE_IDENTITY_VERIFICATION.json`; no message content, account identifier, path, PID or window handle was exported.

The live probe does not prove a new client's complete data-format compatibility, network authentication or message-send success. End-to-end local-data behavior here was verified against synthetic encrypted/message/media fixtures. Sending and fetching an uncached attachment remain unavailable; no live account mutation was attempted.

The source manifest records the logical files and SHA-256 values copied from the 0.5.0 repair. A final comparison found no changes to those original source files during this fork. Original source, private configuration and client state were not modified.
