<div align="center">

<img src="docs/assets/mark.svg" width="64" alt="StillDesk logo">

# StillDesk

### Your agent works. Your cursor stays yours.

Background-first tools for Chrome, Edge, WeChat, QQ, Feishu, and Tencent Meeting.

[Website](https://yangby-117.github.io/stilldesk/) · [Get started](docs/getting-started.html) · [Capabilities](docs/capabilities.md) · [中文](README.zh-CN.md)

![License: MIT](https://img.shields.io/badge/license-MIT-c7f38a?style=flat-square&labelColor=1b251d)
![MCP + CLI](https://img.shields.io/badge/interface-MCP_%2B_CLI-c7f38a?style=flat-square&labelColor=1b251d)
![Native platform: Windows](https://img.shields.io/badge/native_platform-Windows-c7f38a?style=flat-square&labelColor=1b251d)

<img src="docs/assets/stilldesk-brand.png" width="100%" alt="A stationary cursor alongside softly lit glass panels, a brand illustration of quiet background work">

</div>

Your agent should be able to help without taking over the computer you are using.
StillDesk gives it app-specific background interfaces through **one CLI and one
MCP server**. You keep typing. Your browser and native tools do the work quietly.

Browser control uses a normal extension and Native Messaging. It does **not**
use a remote debugging port, CDP, Puppeteer, Playwright connections, or the
extension `debugger` permission. Native app adapters use local records or service
interfaces; an unavailable background action returns a clear capability error.

## See what is actually ready

| App | Available background path | Current boundary |
| --- | --- | --- |
| **Chrome + Edge** | Tabs, navigation, DOM snapshots, forms, scroll, DOM drag, waits, uploads, downloads, visible-tab screenshots | **37 live local-fixture checks per browser**; trusted user gestures and native dialogs remain unsupported |
| **WeChat** | Account-bound local history/search, cursor-only pagination, long-message context, images, verified cached attachments | Inherits the repaired 0.5 read backend; current native sends and uncached network downloads are not available |
| **QQ** | Local personal-client history, search, stable cursors, single-message context and cached images | Read-only; no personal-account sender is implied |
| **Feishu / Lark** | Fixed operations through the official CLI and authorized service APIs | Requires your app/account authorization; writes are previewed before explicit execution |
| **Tencent Meeting** | Local sessions/transcripts and authorized cloud recording reads | Capture requires an explicit request; models and credentials are not bundled |

The browser checks include real HTTP file receipt, SHA-256 comparisons, and
before/after checks of the Windows foreground window and physical cursor.
WeChat and QQ process-status probes also leave both unchanged. These are scoped
observations, not a claim that every website, client version or agent runtime
has been accepted. See the [capability matrix](docs/capabilities.md).

<img src="docs/assets/verification.gif" width="100%" alt="Animated visualization of selected passing steps from the real Chrome fixture test ledger">

*Visualization of the committed test ledger; this is not a desktop recording or
a benchmark animation.*

## Quick start

Node.js 22+ is enough for the browser tools and universal entry point.
Python 3.11+ is required for the optional local messaging adapters.

```sh
git clone https://github.com/YangBY-117/stilldesk.git
cd stilldesk
node scripts/setup.mjs
node bin/stilldesk.mjs doctor
```

Load `packages/browser/extension` as an unpacked extension in Chrome or Edge.
Then review and apply the native host registration for that browser:

```sh
node packages/browser/scripts/register-native-host.mjs --browser=chrome
node packages/browser/scripts/register-native-host.mjs --browser=chrome --apply
# Edge: replace chrome with edge.
```

Registration uses a dedicated StillDesk host and is reversible. It does not edit
your existing agent configurations, launch a new login, or start a recording.

```sh
node bin/stilldesk.mjs browser list_pages
node bin/stilldesk.mjs browser take_snapshot '{"pageId":123}'
node bin/stilldesk.mjs mcp
```

Choose the real page ID and fresh node UIDs from the response. New tabs are
background tabs. An inactive tab is never activated just to take a screenshot.

For the local messaging packages:

```sh
node scripts/setup.mjs --messaging
```

Account binding and authorization are explicit, local setup steps. The repository
contains no account configuration, message databases or sessions.

## Bring your agent

Print a configuration for your client:

```sh
node bin/stilldesk.mjs config codex
node bin/stilldesk.mjs config claude-code
node bin/stilldesk.mjs config gemini
node bin/stilldesk.mjs config kimi
node bin/stilldesk.mjs config openclaw
node bin/stilldesk.mjs config hermes
node bin/stilldesk.mjs config list
```

Targets also include ZCode, DeepSeek Harness, Cursor, VS Code, Copilot, Cline,
OpenCode, generic MCP clients, and portable ChatGPT/Codex desktop plugin files.
Configuration generation and protocol tests are distinct from live testing in a
specific harness. **Claude Code is never launched by project verification.**
See [client compatibility](packages/compat/README.md) for documented formats and
validation levels. The token-authenticated Streamable HTTP adapter is available
via `stilldesk serve`; public HTTPS hosting, connector authentication and any
marketplace submission are separate deployment steps.

## A small contract

- **Your input devices stay yours.** No physical pointer movement, global key
  injection, foreground-window calls, or hidden desktop fallback in default tools.
- **Local access is explicit.** File uploads and downloads require permitted
  upload directories and explicit downloaded-file readback roots. Ordinary browser
  downloads follow the browser's own Downloads settings. Accounts have separate
  local configuration and caches.
- **Plans are not receipts.** A send preview is not a sent message. A populated
  file input is not a server upload receipt. Uncertain outcomes are not retried
  as fresh actions.
- **App content is data.** A message, transcript or web page is never treated as
  authorization or a tool instruction.
- **Unsupported means unsupported.** Native writes are not replaced with a bot
  account, UI clicking, or a successful-looking empty response.

## Architecture

```text
Your agent
    │
MCP stdio / CLI / authenticated Streamable HTTP
    │
StillDesk router
    ├── browser ── local IPC ── Native Messaging ── Chrome / Edge extension
    ├── wechat ── account-bound local reader + explicit capability states
    ├── qq ── account-bound QQNT local reader
    ├── feishu ── official native CLI ── authorized service APIs
    └── meeting ── local session tools / read-only cloud recording adapter
```

No model provider is built in. The tool returns structured observations to your
existing agent; the agent keeps its reasoning and context.

## Verify and contribute

```sh
npm test
npm run check
node scripts/setup.mjs --test-deps
```

Browser live tests use isolated profiles and synthetic local HTTP fixtures.
No live message sends, user-meeting recording, or Claude Code execution are part
of release validation. [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) ·
[Third-party attribution](THIRD_PARTY.md).

StillDesk is early software, and the adapters do not all have the same scope.
We would rather show the exact working path than advertise a silent fallback.

## License

MIT for original StillDesk code. Incorporated third-party components retain
their licenses and notices. The brand illustration is AI-generated; it is not a
product screenshot or evidence of tool execution.
