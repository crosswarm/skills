/**
 * project-context.js — 全站当前项目单例 + 实例配置加载
 *
 * 优先级：URL ?project= > localStorage > 用户账号设置 > 实例 primary_project_key
 * 切换时触发 CustomEvent("project-changed", { detail: newKey })
 * window.INSTANCE_CONFIG 在 DOMContentLoaded 后可用，含 name / primary_project_key /
 *   allowed_project_keys / module_taxonomy
 */
var ProjectCtx = (function () {
  "use strict";

  var STORAGE_KEY = "_currentProject";

  // window.INSTANCE_CONFIG is populated by _loadInstanceConfig below
  window.INSTANCE_CONFIG = window.INSTANCE_CONFIG || {
    name: "",
    slug: "",
    primary_project_key: "",
    allowed_project_keys: [],
    module_taxonomy: [],
  };

  function _fromUrl() {
    try {
      return new URLSearchParams(window.location.search).get("project") || null;
    } catch (e) {
      return null;
    }
  }

  function _fromStorage() {
    try {
      return localStorage.getItem(STORAGE_KEY) || null;
    } catch (e) {
      return null;
    }
  }

  var _current = _fromUrl() || _fromStorage() || "";

  if (_fromUrl()) {
    try {
      localStorage.setItem(STORAGE_KEY, _fromUrl());
    } catch (e) {}
  }

  function get() {
    return _current;
  }

  function set(key) {
    if (!key || key === _current) return;
    _current = key.toUpperCase();
    try {
      localStorage.setItem(STORAGE_KEY, _current);
    } catch (e) {}

    try {
      var base = typeof API_BASE !== "undefined" ? API_BASE : "";
      fetch(base + "/api/user/settings", {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_project: _current }),
      }).catch(function () {});
    } catch (e) {}

    window.dispatchEvent(
      new CustomEvent("project-changed", { detail: _current }),
    );
  }

  // Load instance config (name, primary_project_key, allowed_project_keys)
  function _loadInstanceConfig() {
    try {
      var base = typeof API_BASE !== "undefined" ? API_BASE : "";
      fetch(base + "/api/instance/config")
        .then(function (r) {
          return r.ok ? r.json() : null;
        })
        .then(function (d) {
          if (!d) return;
          window.INSTANCE_CONFIG = Object.assign(window.INSTANCE_CONFIG, d);

          // Set default project from instance config if not already set
          if (!_current && d.primary_project_key) {
            _current = d.primary_project_key;
            try {
              localStorage.setItem(STORAGE_KEY, _current);
            } catch (e) {}
          }

          // Update page title with instance name
          if (d.name) {
            var titleEl = document.querySelector("title");
            if (titleEl && !titleEl.dataset.overridden) {
              titleEl.textContent = d.name + " · " + titleEl.textContent;
              titleEl.dataset.overridden = "1";
            }
            // Update nav instance name badge if present
            var badge = document.getElementById("instance-name-badge");
            if (badge) badge.textContent = d.name;
          }

          window.dispatchEvent(
            new CustomEvent("instance-config-loaded", { detail: d }),
          );
        })
        .catch(function () {});
    } catch (e) {}
  }

  // Sync project from server (user's saved current_project wins over stale localStorage)
  function _syncFromServer() {
    try {
      var base = typeof API_BASE !== "undefined" ? API_BASE : "";
      fetch(base + "/api/auth/me", { credentials: "include" })
        .then(function (r) {
          return r.ok ? r.json() : null;
        })
        .then(function (d) {
          if (!d) return;
          var serverProject = (d.user || {}).current_project;
          if (!_fromUrl() && serverProject && serverProject !== _current) {
            _current = serverProject;
            try {
              localStorage.setItem(STORAGE_KEY, _current);
            } catch (e) {}
          }
        })
        .catch(function () {});
    } catch (e) {}
  }

  function _init() {
    _loadInstanceConfig();
    _syncFromServer();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _init);
  } else {
    _init();
  }

  function getScope() {
    try {
      var bfv1 = JSON.parse(localStorage.getItem("board_filter_v1") || "{}");
      return {
        project_key: (bfv1.project && bfv1.project.value) || _current,
        domain_modules: Array.isArray(bfv1.domain_modules)
          ? bfv1.domain_modules
          : [],
      };
    } catch (e) {
      return { project_key: _current, domain_modules: [] };
    }
  }

  return { get: get, set: set, getScope: getScope };
})();
