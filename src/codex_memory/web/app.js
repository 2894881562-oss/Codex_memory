"use strict";

const $ = (id) => document.getElementById(id);
const state = {csrf: "", panel: "overview", chatOffset: 0, fileOffset: 0, chatLimit: 30,
  readerId: "", readerOffset: 0, readerLimit: 80, readerTotal: 0, readerMore: false, searching: false,
  handoffQuery: null, ready: false, refreshPending: false};
const panelTitles = {overview: "记忆概览", conversations: "聊天档案", projects: "工程与交接",
  memories: "个人记忆", sources: "采集与同步", maintenance: "备份与检查"};

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = String(text);
  return element;
}
function text(value) { return value == null ? "" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value); }
function date(value) {
  if (!value) return "暂无记录";
  const parsed = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("zh-CN", {hour12: false});
}
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = false;
}
function setConnection(connected) {
  $("connection-dot").classList.toggle("offline", !connected);
  $("connection-text").textContent = connected ? "本机服务已连接 · 请核对采集覆盖" : "连接中断 · 请启动本地服务";
}
async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = state.csrf;
    options.body = JSON.stringify(options.body);
    options.method = options.method || "POST";
  }
  const response = await fetch(path, {...options, headers, credentials: "same-origin"});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "本地操作未完成");
  return result;
}
function query(values) { return new URLSearchParams(Object.entries(values).filter(([, value]) => value !== "")).toString(); }
function formValues(form) { return Object.fromEntries(new FormData(form).entries()); }
function result(value) {
  $("operation-result-card").hidden = false;
  $("operation-result").textContent = JSON.stringify(value, null, 2);
}
function importSummary(report) {
  const coverage = report.coverage || {};
  const warnings = Array.isArray(report.warnings) ? report.warnings : [];
  const omitted = Number(report.warnings_omitted || 0);
  const seenKnown = report.conversations_seen !== undefined && report.messages_seen !== undefined;
  const noChats = coverage.status === "no_chat_history" || (seenKnown && !Number(report.conversations_seen) && !Number(report.messages_seen));
  const partial = coverage.status === "partial" || warnings.length > 0 || omitted > 0;
  const complete = coverage.status === "complete" && !partial;
  return {level: noChats || partial ? "warning" : complete ? "complete" : "unknown",
    title: noChats ? "未解析到聊天历史" : partial ? "导入已处理，但存在警告 / 覆盖缺口" : complete ? "所选导出已完整解析" : "导入已处理，覆盖情况待核对",
    description: `${seenKnown ? `解析到 ${Number(report.conversations_seen)} 个会话 / ${Number(report.messages_seen)} 条消息；` : ""}本次新增或更新 ${Number(report.conversations || 0)} 个会话 / ${Number(report.messages || 0)} 条消息。重复导入可能没有新增，不代表没有聊天。完整性只针对所选导出，不保证全账户历史、记忆或附件均已取得。`,
    warnings, omitted, coverage};
}
function showImportReport(report, kind) {
  const panel = $("import-report");
  panel.replaceChildren();
  panel.hidden = kind !== "chatgpt";
  if (kind !== "chatgpt") return;
  const summary = importSummary(report);
  panel.append(node("h3", "", summary.title), node("p", "muted", summary.description));
  const coverage = summary.coverage;
  if (coverage.chat_files_found !== undefined) panel.append(node("p", "muted", `聊天文件：发现 ${coverage.chat_files_found} / 已解析 ${coverage.chat_files_parsed || 0} / 不支持 ${coverage.chat_files_unsupported || 0}。附件：匹配 ${coverage.attachments_matched || 0} / 缺失 ${coverage.attachments_missing || 0} / 歧义 ${coverage.attachments_ambiguous || 0} / 仅外部引用 ${coverage.attachments_external || 0}。引用检查达到限额的消息：${coverage.references_truncated || 0}。`));
  for (const warning of summary.warnings.slice(0, 8)) panel.append(node("p", "source-warning", String(warning).slice(0, 1600)));
  if (summary.warnings.length > 8 || summary.omitted) panel.append(node("p", "source-warning", `另有 ${Math.max(0, summary.warnings.length - 8) + summary.omitted} 条警告未在此预览，请核对下方报告。`));
  panel.append(node("p", "muted", "请搜索一段旧账户的关键原话并检查附件，再生成交接包；解析完成不是跨账户续接验收完成。"));
  notice(summary.title + "；请核对数量和覆盖报告。", summary.level === "warning");
}
async function action(button, task) {
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try { await task(); } catch (error) { notice(error.message || "操作未完成", true); }
  finally { button.disabled = false; button.textContent = previous; }
}
function empty(container, message) { container.replaceChildren(node("p", "empty", message)); }
function item(conversation, snippet = "") {
  const row = node("button", "list-item");
  row.type = "button";
  row.append(node("span", "item-title", conversation.title || "未命名聊天"));
  const meta = node("span", "item-meta");
  meta.append(node("span", "source-tag", conversation.source || "归档"),
    node("span", "", conversation.account || "default"), node("span", "", conversation.project || "未归属工程"),
    node("span", "", conversation.updated_at ? date(conversation.updated_at) : ""));
  row.append(meta);
  if (snippet) row.append(node("div", "item-snippet", snippet));
  row.addEventListener("click", () => openReader(conversation.conversation_id || conversation.id));
  return row;
}
async function showPanel(panel) {
  state.panel = panel;
  for (const key of Object.keys(panelTitles)) $("panel-" + key).hidden = key !== panel;
  document.querySelectorAll("[data-panel]").forEach((button) => button.classList.toggle("active", button.dataset.panel === panel));
  $("page-title").textContent = panelTitles[panel];
  try {
    if (panel === "overview") await refreshOverview();
    if (panel === "conversations") await loadConversations();
    if (panel === "projects") await loadProjects();
    if (panel === "sources") await loadSources();
    if (panel === "memories") await loadUserMemory();
  } catch (error) { notice(error.message, true); }
}

