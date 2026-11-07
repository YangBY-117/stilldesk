# Verification

2026-11-07. Public fork version 0.2.0.

**153 passed, 2 skipped** in the integration's fixture suite. The two inherited Windows tests require symbolic-link capabilities unavailable to that run. Run from this directory:

```powershell
python -m pytest -q
```

The inherited suite covers QQ encrypted-page and committed-WAL validation, account/source/path restrictions, bounded Protobuf decoding, image scope, ledger semantics, same-second keyset pagination and unchanged-source reuse. New tests cover cursor-only history with inherited filters, conflict rejection, exhaustive paginated cached search, compact previews with intact message identity, long-text reconstruction with same-chat context, and status/cached reads during a two-second incremental long-poll. A real stdio MCP server initializes and discovers nine tools without any private configuration.

A separate **live read-only identity** probe found no matching configured QQ process at observation time. The installed version manifest was 9.9.31-49738. Remote login state remained unknown and the tool did not start QQ or ask for another login. Foreground-window and pointer-position values stayed unchanged. `LIVE_IDENTITY_VERIFICATION.json` retains only content-free metadata.

This is a fixture-verified local reader, not a claim that the currently closed app passed a live message read, or that all QQNT versions are compatible. Personal-account sending, cloud-only history, uncached media fetches, login, recall and deletion remain unsupported. No live-account mutation was attempted.

The source manifest records the original logical files and SHA-256 values. A final comparison found no source drift during this fork. Original client/source files and configurations were not modified.

## CI consistency regression repair

The first Windows CI exposed a same-length write that did not advance observable file timestamps while source handles remained open. The acquisition and cache checks now include complete encrypted DB/WAL SHA-256 observations, compare the copied bytes to the initial digest, and recheck the complete pair before acquisition finishes. Each hash/copy pass reads at most the initial file size plus one probe byte with 1 MiB blocks; growing files fail rather than following EOF. Private-cache hashing and rollback-backup fallback copies have the same finite bounds. Cache formats were bumped so older header-only signatures are not reused.

Nine new fixture cases per integration cover fixed metadata and unchanged headers during DB/WAL replacement, stale-cache rejection, ongoing growth during source/private reads, and preservation of the previous destination/stamp when a no-hardlink backup becomes unstable. The original CI failure test remains active. These changes were checked locally; the repaired GitHub CI result must be reported separately.

Freshness validation is now linear I/O over the observed source bytes, including cache-hit freshness checks. `refresh=false` index reads retain their fast path. This is still a bounded stability observation, not a SQLite read lock or a cross-database atomic snapshot; commits after acquisition are allowed and freshness refers to acquisition time.
