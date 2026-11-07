# 实现来源与许可边界

本项目的 QQ 读取模块独立实现。Windows 只读内存、当前用户 DPAPI 和认证快照的基础结构复用本工作区原有 WeChat bridge 自写实现，再按本机 QQNT 数据格式适配；运行时不导入 WeChat bridge。

Protobuf 字段事实参照 MIT 的 artiga033/ntdb_unwrap，固定提交 `b1c5420a1957a833787b57f616243d4fa120526d`。本项目自行实现有界 wire parser，未复制生成代码或导入其 VFS、调试器和 CLI。该上游及其许可证可见：

- https://github.com/artiga033/ntdb_unwrap/tree/b1c5420a1957a833787b57f616243d4fa120526d
- https://github.com/artiga033/ntdb_unwrap/blob/b1c5420a1957a833787b57f616243d4fa120526d/LICENSE

SQLCipher 的加密布局依据官方设计并以本机页面校验确定参数；实现使用 Python hashlib/hmac 和 cryptography，不包含 SQLCipher C 源码。

未导入 QQDecrypt 的自定义非商用代码、nt_msg_db_util 的 GPL 代码、NapCat 的受限混合许可代码，也没有执行任何上游提钥、进程调试、原库 rekey 或消息发送脚本。

Python 依赖及其实际固定版本保存在 requirements.lock.txt；这些依赖保留各自的原许可证。这里保留上述公开来源链接，不打包私人研究证据或厂商二进制。
