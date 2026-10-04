# 跨账户验收结果模板

这是空白模板，没有真实账户 / 消息 / 检查点 ID，也没有已通过结论。请将填写后的副本保存到被 Git 忽略的 `exports/acceptance/<日期>-<运行编号>/result.md`；不要把实际聊天片段、截图、导出、配对令牌或凭据提交到 Git。

## 本次执行信息

- 运行编号：`{{run_id}}`
- 操作者 / 人工复核者：`{{local_alias}}`
- 执行时间与时区：`{{started_at}}` / `{{finished_at}}` / `{{timezone}}`
- 工程版本或提交：`{{version_or_commit}}`
- 明确记忆库路径：`{{data_dir}}`
- 本次项目标签：`{{project}}`
- 允许提供给云端的测试片段范围：`{{approved_scope}}`
- 真实账户 A / B 本地别名：`{{account_a_label}}` / `{{account_b_label}}`
- 用户确认实际为不同账户：`{{yes_no_unknown}}`
- 人工确认依据与本地证据位置（不含密码 / Cookie / token）：`{{account_confirmation_evidence}}`

## 路径状态：分别填写

| 路径 | 状态：pending / failed / passed | 证据类型 | 最后成功步骤 | 未完成项或失败依据 |
| --- | --- | --- | --- | --- |
| 真实官方导出导入 | pending | 尚无真实来源证据 | 未执行 | `{{input_or_step_needed}}` |
| 官方导出与实时采集同 ID 增量 | pending | 尚无真实来源证据 | 未执行 | `{{input_or_step_needed}}` |
| 真实 ChatGPT B：扩展读取 | pending | 尚无第二账户证据 | 未执行 | `{{input_or_step_needed}}` |
| 真实 ChatGPT B：文件交接 | pending | 尚无第二账户证据 | 未执行 | `{{input_or_step_needed}}` |
| 真实 Codex B：同库读取 | pending | 尚无第二账户证据 | 未执行 | `{{input_or_step_needed}}` |
| B 新任务与回写、第三次读取 | pending | 尚无真实闭环证据 | 未执行 | `{{input_or_step_needed}}` |
| 模拟 SDK / 同库客户端（单独记录） | pending | simulation，不是真实账户证明 | 未执行 | `{{protocol_test_evidence}}` |

不得用上表模拟行替换真实行，也不得只改账户标签就把证据类型写成真实第二账户。

## 官方导出与实时采集证据

- 用户明确指定的本地导出路径：`{{export_path}}`
- 原始导出 SHA256：`{{export_sha256}}`
- 导出实际时间 / 覆盖范围 / 是否含测试聊天：`{{export_coverage}}`
- 导入报告位置与真实 source/account/project：`{{import_report_path_and_labels}}`
- 官方外部会话 / 消息 ID（填写到私有副本）：`{{external_conversation_id}}` / `{{external_message_id}}`
- 对应内部会话 / 消息 ID：`{{conversation_id}}` / `{{message_id}}`
- 稳定聊天 URL 和本次唯一测试短句：`{{stable_chat_url}}` / `{{run_nonce_phrase}}`
- DOM ID 模式：`{{stable_data_message_id_or_fallback}}`
- 实时最近成功时间、队列 / 错误状态证据位置：`{{live_capture_evidence}}`
- 官方导出 / DOM 同 ID 实际核对：`{{matched_unmatched_unverified}}`
- 新导出补齐 / 旧导出不回退 / 修订记录核对：`{{incremental_and_revision_evidence}}`
- 断线恢复写入证据：`{{offline_recovery_evidence}}`
- 导出未包含、未渲染或无法取得的部分：`{{known_missing_coverage}}`

## 人工事实基准

在向 B 提问之前，依据用户明确授权的实际来源填写；不把整张标准答案作为测试提问。没有记录的偏好应填“未知”，不是为了凑完整性而猜测。

| 事实类别 | 需复述的实际事实或明确未知 | 来源：检查点 / 消息 / 归档文件 ID 与字段 | 最小必要引用 / 时间 | 当前是否有效，旧版本如何处理 |
| --- | --- | --- | --- | --- |
| 用户目标 | `{{goal_or_unknown}}` | `{{source_id_and_field}}` | `{{minimal_quote_and_time}}` | `{{current_or_superseded}}` |
| 用户明确偏好 | `{{explicit_preference_or_unknown}}` | `{{source_id_and_field}}` | `{{minimal_quote_and_time}}` | `{{current_or_superseded}}` |
| 关键决定 | `{{decision_or_unknown}}` | `{{source_id_and_field}}` | `{{minimal_quote_and_time}}` | `{{current_or_superseded}}` |
| 阻塞 / 未验证条件 | `{{blocker_or_unknown}}` | `{{source_id_and_field}}` | `{{minimal_quote_and_time}}` | `{{current_or_superseded}}` |
| 下一步 | `{{next_step_or_unknown}}` | `{{source_id_and_field}}` | `{{minimal_quote_and_time}}` | `{{current_or_superseded}}` |

## 第二账户逐路径人工核对

为每个实际执行的 ChatGPT / Codex 路径分别复制本节，不用一个共同答案证明所有路径。

