# StillDesk release sanitization review

Review date: 2026-11-07. Final verdict: **PASS WITH WARNINGS**.

No unresolved publication-blocking finding remains in the reviewed source and
staged public candidate. The warnings below describe the release's tested scope
and deployment boundaries; they are not missing secret-removal fixes.

## Reviewed public boundary

The final Git index contained **194 files**: 41 JavaScript files, 59 Python files,
32 JSON files, and documentation, test results, configuration examples and assets.
Every staged blob was read directly from Git's object store and checked. There
were no unstaged paths at that scan. The recursive source-check set additionally
contains two optional synthetic browser-fixture PNGs; both were independently
inspected and contain no account or personal content.

The candidate contains no private/runtime directories, local environments,
generated Python caches, account databases, decrypted records, credentials,
vendor executables or libraries, models, memory dumps, recordings, or source
account sessions. Git ignore behavior was checked for `.private`, `.venv`,
`.env`, generated caches and legacy local runtime directories. The allowlisted
`.env.example` contains placeholders only.

The indexed credential/private-key, static-secret and private-user-path scans
returned **zero findings**. Diagnostics recorded categories, relative paths and
line numbers rather than any possible credential values.

## Six review categories

| Category | Result |
| --- | --- |
| Credentials and secrets | No high-confidence credential, private key or static-secret value found in staged blobs. Runtime credentials are not packaged. |
| Personal information | No account identifiers, messages, recordings, personal absolute paths or private email values found in the public candidate. The declared public author/repository identity is intentional. |
| Internal references | Source provenance uses public upstream links, relative filenames and hashes. Private investigation captures and source-user paths are excluded. |
| Dangerous/private files | No vendor binary, database, dump, private key, account session or recording is staged. Public image assets are addressed separately below. |
| Configuration and trust boundaries | Native commands are fixed and structured; unsupported capabilities fail explicitly; private state and local IPC protections were reviewed and remediated. |
| Git history | This is a newly initialized public staging repository. It had zero commits at review; no private repository history was imported. The first commit must use the reviewed staged tree plus this final report. |

## Findings fixed before approval

- **Private meeting state:** daemon credentials and session journals were moved
  out of the source checkout into per-user state. Atomic writes and exact private
  permissions apply to credentials, reused directories and existing journals.
  Windows state replaces broad explicit grants and sets the current owner;
  linked/junction state objects are rejected. POSIX state uses private modes and
  rejects objects owned by another user.
- **Windows access rules:** browser and platform state no longer rely on partial
  grants that could leave a pre-existing Everyone rule. Directory and
  credential-file fixtures exercise replacement by a current-user/System DACL.
- **Local IPC authentication:** the browser uses mutual HMAC challenge/proof
  authentication. A client does not send its static runtime secret to an
  unverified pipe server.
- **Provider lifecycle:** malformed or oversized protocol output stops input
  processing and terminates its child. Concurrent first calls share the provider
  initialization promise. Cancellation and unknown writes are not silently
  replayed as fresh actions.
- **Installed browser routing:** the unified provider and native host share the
  same runtime resolution. Generated native launchers pin the selected runtime
  rather than depending on an already-running browser's old environment.
- **File-operation descriptions:** upload roots and download readback roots are
  forwarded through the unified entry point. Downloads follow browser settings;
  the configured readback root authorizes hashing, not an arbitrary destination.
- **Configuration usability:** the CLI prints actual client configuration by
  default. Metadata is a separate output option. Portable plugin presentation
  fields use the documented `extensions.com.openai.interface` structure.
- **Native mutation receipts:** Feishu sends and local meeting writes bind
  reusable request IDs to arguments. Completed receipts can be reused; unknown
  outcomes and conflicting arguments are not replayed.

## Independent verification performed in this review

- Root MCP/HTTP, compatibility, native-platform and unified-router fixture tests:
  **30 passed** after the implementation fixes.
- Private meeting filesystem tests: **5 passed**, including reused broad grants,
  existing journals, private atomic credentials and source/state separation;
  repeated successfully after ownership hardening.
- Pinned native launcher and stdio/HMAC roundtrip tests: **3 passed**, including
  a nonstandard home and deliberately incorrect browser-parent environment.
- Affected platform DACL and Feishu receipt tests: **2 passed** again after the
  final ownership change.
- Public image metadata and contents were inspected. The indexed brand PNG has
  no EXIF or text metadata. The eight-frame verification GIF has normal looping
  metadata only and displays the explicitly labeled synthetic test ledger.
- The current OpenCode, OpenClaw and portable OpenAI plugin configuration shapes
  were checked against official documentation. No harness was launched.

