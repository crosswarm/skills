/**
 * AI工单系统 - 前端公共工具函数（合并版）
 * 供所有页面共享使用
 * 路径: /assets/common.js
 */

// Demo guard: 演示账号写操作拦截（后端 middleware 为终极防线，此处为 UX 优化）
(function injectDemoGuard() {
  var _fetch = window.fetch;
  var _isDemo = null;

  function checkDemo() {
    if (_isDemo !== null) return Promise.resolve(_isDemo);
    return _fetch(getApiBase() + "/api/auth/me")
      .then(function (r) {
        return r.json();
      })
      .then(function (d) {
        _isDemo = !!(d && d.user && d.user.is_demo);
        return _isDemo;
      })
      .catch(function () {
        _isDemo = false;
        return false;
      });
  }

  function showDemoToast() {
    var existing = document.getElementById("_demo_toast");
    if (existing) {
      clearTimeout(existing._timer);
      existing.remove();
    }
    var t = document.createElement("div");
    t.id = "_demo_toast";
    t.textContent = "🎬 演示账号：操作已被拦截";
    t.style.cssText =
      "position:fixed;top:24px;right:24px;background:#1f2937;color:#fff;" +
      "padding:12px 20px;border-radius:8px;z-index:99999;" +
      "box-shadow:0 4px 12px rgba(0,0,0,.25);font-size:14px;";
    document.body.appendChild(t);
    t._timer = setTimeout(function () {
      t.remove();
    }, 2500);
  }

  var _DEMO_WRITE_ALLOWED = ["/api/auth/logout", "/api/admin/reset-demo"];

  window.fetch = function (url, options) {
    options = options || {};
    var method = (options.method || "GET").toUpperCase();
    if (["POST", "PUT", "DELETE", "PATCH"].indexOf(method) !== -1) {
      var urlStr = String(url);
      var isAllowed = _DEMO_WRITE_ALLOWED.some(function (p) {
        return urlStr.indexOf(p) !== -1;
      });
      if (isAllowed) return _fetch(url, options);
      return checkDemo().then(function (isDemo) {
        if (isDemo) {
          showDemoToast();
          return new Response(
            JSON.stringify({ detail: "demo_blocked", message: "演示账号" }),
            { status: 403, headers: { "Content-Type": "application/json" } },
          );
        }
        return _fetch(url, options);
      });
    }
    return _fetch(url, options);
  };
})();

/**
 * 获取API基础URL
 * 开发规范: 前后端同域部署，使用相对路径
 * - 后端API: http://localhost:{port}/api/*
 * - 前端页面: http://localhost:{port}/*.html
 * - 生产环境: 使用相对路径，自动同域
 */
const getApiBase = () => {
  // Demo 沙箱：/demo/ 路径下所有 API 请求前缀改为 /demo
  if (
    window.location.pathname === "/demo" ||
    window.location.pathname.startsWith("/demo/")
  ) {
    return "/demo";
  }
  return "";
};

/** API基础URL（页面加载时计算一次） */
const API_BASE = getApiBase();

let llmConfigFetchImpl = (...args) => fetch(...args);

function getStoredLLMConfig() {
  const provider = localStorage.getItem("llm_last_provider") || "none";

  if (provider === "none") {
    return { provider: "none", apiKey: "", modelName: "", baseUrl: "" };
  }

  try {
    const raw = localStorage.getItem(`llm_config_${provider}`);
    const config = raw ? JSON.parse(raw) : {};
    return {
      provider,
      apiKey: config.apiKey || "",
      modelName: config.modelName || "",
      baseUrl: config.baseUrl || "",
    };
  } catch (error) {
    console.warn("[common] 读取LLM配置失败:", error);
    return { provider, apiKey: "", modelName: "", baseUrl: "" };
  }
}

async function getSharedLLMConfig(apiBase = API_BASE, options = {}) {
  const { allowServerFallback = true } = options;
  const localConfig = getStoredLLMConfig();
  if (localConfig.provider !== "none" && localConfig.apiKey) {
    return localConfig;
  }

  if (!allowServerFallback) {
    return localConfig;
  }

  try {
    const response = await llmConfigFetchImpl(`${apiBase}/api/config/llm`);
    if (!response.ok) {
      return localConfig;
    }

    const config = await response.json();
    const provider = config.last_provider || "none";
    if (provider === "none") {
      return { provider: "none", apiKey: "", modelName: "", baseUrl: "" };
    }

    // 新 per-user 端点返回 {providers:{p:{...}}, last_provider}；兼容旧扁平结构
    const providers = config.providers || config;
    const providerConfig = providers[provider] || {};
    return {
      provider,
      apiKey: providerConfig.api_key || "",
      modelName: providerConfig.model_name || "",
      baseUrl: providerConfig.base_url || "",
    };
  } catch (error) {
    console.warn("[common] 读取服务端LLM配置失败:", error);
    return localConfig;
  }
}