async function loadStats() {
  const stats = await api("/api/stats");
  setConnection(true);
  const grid = $("stats-grid");
  grid.replaceChildren();
  for (const [key, label, caption] of [["conversations", "聊天会话", "保留完整来源信息"], ["messages", "归档消息", "支持跨账户全文检索"],
    ["projects", "共享工程", "关联文件与关键决定"], ["checkpoints", "工作检查点", "为下次继续留下进度"]]) {
    const card = node("div", "stat-card");
    card.append(node("div", "stat-label", label), node("div", "stat-number", Number(stats[key] || 0).toLocaleString("zh-CN")), node("div", "stat-caption", caption));
    grid.append(card);
  }
  const status = $("live-status");
  status.replaceChildren();
  const live = stats.live || {};
  const sync = stats.sync || {};
  const chatgpt = stats.chatgpt_archive || {};
  for (const [label, value] of [["最近浏览器采集", date(live.at)], ["当前采集账户", live.account || "尚未配对 / 未采集"],
    ["ChatGPT 已归档会话", String(chatgpt.conversations ?? stats.conversation_sources?.chatgpt ?? 0)],
    ["ChatGPT 最近入库", date(chatgpt.last_ingest_at)],
    ["最近导出导入", chatgpt.last_export_at ? date(chatgpt.last_export_at) : "暂无覆盖报告（未导入或旧格式）"],
    ["最近来源同步", sync.running ? "正在同步…" : date(sync.at)], ["同步状态", sync.error || (sync.running ? "正在扫描配置来源" : "请核对来源与最后成功时间；网页采集还需启用扩展")]]) {
    const row = node("div", "status-row");
    row.append(node("span", "", label), node("span", "", value));
    status.append(row);
  }
  return stats;
}
async function refreshOverview() {
  await loadStats();
  const rows = await api("/api/conversations?limit=5");
  const container = $("recent-conversations");
  container.replaceChildren();
  if (!rows.length) empty(container, "导入第一份聊天，或连接浏览器扩展开始采集。");
  else rows.forEach((conversation) => container.append(item(conversation)));
}
async function loadConversations() {
  const filters = formValues($("search-form"));
  const searching = Boolean(filters.query.trim());
  state.searching = searching;
  const params = {...filters, q: filters.query, limit: state.chatLimit, offset: state.chatOffset};
  delete params.query;
  const rows = await api((searching ? "/api/search?" : "/api/conversations?") + query(params));
  const container = $("conversation-list");
  container.replaceChildren();
  $("conversation-list-title").textContent = searching ? "搜索结果" : "全部聊天";
  $("conversation-count").textContent = searching ? `${rows.length} 条相关消息` : `本页 ${rows.length} 个会话`;
  if (!rows.length) empty(container, searching ? "没有匹配的消息，试试其他关键词。" : "这里还没有聊天。请先导入或同步来源。");
  else rows.forEach((conversation) => container.append(item(conversation, searching ? conversation.snippet || conversation.text || "" : "")));
  $("chat-prev").disabled = searching || state.chatOffset === 0;
  $("chat-next").disabled = searching || rows.length < state.chatLimit;
  $("chat-page").textContent = searching ? "搜索结果（最多 30 条，可缩小范围）" : `第 ${state.chatOffset / state.chatLimit + 1} 页`;
}

