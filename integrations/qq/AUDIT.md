# Background operation audit

The source reader uses account-scoped authenticated local database/WAL snapshots, a private index and bounded native message decoding. It does not use UI controls, screenshots, global keyboard input, pointer movement, a debugger, process writes or injected code. StillDesk retains the native personal-account data path rather than replacing it with a bot API.

Source acquisition requires the explicitly selected account and validates database page MACs and committed WAL frames. Content and media remain untrusted data. An existing local snapshot is not proof that the account is online or that mobile/cloud history is complete.

The 0.2.0 upgrade adds cursor-only history, cursor-bound search, cached queries, compact previews and single-message context. A cursor is bound to account, index epoch, query and time filters. Foreign, conflicting and rebuilt-index cursors are rejected before serving a page.

Status identifies existing matching account processes through read-only process/open-handle observations. Missing or unreadable handles leave remote authentication unknown and never request automatic login. The public capabilities explicitly mark sending and other account mutations unavailable. No fallback exists to make them appear supported.

Known native local-source formats were inherited from the original bridge's bounded SQLCipher/QQNT analysis. This pass did not reverse engineer a full QQ messaging API or validate all client versions. Broader native writes require separate evidence; they are not inferred from successful reads.