/**
 * 显示Toast提示
 * @param {string} message - 提示消息
 * @param {string} type - 提示类型: 'success' | 'error' | 'info' | 'warning'
 * @param {number} duration - 显示时长（毫秒），默认3000ms
 */
function showToast(message, type = "info", duration = 3000) {
  const existingToast = document.querySelector(".toast, .toast-notification");
  if (existingToast) existingToast.remove();

  const icons = {
    success: "\u2713",
    error: "\u2717",
    warning: "\u26A0",
    info: "\u2139",
  };
  const colorMap = {
    success: "ds-toast-success",
    error: "ds-toast-error",
    warning: "ds-toast-warning",
    info: "ds-toast-info",
  };

  // 确保toast容器存在
  let container = document.querySelector(".ds-toast-container");
  if (!container) {
    container = document.createElement("div");
    container.className = "ds-toast-container";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  const cssClass = colorMap[type] || colorMap.info;
  toast.className = `ds-toast ${cssClass}`;
  const iconSpan = document.createElement("span");
  iconSpan.innerHTML = icons[type] || icons.info;
  const msgSpan = document.createElement("span");
  msgSpan.textContent = message;
  toast.appendChild(iconSpan);
  toast.appendChild(msgSpan);

  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(100%)";
    toast.style.transition = "all 300ms ease";
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

/**
 * 格式化日期
 * @param {string|Date} date - 日期字符串或Date对象
 * @param {string} format - 格式，默认 'YYYY-MM-DD'
 * @returns {string} 格式化后的日期字符串
 */
function formatDate(date, format = "YYYY-MM-DD") {
  if (!date) return "-";
  const d = new Date(date);
  if (isNaN(d.getTime())) return "-";

  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hours = String(d.getHours()).padStart(2, "0");
  const minutes = String(d.getMinutes()).padStart(2, "0");

  return format
    .replace("YYYY", year)
    .replace("MM", month)
    .replace("DD", day)
    .replace("HH", hours)
    .replace("mm", minutes);
}

/** 防抖函数 */
function debounce(func, wait = 300) {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
}

/** 节流函数 */
function throttle(func, limit = 300) {
  let inThrottle;
  return function (...args) {
    if (!inThrottle) {
      func.apply(this, args);
      inThrottle = true;
      setTimeout(() => (inThrottle = false), limit);
    }
  };
}

/** 深拷贝对象 */
function deepClone(obj) {
  if (obj === null || typeof obj !== "object") return obj;
  if (obj instanceof Date) return new Date(obj.getTime());
  if (Array.isArray(obj)) return obj.map((item) => deepClone(item));
  return JSON.parse(JSON.stringify(obj));
}

/** 安全的JSON解析 */
function safeJsonParse(str, defaultValue = null) {
  try {
    return JSON.parse(str);
  } catch (e) {
    return defaultValue;
  }
}

/** 从localStorage安全获取数据 */
function getStorageItem(key, defaultValue = null) {
  try {
    const item = localStorage.getItem(key);
    return item !== null ? safeJsonParse(item, item) : defaultValue;
  } catch (e) {
    return defaultValue;
  }
}

/** 安全设置localStorage数据 */
function setStorageItem(key, value) {
  try {
    if (typeof value === "object") {
      localStorage.setItem(key, JSON.stringify(value));
    } else {
      localStorage.setItem(key, String(value));
    }
  } catch (e) {
    console.warn("localStorage设置失败:", e);
  }
}

function legacyCopyToClipboard(text) {
  if (
    typeof document === "undefined" ||
    typeof document.execCommand !== "function"
  ) {
    return false;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-9999px";
  textarea.style.left = "-9999px";
  textarea.style.opacity = "0";
  textarea.style.pointerEvents = "none";

  const previousActiveElement = document.activeElement;
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);

  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (error) {
    console.error("execCommand复制失败:", error);
    copied = false;
  } finally {
    textarea.remove();
    if (
      previousActiveElement &&
      typeof previousActiveElement.focus === "function" &&
      document.contains(previousActiveElement)
    ) {
      previousActiveElement.focus();
    }
  }

  return copied;
}

/** 复制文本到剪贴板 */
async function copyToClipboard(text) {
  const normalizedText = String(text ?? "");

  try {
    if (
      typeof navigator !== "undefined" &&
      navigator.clipboard &&
      typeof navigator.clipboard.writeText === "function"
    ) {
      await navigator.clipboard.writeText(normalizedText);
      return true;
    }
  } catch (err) {
    console.warn("Clipboard API复制失败，尝试降级方案:", err);
  }

  const copied = legacyCopyToClipboard(normalizedText);
  if (!copied) {
    console.error("复制失败: Clipboard API 与降级方案均不可用");
  }
  return copied;
}

/** 检查网络状态 */
function isOnline() {
  return navigator.onLine;
}

/** 等待指定时间 */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// 暴露全局
if (typeof window !== "undefined") {
  window.getStoredLLMConfig = getStoredLLMConfig;
  window.getSharedLLMConfig = getSharedLLMConfig;
}

// 导出
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    getApiBase,
    API_BASE,
    getStoredLLMConfig,
    getSharedLLMConfig,
    __setFetchForTests(fn) {
      llmConfigFetchImpl = fn;
    },
    showToast,
    formatDate,
    debounce,
    throttle,
    deepClone,
    safeJsonParse,
    getStorageItem,
    setStorageItem,
    copyToClipboard,
    isOnline,
    sleep,
  };
}

