# Native platforms

StillDesk's Feishu/Lark and Tencent Meeting tools use native CLI/service APIs and local process audio. They do not move the pointer, send global keys, activate app windows, or fall back to GUI automation. A missing backend returns `not_configured`; missing permissions, account changes and incomplete pages remain distinct states.

The public interface is `listTools()`, `callTool(name, args, options)`, and `dispatch(platform, action, args, options)`. The repository's unified CLI and MCP server use these functions. There are 25 fixed tool schemas. There is no arbitrary command, login, API-path or JavaScript execution tool.

## Feishu / Lark

Install the [official Lark CLI](https://github.com/larksuite/cli), then configure and authorize the intended identity through that CLI. StillDesk does not start an authorization browser or app window. `feishu_status` performs version, offline doctor, configuration and authentication diagnostics, returning only status fields rather than credential values. A successful status call is not proof that the account is authorized for API operations.

Executable discovery checks the normal PATH and official npm installation layout. If necessary, set `STILLDESK_LARK_CLI` to the absolute native executable path. Windows `.cmd`/`.bat` wrappers are deliberately not fed through a shell.

Tools cover chat search, paged message reads, document search/fetch, agenda and sending plain text. Text sends return a useful preview by default. Execute only after the user has authorized that exact recipient and message, by supplying `dry_run:false`, `authorized:true`, and a reusable `request_id`. The official CLI receives that request ID as its idempotency key. A service response does not establish recipient reading.

## Tencent Meeting cloud records

Install the [official tmeet CLI](https://github.com/TencentCloud/tencentmeeting-cli), or set `STILLDESK_TMEET_CLI` to its absolute native executable. Authorize the intended account in the official CLI yourself. StillDesk exposes a fixed read-only command set: account status, recording search, meeting lookup, recording files/addresses, actual paragraph-ID inventory, transcript pages and generated minutes. It cannot create, cancel or join a meeting through this cloud adapter.

Read `meeting_cloud_status`, then explicitly bind its `account_scope` using the argument on data-reading calls or the `STILLDESK_TMEET_ACCOUNT_SCOPE` environment variable. A different official CLI account produces `account_changed`. The scope is a fingerprint, not a token. Do not publish it with account data.

Read every relevant page. `next_page_token` exhaustion only describes that query and account; `all_history_complete` remains false. Do not guess paragraph IDs: use `meeting_transcript_paragraphs` first. Tencent generated minutes are labeled as summaries, separately from transcripts. Recording address lookup does not download large videos.

## Tencent Meeting local sessions and ASR

The source-only backend under `tencent-meeting/local` includes a Windows process-loopback helper, offline SenseVoice ASR, session journals and coverage events. No model, recording, credential, private session or executable is shipped. Mutable state is kept in `STILLDESK_HOME/meeting` (default `~/.stilldesk/meeting`), separately from the code and models. Runtime/session directories use exact current-user + SYSTEM DACLs on Windows and mode 0700 on POSIX; connection credentials are atomically written with private permissions. Loading an existing session also tightens its journal permissions and rejects linked paths. Python dependencies and a built .NET helper are prerequisites for live capture. This is Windows-specific; other operating systems are not claimed as tested.

To prepare a local backend, review and run `tencent-meeting/local/tools/bootstrap.ps1`. It creates an isolated environment, builds our native helper and downloads the pinned model from its upstream release with a SHA-256 receipt. Model/dependency licenses remain applicable. Set `STILLDESK_PYTHON` to this environment's Python executable. A separately prepared backend can be selected through `STILLDESK_MEETING_LOCAL_ROOT`.

Status, device enumeration and existing-session reads do not start recording. The CLI alias `meeting status` checks local prerequisites; cloud authentication is separately inspected with `meeting cloud_status`. Capture requires an explicit user request, selected `target_pid`, `dry_run:false`, `authorized:true`, and `request_id`. Microphone and audio retention default off. Preview calls do not invoke the backend. Completed write receipts are reusable; uncertain writes are never replayed. Pausing, resuming, stopping and exporting use the same explicit execution contract. `STILLDESK_HOME` also controls private request-receipt storage. Feishu sends use these local argument-bound receipts in addition to the vendor's idempotency key.

Keep `session_id` and transcript `next_seq` together. `draining` is not `finished`; inspect coverage events before claiming a complete transcript. Remote voices are not independently verified people. Existing session data stays private and is never packaged.

## Validation

See [VERIFICATION.md](docs/VERIFICATION.md). Unit tests use injected native runners and synthetic data, including the recording-start contract; no real capture or real message send is used as a test. Native CLI status was checked read-only. Full app/account acceptance is separate from these fixture checks.
