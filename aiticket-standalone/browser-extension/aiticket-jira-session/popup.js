/** popup：读写配置 + 触发推送 + 显示状态。 */

const FIELDS = ["jiraBaseUrl", "serviceUrl", "skillToken"];
const DEFAULTS = { jiraBaseUrl: "", serviceUrl: "http://127.0.0.1:18080", skillToken: "" };

function $(id) {
  return document.getElementById(id);
}

function showStatus(s) {
  const el = $("status");
  if (!s) {
    el.style.display = "none";
    return;
  }
  el.style.display = "block";
  el.className = s.ok ? "ok" : "err";
  el.textContent = s.message;
}

async function load() {
  const cfg = await chrome.storage.local.get({ ...DEFAULTS, lastStatus: null });
  FIELDS.forEach((f) => ($(f).value = cfg[f] || DEFAULTS[f]));
  if (cfg.lastStatus) showStatus(cfg.lastStatus);
}

async function requestHostPermission(jiraBaseUrl) {
  // cookies.get 需要对 Jira 源有 host 权限；运行时按需申请
  try {
    const origin = new URL(jiraBaseUrl).origin + "/*";
    const granted = await chrome.permissions.request({ origins: [origin] });
    return granted;
  } catch (e) {
    return false;
  }
}

async function save() {
  const cfg = {};
  FIELDS.forEach((f) => (cfg[f] = $(f).value.trim()));
  await chrome.storage.local.set(cfg);
  if (cfg.jiraBaseUrl) {
    const ok = await requestHostPermission(cfg.jiraBaseUrl);
    if (!ok) {
      showStatus({ ok: false, message: "需要授权访问 Jira 站点 cookie 才能抓会话" });
      return;
    }
  }
  showStatus({ ok: true, message: "已保存" });
}

async function push() {
  await save();
  showStatus({ ok: true, message: "推送中…" });
  const resp = await chrome.runtime.sendMessage({ type: "push" });
  showStatus(resp || { ok: false, message: "无响应" });
}

document.addEventListener("DOMContentLoaded", () => {
  load();
  $("save").addEventListener("click", save);
  $("push").addEventListener("click", push);
});
