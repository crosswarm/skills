/**
 * AITicket Notification Center
 * 全局通知中枢：浏览器系统通知 + 页面顶部 banner
 * 依赖：/api/agents/stream (SSE)
 * 在所有页面 </body> 前引入即可生效
 */
(function () {
  "use strict";

  var NC = window._NC = {};
  var _sse = null;
  var _granted = false;
  var _banner = null;
  var _queue = [];   // 待显示 banner 队列
  var _tid = null;   // banner auto-hide timer

  // ── 初始化 ──────────────────────────────────────────────────────
  NC.init = function () {
    _injectBanner();
    _requestPermission();
    _connectSSE();
  };

  // ── 浏览器通知权限 ───────────────────────────────────────────────
  function _requestPermission() {
    if (!("Notification" in window)) return;
    if (Notification.permission === "granted") {
      _granted = true;
    } else if (Notification.permission !== "denied") {
      Notification.requestPermission().then(function (p) {
        _granted = p === "granted";
      });
    }
  }

  // ── SSE 连接 ─────────────────────────────────────────────────────
  function _connectSSE() {
    if (_sse) { try { _sse.close(); } catch (e) {} }
    try {
      _sse = new EventSource("/api/agents/stream");

      // 系统告警（miss task / jobmaster anomaly）
      _sse.addEventListener("system_alert", function (e) {
        var d = _safeJson(e.data);
        if (!d) return;
        _showBanner(d.title || "系统告警", d.body || "", d.level || "warning");
        _showBrowserNotif(d.title || "系统告警", d.body || "", d.level);
      });

      // action_required（已有场景复用）
      _sse.addEventListener("task_created", function (e) {
        var d = _safeJson(e.data);
        if (!d) return;
        if (d.kind === "hook_input" || d.status === "awaiting_human_review" ||
            d.status === "awaiting_parent_authorization") {
          var title = "待确认：" + (d.title || "任务");
          _showBrowserNotif(title, d.agent_name ? "来自 " + d.agent_name : "", "warning");
          // banner 由页面内已有 action_required strip 处理，不重复
        }
      });

      _sse.onerror = function () {
        setTimeout(_connectSSE, 8000);
      };
    } catch (e) {}
  }

  // ── 注入全局 banner 容器 ─────────────────────────────────────────
  function _injectBanner() {
    if (document.getElementById("nc-global-banner")) return;
    var el = document.createElement("div");
    el.id = "nc-global-banner";
    el.className = "nc-banner nc-hidden";
    el.innerHTML =
      '<span class="nc-banner-icon" id="nc-icon">⚠</span>' +
      '<span class="nc-banner-title" id="nc-title"></span>' +
      '<span class="nc-banner-body" id="nc-body"></span>' +
      '<button class="nc-banner-close" id="nc-close" title="关闭" aria-label="关闭">×</button>';
    document.body.insertBefore(el, document.body.firstChild);
    _banner = el;
    document.getElementById("nc-close").addEventListener("click", function () {
      _hideBanner();
    });
  }

  // ── Banner 显示 ──────────────────────────────────────────────────
  function _showBanner(title, body, level) {
    if (!_banner) return;
    var icon = level === "critical" ? "🔴" : level === "info" ? "ℹ" : "⚠";
    document.getElementById("nc-icon").textContent = icon;
    document.getElementById("nc-title").textContent = title;
    document.getElementById("nc-body").textContent = body ? "  " + body : "";
    _banner.className = "nc-banner nc-banner--" + (level || "warning") + " nc-visible";
    if (_tid) clearTimeout(_tid);
    // critical 不自动消失；其他 12s 后隐藏
    if (level !== "critical") {
      _tid = setTimeout(_hideBanner, 12000);
    }
  }

  function _hideBanner() {
    if (_banner) _banner.className = "nc-banner nc-hidden";
    if (_tid) { clearTimeout(_tid); _tid = null; }
  }

  // ── 浏览器系统通知 ───────────────────────────────────────────────
  function _showBrowserNotif(title, body, level) {
    if (!_granted || !("Notification" in window)) return;
    try {
      var n = new Notification("AITicket · " + title, {
        body: body || "",
        icon: "/favicon.ico",
        badge: "/favicon.ico",
        tag: "aiticket-" + title,  // 同 tag 自动替换，防重复堆叠
        requireInteraction: level === "critical",
      });
      if (level !== "critical") {
        setTimeout(function () { n.close(); }, 10000);
      }
      n.onclick = function () {
        window.focus();
        n.close();
      };
    } catch (e) {}
  }

  // ── 手动触发（供 JobMaster / 后端直接调用） ───────────────────────
  NC.notify = function (title, body, level) {
    _showBanner(title, body, level || "warning");
    _showBrowserNotif(title, body, level || "warning");
  };

  function _safeJson(s) {
    try { return JSON.parse(s); } catch (e) { return null; }
  }

  // ── 自动启动 ─────────────────────────────────────────────────────
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", NC.init);
  } else {
    NC.init();
  }
})();
