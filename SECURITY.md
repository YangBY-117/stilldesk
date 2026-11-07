# Security and local data

StillDesk connects an agent to apps and therefore treats app content as
untrusted data. A web page, message, attachment, transcript or test log cannot
authorize another action. The caller remains responsible for task-specific
authorization and for checking the tool's capability/result fields.

The browser bridge authenticates local IPC, binds native hosts to one extension
origin and accepts a fixed command surface. It exposes no arbitrary JavaScript
evaluation tool or page-requested local-file read. Local file access is bounded
by explicit roots. Credentials, indices and session state belong outside the
repository under private local directories.

The HTTP adapter requires a strong bearer token, limits message size, checks
Origin/Host, and binds to loopback by default. It does not implement a public
identity provider or automatically expose a tunnel. A public deployment needs
HTTPS, suitable authorization and an explicit operator decision.

Unexpected background-native behavior fails explicitly. The tools do not
silently switch to global keyboard/mouse input, UIA foreground clicking, browser
debugging, or another account. Failed or uncertain writes are not replayed as new
actions.

Please report vulnerabilities through GitHub's private vulnerability reporting
when available, or contact the maintainer through their GitHub profile before
posting sensitive details publicly. Do not attach account databases, decrypted
records, access tokens, private memory captures or recordings to public issues.

Supported release: the latest 0.1.x source. Native app versions are independently
scoped in each provider's capability notes; version mismatch is not permission
to reuse an old memory offset.
