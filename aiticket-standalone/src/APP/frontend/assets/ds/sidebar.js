/**
 * sidebar.js - 侧边栏折叠/展开逻辑
 *
 * 配合 nav.js 使用，管理侧边栏状态：
 * - 展开模式 (240px) / 折叠模式 (56px)
 * - localStorage 持久化
 * - 移动端抽屉模式
 */
(function () {
  "use strict";

  var STORAGE_KEY = "ds-sidebar-collapsed";
  var MOBILE_BREAKPOINT = 768;

  var sidebar = null;
  var overlay = null;
  var mainContent = null;

  function isCollapsed() {
    try {
      return localStorage.getItem(STORAGE_KEY) === "true";
    } catch (e) {
      return false;
    }
  }

  function saveState(collapsed) {
    try {
      localStorage.setItem(STORAGE_KEY, String(collapsed));
    } catch (e) {
      /* 忽略 */
    }
  }

  function isMobile() {
    return window.innerWidth < MOBILE_BREAKPOINT;
  }

  function applySidebarState(collapsed) {
    if (!sidebar) return;

    if (collapsed) {
      sidebar.classList.add("ds-sidebar-collapsed");
      sidebar.classList.remove("ds-sidebar-expanded");
    } else {
      sidebar.classList.remove("ds-sidebar-collapsed");
      sidebar.classList.add("ds-sidebar-expanded");
    }

    // 更新 main content margin（仅 fixed 定位的 sidebar 才需要 margin 补偿）
    if (mainContent && !isMobile()) {
      var sidebarPos = getComputedStyle(sidebar).position;
      if (sidebarPos === "fixed") {
        mainContent.style.marginLeft = collapsed
          ? "var(--ds-sidebar-w-collapsed)"
          : "var(--ds-sidebar-w)";
      } else {
        mainContent.style.marginLeft = "0";
      }
    }
  }

  function toggleSidebar() {
    if (isMobile()) {
      toggleMobileSidebar();
      return;
    }

    var collapsed = sidebar.classList.contains("ds-sidebar-collapsed");
    var newState = !collapsed;
    saveState(newState);
    applySidebarState(newState);

    window.dispatchEvent(
      new CustomEvent("sidebarchange", {
        detail: { collapsed: newState },
      }),
    );
  }

  function openMobileSidebar() {
    if (!sidebar || !overlay) return;
    sidebar.classList.add("ds-sidebar-mobile-open");
    overlay.classList.add("open");
    document.body.style.overflow = "hidden";
  }

  function closeMobileSidebar() {
    if (!sidebar || !overlay) return;
    sidebar.classList.remove("ds-sidebar-mobile-open");
    overlay.classList.remove("open");
    document.body.style.overflow = "";
  }

  function toggleMobileSidebar() {
    if (sidebar.classList.contains("ds-sidebar-mobile-open")) {
      closeMobileSidebar();
    } else {
      openMobileSidebar();
    }
  }

  function handleResize() {
    if (!sidebar) return;

    if (isMobile()) {
      // 移动端: 总是隐藏侧边栏（需要手动打开）
      sidebar.classList.remove("ds-sidebar-collapsed", "ds-sidebar-expanded");
      sidebar.classList.remove("ds-sidebar-mobile-open");
      if (overlay) overlay.classList.remove("open");
      if (mainContent) mainContent.style.marginLeft = "0";
      document.body.style.overflow = "";
    } else {
      // 桌面端: 恢复折叠/展开状态
      sidebar.classList.remove("ds-sidebar-mobile-open");
      if (overlay) overlay.classList.remove("open");
      document.body.style.overflow = "";
      applySidebarState(isCollapsed());
    }
  }

  function init() {
    sidebar = document.getElementById("ds-sidebar");
    overlay = document.getElementById("ds-sidebar-overlay");
    mainContent = document.getElementById("ds-main");

    if (!sidebar) return;

    // 初始状态
    if (!isMobile()) {
      applySidebarState(isCollapsed());
    }

    // overlay 点击关闭
    if (overlay) {
      overlay.addEventListener("click", closeMobileSidebar);
    }

    // ESC 关闭移动端侧边栏
    document.addEventListener("keydown", function (e) {
      if (
        e.key === "Escape" &&
        sidebar.classList.contains("ds-sidebar-mobile-open")
      ) {
        closeMobileSidebar();
      }
    });

    // 响应式
    var resizeTimer;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(handleResize, 100);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // 公共 API
  window.DSSidebar = {
    toggle: toggleSidebar,
    open: openMobileSidebar,
    close: closeMobileSidebar,
    isCollapsed: function () {
      return sidebar
        ? sidebar.classList.contains("ds-sidebar-collapsed")
        : false;
    },
  };
})();
