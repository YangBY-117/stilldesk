# Local release validation

Date: 2026-11-07. This record distinguishes local validation from hosted CI,
live-account acceptance and installed-harness acceptance.

| Check | Result | Scope |
| --- | --- | --- |
| JavaScript/protocol/client-format suite | 48 passed, 0 skipped | Browser IPC/quiet gates, platform fixtures, client schemas, core transport and actual unified stdio MCP |
| WeChat fixture suite | 268 passed, 1 skipped | Synthetic local storage/media/cursor data; no live send |
| QQ fixture suite | 144 passed, 2 skipped | Synthetic encrypted pages, messages, media and cursors |
| Meeting state permissions | 5 passed | External state paths, exact private permissions, atomic credentials and reused journals; no audio capture |
| Chrome browser workflows | 37/37 passed | Isolated Chrome for Testing 157 and local HTTP fixtures |
| Edge browser workflows | 37/37 passed | Isolated Edge 156 and local HTTP fixtures |
| Public unified browser MCP flow | 6/6 passed | Real router → Native Messaging → page form/upload → browser download → independent local SHA-256 |
| Source/document checks | Passed | JavaScript and Python parsing, JSON parsing and local Markdown links |
| Public-tree credential/path scan | Passed | Source allowlist, no environment/session/model/private cache distribution |

The unified browser check launches Chrome without `STILLDESK_HOME` or
`STILLDESK_BROWSER_RUNTIME` inherited from the parent, proving the generated
native launcher selects the intended local runtime. It also checks that the
universal `--allow-download-root` reaches actual download readback, rather than
only asserting a configuration shape.

The final browser launcher/authentication changes have targeted real native
launcher → length-prefixed frame → authenticated local pipe → standard MCP
round-trip tests. DOM tests are not counted again merely because a startup
path was corrected.

The three Python skips require symbolic-link privileges unavailable in those
runs. They are not counted as passed. An independent reviewer separately ran
affected protocol, schema and filesystem checks; the final sanitizer report
states its coverage and residual warnings.

No Claude Code process was launched. No real contact received a test message.
No user meeting or microphone capture was started. Native app account-specific
operations requiring unconfigured permissions remain unavailable, as listed in
the [capability matrix](docs/capabilities.md).

The browser test results include before/after foreground-window and physical
cursor observations. The code uses explicit background operations; neither
these observations nor fixture uploads prove acceptance on every real website.

Current hosted CI status belongs to GitHub Actions, not this local record.
Original detailed runtime paths and logs remain outside the public artifact.
