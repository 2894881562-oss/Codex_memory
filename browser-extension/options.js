"use strict";
const form = document.querySelector("#settings");
chrome.storage.local.get("settings").then(({settings = {}}) => {
  for (const name of ["port", "token", "account", "project"]) {
    if (settings[name] !== undefined) form.elements[name].value = settings[name];
  }
  form.elements.enabled.checked = Boolean(settings.enabled);
});
form.addEventListener("submit", async event => {
  event.preventDefault();
  const settings = {
    port: Number(form.elements.port.value), token: form.elements.token.value.trim(),
    account: form.elements.account.value.trim(), project: form.elements.project.value.trim(),
    enabled: form.elements.enabled.checked
  };
  if (!settings.account || !settings.token || settings.port < 1024 || settings.port > 65535) return;
  await chrome.storage.local.set({settings});
  document.querySelector("#notice").textContent = "已保存。请刷新已打开的 ChatGPT 聊天页面。";
});