async function openReader(id) {
  state.readerId = id;
  state.readerOffset = 0;
  $("reader-prev").hidden = false;
  $("reader-next").hidden = false;
  $("reader-handoff").hidden = false;
  try {
    await loadReader();
    $("reader-dialog").showModal();
  } catch (error) { notice(error.message, true); }
}
async function loadReader() {
  const data = await api("/api/conversation?" + query({id: state.readerId, offset: state.readerOffset, limit: state.readerLimit}));
  const conversation = data.conversation || data;
  const messages = data.messages || [];
  const content = $("reader-content");
  const previousScroll = $("reader-dialog").scrollTop;
  $("reader-title").textContent = conversation.title || "聊天档案";
  $("reader-caption").textContent = [conversation.source, conversation.account, conversation.project].filter(Boolean).join(" · ");
  content.replaceChildren();
  for (const message of messages) {
    const entry = node("article", "message");
    const heading = node("div", "message-heading");
    heading.append(node("span", "message-role", message.role || message.kind || "记录"), node("span", "", date(message.timestamp)),
      node("span", "", message.branch ? "分支 " + message.branch : ""));
    entry.append(heading, node("div", "message-text", message.text || "（此条记录没有可显示文本）"));
    const evidence = node("div", "item-meta");
    const copy = node("button", "text-button", "复制证据消息 ID");
    copy.type = "button";
    copy.title = message.id;
    copy.addEventListener("click", () => clipboard(message.id).catch((error) => notice(error.message, true)));
    evidence.append(node("span", "", "消息 " + message.id), copy);
    entry.append(evidence);
    content.append(entry);
  }
  if (!messages.length) empty(content, "这个会话暂无可显示消息。");
  const paging = data.pagination || data;
  state.readerTotal = Number(paging.total || 0);
  state.readerMore = paging.has_more === true;
  $("reader-prev").disabled = state.readerOffset === 0;
  $("reader-next").disabled = !state.readerMore;
  $("reader-page").textContent = `第 ${state.readerOffset / state.readerLimit + 1} 页${state.readerTotal ? " · 共 " + state.readerTotal + " 条" : ""}`;
  $("reader-dialog").scrollTop = previousScroll;
}

