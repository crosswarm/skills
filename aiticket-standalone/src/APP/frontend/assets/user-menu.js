/**
 * 通用顶栏用户头像组件。
 * 用法：
 *   在 .ds-page-header-actions 里放 <div id="user-menu-container"></div>
 *   末尾 <script>UserMenu.init({ requireAdmin: true });</script>
 *
 * 支持选项：
 *   requireAdmin   — true 时非 admin 用户重定向登录页
 *   onSettingsClick — 自定义"账号设置"点击回调；未提供则跳转 /board.html#settings
 */
window.UserMenu = (function () {
  var _currentUser = null;
  var _menuOpen = false;
  var _opts = {};

  function _redirect(reason) {
    var base = typeof API_BASE !== "undefined" ? API_BASE : "";
    var next = encodeURIComponent(location.pathname + location.search);
    location.href =
      base + "/login.html?next=" + next + (reason ? "&reason=" + reason : "");
  }

  async function init(opts) {
    _opts = opts || {};
    var container = document.querySelector("#user-menu-container");
    if (!container) return;

    var user;
    try {
      var r = await fetch(
        (typeof API_BASE !== "undefined" ? API_BASE : "") + "/api/auth/me",
      );
      if (!r.ok) {
        _redirect();
        return;
      }
      var data = await r.json();
      user = data && data.user;
      if (!user) {
        _redirect();
        return;
      }
      if (_opts.requireAdmin && user.role !== "admin") {
        _redirect("admin_required");
        return;
      }
    } catch (e) {
      _redirect();
      return;
    }

    _currentUser = user;
    container.innerHTML = _renderHTML(user);
    _bindEvents();
  }

  function _esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function _renderHTML(user) {
    var displayName = user.display_name || user.username || "?";
    var initial = displayName.charAt(0).toUpperCase();
    var roleLabel = user.role === "admin" ? "管理员" : "成员";
    return (
      '<div id="_um-wrap" style="position:relative;display:inline-block;">' +
      '<button id="_um-btn" style="display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--ds-border,#e2e8f0);border-radius:8px;background:var(--ds-bg-surface,#fff);cursor:pointer;font-size:13px;color:var(--ds-text-primary,#1e293b);transition:all 0.15s;"' +
      " onmouseover=\"this.style.borderColor='var(--ds-primary,#6366f1)'\" onmouseout=\"this.style.borderColor='var(--ds-border,#e2e8f0)'\">" +
      '<span style="display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;font-size:12px;font-weight:600;flex-shrink:0;">' +
      _esc(initial) +
      "</span>" +
      '<span class="ds-hide-mobile" style="max-width:80px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' +
      _esc(displayName) +
      "</span>" +
      '<svg class="ds-hide-mobile" width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24" style="flex-shrink:0;opacity:0.5;"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>' +
      "</button>" +
      '<div id="_um-dd" style="display:none;position:absolute;right:0;top:calc(100% + 6px);min-width:200px;background:var(--ds-bg-surface,#fff);border:1px solid var(--ds-border,#e2e8f0);border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,0.12);z-index:1000;padding:6px;font-size:13px;">' +
      '<div style="padding:8px 10px;font-size:12px;color:var(--ds-text-muted,#64748b);border-bottom:1px solid var(--ds-border,#e2e8f0);margin-bottom:4px;">' +
      '<div style="font-weight:500;color:var(--ds-text-primary,#1e293b);">' +
      _esc(displayName) +
      "</div>" +
      '<div style="font-size:11px;margin-top:2px;">' +
      _esc(roleLabel) +
      "</div>" +
      "</div>" +
      '<button id="_um-settings" style="display:flex;align-items:center;gap:8px;width:100%;padding:8px 10px;border:none;background:none;cursor:pointer;border-radius:6px;color:var(--ds-text-primary,#1e293b);text-align:left;font-size:13px;transition:background 0.1s;"' +
      " onmouseover=\"this.style.background='var(--ds-bg-muted,#f1f5f9)'\" onmouseout=\"this.style.background='none'\">&#9881;&#65039; 账号设置</button>" +
      '<div style="border-top:1px solid var(--ds-border,#e2e8f0);margin-top:4px;padding-top:4px;">' +
      '<button id="_um-logout" style="display:flex;align-items:center;gap:8px;width:100%;padding:8px 10px;border:none;background:none;cursor:pointer;border-radius:6px;color:#ef4444;text-align:left;font-size:13px;transition:background 0.1s;"' +
      " onmouseover=\"this.style.background='#fef2f2'\" onmouseout=\"this.style.background='none'\">&#128682; 退出登录</button>" +
      "</div></div></div>"
    );
  }

  function _toggle(e) {
    e && e.stopPropagation();
    _menuOpen = !_menuOpen;
    var dd = document.getElementById("_um-dd");
    if (dd) dd.style.display = _menuOpen ? "block" : "none";
  }

  function _close() {
    _menuOpen = false;
    var dd = document.getElementById("_um-dd");
    if (dd) dd.style.display = "none";
  }

  function _bindEvents() {
    var btn = document.getElementById("_um-btn");
    if (btn) btn.addEventListener("click", _toggle);

    document.addEventListener("click", function (e) {
      if (_menuOpen && !e.target.closest("#_um-wrap")) _close();
    });

    var settingsBtn = document.getElementById("_um-settings");
    if (settingsBtn) {
      settingsBtn.addEventListener("click", function () {
        _close();
        if (typeof _opts.onSettingsClick === "function") {
          _opts.onSettingsClick();
        } else {
          window.location.href =
            (typeof API_BASE !== "undefined" ? API_BASE : "") +
            "/board.html#settings";
        }
      });
    }

    var logoutBtn = document.getElementById("_um-logout");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", async function () {
        try {
          await fetch(
            (typeof API_BASE !== "undefined" ? API_BASE : "") +
              "/api/auth/logout",
            { method: "POST" },
          );
        } catch (e) {
          /* ignore */
        }
        window.location.href =
          (typeof API_BASE !== "undefined" ? API_BASE : "") + "/login.html";
      });
    }
  }

  return {
    init: init,
    getUser: function () {
      return _currentUser;
    },
  };
})();
