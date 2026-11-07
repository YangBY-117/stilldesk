# Verification

2026-11-07. Public fork version 0.5.1.

**277 passed, 1 skipped** in the integration's fixture suite. The skipped test requires a real Windows symbolic-link privilege; it is not counted as passed. Run from this directory:

```powershell
python -m pytest -q
```

The inherited tests exercise authenticated snapshot/WAL validation, bounded text/card/media parsing, attachment cache locations and digest checks, stable history/search cursors, single-message context, cached reads and concurrency. New tests prove that real-send requests fail before account/file/UI access, the legacy desktop flag cannot enable a fallback, unsupported UI modules are absent, and a tray/hidden process is reused without UIA. The actual stdio MCP server initializes and discovers 16 tools with no private configuration; capability and unconfigured-status calls work.

A separate **live, read-only process identity** probe observed executable version 4.2.0.11 and a uniquely bound existing account process. It recommended reuse and did not recommend logging in again. Foreground-window and pointer-position values were unchanged across that probe. Only booleans/version/count metadata are retained in `LIVE_IDENTITY_VERIFICATION.json`; no message content, account identifier, path, PID or window handle was exported.

The live probe does not prove a new client's complete data-format compatibility, network authentication or message-send success. End-to-end local-data behavior here was verified against synthetic encrypted/message/media fixtures. Sending and fetching an uncached attachment remain unavailable; no live account mutation was attempted.

The source manifest records the logical files and SHA-256 values copied from the 0.5.0 repair. A final comparison found no changes to those original source files during this fork. Original source, private configuration and client state were not modified.

## CI consistency regression repair

The first Windows CI exposed a same-length write that did not advance observable file timestamps while source handles remained open. The acquisition and cache checks now include complete encrypted DB/WAL SHA-256 observations, compare the copied bytes to the initial digest, and recheck the complete pair before acquisition finishes. Each hash/copy pass reads at most the initial file size plus one probe byte with 1 MiB blocks; growing files fail rather than following EOF. Private-cache hashing and rollback-backup fallback copies have the same finite bounds. Cache formats were bumped so older header-only signatures are not reused.

Nine new fixture cases per integration cover fixed metadata and unchanged headers during DB/WAL replacement, stale-cache rejection, ongoing growth during source/private reads, and preservation of the previous destination/stamp when a no-hardlink backup becomes unstable. The original CI failure test remains active. These changes were checked locally; the repaired GitHub CI result must be reported separately.

Freshness validation is now linear I/O over the observed source bytes, including cache-hit freshness checks. `refresh=false` index reads retain their fast path. This is still a bounded stability observation, not a SQLite read lock or a cross-database atomic snapshot; commits after acquisition are allowed and freshness refers to acquisition time.
