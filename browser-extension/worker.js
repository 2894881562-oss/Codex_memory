"use strict";
const MAX_QUEUE_ITEMS = 100;
const MAX_QUEUE_BYTES = 6 * 1024 * 1024;
const MAX_PAYLOAD_BYTES = 4 * 1024 * 1024;
const encoder = new TextEncoder();
let operations = Promise.resolve();

function serialize(action) {
  const result = operations.then(action, action);
  operations = result.catch(() => {});
  return result;
}

function endpoint(settings, route = "live") {
  const port = Number(settings.port || 8765);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("本地端口必须为 1024–65535");
  return `http://127.0.0.1:${port}/api/${route}`;
}

function size(value) { return encoder.encode(JSON.stringify(value)).length; }

async function writeStatus(values) {
  const {status = {}} = await chrome.storage.local.get("status");
  await chrome.storage.local.set({status: {...status, ...values}});
}

async function probeConnection() {
  const {settings = {}} = await chrome.storage.local.get("settings");
  if (!settings.token) {
    await writeStatus({online: false, health_error: "尚未配对", last_health_check: new Date().toISOString()});
    return;
  }
  try {
    const response = await fetch(endpoint(settings, "collector/status"), {
      method: "POST", headers: {"Content-Type": "application/json", "Authorization": `Bearer ${settings.token}`},
      body: "{}", signal: AbortSignal.timeout(3000), credentials: "omit"
    });
    if (!response.ok) throw new Error(`本地服务 HTTP ${response.status}${response.status === 401 || response.status === 403 ? "，请重新配对" : ""}`);
    const result = await response.json();
    if (result.online !== true) throw new Error("本地服务未确认在线");
    await writeStatus({online: true, health_error: "", last_health_check: new Date().toISOString(),
      server_started_at: result.server_started_at || "", last_live: result.last_live || null});
  } catch (error) {
    await writeStatus({online: false, health_error: String(error.message || error), last_health_check: new Date().toISOString()});
  }
}

async function saveSettings(settings, confirmAccount = false) {
  const {settings: previous = {}, queue = []} = await chrome.storage.local.get(["settings", "queue"]);
  if (!settings || !Number.isInteger(settings.port) || settings.port < 1024 || settings.port > 65535
      || typeof settings.account !== "string" || !settings.account.trim() || settings.account.length > 128
      || typeof settings.project !== "string" || settings.project.length > 128
      || typeof settings.token !== "string" || !settings.token.trim()
      || typeof settings.enabled !== "boolean") throw new Error("请填写有效端口、配对令牌和账户标签");
  const changed = Boolean(previous.account && previous.account !== settings.account.trim());
  if (changed && previous.enabled) throw new Error("请先暂停采集，再更换账户标签；旧队列仍保留原标签");
  if (!changed && settings.enabled && confirmAccount !== true) throw new Error("启用前请确认当前 ChatGPT 账户与标签一致，并关闭其他账户旧页面");
  const next = {port: settings.port, token: settings.token.trim(), account: settings.account.trim(),
    project: settings.project.trim(), enabled: changed ? false : Boolean(settings.enabled),
    capture_epoch: changed || !previous.capture_epoch ? crypto.randomUUID() : previous.capture_epoch};
  await chrome.storage.local.set({settings: next});
  await writeStatus({queue: queue.length, capture_blocked: changed ? "账户标签已更换，采集保持暂停；确认目标账户后启用并刷新聊天页" : ""});
  return {ok: true, enabled: next.enabled, account_changed: changed, queued: queue.length};
}

async function enqueue(payload) {
  if (!payload || payload.source !== "chatgpt" || !Array.isArray(payload.messages)
      || !payload.conversation_id || !payload.account || payload.messages.length > 500) {
    throw new Error("无效的归档消息");
  }
  const {settings = {}} = await chrome.storage.local.get("settings");
  if (!settings.enabled) return {ok: false, error: "采集已暂停；页面内容未加入队列"};
  if (payload.account !== settings.account || (settings.capture_epoch && payload.capture_epoch !== settings.capture_epoch)) {
    await writeStatus({capture_blocked: "页面仍使用旧账户标签；确认账户后刷新目标聊天页"});
    return {ok: false, error: "账户标签已变化，请刷新目标账户聊天页后重试"};
  }
  if (size(payload) > MAX_PAYLOAD_BYTES) throw new Error("单次消息超过 4 MB；请使用官方导出补齐");
  const {queue = []} = await chrome.storage.local.get("queue");
  // Keep latest streaming text while offline. Never discard another conversation
  // to make room; queue overflow stays visible and this DOM batch is retried.
  const compatible = queue.findIndex(item => item.payload.account === payload.account
    && item.payload.project === payload.project && item.payload.conversation_id === payload.conversation_id);
  let merged = null;
  if (compatible >= 0) {
    const old = queue[compatible].payload;
    const messages = new Map(old.messages.map(message => [message.id, message]));
    for (const message of payload.messages) messages.set(message.id, message);
    const candidate = {...old, ...payload, messages: Array.from(messages.values())};
    if (candidate.messages.length <= 500 && size(candidate) <= MAX_PAYLOAD_BYTES) merged = candidate;
  }
  const next = queue.slice();
  if (merged) next[compatible] = {...next[compatible], payload: merged};
  else next.push({id: crypto.randomUUID(), payload, attempts: 0, nextAttempt: 0});
  if (next.length > MAX_QUEUE_ITEMS || size(next) > MAX_QUEUE_BYTES) {
    await writeStatus({error: "离线队列已满；未接收的页面消息将重试，请启动本地服务", connected: false, queue: queue.length});
    return {ok: false, error: "queue_full"};
  }
  await chrome.storage.local.set({queue: next});
  await writeStatus({queue: next.length, last_capture: new Date().toISOString(), capture_blocked: "",
    last_page: {account: payload.account, conversation_id: payload.conversation_id,
      rendered_messages: Number(payload.metadata?.rendered_messages || payload.messages.length),
      fallback_message_ids: Boolean(payload.metadata?.fallback_message_ids)}});
  return {ok: true, queued: true};
}

