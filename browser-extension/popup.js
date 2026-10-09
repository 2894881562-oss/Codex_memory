"use strict";
function time(value) { return value ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString() : "—"; }
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
  const status = await chrome.runtime.sendMessage({type: "status"});
  document.querySelector("#connection").textContent = !status.paired ? "等待配对" : status.online
    ? (status.enabled ? "本地服务在线 · 采集已启用" : "本地服务在线 · 采集已暂停") : "本地服务不可达 · 历史成功不代表当前在线";
  document.querySelector("#account").textContent = status.account || "未设置";
  document.querySelector("#queue").textContent = String(status.queue || 0);
  document.querySelector("#captured").textContent = String(status.captured_messages || 0);
  document.querySelector("#capture").textContent = time(status.last_capture);
  document.querySelector("#success").textContent = time(status.last_success);
  document.querySelector("#health").textContent = time(status.last_health_check);
  document.querySelector("#server-success").textContent = time(status.last_live?.at);
  document.querySelector("#queue-accounts").textContent = (status.queued_accounts || []).join("、") || "无积压";
  const page = status.last_page;
  document.querySelector("#coverage").textContent = page ? `最近观察：${page.account} / ${page.conversation_id}，当时渲染 ${page.rendered_messages} 条${page.fallback_message_ids ? "；含临时消息 ID，刷新可能重复" : ""}。仅覆盖打开并渲染的页面，不是全账户历史。` : "尚无页面观察；仅启用扩展不等于已经保存聊天。请刷新具有稳定 URL 的目标聊天页。";
  document.querySelector("#error").textContent = [status.health_error, status.error, status.capture_blocked].filter(Boolean).join("；");
  const project = document.querySelector("#resume-project");
  if (!project.dataset.initialized) {
    project.value = status.project || "";
    project.dataset.initialized = "true";
  }
  } catch {
    document.querySelector("#connection").textContent = "扩展状态读取失败，请重新打开弹窗";
  } finally { refreshing = false; }
}
document.querySelector("#retry").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({type: "retry"});
  await refresh();
});
document.querySelector("#options").addEventListener("click", () => chrome.runtime.openOptionsPage());
document.querySelector("#resume").addEventListener("click", async () => {
  const button = document.querySelector("#resume");
  const notice = document.querySelector("#resume-notice");
  button.disabled = true;
  document.querySelector("#handoff").value = "";
  document.querySelector("#copy").disabled = true;
  document.querySelector("#share-confirm").checked = false;
  notice.textContent = "正在读取本地记忆…";
  try {
    const result = await chrome.runtime.sendMessage({type: "resume", project: document.querySelector("#resume-project").value.trim(), task: document.querySelector("#resume-task").value.trim()});
    if (!result.ok) throw new Error(result.error || "读取失败");
    document.querySelector("#handoff").value = result.handoff;
    notice.textContent = "已读取任务相关交接。请先审阅、勾选分享确认，再复制；粘贴并发送后会进入该 GPT 账户的云端上下文。历史仅作参考。";
  } catch (error) {
    notice.textContent = String(error.message || error);
  } finally {
    button.disabled = false;
  }
});
for (const selector of ["#resume-project", "#resume-task"]) document.querySelector(selector).addEventListener("input", () => {
  document.querySelector("#handoff").value = "";
  document.querySelector("#copy").disabled = true;
  document.querySelector("#share-confirm").checked = false;
  document.querySelector("#resume-notice").textContent = "项目或任务已变更，请重新读取本地记忆。";
});
document.querySelector("#share-confirm").addEventListener("change", event => {
  document.querySelector("#copy").disabled = !event.target.checked || !document.querySelector("#handoff").value;
});
document.querySelector("#copy").addEventListener("click", async () => {
  const handoff = document.querySelector("#handoff");
  if (!document.querySelector("#share-confirm").checked || !handoff.value) return;
  try {
    await navigator.clipboard.writeText(handoff.value);
    document.querySelector("#resume-notice").textContent = "已复制，可粘贴到当前账户的新聊天；请自行确认并发送。";
  } catch {
    handoff.focus();
    handoff.select();
    document.querySelector("#resume-notice").textContent = "浏览器未允许剪贴板写入，文本已选中，请按 Ctrl+C 复制。";
  }
});
refresh();
setInterval(refresh, 5000);
