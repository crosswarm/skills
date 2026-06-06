/*! aiticket © 2026 Qiang Xiao <crossfone@qq.com>. id:aaac6ff8d4a6ce54 */
/**
 * scope-switcher.js — 全站项目+领域模块作用域切换器
 *
 * 使用方式：
 *   1. 在 .ds-page-header-actions 里放 <div id="scope-switcher-container"></div>
 *   2. 末尾 <script src="/static/assets/scope-switcher.js"></script>
 *   3. <script>ScopeSwitcher.init({ onChange: function(scope){} });</script>
 *
 * 事件：
 *   window.dispatchEvent(new CustomEvent('scope-changed', { detail: { project_key, domain_modules } }))
 *
 * 存储：board_filter_v1 (localStorage)
 *   { project: { value, label }, assignee: {...}, domain_modules: [] }
 */
window.ScopeSwitcher = (function () {
  "use strict";

  var STORE_KEY = "board_filter_v1";
  var _onChange = null;

  // state for the open modal
  var _modalProject = null; // { key, name }
  var _modalModules = [];
  var _allModules = [];
  var _projectOptions = [];

  /* ── helpers ── */

  function _apiBase() {
    return typeof API_BASE !== "undefined" ? API_BASE : "";
  }

  function _loadStore() {
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    } catch (e) {
      return {};
    }
  }

  function _saveStore(data) {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(data));
    } catch (e) {}
  }

  function _escHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function _pinyinInitials(str) {
    // lightweight: just return empty — full pinyin lib not needed for basic search
    return "";
  }

  /* ── public: getScope ── */

  function getScope() {
    var s = _loadStore();
    return {
      project_key:
        (s.project && s.project.value) ||
        (typeof ProjectCtx !== "undefined" ? ProjectCtx.get() : "MYPROJECT"),
      domain_modules: Array.isArray(s.domain_modules) ? s.domain_modules : [],
    };
  }

  /* ── trigger label ── */

  function _triggerText(scope) {
    var pk = scope.project_key || "MYPROJECT";
    var mods = scope.domain_modules || [];
    var pkLabel = pk === "ALL" || pk === "" ? "全部项目" : pk;
    if (mods.length === 0) return pkLabel + " · 全部模块";
    if (mods.length === 1) return pkLabel + " · " + mods[0];
    return pkLabel + " · " + mods[0] + "+" + (mods.length - 1);
  }

  function _renderTriggerLabel() {
    var el = document.getElementById("scope-trigger-label");
    if (el) el.textContent = _triggerText(getScope());
  }

  /* ── modal HTML (injected once into document.body) ── */

  function _ensureModal() {
    if (document.getElementById("scope-modal")) return;
    var div = document.createElement("div");
    div.innerHTML =
      '<div id="scope-modal" style="display:none;position:fixed;inset:0;z-index:9000;background:rgba(0,0,0,0.5);align-items:center;justify-content:center" onclick="if(event.target===this)ScopeSwitcher.closeModal()">' +
      '<div style="background:#fff;border-radius:12px;padding:24px;width:480px;max-width:95vw;max-height:85vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,0.2)">' +
      '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">' +
      '<h3 style="font-size:15px;font-weight:600;color:#1e293b;margin:0">作用域设置</h3>' +
      '<button onclick="ScopeSwitcher.closeModal()" style="background:none;border:none;cursor:pointer;color:#94a3b8;padding:4px;line-height:1">' +
      '<svg width="18" height="18" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>' +
      "</button>" +
      "</div>" +
      '<div style="margin-bottom:16px">' +
      '<label style="display:block;font-size:13px;font-weight:500;color:#475569;margin-bottom:6px">项目</label>' +
      '<input id="ss-project-search" type="text" placeholder="搜索项目名称或编码..." autocomplete="off"' +
      ' style="width:100%;box-sizing:border-box;padding:8px 12px;border:1px solid #e2e8f0;border-radius:8px;font-size:13px;outline:none"' +
      ' oninput="ScopeSwitcher._filterProjects(this.value)">' +
      '<div id="ss-project-list" style="margin-top:4px;border:1px solid #f1f5f9;border-radius:8px;overflow:hidden;max-height:160px;overflow-y:auto"></div>' +
      "</div>" +
      '<div style="margin-bottom:20px">' +
      '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">' +
      '<label style="font-size:13px;font-weight:500;color:#475569">领域模块 <span style="color:#94a3b8;font-weight:400">(不选=全部)</span></label>' +
      '<div style="display:flex;gap:8px">' +
      '<button onclick="ScopeSwitcher._selectAll()" style="background:none;border:none;cursor:pointer;font-size:12px;color:#6366f1">全选</button>' +
      '<span style="color:#cbd5e1">|</span>' +
      '<button onclick="ScopeSwitcher._clearAll()" style="background:none;border:none;cursor:pointer;font-size:12px;color:#64748b">清空</button>' +
      "</div>" +
      "</div>" +
      '<div id="ss-modules-list" style="border:1px solid #f1f5f9;border-radius:8px;padding:8px;max-height:200px;overflow-y:auto">' +
      '<div style="font-size:13px;color:#94a3b8;text-align:center;padding:8px">请先选择项目</div>' +
      "</div>" +
      "</div>" +
      '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button onclick="ScopeSwitcher.closeModal()" style="padding:8px 16px;font-size:13px;background:#f1f5f9;border:none;border-radius:8px;cursor:pointer">取消</button>' +
      '<button onclick="ScopeSwitcher._applyScope()" style="padding:8px 16px;font-size:13px;background:#6366f1;color:#fff;border:none;border-radius:8px;cursor:pointer">应用</button>' +
      "</div>" +
      "</div>" +
      "</div>";
    document.body.appendChild(div.firstChild);
  }

  /* ── trigger button ── */

  function _ensureTrigger() {
    var container = document.getElementById("scope-switcher-container");
    if (!container) {
      // auto-insert: place before #user-menu-container inside .ds-page-header-actions
      var actions = document.querySelector(".ds-page-header-actions");
      if (!actions) return;
      container = document.createElement("div");
      container.id = "scope-switcher-container";
      container.style.cssText =
        "display:inline-flex;align-items:center;margin-right:4px;";
      var userMenu = document.getElementById("user-menu-container");
      if (userMenu && userMenu.parentNode === actions) {
        actions.insertBefore(container, userMenu);
      } else {
        actions.insertBefore(container, actions.firstChild);
      }
    }
    if (container.querySelector("#scope-trigger-ss")) return; // already mounted
    var btn = document.createElement("button");
    btn.id = "scope-trigger-ss";
    btn.onclick = openModal;
    btn.setAttribute(
      "style",
      "display:inline-flex;align-items:center;gap:6px;padding:6px 12px;" +
        "background:#fff;border:1px solid #e2e8f0;border-radius:8px;font-size:13px;" +
        "cursor:pointer;color:#334155;white-space:nowrap;" +
        "transition:border-color .15s,background .15s",
    );
    btn.onmouseenter = function () {
      this.style.borderColor = "#6366f1";
      this.style.background = "#eef2ff";
    };
    btn.onmouseleave = function () {
      this.style.borderColor = "#e2e8f0";
      this.style.background = "#fff";
    };
    btn.innerHTML =
      '<svg width="14" height="14" fill="none" stroke="#6366f1" viewBox="0 0 24 24" style="flex-shrink:0">' +
      '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2a1 1 0 01-.293.707L13 13.414V19a1 1 0 01-.553.894l-4 2A1 1 0 017 21v-7.586L3.293 6.707A1 1 0 013 6V4z"/>' +
      "</svg>" +
      '<span id="scope-trigger-label"></span>' +
      '<svg width="11" height="11" fill="none" stroke="#94a3b8" viewBox="0 0 24 24">' +
      '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>' +
      "</svg>";
    container.appendChild(btn);
    _renderTriggerLabel();
  }

  /* ── open / close ── */

  function openModal() {
    _ensureModal();
    var scope = getScope();
    _modalProject = { key: scope.project_key, name: scope.project_key };
    _modalModules = scope.domain_modules.slice();

    var modal = document.getElementById("scope-modal");
    if (modal) modal.style.display = "flex";

    var searchEl = document.getElementById("ss-project-search");
    if (searchEl) {
      searchEl.value = "";
      searchEl.placeholder = "加载中...";
      searchEl.disabled = true;
    }
    var listEl = document.getElementById("ss-project-list");
    if (listEl)
      listEl.innerHTML =
        '<div style="padding:8px 12px;font-size:13px;color:#94a3b8">加载中...</div>';

    _fetchProjects(scope.project_key);
  }

  function closeModal() {
    var modal = document.getElementById("scope-modal");
    if (modal) modal.style.display = "none";
  }

  /* ── project list ── */

  async function _fetchProjects(currentKey) {
    try {
      var base = _apiBase();
      var headers =
        typeof getJiraHeaders === "function" ? getJiraHeaders() : {};
      var res = await fetch(base + "/api/board/meta", {
        credentials: "include",
        headers: headers,
      });
      var data = await res.json();
      var projs = (data.projects || []).sort(function (a, b) {
        return a.name.localeCompare(b.name);
      });
      _projectOptions = [{ value: "ALL", label: "全部项目" }].concat(
        projs.map(function (p) {
          return { value: p.key, label: p.name };
        }),
      );
    } catch (e) {
      _projectOptions = [{ value: "ALL", label: "全部项目" }];
    }

    var searchEl = document.getElementById("ss-project-search");
    if (searchEl) {
      searchEl.placeholder = "搜索项目名称或编码...";
      searchEl.disabled = false;
      searchEl.focus();
    }
    // show current project name in search box
    var cur = _projectOptions.find(function (o) {
      return o.value === currentKey;
    });
    if (cur && searchEl)
      searchEl.value =
        cur.value === "ALL" ? cur.label : cur.label + " (" + cur.value + ")";
    if (cur) _modalProject = { key: cur.value, name: cur.label };

    _renderProjectList("");
    if (currentKey === "ALL") {
      _allModules = [];
      var modListEl = document.getElementById("ss-modules-list");
      if (modListEl) {
        modListEl.innerHTML =
          '<div style="font-size:13px;color:#94a3b8;text-align:center;padding:8px">全部项目不限模块</div>';
      }
    } else {
      _fetchModules(currentKey);
    }
  }

  function _filterProjects(q) {
    _renderProjectList(q);
  }

  function _renderProjectList(q) {
    var listEl = document.getElementById("ss-project-list");
    if (!listEl) return;
    var lower = (q || "").toLowerCase().trim();
    var upper = (q || "").toUpperCase().trim();
    var opts = _projectOptions.filter(function (o) {
      if (!lower) return true;
      return (
        (o.label || "").toLowerCase().includes(lower) ||
        (o.value || "").toUpperCase().includes(upper)
      );
    });
    if (!opts.length) {
      listEl.innerHTML =
        '<div style="padding:8px 12px;font-size:13px;color:#94a3b8">无匹配项目</div>';
      return;
    }
    var html = "";
    opts.forEach(function (o, idx) {
      var isAll = o.value === "ALL";
      var isSel = _modalProject && o.value === _modalProject.key;
      var bg = isSel ? "#eef2ff" : isAll ? "#f8f7ff" : "#fff";
      var color = isSel ? "#6366f1" : isAll ? "#4f46e5" : "#334155";
      var weight = isSel || isAll ? "600" : "400";
      var safeVal = (o.value || "").replace(/'/g, "\\'");
      var safeLbl = (o.label || "").replace(/'/g, "\\'");
      var extra = isAll ? " border-bottom:1px solid #e2e8f0;" : "";
      html +=
        "<div onclick=\"ScopeSwitcher._selectProject('" +
        safeVal +
        "','" +
        safeLbl +
        "')\"" +
        ' style="padding:7px 12px;font-size:13px;cursor:pointer;display:flex;align-items:center;justify-content:space-between;background:' +
        bg +
        ";color:" +
        color +
        ";font-weight:" +
        weight +
        ";" +
        extra +
        '"' +
        " onmouseenter=\"this.style.background='" +
        (isAll ? "#ede9fe" : "#f8fafc") +
        "'\" onmouseleave=\"this.style.background='" +
        bg +
        "'\">" +
        "<span>" +
        _escHtml(o.label) +
        "</span>" +
        (isAll
          ? '<span style="font-size:11px;color:#a5b4fc;margin-left:8px">全部</span>'
          : '<span style="font-size:11px;color:#94a3b8;margin-left:8px">' +
            _escHtml(o.value) +
            "</span>") +
        "</div>";
    });
    listEl.innerHTML = html;
  }

  function _selectProject(key, label) {
    _modalProject = { key: key, name: label };
    _modalModules = [];
    var searchEl = document.getElementById("ss-project-search");
    if (searchEl)
      searchEl.value = key === "ALL" ? label : label + " (" + key + ")";
    var listEl = document.getElementById("ss-project-list");
    if (listEl) listEl.innerHTML = "";
    if (key === "ALL") {
      _allModules = [];
      var modListEl = document.getElementById("ss-modules-list");
      if (modListEl) {
        modListEl.innerHTML =
          '<div style="font-size:13px;color:#94a3b8;text-align:center;padding:8px">全部项目不限模块</div>';
      }
    } else {
      _fetchModules(key);
    }
  }

  /* ── modules ── */

  async function _fetchModules(projectKey) {
    var listEl = document.getElementById("ss-modules-list");
    if (!listEl) return;
    listEl.innerHTML =
      '<div style="font-size:13px;color:#94a3b8;text-align:center;padding:8px">加载中...</div>';
    try {
      var base = _apiBase();
      var headers =
        typeof getJiraHeaders === "function" ? getJiraHeaders() : {};
      var res = await fetch(
        base +
          "/api/board/move-field-options/" +
          encodeURIComponent(projectKey),
        { credentials: "include", headers: headers },
      );
      var data = await res.json();
      _allModules = (data.options || data.domain_module || []).map(
        function (m) {
          return typeof m === "string" ? m : m.value || m.label || String(m);
        },
      );
      _renderModules();
    } catch (e) {
      listEl.innerHTML =
        '<div style="font-size:13px;color:#94a3b8;text-align:center;padding:8px">加载失败</div>';
    }
  }

  function _renderModules() {
    var listEl = document.getElementById("ss-modules-list");
    if (!listEl) return;
    if (!_allModules.length) {
      listEl.innerHTML =
        '<div style="font-size:13px;color:#94a3b8;text-align:center;padding:8px">该项目无领域模块</div>';
      return;
    }
    listEl.innerHTML = _allModules
      .map(function (m) {
        var checked = _modalModules.indexOf(m) !== -1;
        var safe = m.replace(/"/g, "&quot;").replace(/'/g, "\\'");
        return (
          '<label style="display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:4px;cursor:pointer"' +
          " onmouseenter=\"this.style.background='#f8fafc'\" onmouseleave=\"this.style.background=''\">" +
          '<input type="checkbox" ' +
          (checked ? "checked" : "") +
          " onchange=\"ScopeSwitcher._toggleModule('" +
          safe +
          "',this.checked)\">" +
          '<span style="font-size:13px;color:#334155">' +
          _escHtml(m) +
          "</span>" +
          "</label>"
        );
      })
      .join("");
  }

  function _toggleModule(mod, checked) {
    if (checked && _modalModules.indexOf(mod) === -1) _modalModules.push(mod);
    else if (!checked)
      _modalModules = _modalModules.filter(function (m) {
        return m !== mod;
      });
  }

  function _selectAll() {
    _modalModules = _allModules.slice();
    _renderModules();
  }
  function _clearAll() {
    _modalModules = [];
    _renderModules();
  }

  /* ── apply ── */

  function _applyScope() {
    if (!_modalProject) {
      closeModal();
      return;
    }
    var projKey = _modalProject.key;
    var projLabel = _modalProject.name;

    // 1. write board_filter_v1 first (so listeners read new value)
    var store = _loadStore();
    store.project = { value: projKey, label: projLabel };
    store.domain_modules = _modalModules.slice();
    _saveStore(store);

    // 2. sync ProjectCtx (writes _currentProject + backend /api/user/settings + dispatches project-changed)
    if (typeof ProjectCtx !== "undefined") {
      ProjectCtx.set(projKey);
    } else {
      try {
        localStorage.setItem("_currentProject", projKey);
      } catch (e) {}
    }

    // 3. dispatch scope-changed
    var scope = { project_key: projKey, domain_modules: _modalModules.slice() };
    window.dispatchEvent(new CustomEvent("scope-changed", { detail: scope }));

    // 4. update trigger label
    _renderTriggerLabel();

    // 5. callback
    if (typeof _onChange === "function") _onChange(scope);

    closeModal();
  }

  /* ── init ── */

  function init(opts) {
    opts = opts || {};
    _onChange = opts.onChange || null;
    _ensureModal();
    _ensureTrigger();
    _renderTriggerLabel();
    // keep trigger label in sync if scope changes from another tab or page event
    window.addEventListener("scope-changed", function () {
      _renderTriggerLabel();
    });
  }

  return {
    init: init,
    getScope: getScope,
    openModal: openModal,
    closeModal: closeModal,
    _filterProjects: _filterProjects,
    _renderProjectList: _renderProjectList,
    _selectProject: _selectProject,
    _fetchModules: _fetchModules,
    _toggleModule: _toggleModule,
    _selectAll: _selectAll,
    _clearAll: _clearAll,
    _applyScope: _applyScope,
  };
})();