async function flush(force = false, allowPaused = false) {
  const {settings = {}, queue = [], status = {}} = await chrome.storage.local.get(["settings", "queue", "status"]);
  if ((!settings.enabled && !allowPaused) || !settings.token) return;
  let completed = 0;
  let messages = Number(status.captured_messages || 0);
  while (queue.length && completed < 5) {
    const item = queue[0];
    if (!force && item.nextAttempt > Date.now()) break;
    try {
      const response = await fetch(endpoint(settings), {
        method: "POST", headers: {"Content-Type": "application/json", "Authorization": `Bearer ${settings.token}`},
        body: JSON.stringify(item.payload), signal: AbortSignal.timeout(8000), credentials: "omit"
      });
      if (!response.ok) throw new Error(`本地服务 HTTP ${response.status}${response.status === 401 || response.status === 403 ? "，请检查配对令牌" : ""}`);
      const result = await response.json();
      if (result.error) throw new Error(String(result.error));
      messages += item.payload.messages.length;
      queue.shift();
      completed += 1;
      await chrome.storage.local.set({queue});
      await writeStatus({connected: true, error: "", queue: queue.length,
        captured_messages: messages, successful_requests: Number(status.successful_requests || 0) + completed,
        last_success: new Date().toISOString()});
    } catch (error) {
      item.attempts += 1;
      item.nextAttempt = Date.now() + Math.min(60000, 1000 * (2 ** Math.min(item.attempts, 6)));
      await chrome.storage.local.set({queue});
      await writeStatus({connected: false, error: String(error.message || error), queue: queue.length});
      break;
    }
  }
}

async function resume(project, task = "") {
  const {settings = {}} = await chrome.storage.local.get("settings");
  if (!settings.token) throw new Error("请先从本地仪表盘复制配对令牌");
  if (typeof project !== "string" || !project.trim() || project.length > 128) throw new Error("请输入要续接的项目 / 主题名称");
  if (typeof task !== "string" || task.length > 512) throw new Error("当前任务必须是最多 512 字的文本");
  const response = await fetch(endpoint(settings, "resume"), {
    method: "POST", headers: {"Content-Type": "application/json", "Authorization": `Bearer ${settings.token}`},
    body: JSON.stringify({project: project.trim(), task: task.trim()}), signal: AbortSignal.timeout(8000), credentials: "omit"
  });
  const result = await response.json();
  if (!response.ok || result.error) throw new Error(result.error || `本地服务 HTTP ${response.status}`);
  if (typeof result.handoff !== "string") throw new Error("本地服务没有返回交接内容");
  return {ok: true, handoff: result.handoff};
}

chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (!message || !["capture", "status", "retry", "resume", "save-settings", "pause"].includes(message.type)) return false;
  serialize(async () => {
    if (message.type === "capture") {
      const result = await enqueue(message.payload);
      reply(result); // Queue is durable before acknowledging DOM observation.
      if (result.ok) await flush();
    } else if (message.type === "resume") {
      reply(await resume(message.project, message.task === undefined ? "" : message.task));
    } else if (message.type === "retry") {
      await flush(true, true); // Explicitly send retained batches with their original account labels.
      await probeConnection();
      reply({ok: true});
    } else if (message.type === "save-settings") {
      reply(await saveSettings(message.settings, message.confirm_account));
    } else if (message.type === "pause") {
      const {settings = {}} = await chrome.storage.local.get("settings");
      await chrome.storage.local.set({settings: {...settings, enabled: false}});
      reply({ok: true});
    } else {
      await probeConnection(); // A historical successful write is never proof that the service is online now.
      const {status = {}, queue = [], settings = {}} = await chrome.storage.local.get(["status", "queue", "settings"]);
      const queuedAccounts = [...new Set(queue.map(item => item.payload.account))];
      reply({...status, queue: queue.length, queued_accounts: queuedAccounts, enabled: Boolean(settings.enabled), paired: Boolean(settings.token), account: settings.account || "", project: settings.project || ""});
    }
  }).catch(async error => {
    if (["capture", "retry"].includes(message.type)) await writeStatus({error: String(error.message || error), connected: false});
    reply({ok: false, error: String(error.message || error)});
  });
  return true;
});

chrome.alarms.create("local-archive-retry", {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "local-archive-retry") serialize(() => flush()).catch(() => {});
});
chrome.runtime.onStartup.addListener(() => serialize(() => flush(true)));
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.settings) serialize(() => flush(true)).catch(() => {});
});
