# 使用指南

所有命令均以 `python -m codex_memory` 或安装后 `codex-memory` 运行。`--data-dir` 是全局参数，放在子命令之前。以下 PowerShell 命令使用工程 `.venv`，目录 / 账户标签 / 项目名称请替换为你的真实值。

```powershell
$MemoryPython = '.\.venv\Scripts\python.exe'
$MemoryData = 'D:\Private\MemoryVault'
& $MemoryPython -m codex_memory --data-dir $MemoryData init
& $MemoryPython -m codex_memory --data-dir $MemoryData --help
```

## 历史导入

ChatGPT：从各账户正常申请官方导出，将 ZIP 或 `conversations.json` 明确指定给导入器。同一账户始终用同一标签；账户标签参与稳定 ID，标签改变会视为另一个来源账户。

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData import-chatgpt 'D:\Exports\chatgpt-plus-a.zip' --account plus-a --project personal
& $MemoryPython -m codex_memory --data-dir $MemoryData import-chatgpt 'D:\Exports\conversations.json' --account plus-b --project personal
& $MemoryPython -m codex_memory --data-dir $MemoryData import-codex 'C:\Users\你的用户名\.codex' --account plus-a
```

导入器只读允许的会话数据，保留原始来源并报告损坏 / 不支持记录。不会采集 `auth.json`、Cookie、浏览器个人资料、沙箱秘密或修改 Codex 数据。重复导入不删除已有消息；新内容更新活动版本并保留修订；当前导出缺少旧消息不会使它们消失。

Codex 未指定 `--project` 时按会话 `cwd` 的目录名推断项目，不读取该 `cwd` 的工程文件。同名目录会共用显示标签，`--project <明确名称>` 可覆盖推断；对整个 `.codex` 根目录指定一个项目会将这次导入的所有会话都归入该项目，因此混合多工程的来源通常保留自动推断。

不要在一份包含不同登录账户且缺少可辨身份的 Codex 会话目录上轮流更换账户标签并全量导入：同一历史可能被重标为多个账户。需要按已知账户分组明确指定 JSONL / 独立允许的会话目录，或者使用固定设备标签并在检查点注明当前账户。

## 近实时同步聊天与工程

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData sources add codex 'C:\Users\你的用户名\.codex' --account workstation
& $MemoryPython -m codex_memory --data-dir $MemoryData project add 'D:\Projects\example-project' --project example-project --account workstation
& $MemoryPython -m codex_memory --data-dir $MemoryData sources list
& $MemoryPython -m codex_memory --data-dir $MemoryData sync
& $MemoryPython -m codex_memory --data-dir $MemoryData watch --interval 3
```

`project add` 立即执行归档并注册工程来源。`watch` 定期只读同步已配置来源，也可以用 `sources add project` 手动登记工程来源。仪表盘 `serve --sync-interval 3` 也会同步，不需要同时运行两个轮询进程。Codex JSONL 尚未落盘的内容只能在落盘后读到；半行 / 暂缺记录会报告并在下次再处理。ChatGPT 导出目录可以配置为 `chatgpt` 来源，它只是反复检查你放入的官方导出，不会向 OpenAI 请求历史。

`3` 秒表示一轮同步完成后再等待 3 秒启动下一轮，并非每条消息在 3 秒内保存。较大历史首次导入可能需要数分钟，活跃大文件的校验 / 解析也需要时间；仪表盘写入与浏览器请求可能排队，扩展通过有限队列重试。请以同步状态和成功时间判断进度。

Codex 持续来源会检查文件变更：首次保存完整原始 JSONL；若下一次内容保留旧前缀，校验其哈希后仅保存追加后缀和 SHA256 快照链清单，可沿链追溯完整源字节。索引只提交新增行对应的消息，但仍解析完整已变更文件上下文以正确配对 / 去重。发生改写 / 截断时保存新的完整快照；明确 `import-codex` 仍是普通完整导入，不应当成只读末尾的实时流协议。

