/**
 * theme.js - 暗色模式引擎
 *
 * 三态切换: light / dark / system
 * 使用 [data-theme] 属性驱动 CSS 令牌切换
 * 通过 themechange 自定义事件通知 Chart.js 等组件
 */
(function () {
  "use strict";

  var STORAGE_KEY = "ds-theme";
  var ATTR = "data-theme";

  /** 获取系统偏好 */
  function getSystemPreference() {
    if (
      window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
    ) {
      return "dark";
    }
    return "light";
  }

  /** 获取当前有效主题 (light | dark) */
  function getEffectiveTheme(preference) {
    if (preference === "system") {
      return getSystemPreference();
    }
    return preference;
  }

  /** 应用主题到 DOM */
  function applyTheme(effective) {
    document.documentElement.setAttribute(ATTR, effective);
    // 同步 Tailwind dark class（部分页面可能依赖）
    if (effective === "dark") {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
  }

  /** 派发主题变更事件 */
  function dispatchChange(effective, preference) {
    var event = new CustomEvent("themechange", {
      detail: { theme: effective, preference: preference },
    });
    window.dispatchEvent(event);
  }

  /** 读取已保存的偏好 */
  function loadPreference() {
    try {
      var saved = localStorage.getItem(STORAGE_KEY);
      if (saved === "light" || saved === "dark" || saved === "system") {
        return saved;
      }
    } catch (e) {
      /* localStorage不可用 */
    }
    return "system";
  }

  /** 保存偏好 */
  function savePreference(preference) {
    try {
      localStorage.setItem(STORAGE_KEY, preference);
    } catch (e) {
      /* 忽略 */
    }
  }

  // --- 初始化 ---
  var currentPreference = loadPreference();
  var currentEffective = getEffectiveTheme(currentPreference);

  // 尽早应用，避免闪烁
  applyTheme(currentEffective);

  // 监听系统主题变化
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var onSystemChange = function () {
      if (currentPreference === "system") {
        var newEffective = getSystemPreference();
        if (newEffective !== currentEffective) {
          currentEffective = newEffective;
          applyTheme(currentEffective);
          dispatchChange(currentEffective, currentPreference);
        }
      }
    };
    if (mq.addEventListener) {
      mq.addEventListener("change", onSystemChange);
    } else if (mq.addListener) {
      mq.addListener(onSystemChange);
    }
  }

  // --- 公共 API ---
  window.DSTheme = {
    /** 获取当前有效主题 */
    current: function () {
      return currentEffective;
    },

    /** 获取当前偏好 */
    preference: function () {
      return currentPreference;
    },

    /** 设置主题: 'light' | 'dark' | 'system' */
    set: function (preference) {
      if (
        preference !== "light" &&
        preference !== "dark" &&
        preference !== "system"
      ) {
        return;
      }
      currentPreference = preference;
      savePreference(preference);
      var newEffective = getEffectiveTheme(preference);
      if (newEffective !== currentEffective) {
        currentEffective = newEffective;
        applyTheme(currentEffective);
      }
      dispatchChange(currentEffective, currentPreference);
    },

    /** 在 light → dark → system 之间循环切换 */
    toggle: function () {
      var order = ["light", "dark", "system"];
      var idx = order.indexOf(currentPreference);
      var next = order[(idx + 1) % order.length];
      this.set(next);
      return next;
    },

    /** 获取用于读取 CSS 变量的辅助方法（供 Chart.js 使用） */
    getCSSVar: function (name) {
      return getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim();
    },
  };
})();
