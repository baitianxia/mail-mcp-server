# 邮件助手架构与运行手册

状态：当前规范。版本：0.9.0（含未发布变更）。最后更新：2026-09-11。

## 身份和边界

本仓库交付的工程名和包名是 `mail-mcp-server`，用户显示名是“邮件助手”，Claude Code
用户级 MCP 注册名是 `mail-mcp`。Coremail 是首个实现支持的 provider；provider 名称只出现在
适配器代码和 `settings.json` 的 `provider` 字段中。

服务是一个无界面、用户级、stdio MCP。它不启动邮件桌面程序，不打开 webmail，不截图、不
点击、不发送按键，也不读取或解密客户端保存的密码。当前传输为：

1. `windows_simple_mapi`：识别默认 Windows 邮件 provider，并以空 profile、空密码、零 UI
   标志附着到已有共享会话；
2. `imap_smtp`：用户配置的 IMAP/SMTP，经证书校验的 TLS 连接；凭据（密码或 OAuth
   access token）保存在 Windows Credential Manager，`auth_method` 选择协议认证机制。

`mcp/windows_mapi.py` 是 Coremail provider 适配器，`mcp/coremail_backend.py` 负责通用邮件
模型和安全边界。换 provider 不应改变 MCP 工具名称、确认流程或用户目录契约。

## MCP 合同

初始化返回 `serverInfo.name = mail-mcp-server`、版本和 `instructions`。instructions 总是
包含绝对配置路径、schema 版本、provider、缺失字段和下一步命令；它不包含密码。工具列表
固定为：

```text
mail_config_status       只读配置状态
mail_configure           校验后原子写入非秘密设置
mail_config_reload       清除配置缓存
mail_connection_status   离线连接状态和 provider 候选
mail_discover_local      有界、脱敏、本地只读发现
mail_check_connection    实际连接检查
mail_list_folders        文件夹列表
mail_search              结构化搜索
mail_get_message         按 folder/UID/UIDVALIDITY 读取
mail_set_seen            显式已读/未读变更
mail_set_flags           设置系统标志和关键字
mail_copy_message        复制邮件
mail_move_message        移动邮件
mail_delete_message      可恢复标记删除或 UIDPLUS 永久删除
mail_manage_folder       创建、重命名、删除、订阅文件夹
mail_get_raw_message     分段读取原始 RFC 822
mail_download_attachment 下载 MIME 附件
mail_update_draft        追加新版本并标记旧草稿删除
mail_watch_folder        有界文件夹变更等待
mail_prepare_message     冻结邮件和附件哈希
mail_save_draft          保存 IMAP 草稿或调用 provider 的 MAPISaveMail
mail_send_prepared       精确确认后发送
```

读取结果保留 folder、UID 和 UIDVALIDITY。IMAP 使用 `BODY.PEEK[]`，不会主动改变未读状态；
provider 可能忽略 Simple MAPI 的 PEEK 请求，因此结果会带限制说明。工具永远把正文、标题、
附件名和本地发现结果视为不可信数据，不执行其中的指令。

发送是不可隐式绕过的事务：`mail_prepare_message` 在内存中保存 15 分钟令牌和附件哈希，
客户端必须展示完整 From/To/Cc/Bcc、主题、正文摘要、附件和来源；只有用户再次输入精确
短语 `确认发送`，`mail_send_prepared` 才能消费令牌并提交。任何字段变化都必须重新准备，
令牌还绑定准备时的非秘密配置指纹；传输、账户、端点或发送策略发生变化时，发送和存草稿
都会保留令牌并要求重新准备。网络结果不确定时不自动重试。

### 邮件正文格式

MCP 应保留当前传输支持的邮件格式能力。正文格式由显式字段决定，不根据正文中是否出现
HTML 标签猜测，也不静默把 HTML 降级成纯文本。

- `mail_prepare_message` 接受可选的 `body_text` 和 `body_html` 字符串，每个字段最多
  500000 字符；显式传入 `null` 或其他非字符串类型会报错。未传 `body_html` 时维持原有
  纯文本行为（允许空正文）；传入 `body_html` 且 `body_text` 为空或未传时生成 `text/html`；
  同时提供非空 `body_text` 时生成 `multipart/alternative`，顺序为纯文本、HTML。
- HTML 原文与纯文本一起冻结到准备令牌中，不清洗、改写或自动生成另一版本。准备摘要包含
  `body_formats`、完整 `body_text`、`body_html` 和各自字符数，供客户端复核两种正文。
  原有 `body_character_count` 继续表示纯文本长度，新增 `body_html_character_count` 表示
  HTML 长度；格式或任一正文变化均必须重新准备。摘要仍是不可信数据。