Git 工程使用 `git ls-files` 收集允许文件；未纳入 Git 跟踪的文件不会保证覆盖。非 Git 工程按排除规则扫描；秘密文件、构建产物、符号链接、超限文件和记忆库存储目录不收集。工程文件修订留存；这里只提供归档和读取，不主动改写你的工作目录。

Git 来源记录包括 HEAD、分支、`git status --short` 和允许文件的内容哈希 / 快照；没有单独保存完整 `git diff`、Git 对象库或提交历史。这是上下文与文件归档，真实代码仓库仍应单独备份。

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData project files Tao
& $MemoryPython -m codex_memory --data-dir $MemoryData project file Tao 'src/main.py'
& $MemoryPython -m codex_memory --data-dir $MemoryData audit
```

## 浏览器实时采集

加载 `browser-extension` 后，通过仪表盘取得采集令牌。页面脚本只读取 `[data-message-author-role]` 对应的渲染文本，优先使用 `data-message-id`，消息文本变化时提交修订。URL 需包含稳定 `/c/<id>`，首页 / 临时视图不提交。DOM 结构若改变，可能暂时读不到内容，应以扩展状态和仪表盘查询判断覆盖。

采集服务是 `http://127.0.0.1:<port>/api/live`。令牌在 HTTP `Authorization: Bearer` 中传送，不放入 URL；仪表盘的认证 / CSRF 与采集令牌分离。扩展将设置和待提交内容保存在 `chrome.storage.local`；最多 100 批、6 MB，单批不超过 4 MB。断线后最长退避 60 秒，恢复后通过扩展“立即重试”加速。满队列 / 超限会显示错误；尚在页面的未接收批次后续重试，但离开页面前请确认已保存。

暂停归档会停止发送新内容与队列重试，待提交内容仍保留。切换账户时先暂停、更换账户标签，然后打开新账户聊天并重新启用；排队记录保持原账户标签，不会改写归属。不要把采集令牌发给聊天模型或放入 Git。

弹窗的“读取本地记忆”会向保护的 `/api/resume` 发送项目 / 主题名称，直接读取共同记忆库的最新交接内容。点击“复制交接内容”再粘贴到新账户聊天；仅用户点击时写入剪贴板，不读取剪贴板、不自动注入或发送。即使该账户没有可用 MCP 插件，也能完成这种本地读取。归档暂停不影响明确点击的读取动作。

## 搜索、检查点与交接

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData search '凝神' --project Tao
& $MemoryPython -m codex_memory --data-dir $MemoryData list --project Tao
& $MemoryPython -m codex_memory --data-dir $MemoryData show '<上一步返回的内部会话 ID>'
& $MemoryPython -m codex_memory --data-dir $MemoryData checkpoint Tao --goal '完成最近历史提醒' --completed '相关测试通过' --decisions '维持简洁结论优先' --next-steps '验证完整 CLI 路径' --account plus-a
& $MemoryPython -m codex_memory --data-dir $MemoryData handoff --project Tao --output 'D:\Handoffs\Tao-latest-001.md'
```

项目检查点的目标 / 完成事项 / 决策 / 下一步等字段由用户填写，也可在明确启用 MCP 写检查点后请求模型保存；它不是自动猜出的摘要。请在阶段完成、决策改变、换账户或结束工作时追加一次。当前接口只追加检查点，不编辑或覆盖旧记录；纠正内容时保存一份新的检查点，交接取最新记录。交接包包含最新目标 / 进度 / 决策 / 阻塞与精选近期对话及来源，可供另一个账户上传读取。普通聊天可选一个会话导出，或统一归入 `personal` 项目。输出路径不能覆盖已有文件，每次使用新文件名。

## MCP 读取

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData mcp
```

默认 stdio 由客户端启动，不需要手动保持终端。参考 `examples/codex-mcp.toml`，由用户安装到客户端；本工程从不改写全局账户 / MCP 设置。工具可以搜索、分页读取会话、列出项目、恢复项目上下文和读取已归档文件。来源资料不被当成新的系统指令。需要保存检查点时才增加 `--allow-writes`。

