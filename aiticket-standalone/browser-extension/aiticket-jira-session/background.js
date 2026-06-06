/**
 * aiticket Jira 会话同步 — service worker（MV3）。
 *
 * 职责：读取 Jira 站点的 httpOnly JSESSIONID（+ XSRF token），POST 到本地
 * aiticket 服务的 /api/settings/jira-session-binding，用 skill token 鉴权。
 * 触发：① popup 手动「立即推送」 ② 定时 alarm（25 分钟） ③ JSESSIONID cookie 变化。
 *
 * 配置存于 chrome.storage.local：jiraBaseUrl / serviceUrl / skillToken。
 */

const DEFAULTS = {
  jiraBaseUrl: "",
  serviceUrl: "http://127.0.0.1:18080",
  skillToken: "",
};

const XSRF_COOKIE_NAMES = ["atlassian.xsrf.token", "XSRF-TOKEN", "_xsrf"];

async function getConfig() {
  const cfg = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...cfg };
}

function originOf(url) {
  try {
    return new URL(url).origin;
  } catch (e) {
    return "";
  }
}

async function readCookie(url, name) {
  try {
    const c = await chrome.cookies.get({ url, name });
    return c ? c.value : "";
  } catch (e) {
    return "";
  }
}

async function readXsrf(url) {
  for (const name of XSRF_COOKIE_NAMES) {
    const v = await readCookie(url, name);
    if (v) return v;
  }
  return "";
}

/** 抓取并推送会话。返回 {ok, message}。 */
async function pushSession() {
  const cfg = await getConfig();
  if (!cfg.jiraBaseUrl) return setStatus(false, "未配置 Jira 地址");
  if (!cfg.skillToken) return setStatus(false, "未配置 skill token");

  const jsessionid = await readCookie(cfg.jiraBaseUrl, "JSESSIONID");
  if (!jsessionid) {
    return setStatus(false, "未读到 JSESSIONID（请先在浏览器登录 Jira）");
  }
  const xsrf = await readXsrf(cfg.jiraBaseUrl);

  const endpoint = cfg.serviceUrl.replace(/\/$/, "") + "/api/settings/jira-session-binding";
  try {
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + cfg.skillToken,
        "X-Skill-Token": cfg.skillToken,
      },
      body: JSON.stringify({
        jsessionid: jsessionid,
        xsrf_token: xsrf,
        jira_base_url: originOf(cfg.jiraBaseUrl),
      }),
    });
    if (resp.ok) {
      return setStatus(true, "已推送会话 ✓");
    }
    const txt = await resp.text();
    return setStatus(false, `服务返回 ${resp.status}: ${txt.slice(0, 120)}`);
  } catch (e) {
    return setStatus(false, "连接本地服务失败：" + e.message);
  }
}

async function setStatus(ok, message) {
  const status = { ok, message, ts: Date.now() };
  await chrome.storage.local.set({ lastStatus: status });
  try {
    chrome.action.setBadgeText({ text: ok ? "✓" : "!" });
    chrome.action.setBadgeBackgroundColor({ color: ok ? "#16a34a" : "#dc2626" });
  } catch (e) {
    /* badge 非关键 */
  }
  return status;
}

// ── 触发器 ───────────────────────────────────────────────────────────────

// 定时重推（防会话过期后看板掉线）
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("aiticket-push", { periodInMinutes: 25 });
});
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "aiticket-push") pushSession();
});

// JSESSIONID 变化即推（登录/续期后第一时间同步）
let _debounce = null;
chrome.cookies.onChanged.addListener((info) => {
  if (info.cookie && info.cookie.name === "JSESSIONID" && !info.removed) {
    clearTimeout(_debounce);
    _debounce = setTimeout(pushSession, 1500);
  }
});

// popup 手动触发
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "push") {
    pushSession().then(sendResponse);
    return true; // 异步响应
  }
});
