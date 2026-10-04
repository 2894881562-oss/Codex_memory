"use strict";
function time(value) { return value ? new Date(value).toLocaleString() : "—"; }
async function refresh() {
  const status = await chrome.runtime.sendMessage({type: "status"});
  document.querySelector("#connection").textContent = !status.enabled ? "归档未启用" : !status.paired ? "等待配对" : status.error ? "连接异常" : status.connected ? "最近写入成功" : "等待聊天页面消息";
  document.querySelector("#account").textContent = status.account || "未设置";
  document.querySelector("#queue").textContent = String(status.queue || 0);
  document.querySelector("#captured").textContent = String(status.captured_messages || 0);
  document.querySelector("#capture").textContent = time(status.last_capture);
  document.querySelector("#success").textContent = time(status.last_success);
  document.querySelector("#error").textContent = status.error || "";
  const project = document.querySelector("#resume-project");
  if (!project.dataset.initialized) {
    project.value = status.project || "";
    project.dataset.initialized = "true";
  }
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
  notice.textContent = "正在读取本地记忆…";
  try {
    const result = await chrome.runtime.sendMessage({type: "resume", project: document.querySelector("#resume-project").value.trim(), task: document.querySelector("#resume-task").value.trim()});
    if (!result.ok) throw new Error(result.error || "读取失败");
    document.querySelector("#handoff").value = result.handoff;
    document.querySelector("#copy").disabled = false;
    notice.textContent = "已读取任务相关交接。复制到新账户聊天，确认当前目标后继续；历史仅作参考。";
  } catch (error) {
    notice.textContent = String(error.message || error);
  } finally {
    button.disabled = false;
  }
});
for (const selector of ["#resume-project", "#resume-task"]) document.querySelector(selector).addEventListener("input", () => {
  document.querySelector("#handoff").value = "";
  document.querySelector("#copy").disabled = true;
  document.querySelector("#resume-notice").textContent = "项目或任务已变更，请重新读取本地记忆。";
});
document.querySelector("#copy").addEventListener("click", async () => {
  const handoff = document.querySelector("#handoff");
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
setInterval(refresh, 2000);
