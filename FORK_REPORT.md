# Public source preparation

StillDesk is assembled into a new public staging tree rather than publishing
the original working directories or their histories.

| Component | Preparation |
| --- | --- |
| Browser | Forked the original no-debugging extension experiment, parameterized runtime paths, added Edge and background-only behavior, and strengthened local IPC |
| WeChat | Inherited the 0.5.0 read/diagnostic repairs, retained attribution, removed legacy foreground execution modules, and exposed missing native actions explicitly |
| QQ | Copied the account-scoped local reader and added stable read ergonomics and background capability reporting |
| Feishu / Tencent Meeting | Added fixed native/service operations; copied only portable local meeting source without user sessions, models or vendor binaries |
| Client compatibility | Generated format-specific examples from primary documentation without launching harnesses |
| Router / website | Newly authored, with local setup and independently inspected capabilities |

Only source, synthetic tests, attribution, documentation and public visual assets
are prepared for publication. Exclusions include `.local`, `.runtime`, `.private`,
virtual environments, decrypted indices, databases, recordings, memory dumps,
runtime credentials, browser profiles, cached dependency binaries and original
private logs. Runtime native-host launchers are generated outside the repository.

Per-component source manifests contain logical names and SHA-256 values. The
original private configuration, existing app registration and native clients are
not overwritten by creating this fork.

The release is a fresh Git repository. No private source history is imported.
The independent sanitizer report and source checks are required before push.
