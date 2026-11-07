# StillDesk contributor instructions

Build background-first agent tools. Do not seize the user's foreground window,
move the operating-system pointer, type through global keyboard injection, or
silently fall back to UI automation. Unsupported operations must return a typed
capability error. Browser operations use the extension Native Messaging route;
never add CDP, remote-debugging flags, or the debugger extension permission.

Sources in this project were copied into an isolated public staging tree.
Do not edit the original app bridges or their private configuration. Never copy
account databases, messages, tokens, runtime credentials, captured memory,
native vendor binaries, private evidence or existing session recordings here.

Use synthetic fixtures for mutations. Do not send a real message or start a live
meeting recording as a test. Claude Code configuration may be generated and
validated statically, but never launch Claude Code to test it.

Parallel workers own only their assigned files and must preserve others' edits.
Root owns top-level docs, website, assets, releases, and publication decisions.
Browser worker owns packages/browser and tests/browser. Messaging worker owns
integrations/wechat and integrations/qq. Platform worker owns packages/platforms
and packages/compat. Coordinate shared interfaces before changing them.

Evidence must distinguish fixture tests, live application tests, and unverified
client configurations. Never turn a missing adapter into a success response.
