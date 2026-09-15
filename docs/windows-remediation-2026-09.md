# WIN-MAIL-001：通用邮件助手 Windows 交付整改

状态：整改中，未通过统一 Windows 实机验收。负责人：项目维护者。优先级：P0。

## 目标

当前工程的用户身份统一为：

- 工程/包名：`mail-mcp-server`
- 用户显示名：邮件助手
- MCP 注册名：`mail-mcp`
- 用户目录：`%USERPROFILE%\mail-mcp-server\`
- 配置：`%USERPROFILE%\mail-mcp-server\config\settings.json`

Coremail 只作为 provider 或适配器名称出现在实现细节和配置 `provider` 字段中，不出现在产品
名、MCP 注册名、安装目录、公开 ZIP 文件名或用户入口标题中。本次是干净起点，不迁移历史名称
或旧 MCP 别名。

## 交付要求

1. 包、插件清单、MCP 注册、工具描述、日志标识和文档使用通用邮件身份；provider 细节与
   通用邮件能力分层。
2. 配置、运行时版本、锁、日志、回滚记录和注册项限制在本工程目录，不使用共享
   `ClaudeTools`。
3. 提供 `mail_config_status`、`mail_configure`、`mail_config_reload`。状态返回绝对配置路径、
   schema 版本、缺失字段和下一步命令，默认脱敏。
4. 公开制品只有 `mail-mcp-server-<version>-windows-x64.zip`。顶层包含中文
   `README.md`、`START-HERE.html`、`INSTALL.cmd`、`CONFIGURE.cmd`、`OPEN-CONFIG.cmd`、
   `UNINSTALL.cmd`、`config/settings.example.json`、`payload/`、`release-manifest.json` 和
   `SHA256SUMS.txt`。
5. `INSTALL.cmd` 同时支持首次安装和升级：先写入本工程版本目录，完成清单校验、运行时冒烟
   和 MCP 握手，再原子切换；配置在版本目录之外且不被覆盖，失败恢复上一版本，至少保留
   一个最近可用版本。
6. 目标机不运行 npm、pnpm、npx，也不在线下载依赖；正式包携带锁定且可追溯的 Windows x64
   最小运行时，只包含解释器、运行时 DLL、标准库和所需扩展，不包含 Python 文档、头文件、
   导入库、包管理器、测试目录或开发缓存。
7. 中文说明给出解压、安装、首次 Claude 调用、配置、重载、升级、回滚、卸载、日志和常见
   故障最短路径。
8. 同一变更更新 README、架构/运行手册、插件和 MCP 清单、构建脚本及测试，并删除或标记
   过时的 Coremail 产品级表述。

## 验收证据

- [ ] 干净 Windows x64 + Windows PowerShell 5.1 解压后只双击入口即可安装并完成 MCP 握手。
- [ ] Claude Code 能定位、修改并重载 `settings.json`；错误配置给出字段级提示。
- [ ] 覆盖安装保留配置；模拟启动失败时活动版本和配置恢复。
- [ ] 卸载不删除配置，不触及其他工程目录或注册项。
- [ ] `rg` 和打包清单证明公开身份没有 Coremail 产品名；provider 适配器名称可保留在技术层。
- [ ] ZIP、日志、测试输出、示例和提交配置没有真实凭据；提交 Windows 日志、清单、SHA-256、
      运行时来源、许可证和 SBOM。

CI 已配置独立的 `mail-mcp-server-windows-gate-evidence` 证据制品；只有实际 Windows gate 成功
并保存该制品后，才能勾选上述项目。

在上述证据提交前，不把任何本地或跨平台结果标记为“已验收”。

## 完成定义

实现、文档、脚本和证据全部在该仓库提交，并在任务总表登记最终版本和制品哈希后，才可把
状态改为“已验收”。
