# 变更记录

## 未发布

- MCP 新增 `body_html`，支持 IMAP/SMTP 纯 HTML 和文本／HTML 双版本邮件，草稿、发送和
  已发送副本共用 MIME 构造逻辑；准备摘要包含冻结的正文和格式。
- 读取同时保留解码的纯文本与 HTML，分别报告截断，不再丢弃 HTML；附加邮件的嵌套正文
  不混入当前邮件正文。
- 连接状态说明传输的正文格式能力，当前 Simple MAPI 适配器对 HTML 明确报错，不静默降级。
- 移除网页转邮件流程中的纯文本限定，记录其他协议能力缺口，并区分适配器缺失与协议限制。
- 补齐 IMAP 搜索逻辑组合和签名分页、标志与关键字、复制/移动/删除、文件夹管理、原始 MIME
  分段读取、MIME 附件下载、Reply-To、日历正文、CID 内嵌资源和草稿替换；增加 PLAIN、XOAUTH2、
  OAUTHBEARER 认证配置，并在 provider 支持时接入 Simple MAPI 附件、草稿保存和永久删除。
- 重新配置 IMAP/SMTP 密码或 OAuth token 时，配置向导在发布新配置后移除旧 Credential Manager
  条目；连接验证失败会以失败退出，要求使用修正后的秘密重新配置。
- MCP 运行时的 IMAP/SMTP 认证失败、缺失凭据和空凭据现在返回 `CONFIGURE.cmd`、
  `mail_config_reload` 的恢复指引；`mail_connection_status` 也会报告离线发现的凭据缺失。
- Windows `.cmd` 入口先切换到 UTF-8 控制台代码页，避免安装、配置和卸载提示出现中文乱码。
- Windows 发布构建改为从 CI Python 安装中筛选最小运行时；移除 Python 运行时自带文档、
  头文件、导入库、包管理器、测试目录和开发缓存，安装自检脚本改为生产路径
  `scripts/mcp-healthcheck.ps1`。
- 发布白名单区分用户操作文档与维护材料；架构、开发、测试、构建和内部验收文档不再随正式包发布。

## 0.9.0 — 2026-09-07

- 将用户可见身份统一为 `mail-mcp-server`、邮件助手和 `mail-mcp`；Coremail 仅保留为
  provider/适配器实现名称。
- 采用本工程独立的 `%USERPROFILE%\mail-mcp-server\` 状态根和
  `config\settings.json`，不创建共享工具目录，也不迁移历史名称或旧 MCP 别名。
- 新增 `mail_config_status`、`mail_configure`、`mail_config_reload`，配置状态包含绝对路径、
  schema、缺失字段和下一步命令，密码不会进入 MCP 参数或日志。
- 顶层统一提供 `INSTALL.cmd`、`CONFIGURE.cmd`、`OPEN-CONFIG.cmd`、`UNINSTALL.cmd` 和
  `START-HERE.html`；同一安装入口支持首次安装和升级，外置配置保持不变。
- 发布构建改为单一 `mail-mcp-server-<version>-windows-x64.zip`，包含 bundled runtime、
  `release-manifest.json`、`SHA256SUMS.txt` 和包外归档校验文件；本地候选明确标记
  `UNVERIFIED`。
- Windows gate 单独保存构建摘要、清单、运行时许可证、SBOM 和 native/npm 标准用户生命周期
  日志，失败时也保留已生成的诊断证据。
- 保留无界面 Simple MAPI/IMAP/SMTP、准备—复核—`确认发送` 事务、TLS 校验、脱敏本地发现和
  浏览器单向事实桥接能力。
- 准备令牌绑定复核时的非秘密配置指纹；传输、账户、端点或发送策略改变后必须重新准备，避免
  按新的投递目标发送旧复核内容。

## 0.8.x 及更早版本（历史设计）

早期版本逐步加入了用户级 MCP 注册、不可变版本目录、配置事务、Windows Credential
Manager、Simple MAPI provider、附件哈希和 PowerShell 5.1 兼容性修复。这些历史实现不定义
当前用户身份、路径或迁移行为；从 0.9.0 开始以本文件和 `docs/architecture.md` 为准。
