# Contributing

Prefer one small, reproducible background operation over a broad capability
claim. Include its source of authority, supported app versions and a failure mode
for unavailable permissions or native backends.

1. Read `AGENTS.md` and the relevant provider's verification notes.
2. Reproduce against a synthetic fixture. Never commit account data or messages.
3. Keep foreground windows, global input and remote browser debugging out of the
   default path. An explicit capability error is better than a hidden fallback.
4. Run `npm test`, `npm run check` and the affected integration's Python tests.
5. For protocol changes, verify a real stdio MCP exchange. For browser changes,
   use the Native Messaging fixture tests and report cursor/window observations.

Do not run Claude Code during verification. Configuration samples may be checked
against documented schema without launching a harness.

Native reverse engineering should preserve artifact identity, exact version,
limited source ranges and unknown calling/lifetime requirements. A discovered
address is not a usable backend. Do not ship vendor binaries, memory dumps,
private captures, credentials or unsupported writes to a live personal account.

When adding a provider, export explicit schemas and structured status. Keep
account binding, unavailable scopes, incomplete coverage and empty results
distinct. Mutations need previews, exact targets and idempotent request tracking.

PRs should explain the user-visible behavior, tested scope and remaining limits.