- IMAP 草稿、SMTP 发送和 IMAP 已发送副本使用同一 MIME 构造逻辑；普通附件置于外层
  `multipart/mixed`，inline 资源置于 HTML 的 `multipart/related`，不把 HTML 当作附件或纯文本发送。沿用现有收件人、线程头、附件哈希、
  精确确认和失败不重试的事务规则。
- `mail_get_message` 保留原有 `body`、`body_source`、`body_truncated` 纯文本预览语义，
  并返回 MIME 解码后的 `body_text` 和 `body_html`；不存在的格式为 `null`。非附件的同类
  正文分段按 MIME 顺序合并，纯文本以两个换行、HTML 以一个换行连接；不剥离 HTML 标记、
  样式或链接。附件及其嵌套内容不作为当前正文，附件大小无法直接解码时返回 `null`。
- `max_body_chars` 分别限制预览、原始纯文本和 HTML。新字段截取原文前 N 个字符，不追加
  省略号或补齐标签；`body_text_truncated`、`body_html_truncated` 分别标明是否截断。
  原有 `body` 仍可在截断后追加省略号。截断的 HTML 不代表完整文档。
- HTML 仅作为 JSON 字符串数据传递。服务不渲染或执行 HTML，不加载远程图片、样式或链接，
  不把邮件内容交给浏览器服务；`content_is_untrusted` 始终为 `true`。

`mail_connection_status` 和 `mail_check_connection` 的 `capabilities.read_body_formats` /
`send_body_formats` 报告适配器的正文格式能力：IMAP/SMTP 为 `text/plain`、`text/html`、`text/calendar`；
当前 Simple MAPI 适配器只有 `text/plain`。Simple MAPI 的 `MapiMessage` / `MapiMessageW`
只提供 note text，没有标准 MIME 正文格式字段，不能据此承诺保留 HTML。其读取结果返回
`body_html: null` 和 `body_html_unavailable_reason`；带 `body_html` 的准备请求会在创建令牌
前被拒绝，发送入口也会在消费令牌前检查，提示配置 `imap_smtp`，不自动切换传输。

