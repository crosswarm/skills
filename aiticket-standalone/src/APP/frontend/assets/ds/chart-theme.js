/**
 * chart-theme.js - Chart.js 主题集成
 *
 * 从 CSS 变量读取颜色，监听 themechange 事件自动更新图表
 * 用法：
 *   const colors = DSChart.getColors();
 *   const chart = new Chart(ctx, { ... });
 *   DSChart.register(chart);  // 注册后自动跟随主题切换
 */
(function () {
  "use strict";

  var registeredCharts = [];

  function getCSSVar(name) {
    return getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
  }

  /** 获取当前主题的图表配色 */
  function getColors() {
    return {
      // 主色系列（用于多条线/柱）
      primary: getCSSVar("--ds-accent") || "#4f46e5",
      secondary: getCSSVar("--ds-mod-board") || "#0891b2",
      tertiary: getCSSVar("--ds-mod-report") || "#7c3aed",
      quaternary: getCSSVar("--ds-mod-reqpool") || "#059669",
      quinary: getCSSVar("--ds-mod-reqplan") || "#d97706",

      // 语义色
      success: getCSSVar("--ds-success") || "#16a34a",
      warning: getCSSVar("--ds-warning") || "#ca8a04",
      danger: getCSSVar("--ds-danger") || "#dc2626",
      info: getCSSVar("--ds-info") || "#2563eb",

      // 文字 & 网格
      text: getCSSVar("--ds-text-primary") || "#0f172a",
      textSecondary: getCSSVar("--ds-text-secondary") || "#475569",
      textMuted: getCSSVar("--ds-text-muted") || "#94a3b8",
      gridLine: getCSSVar("--ds-border-subtle") || "#f1f5f9",
      border: getCSSVar("--ds-border") || "#e2e8f0",

      // 背景
      surface: getCSSVar("--ds-bg-surface") || "#ffffff",
      page: getCSSVar("--ds-bg-page") || "#f8fafc",
    };
  }

  /** 获取一组渐变色系（用于饼图/柱图） */
  function getPalette(count) {
    var colors = getColors();
    var palette = [
      colors.primary,
      colors.secondary,
      colors.tertiary,
      colors.quaternary,
      colors.quinary,
      colors.success,
      colors.info,
      colors.warning,
      colors.danger,
    ];
    var result = [];
    for (var i = 0; i < count; i++) {
      result.push(palette[i % palette.length]);
    }
    return result;
  }

  /** 生成半透明色（用于区域填充） */
  function withAlpha(hex, alpha) {
    if (hex.startsWith("rgba")) return hex;
    var r = parseInt(hex.slice(1, 3), 16);
    var g = parseInt(hex.slice(3, 5), 16);
    var b = parseInt(hex.slice(5, 7), 16);
    return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
  }

  /** 获取 Chart.js 全局默认配置 */
  function getDefaults() {
    var colors = getColors();
    return {
      color: colors.textSecondary,
      borderColor: colors.border,
      plugins: {
        legend: {
          labels: {
            color: colors.textSecondary,
            font: { size: 12 },
            usePointStyle: true,
            pointStyleWidth: 8,
          },
        },
        tooltip: {
          backgroundColor: colors.text,
          titleColor: "#ffffff",
          bodyColor: "#ffffff",
          borderColor: colors.border,
          borderWidth: 0,
          cornerRadius: 8,
          padding: 10,
          titleFont: { size: 13, weight: "600" },
          bodyFont: { size: 12 },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: colors.textMuted, font: { size: 11 } },
          border: { color: colors.border },
        },
        y: {
          grid: { color: colors.gridLine },
          ticks: { color: colors.textMuted, font: { size: 11 } },
          border: { display: false },
        },
      },
    };
  }

  /** 应用全局默认到 Chart.js */
  function applyGlobalDefaults() {
    if (typeof Chart === "undefined") return;

    var defaults = getDefaults();
    Chart.defaults.color = defaults.color;
    Chart.defaults.borderColor = defaults.borderColor;

    if (Chart.defaults.plugins) {
      Object.assign(Chart.defaults.plugins.legend || {}, defaults.plugins.legend);
      Object.assign(Chart.defaults.plugins.tooltip || {}, defaults.plugins.tooltip);
    }
  }

  /** 注册图表实例（主题切换时自动更新） */
  function register(chartInstance) {
    if (registeredCharts.indexOf(chartInstance) === -1) {
      registeredCharts.push(chartInstance);
    }
  }

  /** 注销图表实例 */
  function unregister(chartInstance) {
    var idx = registeredCharts.indexOf(chartInstance);
    if (idx !== -1) registeredCharts.splice(idx, 1);
  }

  /** 主题变化时更新所有已注册的图表 */
  function updateAllCharts() {
    applyGlobalDefaults();

    registeredCharts = registeredCharts.filter(function (chart) {
      if (!chart.canvas || !chart.canvas.parentNode) return false;

      try {
        var defaults = getDefaults();

        // 更新刻度颜色
        if (chart.options.scales) {
          Object.keys(chart.options.scales).forEach(function (scaleKey) {
            var scale = chart.options.scales[scaleKey];
            if (scale.ticks) scale.ticks.color = defaults.scales[scaleKey === "x" ? "x" : "y"].ticks.color;
            if (scale.grid) scale.grid.color = defaults.scales[scaleKey === "x" ? "x" : "y"].grid.color;
          });
        }

        // 更新插件颜色
        if (chart.options.plugins && chart.options.plugins.legend) {
          chart.options.plugins.legend.labels =
            chart.options.plugins.legend.labels || {};
          chart.options.plugins.legend.labels.color = defaults.plugins.legend.labels.color;
        }

        chart.update("none");
      } catch (e) {
        console.warn("[DSChart] 更新图表失败:", e);
        return false;
      }
      return true;
    });
  }

  // 监听主题变化
  window.addEventListener("themechange", function () {
    // 延迟一帧等CSS变量生效
    requestAnimationFrame(updateAllCharts);
  });

  // 公共 API
  window.DSChart = {
    getColors: getColors,
    getPalette: getPalette,
    withAlpha: withAlpha,
    getDefaults: getDefaults,
    applyGlobalDefaults: applyGlobalDefaults,
    register: register,
    unregister: unregister,
  };
})();