- 路径：`{{chatgpt_extension_or_file_or_codex_mcp_or_codex_file}}`
- 证据类型：`{{real_account_or_simulation}}`
- 当前实际账户本地别名与确认记录：`{{account_alias_and_confirmation}}`
- 新聊天 / 测试执行的本地证据引用：`{{chat_or_run_evidence}}`
- 实际读取入口、task、交接包 SHA256 / 检查点：`{{entrypoint_task_and_handoff}}`
- 模型 / 客户端（仅实际可知值）：`{{model_and_client_or_unknown}}`

| 类别 | 回答必要片段 | 模型给出的实际来源 ID | 人工核对是否存在且支持该事实 | 评价：错误/缺失、部分/未知、正确有证据 | 错误或遗漏说明 |
| --- | --- | --- | --- | --- | --- |
| 目标 | `{{excerpt}}` | `{{id}}` | `{{verification}}` | `{{rating}}` | `{{finding}}` |
| 明确偏好 | `{{excerpt}}` | `{{id_or_correct_unknown}}` | `{{verification}}` | `{{rating}}` | `{{finding}}` |
| 决策 | `{{excerpt}}` | `{{id}}` | `{{verification}}` | `{{rating}}` | `{{finding}}` |
| 阻塞 | `{{excerpt}}` | `{{id}}` | `{{verification}}` | `{{rating}}` | `{{finding}}` |
| 下一步 | `{{excerpt}}` | `{{id}}` | `{{verification}}` | `{{rating}}` | `{{finding}}` |

文字相似度 / 关键词命中仅作为辅助提示；本表不通过字符串匹配自动宣布模型理解完美。

## 错误断言与过度声称

| 检查项 | 实际必要片段 | 支持 / 反驳来源 | 人工结论：不存在、存在、尚未测试 | 对路径状态的影响 |
| --- | --- | --- | --- | --- |
| 虚假“全部手机 / 临时 / 删除历史已保存” | `{{excerpt_or_none}}` | `{{source}}` | `{{review}}` | `{{impact}}` |
| 虚假“原生 Memory / 侧栏已迁移” | `{{excerpt_or_none}}` | `{{source}}` | `{{review}}` | `{{impact}}` |
| 模拟或改标签冒充真实第二账户 | `{{excerpt_or_none}}` | `{{source}}` | `{{review}}` | `{{impact}}` |
| 引用不存在 / 与事实无关 | `{{excerpt_or_none}}` | `{{source}}` | `{{review}}` | `{{impact}}` |
| 把助手建议或旧断言当作用户决定 | `{{excerpt_or_none}}` | `{{source}}` | `{{review}}` | `{{impact}}` |
| 旧导出 / 旧状态覆盖新任务 | `{{excerpt_or_none}}` | `{{source}}` | `{{review}}` | `{{impact}}` |
| 凭空声称直接读 D 盘 / 自动监控未知额度 | `{{excerpt_or_none}}` | `{{source}}` | `{{review}}` | `{{impact}}` |

否定错误断言不算出现错误；没有测试也不能记为“不存在”。重大错误不能用其他事实得分抵消。

## B 新任务、回写、第三次读取

- 用户明确提出的新任务与唯一短句：`{{new_task_and_nonce}}`
- 新任务真实消息 source/account/project 与内部消息 ID：`{{b_message_provenance}}`
- 本地搜索核对证据：`{{search_evidence}}`
- 实际写回方式（人工 CLI / 仪表盘 / 明确允许 MCP）：`{{write_method}}`
- 写回确认的新检查点 ID / 时间：`{{new_checkpoint_id_and_time}}`
- 第三次读取的真实账户路径 / 新聊天证据：`{{third_read_account_and_evidence}}`
- 第三次实际引用及复述的新状态：`{{third_read_sources_and_excerpt}}`
- 没有回退旧状态的人工核对：`{{latest_vs_old_review}}`
- 新消息入库 / 检查点落库 / 第三次正确读取各自状态：`{{three_independent_statuses}}`

## 额度不足或其他中断的阶段总结

- 是否中断及实际原因（额度未知写未知）：`{{interruption_reason_or_unknown}}`
- 最后成功路径 / 步骤与执行时间：`{{last_successful_step}}`
- 最新已确认检查点 / 消息 ID：`{{last_confirmed_ids}}`
- 已完成事项：`{{completed}}`
- 未完成事项 / 已知阻塞：`{{pending_and_blockers}}`
- 下一次具体动作和需要用户提供的内容：`{{resume_action_and_input}}`
- 新内容是否实际写回：`{{yes_no_unverified}}`
- 本地采集服务是否仍运行（未知写未知）：`{{service_running_or_unknown}}`
- 实际可知的额度 / 重置证据，或明确未知：`{{quota_evidence_or_unknown}}`

没有建立自动限额监控，不推测重置时间。中断后的未完成路径保持 pending。

## 人工结论

- 本次真实完成的路径：`{{individually_passed_paths_or_none}}`
- pending 路径及所缺输入：`{{pending_paths_and_inputs}}`
- failed 路径及可复核依据：`{{failed_paths_and_evidence}}`
- 允许表述的已验证范围：`{{bounded_verified_scope}}`
- 不允许外推的覆盖范围：`{{unsupported_claims}}`
- 人工复核者确认与时间：`{{reviewer_confirmation}}`

不得将本次有限事实测试写成“所有内容已同步 / 所有账户自动衔接 / 模型完美理解”。只有真实执行、技术证据和逐项人工语义复核都完成，才对相应路径填写 passed。
