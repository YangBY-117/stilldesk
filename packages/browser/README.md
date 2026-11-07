# StillDesk Browser

Chrome and Edge tools for agents that leave the foreground window and system
pointer to the user. The extension runs against the profile where it is loaded;
commands use Native Messaging and fixed DOM operations. No debugging endpoint,
CDP client, `debugger` permission or global keyboard/mouse injection is used.

## Setup

Requirements: Node.js 22 or newer. No npm dependencies are needed for this
package. Windows Chrome and Edge have live fixture coverage. macOS and Linux
host launchers/locations are implemented but have not had live browser coverage.

```sh
node scripts/prepare.mjs
node scripts/register-native-host.mjs --browser=chrome
node scripts/register-native-host.mjs --browser=chrome --apply
```

For Edge replace `chrome` with `edge`; `chromium` is also supported. Preparation
prints the extension directory. Load it through the browser's normal extensions
page as an unpacked developer extension, once. This install permission is not a
remote-debugging authorization. The native host is named `com.stilldesk.browser`
and allows only the extension ID derived from the packaged public key.

Each running broker accepts one extension connection. Install in the browser
profile you intend the agent to use; simultaneous Chrome/Edge or multiple-profile
multiplexing is not implemented in this early-access version. A second native
connection is rejected rather than silently replacing the first. Separate
process environments/runtimes can be isolated with `STILLDESK_HOME`; the normal
single-instance setup does not automatically route between browsers.

State belongs outside this source tree, under `$STILLDESK_HOME/browser` or
`~/.stilldesk/browser` by default. Preparation pins an absolute runtime path in
the native launcher: `STILLDESK_BROWSER_RUNTIME` if configured at preparation,
otherwise the selected browser state directory plus `runtime`. A browser that
was already running need not inherit the agent's environment. MCP `--runtime`
must match the printed `runtimeDirectory`; changing a custom home or runtime
requires preparing and registering the launcher again. Current-user registration
is reversible and refuses to overwrite subsequent edits on rollback. Windows
batch launchers reject quote, percent-expansion and control characters in paths
rather than interpreting them as commands.

```sh
node cli.mjs list_pages
node mcp-server.mjs --allow-upload-root /absolute/approved/uploads \
  --allow-download-root /absolute/browser/downloads
```

Uploads are refused by default until an upload directory is explicitly allowed.
Downloads use the browser's configured download directory, without a Save As
dialog. The download root grants only independent local-byte verification;
it does not change the browser's configured destination. Both upload and local
download verification are bounded to 32 MiB per operation. Download completion
and independently verified hashes are separate result fields.

## Quiet operation

- New tabs always open in the background; `background=false` is rejected.
- `select_page` is logical selection only. It never activates a tab or window.
- Closing the currently active tab is refused to protect the user view.
- File upload populates an actual file input, including hidden inputs, rather
  than opening the system file picker.
- Pointer, keyboard and drag operations are DOM events. They do not move the
  system pointer, press global keys, or grant trusted user activation.
- A new-window HTML link opens as a background tab. Its custom click handler is
  not executed on that route; inspect the resulting page.
- Screenshots are limited to the already active tab's visible viewport. Asking
  for a background-tab screenshot returns `background_screenshot_unsupported`
  without activation. Native JavaScript dialogs return `native_dialog_unsupported`.

The 24 MCP tools cover tabs, per-tab zoom, navigation/history, document-bound snapshots,
text, click/fill/forms, synthetic hover/keys, scroll/select/check/drag, literal
waiting, chunked upload, browser download and download status, and the explicitly
bounded screenshot/dialog surfaces. Snapshots are derived from DOM, not a full
browser accessibility tree. Refresh snapshots after document changes.

## Verification and page captures

See [verification](docs/VERIFICATION.md) and the repository `tests/browser`
summaries. The live test uses independent profiles and local fixtures; it does
not use an existing account or send anything to a real website.

```sh
node --test tests/*.test.mjs
# Set STILLDESK_BROWSER_TEST_BROWSER to your compatible browser executable.
# Set STILLDESK_BROWSER_TEST_FAMILY to chrome, chromium or edge.
node scripts/e2e.mjs
```

For a local website preview, `capture-page.mjs` opens a dedicated headless
browser. Its active tab can be captured without displaying a window to the user:

```sh
node scripts/capture-page.mjs --executable /path/to/chrome \
  --url http://127.0.0.1:8080/ --output /absolute/viewport.png \
  --width 1440 --height 1000
```

This helper also uses the extension and Native Messaging; it does not introduce
a debugging channel. It temporarily registers only this project's native host
and restores registration afterward. Avoid concurrent registration or capture
commands for the same browser family.

## Remove

```sh
node scripts/register-native-host.mjs --browser=chrome --rollback
node cli.mjs stop
```

Remove the unpacked extension from the browser and the MCP entry from the client.
