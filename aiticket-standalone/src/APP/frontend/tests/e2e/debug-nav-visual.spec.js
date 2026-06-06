import { test } from '@playwright/test';

test('debug desktop nav visual state', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/search.html');

  const navState = await page.evaluate(() => {
    const nav = document.querySelector('nav.desktop-nav');
    const hamburger = document.getElementById('hamburger-btn');
    const navStyle = nav ? window.getComputedStyle(nav) : null;
    const burgerStyle = hamburger ? window.getComputedStyle(hamburger) : null;

    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      navExists: !!nav,
      navChildCount: nav?.querySelectorAll('a').length || 0,
      navText: nav ? nav.textContent.replace(/\s+/g, ' ').trim() : null,
      navRect: nav?.getBoundingClientRect().toJSON() || null,
      navDisplay: navStyle?.display || null,
      navVisibility: navStyle?.visibility || null,
      navOpacity: navStyle?.opacity || null,
      burgerDisplay: burgerStyle?.display || null,
      burgerRect: hamburger?.getBoundingClientRect().toJSON() || null,
    };
  });

  console.log(JSON.stringify(navState, null, 2));
  await page.screenshot({ path: '../test-results/debug-nav-desktop.png', fullPage: false });
});
