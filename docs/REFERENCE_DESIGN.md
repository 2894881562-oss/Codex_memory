# 参考设计与本阶段边界

本次实际核验的项目是：标题为 ChatGPT Export Archiver 的 `luosx0403/chatgpt-sqlite-webui`、`nicknisi/sessions`、`lucaspirola/codex-lcm`，以及会话记忆检索论文对应的 `imlrz/ReFind`。此处的 sessions 指该具体仓库；ReFind 不指同名的幻觉检测项目或 rEFInd 引导管理器。以下参考用于改进本工程设计，没有安装第三方软件、改动账户配置或调用推理 API。[Archiver 中文教程](https://github.com/luosx0403/chatgpt-sqlite-webui/blob/main/README.zh-CN.md)、[sessions](https://github.com/nicknisi/sessions)、[codex-lcm](https://github.com/lucaspirola/codex-lcm/blob/main/README.md)、[ReFind 论文](https://arxiv.org/abs/2608.12888)

## 已核验的参考与取舍

ChatGPT Export Archiver 支持官方导出、`conversations-*.json` 分片、mapping 节点与来源记录；大型导入可在末尾批量建立 FTS，搜索索引是可选能力。可借鉴分片覆盖检查、批量索引与验证。当前工程没有因此获得新的分片导入能力，不能把该参考项目的功能当成本工程已有功能；也不沿用其上传临时文件、远程部署或清理脚本。[中文 README：导入、索引、验证](https://github.com/luosx0403/chatgpt-sqlite-webui/blob/main/README.zh-CN.md)

sessions 区分永久 transcript vault 与可丢弃搜索索引，并提供项目上下文 primer、主题过滤和经过批准的常驻约束。可借鉴“最近任务细节、旧任务标题、当前主题证据”的续接方式；历史中的偏好只能作为候选事实，不能自动变成当前授权。其 setup 会修改其他工具的配置，本阶段不执行；其索引清理行为也不作为删除本工程档案的授权。[sessions README：Context primer、Topic filtering、Transcript vault](https://github.com/nicknisi/sessions)

codex-lcm 的确定性 MCP 工具提供字面/FTS 检索、会话 lineage、工作区范围、工具调用审计和具名标记；README 明确 Codex hook 的覆盖缺口、版本限制，且 Windows 不受支持。可借鉴范围选择、错误显式返回和标记语义；不安装 POSIX hook，不更改用户 Codex 配置，不宣称捕获所有托管工具活动。其 v1 尚未实现摘要 DAG 压缩，不能作为已经成熟的自动压缩方案。[codex-lcm README](https://github.com/lucaspirola/codex-lcm/blob/main/README.md)

ReFind 研究保留原始会话，采用迭代关键词检索、局部上下文、日期缩窄和已看过结果过滤；仓库还提供 BM25 与会话级排序融合。论文结果支持“可控原文检索值得优先验证”，不保证本工程中文任务或跨账户续接达到相同成绩。仓库完整 agent 模式需要 LLM key，免 key 模式只是 BM25 基线；比赛服务的无鉴权部署和限时保留也不适合本工程永久私有档案。[论文](https://arxiv.org/abs/2608.12888)、[方法说明](https://github.com/imlrz/ReFind/blob/main/docs/METHOD_CARD.md)、[仓库运行边界](https://github.com/imlrz/ReFind)

## 本阶段采用的设计

续接输入应包括项目和当前任务描述。确定性检索先在选定范围内寻找相关历史，再将命中与邻近对话展开；最近记录提供时序背景，明确检查点提供已记录目标、决定与下一步。返回内容应区分用户明确记录与历史摘录，没有检查点时不生成“已经确认”的任务状态。这样可以避免只选最新会话而漏掉真正相关的旧任务；这是结合参考后的工程判断，不是直接复现第三方系统。[sessions 上下文与主题过滤](https://github.com/nicknisi/sessions)、[ReFind 局部展开](https://github.com/imlrz/ReFind/blob/main/docs/METHOD_CARD.md)

证据必须带内部会话 ID、消息 ID、账户标签、时间和项目范围，让调用方继续读取完整片段。已有 `search` 与 `fetch_message` 可定位并分段读取脱敏正文；本阶段围绕现有接口改进任务相关续接，不向网络工具开放 raw blob 或任意本地文件。原文中的命令、系统提示和第三方要求始终是待核验历史，当前用户请求决定操作授权。[现有服务代码](../src/codex_memory/service.py)、[codex-lcm 范围与审计设计](https://github.com/lucaspirola/codex-lcm/blob/main/README.md)

输出采用 `max_chars` 字符预算，并优先保留目标、来源引用和有用证据；长正文通过消息 ID 分段展开。字符数不等于 token 数，中文、代码和不同模型的比例均可能变化，因此不能把字符上限宣称为精确 token 上限。参考项目的预算思想可用于约束输出，但当前实现仍是字符预算。[工程接口约定](ENGINEERING.md)、[sessions 常驻约束预算](https://github.com/nicknisi/sessions)

本地服务只执行查询、排序、切片和来源展开；多次改写关键词、判断证据是否充分由用户当前的 GPT/Codex 聊天完成，本服务不启动 ReFind 的 API planner。现有 `create_checkpoint` 是显式保存结构化交接点，不等于系统自动总结或模型上下文压缩；本阶段未实现自动压缩、摘要 DAG、向量推理或偏好自动批准。[现有服务代码](../src/codex_memory/service.py)、[ReFind 运行模式](https://github.com/imlrz/ReFind)、[codex-lcm v1 范围](https://github.com/lucaspirola/codex-lcm/blob/main/README.md)

追加的个人档案设计采用 `proposed` / `confirmed` / `withdrawn` 显式状态、全局 / 项目范围和用户原话内部消息 ID；默认只保存候选，确认需要用户来源且仍须人工核对。最新状态按 `key + project` 选取，不因账户切换分叉；修订和撤回追加记录及 immutable artifacts，已确认的全局 / 匹配项目记忆参与交接。此处借鉴的是候选批准与来源检索思想，不是自动偏好推断、云端 Memory 迁移、平台身份验证或新的付费 API。具体操作与第二账户 `pending` 边界见个人记忆文档。[本工程个人记忆规则](USER_MEMORY.md)、[sessions 的批准与范围设计](https://github.com/nicknisi/sessions)、[ReFind 的原文来源原则](https://github.com/imlrz/ReFind/blob/main/docs/METHOD_CARD.md)

## Raw 增量档案与未来重建的精确边界

现有 `Store.archive` 将原始 bytes 保存为 SHA256 寻址 blob；`artifacts` 与 `artifact_sources` 记录哈希和来源。`_sync_codex` 仍解析变化文件的完整上下文，以保持事件/响应配对正确；只有旧字节前缀哈希完全匹配时，才保存新增 suffix 与 `codex-memory-append-v1` manifest，并提交新增行。文件重写或缩短会回退为完整快照。因此当前已有增量归档，不是只保存模型摘要。[存储实现](../src/codex_memory/store.py)、[增量归档实现](../src/codex_memory/service.py)

manifest 包含 `source_name`、`previous_snapshot_hash`、`byte_offset`、`suffix_hash`、`snapshot_hash` 和 `size`。第一次完整快照的哈希对应真实 blob；后续完整快照哈希可能只出现在 manifest 中，不能假定每个 `snapshot_hash` 都有同名完整 blob。manifest 自身内容哈希又与目标快照哈希不同。未来回放需要先定位来源一致的 manifest，再沿前驱快照递归拼接 suffix，检查 offset、长度、每个 blob 哈希和最终快照哈希，并拒绝循环或缺失链；`source_name` 只作来源标签，不能据此打开任意磁盘路径。[当前 manifest 写入机制](../src/codex_memory/service.py)

当前 `audit` 检查数据库完整性、外键、逐 blob 大小/哈希和索引计数；它不是 manifest 链回放器，也不是完整索引重建命令。目前没有可直接使用的全量重建工具。本阶段不实现导入或重建，也不把“档案含足够证据”描述为“已能自动恢复全部状态”。[当前 audit 与导入代码](../src/codex_memory/service.py)、[sessions 可重建索引与永久 vault 的区别](https://github.com/nicknisi/sessions)

未来应分两层验收：第一层由仍完好的权威 `messages` 表重建派生 FTS/排序索引，保留稳定 ID、当前版本、时间、范围和脱敏规则；第二层由完整快照/append 链恢复原始输入，再按版本化导入规则重新解析。第二层不能仅凭 raw blob 恢复所有人工检查点、显式项目标签、来源授权状态或历史版本选择，它还需要权威元数据及数据库备份。不得把整个 `memory.sqlite3` 当成可随意清除的缓存；禁止借重建删除原始档案或凭空重新批准已暂停的来源。[本工程权威表与 blob 结构](../src/codex_memory/store.py)、[备份/恢复边界](../src/codex_memory/backup.py)、[参考：sessions Transcript vault](https://github.com/nicknisi/sessions)

未来重建至少应验证：原输入已消失时档案仍可回放；连续 append、重写、截断和坏尾行不会混用链；旧版本不会覆盖较新的有效版本；中文查询能定位任务证据；默认交接不被 bootstrap 提示与大量工具噪声占满；重建前后的 ID、引用和搜索结果保持可解释。这是后续验收计划，当前阶段没有执行这些重建用例。[当前解析与存储边界](../src/codex_memory/importers.py)、[参考：ReFind 原文与上下文检索](https://arxiv.org/abs/2608.12888)
