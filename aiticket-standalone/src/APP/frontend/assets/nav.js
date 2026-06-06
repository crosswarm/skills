/**
 * nav.js - 全站共享导航渲染器 v2 (侧边栏优先)
 *
 * 支持两种布局：
 * 1. 侧边栏布局 (默认): 页面包含 #ds-sidebar + #ds-main
 * 2. 全宽布局 (index.html): 无侧边栏，仅注入命令面板快捷键提示
 *
 * 向后兼容：仍支持旧版 desktop-nav / report-sidebar / mobile-nav-overlay
 */
(function () {
  "use strict";

  /* --- 导航配置 --- */
  var NAV_ITEMS = [
    {
      href: "index.html",
      label: "问题分析",
      icon: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path>',
      section: "main",
    },
    {
      href: "board.html",
      label: "智能看板",
      icon: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2"></path>',
      section: "main",
    },
    {
      href: "kb.html",
      label: "知识库",
      icon: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5A4.5 4.5 0 003 9.5v8A2.5 2.5 0 005.5 20H12m0-13C13.168 5.477 14.754 5 16.5 5A4.5 4.5 0 0121 9.5v8a2.5 2.5 0 01-2.5 2.5H12"></path>',
      section: "main",
    },
    {
      href: "guide.html",
      label: "操作指引",
      icon: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>',
      section: "extra",
      hidden: true,
    },
  ];

  /* --- 工具函数 --- */
  function normalizeCurrentPage() {
    var pathname = window.location.pathname || "/";
    var currentPage = pathname.split("/").pop();
    if (!currentPage) return "";
    return currentPage;
  }

  function navHref(href) {
    var base = typeof API_BASE !== "undefined" ? API_BASE : "";
    return base + "/" + href;
  }

  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderIcon(pathD, size) {
    size = size || 18;
    return (
      '<svg width="' +
      size +
      '" height="' +
      size +
      '" fill="none" stroke="currentColor" viewBox="0 0 24 24" style="flex-shrink:0">' +
      pathD +
      "</svg>"
    );
  }

  /* --- 侧边栏渲染 --- */
  function renderSidebar() {
    var sidebar = document.getElementById("ds-sidebar");
    if (!sidebar) return;

    var currentPage = normalizeCurrentPage();
    var mainSection = NAV_ITEMS.filter(function (i) {
      return i.section === "main" && !i.hidden;
    });
    var extraSection = NAV_ITEMS.filter(function (i) {
      return i.section === "extra" && !i.hidden;
    });

    // Header
    var html =
      '<div class="ds-sidebar-header">' +
      '  <div class="ds-sidebar-logo" onclick="DSSidebar.toggle()" style="cursor:pointer" title="展开/折叠">AI</div>' +
      '  <span class="ds-sidebar-title">工单智能平台</span>' +
      '  <button class="ds-btn-ghost ds-btn-icon ds-sidebar-collapse-btn" ' +
      '    onclick="DSSidebar.toggle()" title="折叠" style="margin-left:auto;min-height:32px;min-width:32px;padding:4px">' +
      '    <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24">' +
      '      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 19l-7-7 7-7m8 14l-7-7 7-7"></path>' +
      "    </svg>" +
      "  </button>" +
      "</div>";

    // Main nav
    html += '<nav class="ds-sidebar-nav">';
    html += renderNavSection(mainSection, currentPage);

    // Extra section
    if (extraSection.length) {
      html +=
        '<div class="ds-sidebar-section-label" style="margin-top:0.5rem">其他</div>';
      html += renderNavSection(extraSection, currentPage);
    }
    html += "</nav>";

    // Footer
    html += '<div class="ds-sidebar-footer">';

    // Command palette hint
    html +=
      '<button class="ds-sidebar-link ds-sidebar-label" onclick="DSCommandPalette && DSCommandPalette.open()" style="width:100%">' +
      renderIcon(
        '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path>',
        16,
      ) +
      '<span class="ds-sidebar-label" style="flex:1;text-align:left">快速搜索</span>' +
      '<kbd style="padding:1px 5px;font-size:11px;background:var(--ds-bg-muted);border:1px solid var(--ds-border);border-radius:3px;color:var(--ds-text-muted);font-family:var(--ds-font-mono)">\u2318K</kbd>' +
      "</button>";

    // AI config
    html +=
      '<button class="ds-sidebar-link" onclick="DSLLMConfig && DSLLMConfig.open()" style="width:100%">' +
      renderIcon(
        '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path>',
        16,
      ) +
      '<span class="ds-sidebar-label">AI 配置</span>' +
      "</button>";

    // Theme toggle
    html +=
      '<button class="ds-sidebar-link" onclick="DSTheme && DSTheme.toggle()" style="width:100%">' +
      renderIcon(
        '<circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>',
        16,
      ) +
      '<span class="ds-sidebar-label">切换主题</span>' +
      "</button>";

    // ICP (always visible, even when collapsed)
    html +=
      '<div style="padding:0.5rem 0.75rem;font-size:11px;color:var(--ds-text-muted);text-align:center;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;">' +
      '<a href="https://beian.miit.gov.cn/" target="_blank" ' +
      'style="color:inherit;text-decoration:none">京ICP备2021003033号-2</a>' +
      "</div>";

    html += "</div>"; // footer end

    sidebar.innerHTML = html;
  }

  function renderNavSection(items, currentPage) {
    return items
      .map(function (item) {
        var isActive = currentPage === item.href;
        return (
          '<a href="' +
          navHref(item.href) +
          '" class="ds-sidebar-link' +
          (isActive ? " active" : "") +
          '" ' +
          (isActive ? 'aria-current="page"' : "") +
          ">" +
          renderIcon(item.icon) +
          '<span class="ds-sidebar-label">' +
          escapeHtml(item.label) +
          "</span></a>"
        );
      })
      .join("\n");
  }

  /* --- 旧版兼容渲染器 --- */
  function renderDesktopLink(item, currentPage) {
    var isActive = currentPage === item.href;
    var baseClass =
      "nav-link px-3 py-1.5 text-sm rounded-lg transition flex items-center gap-1.5 whitespace-nowrap flex-shrink-0";
    var stateClass = isActive
      ? "bg-indigo-100 text-indigo-700 font-medium"
      : "text-slate-500 hover:text-indigo-600 hover:bg-indigo-50";

    return (
      '<a href="' +
      navHref(item.href) +
      '" class="' +
      baseClass +
      " " +
      stateClass +
      '">' +
      renderIcon(item.icon, 16) +
      escapeHtml(item.label) +
      "</a>"
    );
  }

  function renderOldSidebarLink(item, currentPage) {
    var isActive = currentPage === item.href;
    var baseClass =
      "text-sm px-3 py-2.5 rounded transition flex items-center gap-2";
    var stateClass = isActive
      ? "bg-indigo-600 text-white font-medium"
      : "bg-slate-800 text-slate-400 hover:text-white hover:bg-slate-700";

    return (
      '<a href="' +
      navHref(item.href) +
      '" class="' +
      baseClass +
      " " +
      stateClass +
      '">' +
      renderIcon(item.icon, 16) +
      escapeHtml(item.label) +
      "</a>"
    );
  }

  function renderMobileLink(item, currentPage) {
    var isActive = currentPage === item.href;
    return (
      '<a href="' +
      navHref(item.href) +
      '" class="nav-item' +
      (isActive ? " active" : "") +
      '">' +
      renderIcon(item.icon, 20) +
      "<span>" +
      escapeHtml(item.label) +
      "</span></a>"
    );
  }

  /* --- 初始化 --- */
  function initSharedNav() {
    var currentPage = normalizeCurrentPage();

    // 新版侧边栏
    if (document.getElementById("ds-sidebar")) {
      renderSidebar();
    }

    // 旧版桌面导航 (兼容)
    document.querySelectorAll("nav.desktop-nav").forEach(function (nav) {
      nav.innerHTML = NAV_ITEMS.filter(function (i) {
        return i.section === "main";
      })
        .map(function (item) {
          return renderDesktopLink(item, currentPage);
        })
        .join("\n");
    });

    // 旧版报告侧栏 (兼容)
    document
      .querySelectorAll('[data-nav-variant="report-sidebar"]')
      .forEach(function (nav) {
        nav.innerHTML = NAV_ITEMS.filter(function (i) {
          return i.section === "main";
        })
          .map(function (item) {
            return renderOldSidebarLink(item, currentPage);
          })
          .join("\n");
      });

    // 旧版移动导航 (兼容)
    var overlay = document.getElementById("mobile-nav-overlay");
    if (overlay) {
      var linksHtml = NAV_ITEMS.filter(function (i) {
        return i.section === "main";
      })
        .map(function (item) {
          return renderMobileLink(item, currentPage);
        })
        .join("\n");

      overlay.innerHTML =
        '<div class="mobile-nav-panel">' +
        '  <div class="mobile-nav-header">' +
        '    <div class="nav-logo">AI</div>' +
        '    <div class="nav-title">工单智能平台</div>' +
        "  </div>" +
        '  <button class="mobile-nav-close" id="mobile-nav-close" aria-label="关闭导航">' +
        '    <svg width="20" height="20" fill="none" stroke="currentColor" viewBox="0 0 24 24">' +
        '      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>' +
        "    </svg>" +
        "  </button>" +
        "  <nav>" +
        linksHtml +
        "</nav></div>";
    }

    // 绑定旧版移动端事件
    bindMobileEvents();
  }

  function bindMobileEvents() {
    var hamburgerBtn = document.getElementById("hamburger-btn");
    var overlay = document.getElementById("mobile-nav-overlay");

    if (!hamburgerBtn || !overlay) return;

    hamburgerBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      // 如果有新版侧边栏，用新版
      if (window.DSSidebar) {
        DSSidebar.open();
        return;
      }
      overlay.classList.add("open");
      document.body.style.overflow = "hidden";
    });

    overlay.addEventListener("click", function (e) {
      if (
        e.target === overlay ||
        e.target.closest("#mobile-nav-close") ||
        e.target.closest(".nav-item")
      ) {
        overlay.classList.remove("open");
        document.body.style.overflow = "";
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && overlay.classList.contains("open")) {
        overlay.classList.remove("open");
        document.body.style.overflow = "";
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initSharedNav);
  } else {
    initSharedNav();
  }
})();

/* --- 项目切换器 (已迁移至 scope-switcher.js，保留空对象兼容旧调用) --- */
var NavProject = {
  toggle: function () {},
  open: function () {},
  close: function () {},
  select: function () {},
};
