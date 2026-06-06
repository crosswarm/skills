/**
 * CDN 韧性模块
 * 用于处理 Tailwind CSS CDN 加载失败的情况
 *
 * 使用方式：
 * <script src="assets/cdn-resilience.js"></script>
 * 或在 <head> 中直接内联此代码
 */

(function() {
    'use strict';

    // 关键样式 - 用于 CDN 加载慢或失败时保持基本布局
    const CRITICAL_STYLES = `
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            margin: 0;
            line-height: 1.5;
        }

        /* 核心布局 */
        .hidden { display: none !important; }
        .block { display: block; }
        .flex { display: flex; }
        .inline-flex { display: inline-flex; }
        .grid { display: grid; }
        .flex-col { flex-direction: column; }
        .flex-wrap { flex-wrap: wrap; }
        .flex-1 { flex: 1 1 0%; }
        .items-center { align-items: center; }
        .items-start { align-items: flex-start; }
        .justify-center { justify-content: center; }
        .justify-between { justify-content: space-between; }

        /* 间距 */
        .gap-1 { gap: 0.25rem; }
        .gap-2 { gap: 0.5rem; }
        .gap-3 { gap: 0.75rem; }
        .gap-4 { gap: 1rem; }
        .gap-6 { gap: 1.5rem; }
        .p-2 { padding: 0.5rem; }
        .p-3 { padding: 0.75rem; }
        .p-4 { padding: 1rem; }
        .p-6 { padding: 1.5rem; }
        .px-2 { padding-left: 0.5rem; padding-right: 0.5rem; }
        .px-3 { padding-left: 0.75rem; padding-right: 0.75rem; }
        .px-4 { padding-left: 1rem; padding-right: 1rem; }
        .px-6 { padding-left: 1.5rem; padding-right: 1.5rem; }
        .py-1 { padding-top: 0.25rem; padding-bottom: 0.25rem; }
        .py-2 { padding-top: 0.5rem; padding-bottom: 0.5rem; }
        .py-3 { padding-top: 0.75rem; padding-bottom: 0.75rem; }
        .py-4 { padding-top: 1rem; padding-bottom: 1rem; }
        .pt-20 { padding-top: 5rem; }
        .pb-8 { padding-bottom: 2rem; }
        .mb-2 { margin-bottom: 0.5rem; }
        .mb-3 { margin-bottom: 0.75rem; }
        .mb-4 { margin-bottom: 1rem; }
        .mb-6 { margin-bottom: 1.5rem; }
        .mt-2 { margin-top: 0.5rem; }
        .mt-3 { margin-top: 0.75rem; }
        .mt-4 { margin-top: 1rem; }
        .ml-2 { margin-left: 0.5rem; }
        .mr-2 { margin-right: 0.5rem; }

        /* 尺寸 */
        .w-4 { width: 1rem; }
        .w-5 { width: 1.25rem; }
        .w-6 { width: 1.5rem; }
        .w-8 { width: 2rem; }
        .w-12 { width: 3rem; }
        .w-24 { width: 6rem; }
        .w-full { width: 100%; }
        .h-4 { height: 1rem; }
        .h-5 { height: 1.25rem; }
        .h-6 { height: 1.5rem; }
        .h-8 { height: 2rem; }
        .h-full { height: 100%; }
        .min-h-screen { min-height: 100vh; }

        /* 文字 */
        .text-xs { font-size: 0.75rem; }
        .text-sm { font-size: 0.875rem; }
        .text-base { font-size: 1rem; }
        .text-lg { font-size: 1.125rem; }
        .text-xl { font-size: 1.25rem; }
        .text-2xl { font-size: 1.5rem; }
        .font-medium { font-weight: 500; }
        .font-semibold { font-weight: 600; }
        .font-bold { font-weight: 700; }
        .text-center { text-align: center; }
        .text-white { color: #ffffff; }
        .text-slate-400 { color: #94a3b8; }
        .text-slate-500 { color: #64748b; }
        .text-slate-600 { color: #475569; }
        .text-slate-700 { color: #334155; }
        .text-slate-800 { color: #1e293b; }
        .text-indigo-600 { color: #4f46e5; }
        .text-amber-700 { color: #b45309; }

        /* 背景 */
        .bg-white { background-color: #ffffff; }
        .bg-slate-50 { background-color: #f8fafc; }
        .bg-slate-100 { background-color: #f1f5f9; }
        .bg-slate-200 { background-color: #e2e8f0; }
        .bg-indigo-50 { background-color: #eef2ff; }
        .bg-indigo-600 { background-color: #4f46e5; }
        .bg-amber-50 { background-color: #fffbeb; }

        /* 边框 */
        .border { border-width: 1px; }
        .border-2 { border-width: 2px; }
        .border-slate-200 { border-color: #e2e8f0; }
        .border-amber-200 { border-color: #fde68a; }
        .rounded { border-radius: 0.25rem; }
        .rounded-lg { border-radius: 0.5rem; }
        .rounded-xl { border-radius: 0.75rem; }
        .rounded-full { border-radius: 9999px; }

        /* 阴影 */
        .shadow { box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1); }
        .shadow-lg { box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1); }
        .shadow-xl { box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1); }

        /* 定位 */
        .fixed { position: fixed; }
        .absolute { position: absolute; }
        .relative { position: relative; }
        .sticky { position: sticky; }
        .inset-0 { top: 0; right: 0; bottom: 0; left: 0; }
        .top-4 { top: 1rem; }
        .right-4 { right: 1rem; }
        .z-50 { z-index: 50; }

        /* 其他 */
        .overflow-hidden { overflow: hidden; }
        .overflow-auto { overflow: auto; }
        .cursor-pointer { cursor: pointer; }
        .transition { transition-property: all; transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1); transition-duration: 150ms; }
        .transform { transform: var(--tw-transform); }

        /* 玻璃效果 */
        .glass {
            background: rgba(255, 255, 255, 0.85);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.18);
        }

        /* 表单元素 */
        select, input[type="text"], input[type="date"] {
            font-size: 0.875rem;
            padding: 0.375rem 0.75rem;
            border: 1px solid #e2e8f0;
            border-radius: 0.5rem;
            background-color: white;
        }
        select:focus, input:focus {
            outline: none;
            box-shadow: 0 0 0 2px #4f46e5;
        }
        button {
            cursor: pointer;
            transition: all 0.2s;
        }
    `;

    // 立即注入关键样式
    function injectCriticalStyles() {
        const style = document.createElement('style');
        style.id = 'cdn-resilience-critical';
        style.textContent = CRITICAL_STYLES;
        document.head.appendChild(style);
    }

    // 检测 Tailwind 是否生效
    function checkTailwind() {
        const testEl = document.createElement('div');
        testEl.className = 'hidden';
        testEl.style.cssText = 'position:absolute;visibility:hidden;';
        document.body.appendChild(testEl);
        const isHidden = window.getComputedStyle(testEl).display === 'none';
        document.body.removeChild(testEl);

        if (!isHidden) {
            console.warn('[CDN-Resilience] Tailwind CSS styles not applied, fallback activated');
            document.documentElement.classList.add('tailwind-fallback');
            return false;
        }
        return true;
    }

    // 初始化
    function init() {
        injectCriticalStyles();

        // 多次检测确保 Tailwind 有足够时间初始化
        if (document.readyState === 'complete') {
            setTimeout(checkTailwind, 100);
        } else {
            window.addEventListener('load', function() {
                setTimeout(checkTailwind, 100);
                setTimeout(checkTailwind, 500);
                setTimeout(checkTailwind, 1000);
            });
        }
    }

    // 立即执行
    init();

    // 导出（如果需要）
    if (typeof window !== 'undefined') {
        window.CDNResilience = { checkTailwind, CRITICAL_STYLES };
    }
})();