# Third-party attribution

The WCDB Config.Cipher discovery layout in `wechat_bridge/keys.py` and the
SQLCipher4 format guidance used by this project are adapted from
[fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica),
commit `d322c4a9b1124228820bab363e5676016f188fc6`, `wechatauto/db.py`.
Copyright belongs to the original contributors. The upstream license is Apache-2.0;
its complete text is in `licenses/wechatauto-Apache-2.0.txt`.

The local DAT image layouts, Config/global_config image-key derivation guidance,
and read-only AES candidate discovery in `wechat_bridge/media.py` additionally
reference `wechatauto/media.py` and `wechatauto/db.py` at that same commit.
The public implementation retains its attribution and the Apache-2.0 license.

This bridge uses its own account-locked key storage, message access surface and
cursor semantics. It does not import upstream UI automation, sending, media,
recall, logging setup, or executable fallback modules. The historical reference source is not bundled in this release.

The 0.3.0 UIA adapter and reversible accessibility gate additionally reference
the UI control names, Qt active-byte pattern and 4.1.15.13 RVA documented in
`wechatauto/uia_driver.py` and `wechatauto/accessibility.py` at the same fixed
commit. The UI adapters and their process-write gates are not shipped in StillDesk.
That historical implementation verified DLL identity and UI input checks.
StillDesk excludes it and does not depend on `uiautomation` or `comtypes`.
The remaining local-reader attribution and applicable licenses are retained.

SQLCipher: <https://www.zetetic.net/sqlcipher/design/>.
SQLite WAL: <https://www.sqlite.org/fileformat2.html#walformat>.
MCP Python SDK: <https://github.com/modelcontextprotocol/python-sdk/tree/v1.x>.

`imageio-ffmpeg` supplies the local FFmpeg binary used for WXGF/HEVC first-frame
decoding. Its upstream and the bundled binary's licensing information are
available at <https://github.com/imageio/imageio-ffmpeg> and <https://ffmpeg.org/legal.html>.
The bridge invokes it with fixed arguments, local pipe input, bounded pixels,
and a timeout; it does not import upstream Weixin GUI/download functionality.
