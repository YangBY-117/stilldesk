# StillDesk WeChat

Background-only tools for account-bound local WeChat history, search, images and verified cached attachments. This is the public fork of the local 0.5.0 repair; no account databases, credentials or captured client memory are included.

The package deliberately does not ship the old desktop driver, accessibility gate or global-input modules. Text and media sending can be previewed, but execution returns `background_backend_unavailable`. Missing attachments cannot currently be fetched from the network. No operation will bring WeChat forward to finish a request.

## Setup

Windows, Python 3.11+ and an existing, explicitly selected WeChat account are required for native local reads. Protocol discovery and capability queries can run without an account.

```powershell
python -m pip install -e .
python -m wechat_bridge.cli capabilities
python -m wechat_bridge.cli status
python -m wechat_bridge.cli --config '<private-state>/config.json' init --account-dir '<selected-account-directory>' --executable '<Weixin.exe>' --state-dir '<private-state>'
python -m wechat_bridge.cli --config '<private-state>/config.json' sync
python -m wechat_bridge.server --config '<private-state>/config.json'
```

`init` never selects an account automatically. Existing client processes are reused. A hidden window, tray icon, unavailable UI or unknown remote login state never triggers another login.

History supports cursor-only continuation, search cursors, `refresh=false`, compact previews, and `wechat_get_message` with same-chat context and long-text segments. Cached attachment export revalidates the exact message/card and file digest; an existing local copy is not called a network download.

The MCP surface has 16 tools. Call `wechat_capabilities` to distinguish implemented background operations from unavailable native actions. `allow_desktop=true` is rejected even when a legacy configuration enabled desktop actions.

See [AUDIT.md](AUDIT.md), [VERIFICATION.md](VERIFICATION.md) and [THIRD_PARTY.md](THIRD_PARTY.md). Keep all configurations, decrypted snapshots, messages, DPAPI material and exports in a private state directory outside the account source and repository.
