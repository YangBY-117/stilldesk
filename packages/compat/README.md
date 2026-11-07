# Agent client configurations

StillDesk exposes a CLI and local MCP stdio server. This package generates the documented configuration shapes for **15 client targets**. It never starts a client, edits an installed client configuration, weakens approval settings, or transports existing client credentials.

```js
import { generateConfig, listClients } from './packages/compat/index.mjs';
const example = generateConfig('gemini', { root: '/absolute/path/to/stilldesk' });
console.log(example.content);
```

Use the repository command `node bin/stilldesk.mjs config <client>` to generate a reviewable snippet. Merge only the named server. Optional `uploads` and `downloads` entries become explicit `--allow-upload-root` and `--allow-download-root` arguments; no filesystem root is allowed automatically.

## Compatibility levels

All entries below are **official-documentation checked and statically tested configurations**. The shared MCP endpoint has separate protocol tests in the main project. This package does not claim end-to-end runtime acceptance in all 15 client applications. In particular, **Claude Code was not opened or executed**, as requested.

| Target | Generated shape / destination | Evidence |
| --- | --- | --- |
| Codex | TOML `mcp_servers`, `~/.codex/config.toml` | [Official MCP docs](https://developers.openai.com/codex/mcp); TOML parsed with Python's standard library |
| Claude Code | JSON `mcpServers`, project `.mcp.json` | [Official docs](https://code.claude.com/docs/en/mcp); configuration only |
| Gemini CLI | JSON `mcpServers`, `~/.gemini/settings.json` | [Official docs](https://geminicli.com/docs/tools/mcp-server/) |
| Kimi Code | JSON `mcpServers`, `~/.kimi-code/mcp.json` or project `.kimi-code/mcp.json` | [MCP docs](https://moonshotai.github.io/kimi-code/en/customization/mcp.html), [data-root docs](https://moonshotai.github.io/kimi-code/en/configuration/data-locations.html) |
| ZCode | JSON `mcpServers`, paste in MCP Full configuration | [Official docs](https://zcode.z.ai/en/docs/mcp-services) |
| DeepSeek Harness | YAML Cordis entry for `@deepseek-ai/dsh-mcp-client` | [Official package reference](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md) |
| OpenClaw | JSON `mcp.servers` with `transport:"stdio"` | [Official docs](https://docs.openclaw.ai/tools/mcp) |
| Hermes Agent | YAML `mcp_servers`, `~/.hermes/config.yaml` | [Official docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/) |
| Cursor | JSON `mcpServers`, `.cursor/mcp.json` | [Official docs](https://cursor.com/docs/context/mcp) |
| VS Code local harness | JSON `servers`, `.vscode/mcp.json` | [Official format reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) |
| GitHub Copilot portable harness | JSON `mcpServers`, workspace `.mcp.json` or user `mcp-config.json` | [Official format reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) |
| Cline | JSON `mcpServers`, Cline's MCP settings | [Official docs](https://docs.cline.bot/mcp/mcp-overview) |
| OpenCode V2 | JSON `mcp.servers`, command array, `type:"local"` | [Official docs](https://opencode.ai/docs/mcp-servers/); V2 does not use the older `mcp.<name>` shape |
| ChatGPT / Codex desktop plugin | Root `plugin.json` and portable `mcp.json` | [Official packaging docs](https://developers.openai.com/plugins/build/plugins) |
| Other MCP clients | Generic JSON `mcpServers` server definition | [MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) |

Examples live in `examples/`. They use placeholder source paths and contain no secrets. Generate fresh paths on the machine where the MCP server will run. A remote harness cannot access a different machine's browser or native apps merely by copying a local stdio configuration.

## ChatGPT plugin and remote access

Current portable plugins use root `plugin.json`, `mcp.json`, optional `skills/` and `assets/`. A `.codex-plugin/plugin.json` overlay is optional compatibility material; the obsolete `ai-plugin.json` pattern is not used. The generated manifest is packaging material, **not a plugin-directory listing or submission receipt**.

The main project can expose an authenticated local Streamable HTTP endpoint. ChatGPT cloud deployment additionally needs an accessible HTTPS endpoint and a supported authentication/installation flow. A local port, static bearer-token fixture test or manifest file does not prove cloud/plugin-directory acceptance. StillDesk does not upload private messages or open a public tunnel automatically.

## Validation

Run `node --test packages/compat/tests/*.test.mjs`. Six tests validate all generated JSON examples, client-specific structures, quoting and explicit upload roots, the static-only Claude Code contract, Codex TOML parsing, and separation of plugin metadata from deployment claims. No harness is launched by these tests.
