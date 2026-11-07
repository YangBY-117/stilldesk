# Third-party components and provenance

StillDesk's original router, browser extension, CLI, compatibility generator,
website and integration changes are MIT-licensed. Dependencies and incorporated
upstream work keep their own licenses.

- **WeChat local reader:** the repaired local bridge is included as a public,
  account-configuration-free fork. Its cipher/key-layout and media guidance
  includes work from `fanyuantaier/wechatauto-replica` at commit
  `d322c4a9b1124228820bab363e5676016f188fc6`, Apache-2.0. Retained attribution and
  license text are in `integrations/wechat/THIRD_PARTY.md` and its referenced
  license file. No upstream foreground automation is imported or distributed.
- **QQ local reader:** a public fork of the original local QQNT reader; see
  `integrations/qq/THIRD_PARTY.md` for its source and dependency notices.
- **Tencent Meeting background audio:** owned local process-loopback and
  transcription source is included without compiled executables, ASR models,
  recordings or user sessions. It references Microsoft's ApplicationLoopback
  example and sherpa-onnx; see `packages/platforms/THIRD_PARTY.md`.
- **Feishu and Tencent cloud services:** the project invokes separately
  installed official/native CLIs or documented interfaces. Their binaries and
  credentials are not distributed here. Their terms and licenses remain theirs.
- **MCP Python SDK, cryptography, psutil, Pillow, APSW, zstandard and media
  dependencies:** installed by the optional Python integration environment and
  governed by their published licenses. No dependency environment is included
  in the source archive. `imageio-ffmpeg` may supply a separately installed
  FFmpeg executable; review its notices for the distributed build.
- **Chrome, Chrome for Testing, Chromium and Microsoft Edge:** installed
  separately. Browser binaries, drivers and vendor source bundles are not
  included. The StillDesk browser package is an original extension/native host.

Source manifests record logical source names and hashes without local user
paths. Private REA results, memory snapshots and account artifacts are excluded.
The website brand image is AI-generated artwork and does not reproduce a vendor
interface or serve as validation evidence. Product and platform names identify
interoperability; no vendor endorsement is claimed.