The release also retains owning-worker evidence for Chrome and Edge local live
workflows and native messaging tests. The final unified-router result records
**6/6** actual local browser checks, including form routing, real HTTP upload,
download readback/SHA-256 and runtime selection without inherited browser state.
Those records are separate from this review's independently executed fixtures.

No real message was sent, no live meeting audio or microphone was captured, no
account was asked to log in again, and **Claude Code was not opened or executed**
as part of this review.

## Public claims and assets

The operational source contains no remote browser debugging connection or
extension debugger permission, physical-pointer control, global-key injection,
foreground-window activation or hidden UI automation fallback. Documentation
and tests that mention prohibited APIs are not executable use of them.

WeChat's old foreground sender is absent from the public adapter. Unavailable
current-client native writes and uncached attachment retrieval remain visible
in the README and capability matrix. QQ is described as read-only. Feishu and
Tencent account/service authorization are not presented as already accepted.

The brand illustration is explicitly AI-generated and is not a screenshot. The
website's animated workflow is labeled as an illustration. The README GIF is
labeled as a visualization of the committed test ledger, not a desktop recording
or independent performance benchmark. No fabricated adoption, star count,
endorsement or benchmark claim was introduced.

## Retained scope warnings

1. Windows is the live-verified native platform. Other registration locations or
   exported client formats are not live acceptance in every OS/harness.
2. Browser gesture, native-dialog, inactive-tab screenshot and website-specific
   limits remain explicit. Scoped cursor/window checks do not prove that vendor
   apps or websites can never display their own notifications.
3. Current native app capability gaps remain part of the release contract.
   A configuration, a send preview and a fixture are not an authenticated live
   account operation or a delivery receipt.
4. The HTTP transport is authenticated and loopback by default. Public HTTPS,
   connector authorization and plugin-directory approval are separate operator
   deployment steps and are not implied by source publication.
5. Publish only the reviewed tracked source tree. Any later source, dependency,
   asset, configuration, generated archive or history change requires the
   relevant checks and a fresh scan before distribution.

## Follow-up: content-based acquisition consistency

After the initial publication review, the first public source-check run exposed
a real QQ snapshot-consistency failure. The earlier metadata/header checks could
miss a same-length write beyond the observed header when filesystem identity
and timestamps did not change. The shared WeChat acquisition/cache mechanism
had the same underlying gap. The failing case was retained, not weakened or
skipped. A versioned release remained gated on the repaired suites and CI.

This follow-up is a focused code review of the two public `cipher.py` modules,
their acquisition/cache regression tests and updated verification summaries. It
does not replace or repeat the initial full-tree secret scan, and it did not
touch either original private bridge project or any account data.

The reviewed fix now includes the complete encrypted DB and WAL contents in the
source signature. Observation, encrypted acquisition, private-cache hashing and
fallback rollback-backup copying use unbuffered reads of at most 1 MiB per block.
Each read pass consumes only its initially observed length N plus one growth
probe; shortening, growth or identity replacement fails explicitly rather than
following an expanding EOF. Acquisition compares its streamed SHA-256 against
the initial complete digest and rechecks the complete DB/WAL signature after
copying. Cache format and MAC domains are upgraded (QQ v3, WeChat v2).

Changed private-cache input is treated as a cache miss. Failure while making a
fallback backup does not publish the replacement database or stamp. The tests
also preserve independent validation of the acquired historical view when the
real client subsequently commits new data. This remains an unlocked,
per-database checked capture, not a transaction across application shards.

Independent verification against the final follow-up source:

- QQ cipher/acquisition tests: **71 passed**, including all nine new adversarial
  cases for metadata/header collisions, cache refresh, growing input and backup
  failure behavior.
- WeChat cipher/acquisition tests: **34 passed**, including the corresponding
  nine new cases.
- Existing cache-only pagination and long-poll isolation checks: **4 passed**
  across the two adapters. `refresh=false` behavior remains intact.

The owning worker separately reports full-suite results of QQ **153 passed /
2 skipped** and WeChat **277 passed / 1 skipped**. Those complete-suite counts
are distinct from this review's independently executed focused checks.

The N+1 limit is **per pass**, not a constant total-byte or wall-clock budget for
an entire refresh. Full-content observation and validation are linear in input
size, can involve multiple passes, and source-change retries remain capped at
three acquisition attempts. This is a consistency improvement, not a measured
speed claim or a guarantee of freshness at return time.

Follow-up verdict: **no source-review blocker remains for the next CI run**.
That statement does not mark the later CI run, release archive or version tag
as passed. The root release workflow must still verify its final results and
re-scan changed public text before distributing a versioned package.
