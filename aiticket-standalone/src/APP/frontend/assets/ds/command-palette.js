/**
 * command-palette.js - 全局命令面板 (Cmd+K / Ctrl+K)
 *
 * 参考 cmdk 的交互模式：
 * - 模糊搜索导航页面 + 页面内快捷操作
 * - 分组："导航"、"操作"、"最近访问"
 * - 键盘方向键导航 + 回车确认
 */
(function () {
  "use strict";

  var RECENT_KEY = "ds-cmd-recent";
  var MAX_RECENT = 5;
  var MAX_RESULTS = 10;

  // --- 注册表 ---
  var navItems = [
    {
      id: "nav-board",
      label: "智能看板",
      href: "board.html",
      group: "导航",
      icon: "layout",
      aliases: ["看板", "kanban", "工单"],
    },
    {
      id: "nav-kb",
      label: "知识库",
      href: "kb.html",
      group: "导航",
      icon: "book-open",
      aliases: ["知识", "kb", "文档"],
    },
    {
      id: "nav-guide",
      label: "操作指引",
      href: "guide.html",
      group: "导航",
      icon: "help-circle",
      aliases: ["帮助", "指南", "指引"],
    },
  ];

  var actionItems = [
    {
      id: "act-theme-toggle",
      label: "切换主题（亮/暗/系统）",
      group: "操作",
      icon: "sun",
      action: function () {
        if (window.DSTheme) window.DSTheme.toggle();
      },
    },
    {
      id: "act-theme-light",
      label: "亮色模式",
      group: "操作",
      icon: "sun",
      action: function () {
        if (window.DSTheme) window.DSTheme.set("light");
      },
    },
    {
      id: "act-theme-dark",
      label: "暗色模式",
      group: "操作",
      icon: "moon",
      action: function () {
        if (window.DSTheme) window.DSTheme.set("dark");
      },
    },
  ];

  var allItems = navItems.concat(actionItems);

  // --- UI ---
  var paletteEl = null;
  var overlayEl = null;
  var inputEl = null;
  var listEl = null;
  var activeIndex = 0;
  var visibleItems = [];

  function createDOM() {
    // Overlay
    overlayEl = document.createElement("div");
    overlayEl.className = "ds-cmd-overlay";
    overlayEl.addEventListener("click", close);

    // Palette container
    paletteEl = document.createElement("div");
    paletteEl.className = "ds-cmd-palette";
    paletteEl.setAttribute("role", "dialog");
    paletteEl.setAttribute("aria-modal", "true");
    paletteEl.setAttribute("aria-label", "命令面板");

    // Search input
    var inputWrap = document.createElement("div");
    inputWrap.className = "ds-cmd-input-wrap";
    inputWrap.innerHTML =
      '<svg class="ds-cmd-search-icon" width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>';

    inputEl = document.createElement("input");
    inputEl.className = "ds-cmd-input";
    inputEl.type = "text";
    inputEl.placeholder = "搜索页面或操作...";
    inputEl.setAttribute("autocomplete", "off");
    inputEl.setAttribute("spellcheck", "false");
    inputEl.addEventListener("input", onInput);
    inputEl.addEventListener("keydown", onKeydown);
    inputWrap.appendChild(inputEl);

    var kbdHint = document.createElement("kbd");
    kbdHint.className = "ds-cmd-kbd";
    kbdHint.textContent = "ESC";
    inputWrap.appendChild(kbdHint);

    // Result list
    listEl = document.createElement("div");
    listEl.className = "ds-cmd-list";
    listEl.setAttribute("role", "listbox");

    paletteEl.appendChild(inputWrap);
    paletteEl.appendChild(listEl);
    overlayEl.appendChild(paletteEl);

    // 阻止 palette 内的点击冒泡到 overlay
    paletteEl.addEventListener("click", function (e) {
      e.stopPropagation();
    });

    document.body.appendChild(overlayEl);
  }

  // --- 搜索 ---
  function score(item, query) {
    var q = query.toLowerCase();
    var label = item.label.toLowerCase();

    // 精确匹配
    if (label === q) return 100;
    // 前缀匹配
    if (label.startsWith(q)) return 80;
    // 包含匹配
    if (label.indexOf(q) !== -1) return 60;
    // 别名匹配
    if (item.aliases) {
      for (var i = 0; i < item.aliases.length; i++) {
        var alias = item.aliases[i].toLowerCase();
        if (alias.indexOf(q) !== -1) return 40;
      }
    }
    // href 匹配
    if (item.href && item.href.toLowerCase().indexOf(q) !== -1) return 20;
    return 0;
  }

  function search(query) {
    if (!query) {
      // 无搜索词: 显示最近访问 + 全部导航
      var recent = getRecent();
      var recentItems = recent
        .map(function (id) {
          return allItems.find(function (it) {
            return it.id === id;
          });
        })
        .filter(Boolean)
        .map(function (it) {
          return Object.assign({}, it, { group: "最近访问" });
        });
      return recentItems.concat(navItems).slice(0, MAX_RESULTS);
    }

    var scored = allItems
      .map(function (item) {
        return { item: item, score: score(item, query) };
      })
      .filter(function (s) {
        return s.score > 0;
      })
      .sort(function (a, b) {
        return b.score - a.score;
      })
      .slice(0, MAX_RESULTS)
      .map(function (s) {
        return s.item;
      });

    return scored;
  }

  // --- 最近访问 ---
  function getRecent() {
    try {
      var raw = localStorage.getItem(RECENT_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (e) {
      return [];
    }
  }

  function addRecent(id) {
    var recent = getRecent().filter(function (r) {
      return r !== id;
    });
    recent.unshift(id);
    if (recent.length > MAX_RECENT) recent = recent.slice(0, MAX_RECENT);
    try {
      localStorage.setItem(RECENT_KEY, JSON.stringify(recent));
    } catch (e) {
      /* 忽略 */
    }
  }

  // --- 渲染 ---
  function renderList(items) {
    visibleItems = items;
    activeIndex = 0;

    if (!items.length) {
      listEl.innerHTML = '<div class="ds-cmd-empty">没有找到匹配的结果</div>';
      return;
    }

    var html = "";
    var lastGroup = "";

    items.forEach(function (item, idx) {
      if (item.group !== lastGroup) {
        html +=
          '<div class="ds-cmd-group">' + escapeHtml(item.group) + "</div>";
        lastGroup = item.group;
      }
      html +=
        '<div class="ds-cmd-item' +
        (idx === 0 ? " active" : "") +
        '" data-index="' +
        idx +
        '" role="option">' +
        '<span class="ds-cmd-item-label">' +
        escapeHtml(item.label) +
        "</span>" +
        (item.href
          ? '<span class="ds-cmd-item-hint">' +
            escapeHtml(item.href) +
            "</span>"
          : "") +
        "</div>";
    });

    listEl.innerHTML = html;

    // 绑定hover和click
    var itemEls = listEl.querySelectorAll(".ds-cmd-item");
    itemEls.forEach(function (el) {
      el.addEventListener("mouseenter", function () {
        setActive(parseInt(el.dataset.index, 10));
      });
      el.addEventListener("click", function () {
        execute(parseInt(el.dataset.index, 10));
      });
    });
  }

  function setActive(index) {
    if (index < 0 || index >= visibleItems.length) return;
    var items = listEl.querySelectorAll(".ds-cmd-item");
    items.forEach(function (el) {
      el.classList.remove("active");
    });
    activeIndex = index;
    if (items[activeIndex]) {
      items[activeIndex].classList.add("active");
      items[activeIndex].scrollIntoView({ block: "nearest" });
    }
  }

  function execute(index) {
    var item = visibleItems[index];
    if (!item) return;

    addRecent(item.id);

    if (item.action) {
      close();
      item.action();
    } else if (item.href) {
      close();
      window.location.href = item.href;
    }
  }

  // --- 事件处理 ---
  function onInput() {
    var query = inputEl.value.trim();
    var results = search(query);
    renderList(results);
  }

  function onKeydown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(Math.min(activeIndex + 1, visibleItems.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(Math.max(activeIndex - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      execute(activeIndex);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  }

  // --- 开关 ---
  function open() {
    if (!paletteEl) createDOM();
    overlayEl.classList.add("open");
    inputEl.value = "";
    renderList(search(""));
    requestAnimationFrame(function () {
      inputEl.focus();
    });
  }

  function close() {
    if (overlayEl) {
      overlayEl.classList.remove("open");
    }
  }

  function isOpen() {
    return overlayEl && overlayEl.classList.contains("open");
  }

  // --- 全局快捷键: Cmd+K / Ctrl+K ---
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === "k") {
      e.preventDefault();
      if (isOpen()) {
        close();
      } else {
        open();
      }
    }
  });

  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // 公共 API
  window.DSCommandPalette = {
    open: open,
    close: close,
    isOpen: isOpen,
    /** 注册额外的操作项 */
    register: function (items) {
      items.forEach(function (item) {
        item.group = item.group || "操作";
        actionItems.push(item);
      });
      allItems = navItems.concat(actionItems);
    },
  };
})();