/**
 * DSLLMConfig — 全局 AI 配置管理（侧边栏入口）
 * 读写后端 /api/config/llm，所有页面共享
 */
window.DSLLMConfig = (function () {
  var MODAL_ID = "ds-llm-config-modal";
  var BASE = typeof getAPIBase === "function" ? getAPIBase() : "";
  var API = BASE + "/api/config/llm";
  var SYSTEM_API = BASE + "/api/config/llm/system";
  var FEATURES_API = BASE + "/api/config/llm/features";
  var _isAdmin = false;
  var _activeTab = "my";
  var _myCfg = {}; // 当前用户已配 provider 映射（后端真相源）
  var _sysCfg = {}; // 系统级 provider 映射（仅 admin）
  // 真相源是后端；localStorage 仅作离线/本地缓存回显
  var PROVIDERS = [
    "gemini",
    "openai",
    "aliyun",
    "minimax",
    "deepseek",
    "zhipu",
    "kimi",
    "local",
  ];

  var tabStyle =
    "padding:8px 16px;border:none;border-bottom:2px solid transparent;background:none;cursor:pointer;font-size:var(--ds-text-sm);color:var(--ds-text-muted);";
  var tabActiveStyle =
    tabStyle +
    "color:var(--ds-accent);border-bottom-color:var(--ds-accent);font-weight:var(--ds-font-semibold);";
  var selectStyle =
    "width:100%;padding:6px 8px;border:1px solid var(--ds-border);border-radius:var(--ds-radius-md);background:var(--ds-bg-surface);color:var(--ds-text-primary);font-size:var(--ds-text-sm);";
  var inputStyle =
    "display:block;width:100%;margin-top:4px;padding:8px;border:1px solid var(--ds-border);border-radius:var(--ds-radius-md);background:var(--ds-bg-surface);color:var(--ds-text-primary);font-size:var(--ds-text-sm);box-sizing:border-box;";
  var btnStyle =
    "padding:8px 16px;border:1px solid var(--ds-border);border-radius:var(--ds-radius-md);background:none;cursor:pointer;color:var(--ds-text-secondary);font-size:var(--ds-text-sm);";
  var btnPrimaryStyle =
    "padding:8px 16px;border:none;border-radius:var(--ds-radius-md);background:var(--ds-accent);color:white;cursor:pointer;font-size:var(--ds-text-sm);";
  // 系统级（管理员）区域：醒目边框 + 底色，与用户个人配置强区分
  var sysBoxStyle =
    "margin-top:18px;padding:14px;border:2px solid #d97706;border-radius:var(--ds-radius-md);background:rgba(217,119,6,0.06);";
  var sysTitleStyle =
    "margin:0 0 4px;font-size:var(--ds-text-sm);font-weight:var(--ds-font-semibold);color:#b45309;display:flex;align-items:center;gap:6px;";
  var sysHintStyle =
    "margin:0 0 10px;font-size:var(--ds-text-xs);color:#92400e;";

  function _esc(v) {
    return String(v == null ? "" : v)
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;");
  }

  // 本地缓存（离线回显），后端保存成功后以后端为准
  function _cacheProvider(provider, cfg) {
    try {
      localStorage.setItem("llm_last_provider", provider);
      localStorage.setItem(
        "llm_config_" + provider,
        JSON.stringify({
          apiKey: cfg.api_key || "",
          modelName: cfg.model_name || "",
          baseUrl: cfg.base_url || "",
        }),
      );
    } catch (e) {}
  }

  function _readCache(provider) {
    try {
      var raw = localStorage.getItem("llm_config_" + provider);
      if (!raw) return null;
      var c = JSON.parse(raw);
      return {
        api_key: c.apiKey || "",
        model_name: c.modelName || "",
        base_url: c.baseUrl || "",
      };
    } catch (e) {
      return null;
    }
  }

  function ensureModal() {
    if (document.getElementById(MODAL_ID)) return;
    var div = document.createElement("div");
    div.id = MODAL_ID;
    div.style.cssText =
      "position:fixed;inset:0;z-index:200;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,0.5);";
    div.onclick = function (e) {
      if (e.target === div) close();
    };
    div.innerHTML =
      '<div style="background:var(--ds-bg-surface);border-radius:var(--ds-radius-lg);width:min(520px,90vw);max-height:90vh;overflow:auto;box-shadow:var(--ds-shadow-xl);">' +
      '<div style="padding:16px 20px;border-bottom:1px solid var(--ds-border-subtle);display:flex;justify-content:space-between;align-items:center;">' +
      '<h2 style="font-size:var(--ds-text-lg);font-weight:var(--ds-font-semibold);color:var(--ds-text-primary);margin:0;">AI 配置</h2>' +
      '<button onclick="DSLLMConfig.close()" style="background:none;border:none;cursor:pointer;font-size:20px;color:var(--ds-text-muted);">&times;</button>' +
      "</div>" +
      '<div id="ds-llm-tabs" style="display:none;padding:0 20px;border-bottom:1px solid var(--ds-border-subtle);"></div>' +
      '<div id="ds-llm-config-body" style="padding:20px;"></div>' +
      "</div>";
    document.body.appendChild(div);
  }

  function checkAdmin() {
    return fetch(BASE + "/api/auth/me", { credentials: "include" })
      .then(function (r) {
        return r.ok ? r.json() : {};
      })
      .then(function (u) {
        var user = u.user || u;
        _isAdmin = user.role === "admin";
      })
      .catch(function () {
        _isAdmin = false;
      });
  }

  function renderTabs() {
    var tabsEl = document.getElementById("ds-llm-tabs");
    if (!_isAdmin) {
      tabsEl.style.display = "none";
      return;
    }
    tabsEl.style.display = "flex";
    tabsEl.innerHTML =
      '<button id="ds-tab-my" onclick="DSLLMConfig._switchTab(\'my\')" style="' +
      (_activeTab === "my" ? tabActiveStyle : tabStyle) +
      '">我的配置</button>' +
      '<button id="ds-tab-features" onclick="DSLLMConfig._switchTab(\'features\')" style="' +
      (_activeTab === "features" ? tabActiveStyle : tabStyle) +
      '">功能路由</button>';
  }

  function switchTab(tab) {
    _activeTab = tab;
    renderTabs();
    if (tab === "my") loadConfig();
    else loadFeatureRouting();
  }

  function open() {
    ensureModal();
    _activeTab = "my";
    document.getElementById(MODAL_ID).style.display = "flex";
    document.getElementById("ds-llm-config-body").innerHTML =
      '<p style="color:var(--ds-text-muted);">加载中...</p>';
    checkAdmin().then(function () {
      renderTabs();
      loadConfig();
    });
  }

  function close() {
    var m = document.getElementById(MODAL_ID);
    if (m) m.style.display = "none";
  }

  // 渲染当前用户个人配置表单（后端为真相源），admin 额外渲染系统级区
  function _renderMyConfig(provider) {
    var body = document.getElementById("ds-llm-config-body");
    if (!body) return;
    var current = _myCfg[provider] || _readCache(provider) || {};
    var html =
      '<div style="display:flex;flex-direction:column;gap:12px;">' +
      '<p style="margin:0;font-size:var(--ds-text-xs);color:var(--ds-text-muted);">这是你的个人 LLM 配置，仅对你自己生效（不影响其他用户）。</p>' +
      '<label style="font-size:var(--ds-text-sm);color:var(--ds-text-secondary);">Provider' +
      '<select id="ds-llm-provider" style="' +
      inputStyle +
      '" onchange="DSLLMConfig._onProviderChange(this.value)">' +
      PROVIDERS.map(function (p) {
        return (
          '<option value="' +
          p +
          '"' +
          (p === provider ? " selected" : "") +
          ">" +
          p +
          "</option>"
        );
      }).join("") +
      "</select></label>" +
      '<label style="font-size:var(--ds-text-sm);color:var(--ds-text-secondary);">API Key' +
      '<input id="ds-llm-apikey" type="password" value="' +
      _esc(current.api_key) +
      '" style="' +
      inputStyle +
      '"></label>' +
      '<label style="font-size:var(--ds-text-sm);color:var(--ds-text-secondary);">Model Name' +
      '<input id="ds-llm-model" value="' +
      _esc(current.model_name) +
      '" style="' +
      inputStyle +
      '" placeholder="e.g. gemini-2.0-flash"></label>' +
      '<label style="font-size:var(--ds-text-sm);color:var(--ds-text-secondary);">Base URL (可选)' +
      '<input id="ds-llm-baseurl" value="' +
      _esc(current.base_url) +
      '" style="' +
      inputStyle +
      '" placeholder="留空使用默认"></label>' +
      '<div id="ds-llm-test-result" style="display:none;padding:8px 12px;border-radius:var(--ds-radius-md);font-size:var(--ds-text-sm);margin-top:4px;"></div>' +
      '<div style="display:flex;gap:8px;justify-content:space-between;align-items:center;margin-top:8px;">' +
      '<button onclick="DSLLMConfig.test()" id="ds-llm-test-btn" style="' +
      btnStyle +
      '">测试连接</button>' +
      '<div style="display:flex;gap:8px;">' +
      '<button onclick="DSLLMConfig.deleteProvider()" style="' +
      btnStyle +
      ';color:var(--ds-danger);" title="删除当前 provider 的个人配置">删除</button>' +
      '<button onclick="DSLLMConfig.save()" style="' +
      btnPrimaryStyle +
      '">保存</button>' +
      "</div></div>" +
      // admin 专属系统级区占位（loadSystemConfig 异步填充）
      (_isAdmin ? '<div id="ds-llm-system-region"></div>' : "") +
      "</div>";
    body.innerHTML = html;
    if (_isAdmin) loadSystemConfig();
  }

  function loadConfig() {
    var body = document.getElementById("ds-llm-config-body");
    body.innerHTML = '<p style="color:var(--ds-text-muted);">加载中...</p>';
    fetch(API, { credentials: "include" })
      .then(function (r) {
        return r.json();
      })
      .then(function (cfg) {
        // 契约：{providers:{p:{api_key,model_name,base_url}}, last_provider}
        // 兼容旧版扁平结构 cfg[p]
        _myCfg = cfg && cfg.providers ? cfg.providers : {};
        if (!cfg || !cfg.providers) {
          PROVIDERS.forEach(function (p) {
            if (cfg && cfg[p]) _myCfg[p] = cfg[p];
          });
        }
        var provider =
          (cfg && cfg.last_provider) ||
          (function () {
            try {
              return localStorage.getItem("llm_last_provider");
            } catch (e) {
              return null;
            }
          })() ||
          "gemini";
        _renderMyConfig(provider);
      })
      .catch(function () {
        // 后端不可达：用本地缓存离线回显，并提示
        var provider = "gemini";
        try {
          provider = localStorage.getItem("llm_last_provider") || "gemini";
        } catch (e) {}
        _myCfg = {};
        _renderMyConfig(provider);
        if (typeof showToast === "function")
          showToast("无法连接后端，已离线回显本地缓存", "warning");
      });
  }

  // 系统级 LLM（仅 admin）：醒目区块，读写 /api/config/llm/system
  function loadSystemConfig() {
    var region = document.getElementById("ds-llm-system-region");
    if (!region) return;
    region.innerHTML =
      '<div style="' +
      sysHintStyle +
      'margin-top:18px;">加载系统级配置...</div>';
    fetch(SYSTEM_API, { credentials: "include" })
      .then(function (r) {
        return r.json();
      })
      .then(function (cfg) {
        _sysCfg = cfg && cfg.providers ? cfg.providers : cfg || {};
        var sp =
          (cfg && cfg.last_provider) || Object.keys(_sysCfg)[0] || "gemini";
        _renderSystemConfig(sp);
      })
      .catch(function () {
        region.innerHTML =
          '<div style="' +
          sysBoxStyle +
          '"><div style="' +
          sysHintStyle +
          '">系统级配置加载失败</div></div>';
      });
  }

  function _renderSystemConfig(provider) {
    var region = document.getElementById("ds-llm-system-region");
    if (!region) return;
    var current = _sysCfg[provider] || {};
    region.innerHTML =
      '<div style="' +
      sysBoxStyle +
      '">' +
      '<p style="' +
      sysTitleStyle +
      '"><span>⚙️ 系统级 LLM · 仅管理员 · 全局兜底</span></p>' +
      '<p style="' +
      sysHintStyle +
      '">此配置面向所有用户，作为开启兜底功能时的后备凭据，与上方你的个人配置相互独立。</p>' +
      '<div style="display:flex;flex-direction:column;gap:10px;">' +
      '<label style="font-size:var(--ds-text-sm);color:#92400e;">Provider' +
      '<select id="ds-sys-provider" style="' +
      inputStyle +
      '" onchange="DSLLMConfig._onSysProviderChange(this.value)">' +
      PROVIDERS.map(function (p) {
        return (
          '<option value="' +
          p +
          '"' +
          (p === provider ? " selected" : "") +
          ">" +
          p +
          "</option>"
        );
      }).join("") +
      "</select></label>" +
      '<label style="font-size:var(--ds-text-sm);color:#92400e;">API Key' +
      '<input id="ds-sys-apikey" type="password" value="' +
      _esc(current.api_key) +
      '" style="' +
      inputStyle +
      '"></label>' +
      '<label style="font-size:var(--ds-text-sm);color:#92400e;">Model Name' +
      '<input id="ds-sys-model" value="' +
      _esc(current.model_name) +
      '" style="' +
      inputStyle +
      '" placeholder="e.g. gemini-2.0-flash"></label>' +
      '<label style="font-size:var(--ds-text-sm);color:#92400e;">Base URL (可选)' +
      '<input id="ds-sys-baseurl" value="' +
      _esc(current.base_url) +
      '" style="' +
      inputStyle +
      '" placeholder="留空使用默认"></label>' +
      '<div style="display:flex;justify-content:flex-end;">' +
      '<button onclick="DSLLMConfig._saveSystem()" style="' +
      btnPrimaryStyle +
      ';background:#d97706;">保存系统级配置</button>' +
      "</div></div></div>";
  }

  function onSysProviderChange(provider) {
    _renderSystemConfig(provider);
  }

  function saveSystem() {
    var provider = document.getElementById("ds-sys-provider").value;
    var payload = {
      provider: provider,
      api_key: document.getElementById("ds-sys-apikey").value,
      model_name: document.getElementById("ds-sys-model").value,
      base_url: document.getElementById("ds-sys-baseurl").value,
    };
    fetch(SYSTEM_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, body: j };
        });
      })
      .then(function (res) {
        if (res.ok && res.body.status !== "error") {
          _sysCfg[provider] = {
            api_key: payload.api_key,
            model_name: payload.model_name,
            base_url: payload.base_url,
          };
          if (typeof showToast === "function")
            showToast("系统级配置已保存", "success");
        } else {
          if (typeof showToast === "function")
            showToast(
              res.body.detail || res.body.message || "保存失败",
              "error",
            );
        }
      })
      .catch(function () {
        if (typeof showToast === "function") showToast("保存失败", "error");
      });
  }

  // 删除当前 provider 的个人配置
  function deleteProvider() {
    var provider = document.getElementById("ds-llm-provider").value;
    fetch(API + "/" + encodeURIComponent(provider), {
      method: "DELETE",
      credentials: "include",
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, body: j };
        });
      })
      .then(function (res) {
        if (res.ok && res.body.status !== "error") {
          delete _myCfg[provider];
          try {
            localStorage.removeItem("llm_config_" + provider);
          } catch (e) {}
          if (typeof showToast === "function")
            showToast("已删除该 provider 的个人配置", "success");
          _renderMyConfig(provider);
        } else {
          if (typeof showToast === "function")
            showToast(
              res.body.detail || res.body.message || "删除失败",
              "error",
            );
        }
      })
      .catch(function () {
        if (typeof showToast === "function") showToast("删除失败", "error");
      });
  }

  function loadFeatureRouting() {
    var body = document.getElementById("ds-llm-config-body");
    body.innerHTML = '<p style="color:var(--ds-text-muted);">加载中...</p>';
    fetch(FEATURES_API, { credentials: "include" })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        var routing = data.routing || {};
        var features = data.features || [];
        var providers = data.available_providers || [];
        var fallback = data.fallback || {};
        var defaultProvider = routing._default || "";

        function makeToggle(featureId, on) {
          return (
            '<label style="display:inline-flex;align-items:center;gap:4px;font-size:var(--ds-text-xs);color:var(--ds-text-muted);white-space:nowrap;cursor:pointer;">' +
            '<input type="checkbox" id="ds-fb-' +
            featureId +
            '"' +
            (on ? " checked" : "") +
            ' style="cursor:pointer;">系统兜底</label>'
          );
        }

        function makeSelect(featureId, selected) {
          var opts =
            '<option value=""' +
            (!selected ? " selected" : "") +
            ">使用默认</option>";
          providers.forEach(function (p) {
            opts +=
              '<option value="' +
              p +
              '"' +
              (p === selected ? " selected" : "") +
              ">" +
              p +
              "</option>";
          });
          return (
            '<select id="ds-fr-' +
            featureId +
            '" style="' +
            selectStyle +
            '">' +
            opts +
            "</select>"
          );
        }

        var rows = features
          .map(function (f) {
            return (
              '<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--ds-border-subtle);">' +
              '<span style="flex:1;font-size:var(--ds-text-sm);color:var(--ds-text-primary);">' +
              f.name +
              "</span>" +
              '<div style="width:96px;text-align:right;">' +
              makeToggle(f.id, !!fallback[f.id]) +
              "</div>" +
              '<div style="width:180px;">' +
              makeSelect(f.id, routing[f.id] || "") +
              "</div>" +
              "</div>"
            );
          })
          .join("");

        body.innerHTML =
          '<div style="display:flex;flex-direction:column;gap:4px;">' +
          '<p style="font-size:var(--ds-text-xs);color:var(--ds-text-muted);margin:0 0 4px;">为系统后台功能指定 LLM Provider，API Key 复用已配置的 Provider 凭据。</p>' +
          '<p style="font-size:var(--ds-text-xs);color:var(--ds-text-muted);margin:0 0 8px;line-height:1.5;"><b>系统兜底</b>：关＝该功能强制使用用户自配 LLM（用户没配则报错引导）；开＝用户没配时回退使用系统级 LLM。智能回复等默认关。</p>' +
          '<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:2px solid var(--ds-border);">' +
          '<span style="flex:1;font-size:var(--ds-text-sm);font-weight:var(--ds-font-semibold);color:var(--ds-text-primary);">系统默认</span>' +
          '<div style="width:96px;"></div>' +
          '<div style="width:180px;">' +
          makeSelect("_default", defaultProvider) +
          "</div>" +
          "</div>" +
          rows +
          '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">' +
          '<button onclick="DSLLMConfig.close()" style="' +
          btnStyle +
          '">取消</button>' +
          '<button onclick="DSLLMConfig._saveFeatureRouting()" style="' +
          btnPrimaryStyle +
          '">保存</button>' +
          "</div></div>";
      })
      .catch(function () {
        body.innerHTML =
          '<p style="color:var(--ds-danger);">加载失败（需管理员权限）</p>';
      });
  }

  function saveFeatureRouting() {
    var routing = {};
    var selects = document.querySelectorAll("[id^='ds-fr-']");
    selects.forEach(function (sel) {
      var featureId = sel.id.replace("ds-fr-", "");
      if (sel.value) routing[featureId] = sel.value;
    });
    // 逐功能"允许系统级兜底"开关
    var fallback = {};
    var toggles = document.querySelectorAll("[id^='ds-fb-']");
    toggles.forEach(function (cb) {
      var featureId = cb.id.replace("ds-fb-", "");
      fallback[featureId] = !!cb.checked;
    });
    fetch(FEATURES_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ routing: routing, fallback: fallback }),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (res) {
        if (res.status === "success") {
          if (typeof showToast === "function")
            showToast("功能路由已保存", "success");
          close();
        } else {
          if (typeof showToast === "function")
            showToast(res.detail || "保存失败", "error");
        }
      })
      .catch(function () {
        if (typeof showToast === "function") showToast("保存失败", "error");
      });
  }

  function save() {
    var provider = document.getElementById("ds-llm-provider").value;
    var apiKey = document.getElementById("ds-llm-apikey").value;
    var modelName = document.getElementById("ds-llm-model").value;
    var baseUrl = document.getElementById("ds-llm-baseurl").value;
    var payload = {
      provider: provider,
      api_key: apiKey,
      model_name: modelName,
      base_url: baseUrl,
    };
    fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, body: j };
        });
      })
      .then(function (res) {
        if (!res.ok || res.body.status === "error") {
          if (typeof showToast === "function")
            showToast(
              res.body.detail || res.body.message || "保存失败",
              "error",
            );
          return;
        }
        // 后端为准：更新内存缓存 + 本地离线缓存
        _myCfg[provider] = {
          api_key: apiKey,
          model_name: modelName,
          base_url: baseUrl,
        };
        _cacheProvider(provider, payload);
        if (typeof showToast === "function")
          showToast("个人 AI 配置已保存", "success");
        close();
      })
      .catch(function () {
        if (typeof showToast === "function") showToast("保存失败", "error");
      });
  }

  // 切换 provider 时从已加载的后端缓存回显，避免重复请求
  function onProviderChange(provider) {
    var c = _myCfg[provider] || _readCache(provider) || {};
    var keyEl = document.getElementById("ds-llm-apikey");
    var modelEl = document.getElementById("ds-llm-model");
    var baseEl = document.getElementById("ds-llm-baseurl");
    if (keyEl) keyEl.value = c.api_key || "";
    if (modelEl) modelEl.value = c.model_name || "";
    if (baseEl) baseEl.value = c.base_url || "";
  }

  function test() {
    var provider = document.getElementById("ds-llm-provider").value;
    var apiKey = document.getElementById("ds-llm-apikey").value;
    var model = document.getElementById("ds-llm-model").value;
    var baseUrl = document.getElementById("ds-llm-baseurl").value;
    var resultEl = document.getElementById("ds-llm-test-result");
    var testBtn = document.getElementById("ds-llm-test-btn");
    if (!apiKey) {
      resultEl.style.display = "block";
      resultEl.style.cssText +=
        ";background:var(--ds-warning-bg,#fef3c7);color:var(--ds-warning,#92400e);";
      resultEl.textContent = "请先填写 API Key";
      return;
    }
    testBtn.disabled = true;
    testBtn.textContent = "测试中...";
    resultEl.style.display = "block";
    resultEl.style.cssText +=
      ";background:var(--ds-bg-muted,#f8fafc);color:var(--ds-text-muted,#64748b);";
    resultEl.textContent = "正在测试连接...";
    var testApi = BASE + "/api/llm/test";
    fetch(testApi, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({
        provider: provider,
        api_key: apiKey,
        model_name: model,
        base_url: baseUrl,
      }),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (res) {
        testBtn.disabled = false;
        testBtn.textContent = "测试连接";
        if (res.success || res.status === "success") {
          resultEl.style.cssText +=
            ";background:var(--ds-success-bg,#f0fdf4);color:var(--ds-success,#166534);";
          resultEl.textContent =
            "连接成功" + (res.model ? " " + res.model : "");
        } else {
          resultEl.style.cssText +=
            ";background:var(--ds-danger-bg,#fef2f2);color:var(--ds-danger,#991b1b);";
          resultEl.textContent = res.message || res.error || "连接失败";
        }
      })
      .catch(function (e) {
        testBtn.disabled = false;
        testBtn.textContent = "测试连接";
        resultEl.style.cssText +=
          ";background:var(--ds-danger-bg,#fef2f2);color:var(--ds-danger,#991b1b);";
        resultEl.textContent = "请求失败: " + e.message;
      });
  }

  return {
    open: open,
    close: close,
    save: save,
    test: test,
    deleteProvider: deleteProvider,
    _onProviderChange: onProviderChange,
    _onSysProviderChange: onSysProviderChange,
    _saveSystem: saveSystem,
    _switchTab: switchTab,
    _saveFeatureRouting: saveFeatureRouting,
  };
})();
