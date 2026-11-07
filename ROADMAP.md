# Delivery scope

- [x] Browser: Chrome and Edge, background navigation, snapshots, interactions,
      uploads, downloads, dialogs where possible, and complete local workflows.
- [x] User-control audit: no pointer movement, global keys, foreground calls,
      forced window display, or hidden UI fallback in public default tools.
- [x] WeChat: inherit the current repair, preserve improved reads and cursors,
      identify native capability gaps, and refuse foreground-dependent writes.
- [x] QQ: native/background history and media access with explicit capability
      reporting; investigate viable background action paths.
- [x] Feishu/Lark: native service APIs through the official authenticated CLI.
- [x] Tencent Meeting: background local and cloud operations without starting
      recording implicitly; preserve coverage and authorization boundaries.
- [x] Agent clients: one CLI, MCP stdio and documented remote connector option;
      export verified-format configurations for mainstream harnesses and agent
      runtimes. No Claude Code execution.
- [x] Public site, README, visual assets and reproducible demos with accurate
      capabilities and source attribution.
- [x] Independent sanitizer and release review before publishing to GitHub.

Release interpretation approved by the user: cover ordinary end-to-end webpage
workflows and state the small set of browser-imposed limitations explicitly.
Do not advertise universal parity with every human or operating-system action.

## Native follow-up work

- [ ] WeChat current-client native send ABI, callback/thread/lifetime validation.
- [ ] Uncached WeChat attachment network retrieval with a verified background path.
- [ ] QQ personal-client background writes, only if a supported path is established.
- [ ] Multi-browser/profile multiplexing with an explicit instance selector.
- [ ] Additional real website and installed-harness acceptance suites; Claude Code
      remains excluded from execution verification at the user's request.
