/* Runs against tests/web_fixture.py on 8767. No cloud model is used. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');

const base = 'http://127.0.0.1:8767';
const screenshots = [
  [1440, 900, 'artifacts/workbench-redesign-1440.png'],
  [1024, 768, 'artifacts/workbench-redesign-1024.png'],
  [768, 1024, 'artifacts/workbench-redesign-768.png'],
  [375, 812, 'artifacts/workbench-redesign-375.png'],
];

async function visible(page, selector) {
  return page.locator(selector).evaluate(element => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width > 0 && rect.height > 0;
  });
}

async function horizontalBounds(page, selector) {
  return page.locator(selector).evaluate(element => {
    const rect = element.getBoundingClientRect();
    return {left: rect.left, right: rect.right};
  });
}

(async () => {
  fs.mkdirSync('artifacts', {recursive: true});
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(base, {waitUntil: 'networkidle'});
    for (const selector of [
      '#agent-dock', '#session-panel', '#thread-panel', '#work-rail',
      '#model-status', '#model-name', '#model-state', '#run-detail-sheet',
    ]) {
      assert.equal(await page.locator(selector).count(), 1, `missing ${selector}`);
    }
    assert((await page.locator('#agent-dock [data-agent-id]').count()) >= 2,
      'desktop dock must show multiple enabled agents');
    assert((await page.locator('#model-name').innerText()).trim().length > 0,
      'composer must show the current model name');
    assert.equal((await page.locator('#model-state').innerText()).trim(), '就绪');

    for (const [width, height, path] of screenshots) {
      await page.setViewportSize({width, height});
      await page.waitForTimeout(240);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
        `page must not overflow horizontally at ${width}x${height}`);
      assert.equal(await visible(page, '#model-status'), true,
        `model status must stay visible at ${width}x${height}`);
      if (width >= 1200) {
        assert.equal(await visible(page, '#agent-dock'), true);
        assert.equal(await visible(page, '#session-panel'), true);
        assert.equal(await visible(page, '#work-rail'), true);
      } else if (width >= 768) {
        assert.equal(await visible(page, '#agent-dock'), true);
        assert.equal(await visible(page, '#mobile-agent-switch'), false);
        const sessionBounds = await horizontalBounds(page, '#session-panel');
        const workBounds = await horizontalBounds(page, '#work-rail');
        assert(sessionBounds.right <= 0,
          `closed session drawer must be fully outside the viewport at ${width}px`);
        assert(workBounds.left >= width,
          `closed work drawer must be fully outside the viewport at ${width}px`);
      } else {
        assert.equal(await visible(page, '#agent-dock'), false);
        assert.equal(await visible(page, '#mobile-agent-switch'), true);
      }
      await page.screenshot({path, fullPage: true});
    }
    await page.setViewportSize({width: 1190, height: 800});
    await page.waitForTimeout(240);
    assert.equal(await page.locator('#sidebar-panel').evaluate(element => element.inert), true,
      'the closed session drawer must be inert across the full tablet breakpoint');
    assert.equal(await page.locator('#work-rail').evaluate(element => element.inert), true,
      'the closed work drawer must be inert across the full tablet breakpoint');

    await page.setViewportSize({width: 375, height: 812});
    for (const selector of [
      '#session-create-toggle', '#composer-import', '.composer-settings > summary',
      '#model-status > summary', '#cancel',
    ]) {
      const minHeight = await page.locator(selector).evaluate(element =>
        Number.parseFloat(getComputedStyle(element).minHeight));
      assert(minHeight >= 44, `${selector} must keep a 44px touch target on mobile`);
    }
    assert.deepEqual(errors, []);
    console.log('PASS: approved workbench skeleton, agent dock, model status, responsive layout, screenshots');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
