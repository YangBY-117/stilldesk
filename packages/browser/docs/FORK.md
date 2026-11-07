# Source fork and license notes

StillDesk Browser 0.2 is derived from the author's Chrome Direct Lab 0.1
prototype. Only an explicit source/test/sample allowlist was copied. No browser
vendor binary, runtime credential, account data, original history, private
profile, or original evidence bundle was copied into this public package.

The implementation uses Node built-ins and Chrome/Edge extension APIs, with no
third-party npm runtime dependency. The repository MIT license applies to this
package. Product names identify compatibility, not endorsement.

The prototype's REA investigation identified the prior remote-debugging upload
chain; this fork changes source available in full and does not claim to
reverse-engineer Chrome or Edge native binaries. Live behavior is established
by the fixture results, separately from static source inspection.

Original prototype source fingerprints before adaptation:

| Relative source | SHA-256 |
| --- | --- |
| `broker.mjs` | `ba7932b6582de431ada9faeed61f52b3bba02e1a08eca57681c4440f21231e0f` |
| `native-host.mjs` | `db4d65a5e26bb0d0de49565ffeb3d00f4df972aa28fbe240bf800b116e62a754` |
| `mcp-server.mjs` | `b319245217f8556109c8dfeebf2b22bbececc5b9dbc9601b4e9b50f800fbf677` |
| `cli.mjs` | `b39a76562d0f5396947b6e910c19c4b5077b23b8aba967d3e7a9fd82a93dc7fe` |
| `lib/transport.mjs` | `aef91398e462f3dbc740e821c0377489e5be9dc2e552b85fe5b02d2d977d9f63` |
| `extension/actions.js` | `92f4dad7bca69898eed32abbeb2cacf904242ff16b6d390d995f1316f68cdd93` |
| `extension/dom.js` | `946c08c50d5d99317ecb4e926a307dda97f36029e402cbd0ca7fa83a68524d06` |
| `extension/background.js` | `68bc1802752e344578cb39dea923ef527eb56081ec482aa49351c1c13b4d51fb` |
| `scripts/prepare.mjs` | `49a4ffb68bbca3c130e48da75c9bf2c8d9e18985f08a6f7b67289b2ad113e17d` |
| `scripts/register-native-host.mjs` | `d555a9e55fbc2e4de90a26feaff7f7454327efd929c73cfd00bdb01498be178a` |
| `scripts/e2e.mjs` | `7059a8d52c55b6fa41db3cdb8bfb75d88fd232a974e01680e90caff79cb2693e` |
| `tests/transport.test.mjs` | `58ed15c54e846bc94e35acb2f495342bde567fc16c0f8c8ffb5e1cab191507ec` |
