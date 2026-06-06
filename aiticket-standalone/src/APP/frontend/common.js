/**
 * 兼容 shim — 重定向到权威版本 /assets/common.js
 * 所有页面应统一引用 /assets/common.js
 * 此文件保留仅为向后兼容，内容与 /assets/common.js 同步
 */

// 动态加载权威版本
(function () {
    var s = document.createElement('script');
    s.src = '/assets/common.js';
    document.head.appendChild(s);
})();
