# Verification and native evidence

Date: 2026-11-07. The platform package has 14 Node fixture tests and 5 Python filesystem-only tests, all passing. The compatibility package separately has 6 static-format tests, all passing. Run from the repository root:

```text
node --test packages/platforms/tests/*.test.mjs packages/compat/tests/*.test.mjs
python -m unittest discover -s packages/platforms/tests -p test_private_state.py -v
```

Platform fixtures cover fixed tool discovery, useful previews without credentials, explicit mutation authorization, upstream idempotency keys, literal shell-like text, pagination/timezone boundaries, status credential filtering, Tencent account binding, contradictory cursors, local capture defaults, stale/duplicate mutation receipts, response budgets, cancellation, and missing-backend failures. These tests do not send a real message, create a meeting or start microphone/process capture.

Read-only live diagnostic checks found an installed official Lark CLI **1.0.4** and Tencent CLI **v1.0.19**. Both executables could be invoked with hidden subprocess windows. Lark was not authenticated for the new adapter, so authenticated chat/document acceptance was not performed. Tencent authentication status could be read, but this new adapter did not run account-content acceptance. No account identifiers, credentials or transcripts are retained in this report. No client was asked to log in again.

[REA_SUMMARY.json](REA_SUMMARY.json) pins the exact binaries inspected using REA's `inspect_go_binary`. It records artifact SHA-256 and evidence IDs. That operation is static build-metadata inspection: it does not recover the application's source, establish every native API's behavior, or prove any runtime operation beyond the separate status observations.

The local process-audio backend was copied from the author's existing meeting tool using an explicit 14-file source allowlist. Public changes disable microphone capture by default, remove the legacy standalone Python MCP entry, separate mutable private state from the source/model directory, and require the unified adapter's explicit start contract. Source provenance is in `tencent-meeting/local/SOURCE_MANIFEST.json`. The native .NET helper compiled successfully without being executed. Filesystem fixtures verify externalized state, atomic private credentials, removal of pre-existing explicit Everyone grants, tightening reused session journals, and rejection of relative state roots. Node fixtures separately verify exact directory/file DACLs and parameter-bound mutation receipts. Neither compilation nor these tests are a real meeting-capture acceptance test. Models, native build outputs and private sessions are excluded from source packages.

The no-takeover implementation boundary is structural: fixed service APIs, `shell:false`, `windowsHide:true`, no input/foreground APIs, and no GUI fallback. This is not a claim that every vendor executable can never display any UI in every future version. The tested fixed commands do not invoke vendor login or app activation commands. Any new command must preserve that boundary and be verified before advertising support.

Claude Code was not launched. Harness support refers to documented generated formats and MCP/CLI interoperability, not a claim that every named app was started and accepted the tools.
