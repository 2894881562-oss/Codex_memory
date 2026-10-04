# 可继承个人记忆

本功能把用户明确表达的个人事实、偏好和工作约束保存到同一个本地记忆库，让后续账户在获得读取能力后引用这些记录。它不是 ChatGPT 云端 Memory、原生侧栏或账户身份的迁移，也不调用付费推理 API。更换账户不会自动赋予云端聊天访问本机文件的能力；仍需连接本地 MCP，或由用户审阅后复制 / 上传交接包。[本地接口](../src/codex_memory/service.py)、[跨账户验收](CROSS_ACCOUNT_ACCEPTANCE.md)

## 状态与确认规则

每次保存都是新记录，状态必须明确。省略 `--status` 时默认 `proposed`，不能因为助手觉得某句话像偏好，就自行标为 `confirmed`。

| 状态 | 含义 | 默认进入交接包 |
| --- | --- | --- |
| `proposed` | 待用户核对的候选事实 / 偏好 | 否 |
| `confirmed` | 已选取用户明确原话作为确认来源 | 是，受范围和长度预算约束 |
| `withdrawn` | 撤回该键当前的记忆，不再默认使用 | 否 |

`confirmed` 在交接中表示“用户确认来源”，保存时必须提供已归档、角色为 `user` 的消息内部 ID。助手回复、工具输出和没有来源的概括不能代替用户证据。软件能检查消息是否存在及其角色，但不会自动证明概括准确、原话仍有效或说话者的真实身份。操作者仍应查看原话，确认范围、否定语气和含义，再选择确认状态；不能只找到任意一条用户消息就为无关结论背书。[保存边界](../src/codex_memory/service.py)

平台身份没有经过验证。`--account codex-local` 之类的账户字段只是本地来源标签，不是登录、认证或同一人的证明。已确认的历史偏好仍是参考数据：它不能授权新的文件修改、安装、发送、删除或账户操作，也不能覆盖当前用户明确要求和系统安全规则。

## 范围、最新版本与留存

范围由 `project` 字段决定，不另设 `--scope`：不传 `--project` 表示个人全局记忆；传入 `--project Tao` 只适用于精确匹配该标签的项目。全局已确认记忆自动参与 handoff；项目已确认记忆仅在续接该项目时参与。两者都受交接包字符预算限制，不能保证所有长记录一次返回。

服务按 **`key + project`** 选择最新记录，与账户标签无关。换账户保存同一键、同一项目是在追加修订，不是建立另一个账户独立版本；相同键在全局和某个项目下仍是两个不同范围的记录。最新状态若变为 `proposed` 或 `withdrawn`，旧的 `confirmed` 不会自动回退成为有效记忆。键名应表达稳定含义，值为 1..4000 个字符；键名使用 2..64 个字符的 ASCII 标识。[服务与 CLI](../src/codex_memory/service.py)、[命令参数](../src/codex_memory/cli.py)

记录及其原始 JSON artifact 采用追加留存，原 artifact 不因修订或撤回而覆盖。证据消息 ID 可追溯到归档原话；原始档案、消息修订和记忆记录共同保留审计依据。撤回表示停止使用，不是物理删除或清除备份，也不撤回此前已经分享给其他账户的内容。当前这不是全量索引重建工具；应保留权威数据库及其备份，不把个人记忆表当作可丢弃缓存。[存储与不可覆盖档案](../src/codex_memory/store.py)、[重建边界](REFERENCE_DESIGN.md)

## 如何取得原话消息 ID

先确保用户允许的真实聊天已经导入或采集，再用原话中的关键词搜索。以下均为模板，`<...>` 必须替换为实际值，不要原样运行。

```powershell
$MemoryPython = '.\.venv\Scripts\python.exe'
$MemoryData = 'D:\Codex\Codex_memory\data'
& $MemoryPython -m codex_memory --data-dir $MemoryData search '<用户原话关键词>' --account codex-local --limit 20
& $MemoryPython -m codex_memory --data-dir $MemoryData show '<搜索结果 conversation_id>' --limit 100
```

搜索结果中的 `message_id`，或 `show` 结果中 `messages` 数组里角色为 `user` 的 `id`，就是 `--evidence` 需要的内部消息 ID。它不是 `conversation_id`、平台原始消息 ID、文件行号或助手自行编造的编号。搜索结果只显示短片段，应在会话中核对完整原话；超出当前页时用 `show --offset` 继续读取。

