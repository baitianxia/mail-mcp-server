# Claude Code CLI 通过 NVM 目录联接时被误报为未安装

## 基本信息

- 项目：mail-mcp-server
- 发现日期：2026-09-16
- 严重程度：阻塞
- 适用环境：Windows，CMD 可运行 Claude Code 2.1.84，Node 由 NVM for Windows 管理
- 初始修复基线：`e336283`
- 最终修复：`ee2a6ec`（Windows gate 运行 `35148392589`）

## 症状

正式包清单校验成功后，安装器显示 `Claude Code CLI was not found`。用户在 CMD 中执行
`claude --version` 正常，返回 `2.1.84 (Claude Code)`。

已采集的 `where.exe claude` 结果为：

```text
C:\nvm4w\nodejs\claude
C:\nvm4w\nodejs\claude.cmd
C:\Users\tianxiabai\AppData\Roaming\npm\claude
C:\Users\tianxiabai\AppData\Roaming\npm\claude.cmd
```

## 根因

用户未提供 `where node`，但 `where.exe claude` 已确认入口位于 NVM 管理目录；独立 Windows 门禁已用真实 NVM 目录联接复现并确认。现有代码存在一个确定的错误：
`Resolve-NpmClaudeInvocation` 对命令入口、包目录、bin 文件和 Node 可执行文件统一调用
`Test-CoremailPathChainSafe`，把所有符号链接和目录联接都拒绝了。NVM 的 `C:\nvm4w\nodejs`
通常就是指向当前 Node 版本目录的目录联接，因此一个能运行的 npm Claude CLI 会在候选校验阶段
被丢弃，最后错误地显示成“没有 CLI”。

这是把“发布包/配置路径禁止链接”的安全边界错误套到了用户已安装的外部 CLI 上。

## 修复

- 外部 CLI 通过 Windows 文件句柄的 `GetFinalPathNameByHandle` 解析最终本地路径。
- 最终路径仍校验为普通本地 PE，并继续过滤 `WindowsApps` Desktop 别名。
- npm 包按 UTF-8 读取 `package.json`，校验包名、声明的 bin 和最终 Node 路径。
- 安装日志记录候选的接受、拒绝及最终路径，不再把拒绝原因折叠成“未安装”。
- Windows 源码门禁使用真实文件符号链接和目录联接，覆盖 WinGet、NVM、npm 入口；完整发布包生命周期门禁也通过。

## 验证结果

- macOS 本地单测：86/86 通过。
- Windows gate：运行 `35148392589`，结果为 success；源码探针、完整发布包安装/升级生命周期和发布包上传均通过。
- 用户实际路径：`where.exe claude` 已确认；未要求用户补充 `where node`，因为门禁已覆盖相同的 NVM 拓扑。

