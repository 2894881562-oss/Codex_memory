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

async function enqueue(payload) {
  if (!payload || payload.source !== "chatgpt" || !Array.isArray(payload.messages)
      || !payload.conversation_id || !payload.account || payload.messages.length > 500) {
    throw new Error("无效的归档消息");
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
  await writeStatus({queue: next.length, last_capture: new Date().toISOString()});
  return {ok: true, queued: true};
}

async function flush(force = false) {
  const {settings = {}, queue = [], status = {}} = await chrome.storage.local.get(["settings", "queue", "status"]);
  if (!settings.enabled || !settings.token) return;
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
  if (!message || !["capture", "status", "retry", "resume"].includes(message.type)) return false;
  serialize(async () => {
    if (message.type === "capture") {
      const result = await enqueue(message.payload);
      reply(result); // Queue is durable before acknowledging DOM observation.
      if (result.ok) await flush();
    } else if (message.type === "resume") {
      reply(await resume(message.project, message.task === undefined ? "" : message.task));
    } else if (message.type === "retry") {
      await flush(true);
      reply({ok: true});
    } else {
      const {status = {}, queue = [], settings = {}} = await chrome.storage.local.get(["status", "queue", "settings"]);
      reply({...status, queue: queue.length, enabled: Boolean(settings.enabled), paired: Boolean(settings.token), account: settings.account || "", project: settings.project || ""});
    }
  }).catch(async error => {
    if (message.type !== "resume") await writeStatus({error: String(error.message || error), connected: false});
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
