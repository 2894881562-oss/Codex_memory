"use strict";
const form = document.querySelector("#settings");
chrome.storage.local.get("settings").then(({settings = {}}) => {
  for (const name of ["port", "token", "account", "project"]) {
    if (settings[name] !== undefined) form.elements[name].value = settings[name];
  }
  form.elements.enabled.checked = Boolean(settings.enabled);
});
document.querySelector("#pause").addEventListener("click", async () => {
  const result = await chrome.runtime.sendMessage({type: "pause"});
  if (!result.ok) return;
  form.elements.enabled.checked = false;
  document.querySelector("#notice").textContent = "采集已暂停。可先在弹窗手动重试旧队列；换标签不会改写旧队列。";
});
form.addEventListener("submit", async event => {
  event.preventDefault();
  const settings = {
    port: Number(form.elements.port.value), token: form.elements.token.value.trim(),
    account: form.elements.account.value.trim(), project: form.elements.project.value.trim(),
    enabled: form.elements.enabled.checked
  };
  if (!settings.account || !settings.token || settings.port < 1024 || settings.port > 65535) return;
  const result = await chrome.runtime.sendMessage({type: "save-settings", settings,
    confirm_account: form.elements.confirm_account.checked});
  if (!result.ok) { document.querySelector("#notice").textContent = result.error || "保存失败"; return; }
  form.elements.enabled.checked = result.enabled;
  form.elements.confirm_account.checked = false;
  document.querySelector("#notice").textContent = result.account_changed
    ? `新标签已保存，仍保持暂停；${result.queued} 批旧队列保留原标签。切换网页账户、关闭旧页，再确认启用并刷新目标聊天页。`
    : "已保存。已启用时请刷新目标聊天页；若没有新页面观察，请核对弹窗覆盖状态。";
});
