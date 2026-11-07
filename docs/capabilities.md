# Capability and evidence matrix

Release: StillDesk 0.1.0. Last reviewed: 2026-11-07.

The release target is ordinary end-to-end browser workflows, with a small set
of browser-imposed limits stated explicitly. It is not universal parity with
every human or operating-system action.

## No-takeover contract

Default tool paths do not call foreground-window activation, physical pointer
movement, global keyboard injection, or a hidden UI automation fallback.
Browser tab creation is inactive; selection is logical, and active-tab closure
is refused to avoid changing the user's view. A background tab is not activated
for a screenshot. The tools never enable Chrome remote debugging.

The contract describes the code's behavior. Before/after Windows cursor and
foreground checks in live tests support it within those tests; they are not a
proof that a third-party website or app can never display its own notification.

## Providers

| Provider | Verified scope | Limits and setup |
| --- | --- | --- |
| Browser | Chrome for Testing 157 and Edge 156, each 37/37 isolated local-fixture checks; uploads and downloads checked by bytes and SHA-256; mouse and foreground unchanged | Native Messaging extension installation; fixed runtime binds one browser/profile; no trusted user activation, native dialog automation or inactive-tab screenshot |
| WeChat 0.5.1 | 268 fixture tests passed, 1 skipped; real stdio MCP; content-free real process/session identity probe left cursor/window unchanged | Local account binding required; current native send and uncached download remain unavailable; no UI sender ships |
| QQ 0.2.0 | 144 fixture tests passed, 2 skipped; real stdio MCP; status probe did not start the closed client | Local personal-account history only, not bot-account or network write control |
| Feishu | Fixed operation schemas and fixture tests; installed official CLI diagnostic observed unauthenticated state without opening login | App/user authorization and service scopes remain required; no real message send was used as a test |
| Tencent Meeting | Fixed cloud read allowlist and local-session controls; fixture tests; no recording starts during setup/status | Explicit account binding for cloud; local capture requires explicit target and authorization; models/native build prerequisites are separate |

WeChat and QQ account probes retained only booleans, versions and observation
counts. No messages, account IDs, PID/window handles or personal paths are part
of public verification. Symbolic-link privilege skips are not counted as pass.

Browser uploads require explicit local read roots and are limited to 16 files,
32 MiB total. Downloads use the browser's own Downloads settings and transfer
through its normal authenticated session. `--allow-download-root` permits local
readback and hashing of known download results; it is not a global filesystem
read grant or a guarantee that the browser cannot download elsewhere.

Some programmatic controls differ from human input. DOM events do not create
trusted user activation; hover cannot promise every native CSS/pointer side
effect; a new-window link is opened inactive and its custom click handler is not
executed. These boundaries are returned rather than hidden behind CDP or UI.

## Client compatibility

Fifteen configuration targets are generated from official documented formats:
Codex, Claude Code, Gemini CLI, Kimi Code, ZCode, DeepSeek Harness, OpenClaw,
Hermes Agent, Cursor, VS Code, GitHub Copilot, Cline, OpenCode, ChatGPT/Codex
desktop plugin files, and generic stdio MCP clients.

| Level | What it means here |
| --- | --- |
| Schema/configuration | Generated JSON/TOML/YAML is checked against the currently documented shape; command arguments remain structured |
| Protocol | Actual local stdio MCP initializes, discovers tools and routes requests; the HTTP adapter separately tests bearer/origin/host/session behavior |
| Harness live acceptance | Not claimed for the fifteen clients. No Claude Code executable is launched at all |
| Marketplace / public hosting | A portable plugin package and HTTP transport are provided; directory approval and public HTTPS/OAuth deployment are separate |

See [compatibility notes](../packages/compat/README.md). A model name such as
DeepSeek is not itself a harness capability; this project specifically exports
the documented DeepSeek Harness entry.

## Evidence locations

- [Browser verification](../packages/browser/docs/VERIFICATION.md)
- [Chrome fixture results](../tests/browser/e2e-chrome.json)
- [Edge fixture results](../tests/browser/e2e-edge.json)
- [WeChat verification](../integrations/wechat/VERIFICATION.md)
- [QQ verification](../integrations/qq/VERIFICATION.md)
- [Platform tools](../packages/platforms/README.md)
- [Release sanitizer](../SANITIZATION_REPORT.md)

REA informed the original Chrome chain analysis and native WeChat identity /
calling-convention investigation. Static artifact evidence and a function
address do not establish a callable background sender. Private captures are
excluded from this public source tree.