async function loadProjects() {
  const projects = await api("/api/projects");
  const select = $("project-select");
  const previous = select.value || localStorage.getItem("memory-last-project") || "";
  select.replaceChildren(node("option", "", "选择一个工程"));
  select.firstChild.value = "";
  for (const project of projects) {
    const option = node("option", "", project.name || project.project);
    option.value = project.name || project.project;
    select.append(option);
  }
  if (projects.some((project) => (project.name || project.project) === previous)) select.value = previous;
  if (select.value) await loadProject();
}
async function loadProject() {
  const project = $("project-select").value;
  if (!project) {
    empty($("project-state"), "选择工程，查看上次留下的目标、决定和下一步。");
    empty($("project-files"), "选择一个工程查看文件档案。");
    return;
  }
  localStorage.setItem("memory-last-project", project);
  $("checkpoint-form").elements.project.value = project;
  const data = await api("/api/project?" + query({project}));
  const checkpoint = data.latest_checkpoint || data.checkpoint || data.latest || null;
  const container = $("project-state");
  container.replaceChildren();
  if (!checkpoint) empty(container, "这个工程还没有工作检查点。可以在下方保存当前进度。");
  else {
    for (const [field, label] of [["goal", "当前目标"], ["completed", "已完成"], ["decisions", "关键决定"], ["next_steps", "下一步"], ["blockers", "待解决问题"]]) {
      if (!checkpoint[field]) continue;
      const block = node("div", "state-block");
      block.append(node("div", "state-label", label), node("div", "state-value", text(checkpoint[field])));
      container.append(block);
    }
    container.append(node("p", "muted", `${checkpoint.account || "default"} · ${date(checkpoint.created_at || checkpoint.timestamp)}`));
  }
  const conversations = data.related_conversations || data.conversations || [];
  if (conversations.length) {
    container.append(node("h3", "", "相关聊天"));
    conversations.slice(0, 4).forEach((conversation) => container.append(item(conversation)));
  }
  await loadProjectFiles();
}
async function loadProjectFiles() {
  const project = $("project-select").value;
  if (!project) return;
  const files = await api("/api/files?" + query({project, limit: 50, offset: state.fileOffset}));
  const list = $("project-files");
  list.replaceChildren();
  if (!files.length) empty(list, "尚未归档项目文件；请在采集页添加项目目录。");
  for (const file of files) {
    const name = file.relative_path || file.path || file.name;
    const button = node("button", "file-item", name + (file.present === 0 || file.present === false ? " · 原路径已不存在（保留历史）" : ""));
    button.title = `${file.size || 0} 字节 · ${file.sha256 || file.hash || ""}`;
    button.addEventListener("click", async () => {
      try {
        const data = await api("/api/file?" + query({project, path: name}));
        state.readerId = "";
        $("reader-title").textContent = name;
        $("reader-caption").textContent = project + " · 已归档文件";
        const fileText = data.is_text === false ? "此文件为二进制或不支持的文本编码，原始内容已保留在本地档案；这里不展示原始字节。" : data.text ?? data.content ?? data.limitation ?? "（空文件）";
        $("reader-content").replaceChildren(node("pre", "", fileText));
        if (data.truncated) $("reader-content").append(node("p", "muted", "文件内容已按字符上限截断；可通过 CLI / MCP 请求更大的范围。"));
        if (data.present === 0 || data.present === false) $("reader-content").append(node("p", "muted", "原工程路径已不存在该文件，此处保留的是最后一次归档的历史版本。"));
        for (const id of ["reader-prev", "reader-next", "reader-handoff"]) $(id).hidden = true;
        $("reader-page").textContent = "";
        $("reader-dialog").showModal();
      } catch (error) { notice(error.message, true); }
    });
    list.append(button);
  }
  $("file-prev").disabled = state.fileOffset === 0;
  $("file-next").disabled = files.length < 50;
  $("file-page").textContent = `第 ${state.fileOffset / 50 + 1} 页`;
}
async function loadSources() {
  const rows = await api("/api/sources");
  const container = $("source-list");
  container.replaceChildren();
  if (!rows.length) empty(container, "添加一个来源，让聊天和工程的变化持续归档。");
  for (const source of rows) {
    const row = node("div", "list-item");
    row.append(node("div", "item-title", source.path));
    const meta = node("div", "item-meta");
    meta.append(node("span", "source-tag", source.kind), node("span", "", source.account || "default"),
      node("span", "", source.project || "未归属工程"), node("span", "", "上次同步 " + date(source.last_run)));
    row.append(meta);
    const report = typeof source.last_report === "string" ? (() => {try {return JSON.parse(source.last_report);} catch {return {};}})() : source.last_report || {};
    if (report.error) row.append(node("div", "source-warning", report.error));
    if (source.kind === "chatgpt" && report.coverage) row.append(node("p", "muted", importSummary(report).title + "（仅所选导出范围）"));
    if (report.warnings && report.warnings.length) row.append(node("div", "source-warning", report.warnings.slice(0, 3).join("\n")));
    container.append(row);
  }
}
async function openHandoff(values) {
  const data = await api("/api/handoff?" + query(values));
  state.handoffQuery = values;
  $("handoff-content").value = data.text;
  $("handoff-share-confirm").checked = false;
  $("handoff-copy").disabled = true;
  $("handoff-download").setAttribute("aria-disabled", "true");
  $("handoff-download").href = "/api/handoff?" + query({...values, download: "1"});
  $("handoff-dialog").showModal();
}
async function loadUserMemory() {
  const filter = $("memory-filter");
  const rows = await api("/api/user-memory?" + query({project: filter.elements.project.value.trim(),
    include_proposed: filter.elements.include_proposed.checked ? "1" : "0", limit: 100}));
  const container = $("user-memory-list");
  container.replaceChildren();
  if (!rows.length) empty(container, "这个范围尚无个人记忆。可以先保存待确认建议，再用用户原话确认。");
  for (const memory of rows) {
    const row = node("article", "list-item");
    const heading = node("div", "card-heading");
    const status = {confirmed: "已确认", proposed: "待确认建议", withdrawn: "已撤回"}[memory.status] || memory.status;
    heading.append(node("span", "item-title", memory.key), node("span", "badge memory-" + memory.status, status));
    row.append(heading, node("div", "memory-value", memory.value));
    const meta = node("div", "item-meta");
    meta.append(node("span", "", memory.project || "个人全局"), node("span", "", memory.account || "default"),
      node("span", "", date(memory.created_at || memory.recorded_at)), node("span", "", "记忆版本 " + (memory.hash || memory.version || memory.id || "")));
    row.append(meta);
    const evidence = memory.evidence || memory.evidence_ids || [];
    if (Array.isArray(evidence) && evidence.length) row.append(node("p", "muted", "证据消息：" + evidence.map((item) => typeof item === "string" ? item : item.message_id || item.id || text(item)).join("、")));
    const edit = node("button", "text-button", "填写为新版本");
    edit.type = "button";
    edit.addEventListener("click", () => {
      const form = $("memory-form");
      for (const field of ["key", "value", "project", "account"]) form.elements[field].value = memory[field] || (field === "account" ? "default" : "");
      form.elements.status.value = "proposed";
      form.elements.evidence.value = "";
      form.scrollIntoView({behavior: "smooth", block: "start"});
      notice("已填入旧内容，保存仍默认待确认；确认或撤回请由你明确选择状态。");
    });
    row.append(edit);
    container.append(row);
  }
}
async function clipboard(value) {
  if (!value) throw new Error("请先获取内容。");
  await navigator.clipboard.writeText(value);
  notice("已复制，可以在另一账户使用。");
}
async function refresh() {
  if (state.refreshPending) return;
  state.refreshPending = true;
  try {
    await loadStats();
    if (state.panel === "overview") await refreshOverview();
    if (state.panel === "conversations") await loadConversations();
    if (state.panel === "sources") await loadSources();
    if (state.panel === "projects") await loadProject();
    if (state.panel === "memories") await loadUserMemory();
    if ($("reader-dialog").open && state.readerId) await loadReader();
  } catch { setConnection(false); }
  finally { state.refreshPending = false; }
}