通过 MCP 时先用 `search_memory` / `fetch_thread` 定位，再用 `fetch_message(message_id, offset, max_chars)` 分段核对。面板的聊天阅读器可核对原话；若当前界面没有直接显示可复制的内部 ID，可通过上述 CLI / MCP 获取。网络读取返回的是索引中的脱敏正文，不开放 raw blob 或任意磁盘文件；“原话可查”不代表向新账户暴露认证信息。[消息读取接口](../src/codex_memory/service.py)、[MCP 工具](../src/codex_memory/mcp_server.py)

## 创建、确认、修订与撤回示例

以下值只是演示，不是对本用户偏好的断言。确认和修订前，应先归档相应的真实用户原话，并人工核对证据。

创建候选记忆，省略状态即为 `proposed`；候选不会默认进入 handoff：

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData memory save --key personal.reply_style --value '先给简洁结论，需要时再展开说明。' --account codex-local
& $MemoryPython -m codex_memory --data-dir $MemoryData memory list --include-proposed --limit 100
```

用户原话明确确认后，用同一键追加确认记录。可重复使用 `--evidence` 引用多条必要原话：

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData memory save --key personal.reply_style --value '先给简洁结论，需要时再展开说明。' --status confirmed --evidence '<支持该偏好的用户消息内部 ID>' --account codex-local
```

若用户在另一个账户明确修订该偏好，先采集 / 导入新原话，再保存同一键的新记录。账户标签变化不妨碍最新版本连续性：

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData memory save --key personal.reply_style --value '一般先给结论；代码审查需要先列出具体风险及证据。' --status confirmed --evidence '<新修订的用户消息内部 ID>' --account codex-second
```

仅属于某个项目的约束必须指定项目，不应因为一次项目要求而提升为全局偏好：

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData memory save --key workflow.review_order --value '此项目先审查风险，再按用户确认的顺序实施。' --project Tao --status confirmed --evidence '<项目要求的用户消息内部 ID>' --account codex-local
& $MemoryPython -m codex_memory --data-dir $MemoryData memory list --project Tao --limit 100
```

撤回时同样追加记录，建议附上撤回的原话；不删除旧证据或批量清理文件：

```powershell
& $MemoryPython -m codex_memory --data-dir $MemoryData memory save --key personal.reply_style --value '用户撤回此偏好，不再作为默认回复方式。' --status withdrawn --evidence '<撤回要求的用户消息内部 ID>' --account codex-second
& $MemoryPython -m codex_memory --data-dir $MemoryData memory list --include-proposed --limit 100
```

`memory list` 默认只列当前有效的已确认记录，最多返回 100 条，默认也是 100。不传项目时只取全局；指定项目时取全局和该项目。`--include-proposed` 是复核模式，会同时包括最新的待确认及已撤回记录，不会将它们变成有效事实；该命令不提供 `--status` 参数或完整修订历史列表。

面板可查看与保存个人记忆，保存时应明确选择状态并填写证据。MCP 默认只读；只有操作者明确开启 `mcp --allow-writes` 才提供个人记忆保存工具。开放写工具不等于批准模型根据历史批量提炼或自动确认偏好，每次写入仍需当前用户请求；没有写工具时使用本地 CLI / 面板。[CLI](../src/codex_memory/cli.py)、[面板端点](../src/codex_memory/server.py)、[MCP 写入开关](../src/codex_memory/mcp_server.py)

## 跨账户验收与当前结论

本阶段从当前用户明确要求中逐条选取少量事实，每条单独记录来源，不根据助手旧回复补全未知个人信息。真实第二账户的读取、准确理解、引用核对、新原话写回和再次读取仍须独立验收；当前保持 `pending`，不能把本地单元测试、不同账户标签或同库模拟客户端当作通过。[验收流程](CROSS_ACCOUNT_ACCEPTANCE.md)

第二账户接续时应先读取已确认记忆及对应来源，区分全局与项目范围，指出未知或过时内容，再按当前请求工作。用户新增偏好须归档原话并显式保存 / 修订；不能以一句“我记住了”代替实际落库。个人记忆是可查证的上下文，不是“完全熟知用户”、永久不会忘记或无任何读取步骤就能无缝接通的承诺。