MCP 的 `fetch_thread` 同时按消息页和正文总字符预算返回：`max_chars` 默认 50000，可设 1000..100000；`text_truncated` 表示本页正文仍有截断，分页并不保证一条长消息已完整返回。使用搜索 / 会话返回的内部消息 ID 调用 `fetch_message(message_id, offset=0, max_chars=12000)`，再按已返回文本长度推进字符偏移，直到 `has_more` 为 false。该工具只读取索引中已脱敏的消息片段，不打开原始档案或任意本地文件。

`mcp --transport streamable-http --port 8766` 仅绑定回环地址并要求采集 Bearer 令牌。它面向你控制的本地客户端；未实现 ChatGPT 公网连接所需的 OAuth / 公网部署，不应直接把仪表盘或此接口放到公网。普通 ChatGPT 使用交接包即可继续；有合适本地工具 / 插件访问能力的客户端可以直接读 MCP，实际接入需分别验收。

## 备份与恢复

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData backup 'D:\Backups\memory-001.zip'
```

这是 SQLite 一致性快照加原始档案的可迁移备份。路径必须尚不存在，不会自动清理旧备份。数据库 / 原始档案默认未加密。密码加密只保护最终备份容器，不加密运行库。备份通过有上限的内存 SQLite 快照获取一致视图；普通 ZIP 直接写入明确指定的新目标，加密 ZIP 在内存中构建后写入加密容器；恢复也在内存中校验 SQLite，不创建明文暂存数据库 / ZIP。需加密时安装 encryption / all 依赖，以临时环境变量传入密码：

大小上限：SQLite 快照 2 GiB、单个原始档案 512 MiB、备份总展开内容 8 GiB、加密 ZIP 512 MiB。峰值内存可能包含多份数据库 / ZIP 副本，应确保可用 RAM 明显大于需要处理的内存内容；文件未超过上限不代表一定有足够内存。

```powershell
$MemorySecurePassword = Read-Host '备份密码' -AsSecureString
$MemoryCredential = New-Object System.Management.Automation.PSCredential('backup', $MemorySecurePassword)
$env:MEMORY_BACKUP_PASSWORD = $MemoryCredential.GetNetworkCredential().Password
try {
    & $MemoryPython -m codex_memory --data-dir $MemoryData backup 'D:\Backups\memory-002.encrypted' --password-env MEMORY_BACKUP_PASSWORD
} finally {
    [Environment]::SetEnvironmentVariable('MEMORY_BACKUP_PASSWORD', $null, 'Process')
}
```

环境变量仍会进入该进程环境，其他同权限本地进程可能可见。不要在命令参数 / 日志中明文写密码。备份密码丢失无法恢复加密备份。

恢复只能指向新的空目录。复制工程到新电脑并安装依赖后：

```powershell
& $MemoryPython -m codex_memory --data-dir 'D:\Private\MemoryRestored' restore 'D:\Backups\memory-001.zip'
& $MemoryPython -m codex_memory --data-dir 'D:\Private\MemoryRestored' audit
```

跨机器恢复后，来源中的绝对路径仍是旧路径，需要重新指定当前电脑的来源。浏览器也应使用恢复后的仪表盘重新配对；Codex MCP 的 Python / 存储路径由你重新配置。认证账户、OAuth、Cookie 和 API Key 不在备份中。

恢复后的所有来源会标记为 `needs_confirmation` 并暂停自动扫描：旧路径只作为历史数据保留，`sync` / 仪表盘不会因打开备份就读取这些本地路径。先用 `sources list` 检查来源，再明确重新添加同一类型、路径、账户标签与项目以确认并启用；路径已变化时添加当前机器上的新来源，旧来源保持暂停。这仅影响新恢复库，不改变原运行库已有来源。

```powershell
& $MemoryPython -m codex_memory --data-dir 'D:\Private\MemoryRestored' sources list
& $MemoryPython -m codex_memory --data-dir 'D:\Private\MemoryRestored' sources add codex 'C:\Users\你的用户名\.codex' --account workstation
```

上例只有在原来源同为 `codex`、该路径、`workstation` 且项目为空时才确认原条目；其他配置需按实际来源精确填写。明确添加来源是授予该来源只读扫描范围的操作，请检查路径与标签。