document.querySelectorAll("[data-panel]").forEach((button) => button.addEventListener("click", () => showPanel(button.dataset.panel)));
document.querySelectorAll("[data-go]").forEach((button) => button.addEventListener("click", () => showPanel(button.dataset.go)));
$("refresh-button").addEventListener("click", refresh);
$("search-form").addEventListener("submit", (event) => {event.preventDefault(); state.chatOffset = 0; action(event.submitter, loadConversations);});
$("clear-search").addEventListener("click", () => {$("search-form").reset(); state.chatOffset = 0; action($("clear-search"), loadConversations);});
$("chat-prev").addEventListener("click", () => {state.chatOffset = Math.max(0, state.chatOffset - state.chatLimit); action($("chat-prev"), loadConversations);});
$("chat-next").addEventListener("click", () => {state.chatOffset += state.chatLimit; action($("chat-next"), loadConversations);});
$("reader-close").addEventListener("click", () => {$("reader-dialog").close(); state.readerId = "";});
$("reader-dialog").addEventListener("close", () => {state.readerId = "";});
$("reader-prev").addEventListener("click", () => {state.readerOffset = Math.max(0, state.readerOffset - state.readerLimit); action($("reader-prev"), loadReader);});
$("reader-next").addEventListener("click", () => {state.readerOffset += state.readerLimit; action($("reader-next"), loadReader);});
$("reader-handoff").addEventListener("click", () => action($("reader-handoff"), () => openHandoff({id: state.readerId})));
$("project-select").addEventListener("change", () => {state.fileOffset = 0; $("handoff-task").value = ""; loadProject().catch((error) => notice(error.message, true));});
$("file-prev").addEventListener("click", () => {state.fileOffset = Math.max(0, state.fileOffset - 50); action($("file-prev"), loadProjectFiles);});
$("file-next").addEventListener("click", () => {state.fileOffset += 50; action($("file-next"), loadProjectFiles);});
$("project-handoff").addEventListener("click", () => action($("project-handoff"), async () => {
  if (!$("project-select").value) throw new Error("请先选择一个工程。");
  await openHandoff({project: $("project-select").value, task: $("handoff-task").value.trim()});
}));
$("handoff-close").addEventListener("click", () => $("handoff-dialog").close());
$("handoff-share-confirm").addEventListener("change", (event) => {
  $("handoff-copy").disabled = !event.target.checked;
  $("handoff-download").setAttribute("aria-disabled", String(!event.target.checked));
});
$("handoff-download").addEventListener("click", (event) => {if (!$("handoff-share-confirm").checked) {event.preventDefault(); notice("请先审阅并确认要分享的交接内容。", true);}});
$("handoff-copy").addEventListener("click", () => {if ($("handoff-share-confirm").checked) action($("handoff-copy"), () => clipboard($("handoff-content").value));});
$("pairing-button").addEventListener("click", () => action($("pairing-button"), async () => {
  const pairing = await api("/api/pairing");
  $("pairing-token").value = pairing.collector_token;
  $("pairing-endpoint").value = pairing.endpoint;
  notice("将地址和令牌复制到浏览器扩展设置，并填写当前账户标签。");
}));
$("copy-pairing").addEventListener("click", () => action($("copy-pairing"), () => clipboard($("pairing-token").value)));
$("sync-button").addEventListener("click", () => action($("sync-button"), async () => {result(await api("/api/sync", {body: {}})); await loadSources(); await loadStats(); notice("来源同步完成，请查看操作结果及覆盖提示。");}));
$("source-form").addEventListener("submit", (event) => {
  event.preventDefault(); action(event.submitter, async () => {result(await api("/api/sources", {body: formValues(event.target)})); await loadSources(); notice("来源已保存；面板运行时将持续同步。");});
});
$("import-form").addEventListener("submit", (event) => {
  event.preventDefault(); action(event.submitter, async () => {
    const values = formValues(event.target);
    const report = await api(values.kind === "project" ? "/api/project" : "/api/import", {body: values});
    result(report);
    await loadStats();
    if (values.kind === "chatgpt") showImportReport(report, values.kind);
    else {$("import-report").hidden = true; notice("导入已处理，请查看操作结果及警告。");}
  });
});
$("checkpoint-form").addEventListener("submit", (event) => {
  event.preventDefault(); action(event.submitter, async () => {
    const values = formValues(event.target);
    await api("/api/checkpoint", {body: values});
    localStorage.setItem("memory-last-project", values.project);
    await loadProjects(); notice("工作检查点已保存到共享记忆库。");
  });
});
$("backup-form").addEventListener("submit", (event) => {
  event.preventDefault(); action(event.submitter, async () => {
    const values = formValues(event.target);
    try {$("maintenance-result").textContent = JSON.stringify(await api("/api/backup", {body: values}), null, 2); notice("备份已创建。");}
    finally {event.target.elements.password.value = "";}
  });
});
$("audit-button").addEventListener("click", () => action($("audit-button"), async () => {
  $("maintenance-result").textContent = JSON.stringify(await api("/api/audit", {body: {}}), null, 2);
}));
$("memory-filter").addEventListener("submit", (event) => {event.preventDefault(); action(event.submitter, loadUserMemory);});
$("memory-form").addEventListener("submit", (event) => {
  event.preventDefault(); action(event.submitter, async () => {
    const values = formValues(event.target);
    values.evidence = values.evidence.split(/[\s,;]+/).filter(Boolean);
    if (values.status === "confirmed" && !values.evidence.length) throw new Error("确认记忆需要用户原话的证据消息 ID。");
    await api("/api/user-memory", {body: values});
    $("memory-filter").elements.project.value = values.project;
    $("memory-filter").elements.include_proposed.checked = true;
    await loadUserMemory();
    event.target.elements.status.value = "proposed";
    notice(values.status === "confirmed" ? "已追加确认版本，将按范围带入交接包。" : values.status === "withdrawn" ? "已记录撤回版本，旧版本仍保留。" : "已保存待确认建议，不会自动带入交接包。");
  });
});

(async () => {
  try {
    const session = await api("/api/session");
    state.csrf = session.csrf_token;
    $("vault-path").textContent = session.data_dir;
    state.ready = true;
    await refreshOverview();
    window.setInterval(() => {if (!document.hidden && state.ready) refresh();}, 5000);
  } catch (error) {setConnection(false); notice(error.message || "请刷新面板，并确认本地服务正在运行。", true);}
})();
