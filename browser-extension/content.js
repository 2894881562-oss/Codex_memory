/* Capture rendered chat DOM only. No private API, cookie, or credential access. */
(() => {
  "use strict";
  const seen = new Map();
  const identities = new WeakMap();
  let lastConversation = "";
  let scheduled = null;
  let scheduledAt = 0;
  let running = false;
  let rerun = false;

  function conversationId() {
    // Includes custom-GPT /g/<slug>/c/<uuid> routes, but never temporary/home views.
    const match = location.pathname.match(/(?:^|\/)c\/([a-zA-Z0-9_-]{8,128})(?:\/|$)/);
    return match ? match[1] : "";
  }

  function schedule() {
    if (scheduled && Date.now() - scheduledAt >= 1500) return;
    clearTimeout(scheduled);
    if (!scheduledAt) scheduledAt = Date.now();
    scheduled = setTimeout(() => {
      scheduled = null;
      scheduledAt = 0;
      capture();
    }, 1000);
  }

  function messageIdentity(node) {
    const named = node.closest("[data-message-id]") || node;
    const existing = named.getAttribute("data-message-id");
    if (existing) return existing;
    // Last-resort identity remains stable for this actual DOM node. Reloads can
    // create new fallback IDs, reported in metadata instead of pretending exactness.
    if (!identities.has(named)) identities.set(named, `dom:${crypto.randomUUID()}`);
    return identities.get(named);
  }

  function collect() {
    const output = [];
    const ids = new Set();
    for (const node of document.querySelectorAll("[data-message-author-role]")) {
      if (node.closest("[hidden], [aria-hidden='true']") || !node.getClientRects().length) continue;
      const role = node.getAttribute("data-message-author-role");
      if (!["user", "assistant", "system", "tool", "developer"].includes(role)) continue;
      const content = node.querySelector(".markdown") || node.querySelector(".whitespace-pre-wrap") || node;
      const text = content.innerText.trim();
      if (!text) continue;
      const id = messageIdentity(node);
      if (ids.has(id)) continue;
      ids.add(id);
      output.push({id, role, text});
    }
    return output;
  }

  async function capture() {
    if (running) { rerun = true; return; }
    running = true;
    try {
      const id = conversationId();
      if (!id) { lastConversation = ""; seen.clear(); return; }
      if (id !== lastConversation) { seen.clear(); lastConversation = id; }
      const {settings = {}} = await chrome.storage.local.get("settings");
      if (!settings.enabled || !settings.token || !settings.account) return;
      const messages = collect().filter(message => seen.get(message.id) !== `${message.role}\n${message.text}`);
      for (let offset = 0; offset < messages.length; offset += 200) {
        const batch = messages.slice(offset, offset + 200);
        const payload = {
          source: "chatgpt", account: settings.account, project: settings.project || "",
          conversation_id: id, title: document.title.replace(/\s*[-|]\s*ChatGPT\s*$/i, ""),
          messages: batch,
          metadata: {
            url: `${location.origin}${location.pathname}`, capture: "rendered-dom",
            observed_at: new Date().toISOString(), incomplete_coverage: true,
            fallback_message_ids: batch.some(message => message.id.startsWith("dom:"))
          }
        };
        const reply = await chrome.runtime.sendMessage({type: "capture", payload});
        if (!reply || !reply.ok) break;
        for (const message of batch) seen.set(message.id, `${message.role}\n${message.text}`);
      }
    } catch (error) {
      // Extension reload can invalidate this isolated script; no retries outside
      // the extension transport, and no application page changes.
      if (!String(error).includes("Extension context invalidated")) console.debug("Local archive capture unavailable", String(error));
    } finally {
      running = false;
      if (rerun) { rerun = false; schedule(); }
    }
  }

  new MutationObserver(schedule).observe(document.body, {subtree: true, childList: true, characterData: true});
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.settings) { seen.clear(); schedule(); }
  });
  // Route changes and suspended service workers recover without a hidden API.
  setInterval(capture, 10000);
  addEventListener("popstate", schedule);
  addEventListener("online", schedule);
  addEventListener("visibilitychange", schedule);
  schedule();
})();
