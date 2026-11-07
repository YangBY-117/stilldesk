# Source and third-party notes

The local meeting engine, journals and Windows audio helper were authored for the existing local meeting-assistant project and copied into this isolated public tree. The source manifest records the original file hashes. No existing user session, downloaded model, runtime credential, client executable or vendor DLL was copied.

The Windows process-loopback interface follows Microsoft's [ApplicationLoopback sample](https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/ApplicationLoopback). No injection, suspension or patching of Tencent Meeting is performed.

Offline recognition uses [sherpa-onnx SenseVoice](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html). Its model and dependency licenses remain applicable and must be retained by installers. Models are downloaded explicitly rather than included in this source repository.

[Lark CLI](https://github.com/larksuite/cli) and [Tencent Meeting CLI](https://github.com/TencentCloud/tencentmeeting-cli) are independent upstream programs. StillDesk calls installed copies; their binaries and source trees are not vendored into this package. Each upstream's own license and account authorization apply.