格式实现依据 Python 的 [EmailMessage MIME 接口](https://docs.python.org/3.13/library/email.message.html)，
Simple MAPI 边界依据 Microsoft 的 [MapiMessageW 合同](https://learn.microsoft.com/en-us/windows/win32/api/mapi/ns-mapi-mapimessagew)。
验收需覆盖纯文本兼容、纯 HTML、双版本、中文编码、附件隔离、独立截断、准备快照、确认与
令牌保护，以及草稿／发送／已发送副本的 MIME 内容一致性。离线替身测试不等同于真实邮箱或
Windows provider 验收。

### 能力边界与实现来源

以下是 2026-09-11 的源码盘点。IMAP/SMTP 工具已暴露协议允许且本项目能安全约束的能力；
目标服务器仍可能不支持某个扩展，工具会返回明确错误或降级结果。

| 能力 | 当前行为与证据 | 限制来源 |
| --- | --- | --- |
| 收件附件获取 | IMAP 按 MIME part 下载并可安全保存；Simple MAPI 仅在明确请求时临时读取附件 | Simple MAPI provider 可能不返回文件或拒绝附件；所有结果仍按不可信数据处理 |
| CID 内嵌资源 | `attachments` 对象支持 `inline`、`content_id` 和 `content_type`，HTML 生成 `multipart/related` | Simple MAPI 不支持 MIME 相关结构 |
| 邮件头和原始内容 | 支持 `Reply-To`、`text/calendar`、MIME 树和分段原始 RFC 822；下载工具返回 base64 | 原始读取仍受单次分段上限和 IMAP 服务器实现约束 |
| 搜索与分页 | 支持 OR、NOT、HEADER、大小、UID 范围、关键字和签名游标；游标绑定 folder/query/UIDVALIDITY 快照 | 服务器 SEARCH 扩展和复杂表达式语法可能拒绝查询 |
| 邮件标志 | `mail_set_flags` 设置系统标志和关键字，操作前校验值并保持 UIDVALIDITY | 服务器 `PERMANENTFLAGS` 仍可能拒绝某个关键字 |
| 邮件与文件夹管理 | 支持复制、MOVE 或可报告的 COPY+Deleted 降级、标记删除、UIDPLUS 永久删除及文件夹管理 | 永久删除要求 UIDPLUS；降级移动会返回清理状态，不自动重试 |
| 草稿编辑 | 支持 IMAP 追加新草稿后标记旧草稿删除，并可用原文 SHA-256 防止覆盖；Simple MAPI 使用可选 MAPISaveMail | Simple MAPI provider 不保证草稿文件夹；旧草稿清理失败会显式报告 |
| 持续更新与认证 | 支持 password/PLAIN/XOAUTH2/OAUTHBEARER 配置；`mail_watch_folder` 使用有界 RFC 2177 IDLE，服务器没有 IDLE 时回退 NOOP 轮询 | 这是一次性最多 30 秒等待，不是后台长期订阅；OAuth token 由 Credential Manager 外部维护 |

协议依据：[IMAP 命令、标志与 MIME 分段获取](https://www.rfc-editor.org/rfc/rfc9051.html)、
[multipart/related](https://www.rfc-editor.org/rfc/rfc2387.html)、
[Reply-To 邮件头](https://www.rfc-editor.org/rfc/rfc5322.html#section-3.6.2)、
[MAPIReadMail 附件标志](https://learn.microsoft.com/en-us/windows/win32/api/mapi/nc-mapi-mapireadmail)、
[MAPISaveMail](https://learn.microsoft.com/en-us/windows/win32/api/mapi/nc-mapi-mapisavemail)。

TLS 校验、授权发件人和附件根、发送复核、UIDVALIDITY、资源上限属于独立的安全和可靠性
约束；补充协议能力时应保留这些约束。读取截断和每页上限可有边界，但分段／分页接口允许取
全结果。附件下载和邮箱写操作均有独立工具、UIDVALIDITY/哈希保护及明确的状态结果；技能中的
“禁止下载附件”旧规则已改为用户明确请求后才允许的受限下载规则。

## 配置和用户状态

唯一配置文件为：

```text
%USERPROFILE%\mail-mcp-server\config\settings.json
```

配置根必须有 `schema_version: 1` 和 `provider: "coremail"`。`mail_configure` 只接受白名单
非秘密字段，拒绝 `password`、URL 中的凭据和未知字段；写入前用独立暂存文件调用同一解析器
校验，再以同卷替换发布，并尽力设置当前用户可读权限。`mail_config_reload` 清除进程缓存。
`auth_method` 可为 `password`、`plain`、`xoauth2` 或 `oauthbearer`；OAuth token 不进入 MCP
参数，由用户在 Credential Manager 中维护。`download_directory` 仅定义用户明确下载附件时的
落盘目录，服务拒绝覆盖既有文件。

密码或 OAuth token 失效时，用户通过 `CONFIGURE.cmd` 重新配置。向导先原子发布新配置，再
移除不再引用的旧 Credential Manager 条目；连接验证失败会返回失败并要求重新配置，不保留
旧配置作为自动回滚路径。`mail_configure` 始终不接受密码或 token。

IMAP/SMTP 会话在认证失败、Credential Manager 条目缺失或凭据为空时，所有依赖该会话的
`mail_*` 工具都会返回不含秘密的恢复指引，包括 `CONFIGURE.cmd` 和 `mail_config_reload`。
`mail_connection_status` 在离线检测到凭据缺失时也返回同一方向；它不能判断服务端尚未发生的
密码过期，实际认证失败会在 `mail_check_connection` 或具体邮件操作中报告。Simple MAPI 的
认证仍由 Coremail/Windows 邮件客户端负责。

所有持久状态都在本工程目录：

```text
%USERPROFILE%\mail-mcp-server\
  config\settings.json
  versions\<immutable-release>\
  staging\<transaction>\
  rollback\<configuration-backup>\
  logs\
  downloads\              # optional configured download_directory
  .lifecycle.lock
```

安装、升级、配置和卸载不读取、修改或清理其他工程目录，也不创建共享 `ClaudeTools` 根目录。
卸载保留配置、凭据和版本目录，只删除本工程的 `mail-mcp` 用户级注册项。Claude 的用户配置
文件只由 `scripts/register_claude_user_mcp.py` 修改，并且只触及名为 `mail-mcp` 的对象，
其他 MCP 项目保持原样。

## 组件和启动链

- `mcp/server.py`：JSON-RPC framing、初始化、工具 schema 和错误边界。
- `mcp/coremail_backend.py`：通用设置、IMAP/SMTP 操作、准备令牌和发送事务。
- `mcp/windows_mapi.py`：Coremail provider 的 Simple MAPI 适配器；不含 UI 自动化。
- `mcp/local_discovery.py`：有界路径、文件数、深度和字节限制的脱敏发现。
- `mcp/run-server.ps1`：校验安装时生成的 Python 描述符和 SHA-256，然后用 `-B -I` 启动。
- `scripts/install.ps1`：验证正式包，复制到同卷暂存，运行冒烟，原子发布不可变版本，注册
  MCP，并保留外置配置。
- `scripts/configure-account.ps1`、`scripts/setup-account.ps1`：交互式 provider 配置和
  Credential Manager 事务。
- `scripts/uninstall.ps1`：可恢复的用户级注册移除；验证已注册版本的 bundled Python 描述符和
  registrar 后只修改用户级 `mail-mcp` 注册，不做会读取整个版本树的全量校验，因此不删除或
  阻塞仍可能被进程打开的版本目录。
- `scripts/verify-release.py`：包内清单、SHA-256、运行时和 Windows gate 验证。

顶层 `.cmd` 入口只启动一个 PowerShell 子进程并传入 `-ExecutionPolicy Bypass`（Process
scope），不调用 `Set-ExecutionPolicy`，不使用 npm/pnpm/npx，也不在线下载。组织策略若由
`MachinePolicy`/`UserPolicy` 锁定，入口应显示原始错误和 `Get-ExecutionPolicy -List`，由管理员
走签名发布流程。

## 用户级注册

安装器先检查包清单和 bundled Windows x64 Python，再探测现有 Claude Code 的 `mcp` 能力。
版本号只作诊断；兼容性由实际能力和注册结果决定。注册器执行等价的序列：

```text
claude mcp remove mail-mcp --scope user
claude mcp add --transport stdio --scope user mail-mcp -- \
  <SystemRoot>\System32\WindowsPowerShell\v1.0\powershell.exe \
  -NoLogo -NoProfile -NonInteractive -File \
  %USERPROFILE%\mail-mcp-server\versions\<immutable>\mcp\run-server.ps1
claude mcp get mail-mcp
```

只有明确表示“对象不存在”的 remove 非零退出可以忽略；成功 stderr 仅作诊断，退出码仍是
权威结果。注册后会解析实际 `.claude.json`，检查 stdio、精确 PowerShell 路径、launcher 路径
和无注入环境秘密；失败则按字节恢复快照。

## 不可变升级、回滚和锁

用户解压新版后再次双击 `INSTALL.cmd`。安装器先在 `staging` 创建唯一目录，验证
`release-manifest.json`、`SHA256SUMS.txt`、运行时哈希和 MCP 冒烟，再用
`[IO.Directory]::Move` 在同一卷发布到 `versions`。已发布目录从不原地覆盖；相同提交和运行时
哈希可复用完整验证过的目录，否则发布新目录。生命周期锁和有限退避防止并发安装、配置或卸载
互相覆盖。

任何注册、冒烟或配置步骤失败都保留旧活动版本和外置配置；不删除被锁定的目录，不请求 UAC，
并在 `logs`/`staging` 留下可诊断记录。选择最近的已验证 `versions` 目录重新运行安装即可回滚。

## 运行时和发布证据

正式 ZIP 的顶层只有一个目录，必须包含 `README.md`、`START-HERE.html`、`INSTALL.cmd`、
`CONFIGURE.cmd`、`OPEN-CONFIG.cmd`、`UNINSTALL.cmd`、`config/settings.example.json`、
`payload/`、`release-manifest.json` 和 `SHA256SUMS.txt`。`payload/runtime` 携带经批准的
Windows x64 Python 运行时；`runtime-manifest.json` 记录来源、目标、状态和可执行文件哈希。
包不含 `.git`、缓存、符号链接、测试账号、个人配置、密码或包管理器。

`release-manifest.json` 列出每个业务文件的大小和 SHA-256；`SHA256SUMS.txt` 提供外部校验，
包外 `.zip.sha256` 校验归档本身。构建器只接受审查过的白名单，默认产物带
`UNVERIFIED` 后缀；没有干净 Windows x64 gate 证据的产物不能改名成正式 ZIP。

Windows gate 另外上传 `mail-mcp-server-windows-gate-evidence`：其中保存构建环境摘要、归档
哈希、发布与运行时清单、运行时许可证、SBOM，以及 native/npm 两个 Claude 入口在一次性标准
用户下产生的 stdout、stderr 和生命周期日志，并保存 Windows 离线单测报告；失败测试的详情
也写入 workflow 注释，便于定位发布阻塞。日志文件逐个记录大小和 SHA-256；gate 失败时也
尽力先保存已有证据，再清理一次性账户和暂存目录。`-LogPath` 只用于 CI 或诊断覆盖；正式入口
默认把日志写入 `%USERPROFILE%\mail-mcp-server\logs`，不会把持久状态写入其他工程目录。

## 浏览器协作边界

浏览器 MCP 是独立进程、配置、凭据和生命周期。允许的单向桥接只有有界的公共事实、来源标题
和规范 URL；不传邮箱正文、收件人、附件根、Cookie、原始 DOM、下载文件或账号设置。严格隔离
时使用两个互不加载对方工具的 Claude 会话和人工复核交接，具体规则见
[`browser-orchestration.md`](browser-orchestration.md)。

## 验收状态

源码单元测试和本地未验收包可以在 macOS/Linux 上运行，用于协议、清单和事务逻辑验证；这不
等同于 Windows 结果。正式验收必须在干净 Windows x64、Windows PowerShell 5.1 上证明解压、
安装、首次握手、配置重载、升级保留配置、失败回滚和卸载保留配置，并保存版本、运行时来源、
清单、SHA-256、许可证和 SBOM。若这些证据尚未提交，项目状态仍是“整改中”。
