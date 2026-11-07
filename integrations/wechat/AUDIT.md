# Background operation audit

The inherited 0.5.0 reader fixes are real source changes: search and history cursors, cached queries, long-message context, compact output, account/process identity independent of tray visibility, and authenticated local attachment candidates.

The predecessor's mutation driver could activate a window and inject global input. StillDesk excludes that driver, its accessibility process-write gate, its editor watcher and their GUI/COM dependencies. Actual text/media send and the legacy desktop-download flag fail before account, file or UI access. Preview results explicitly say execution is unsupported.

The shipped paths are authenticated local SQLCipher/WAL snapshots, local indexes, digest-verified attachment copies and local image decoding. Process access used for account/key discovery requests query/read permissions; this fork contains no client process writes, remote-debugging connection, injected client code or window/input takeover route.

## Native capability gap

The inherited investigation located download-related functions, manager/context lookups and queue boundaries in the selected client. Callback ABI, correct account-owned manager, request layout, thread affinity and asynchronous object lifetime are not all verified. Calling an address is not a usable native downloader. No unverified call or third-party injection helper is installed in a live account.

REA independently inspected the 4.2.0.11 `ilink_wrapper.dll` resource table in this pass. Its SHA-256 is `345233f74ffc490d7dc406e3c17d41e79ba7fa808632d05a2369923dc64e166f`; Evidence `ev_765d006fe29feba094f59c106cc9a26ca7818372d1e73388b4b411509fc919ac`. This is a static file-identity/resource observation, not recovered business behavior or a successful download. The public tree excludes vendor binaries, runtime addresses, account observations and private reverse-engineering bundles.

Release claims must keep uncached attachment acquisition and actual sending marked unavailable until a background provider has its own lifecycle, account binding, idempotency and end-result acceptance evidence.
