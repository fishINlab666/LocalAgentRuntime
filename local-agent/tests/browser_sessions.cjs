/* Runs against tests/web_fixture.py on 8767. No cloud model is used. */
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport: {width: 1280, height: 900}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const base = 'http://127.0.0.1:8767';
  let sessionPosts = 0, runPosts = 0;
  page.on('request', request => {
    if (request.method() !== 'POST') return;
    if (request.url() === base + '/api/sessions') sessionPosts++;
    if (/\/api\/sessions\/[^/]+\/runs$/.test(request.url())) runPosts++;
  });

  async function waitTerminal() {
    await page.waitForFunction(() => {
      const label = document.querySelector('#run-status')?.textContent;
      return ['回答已完成', '信息未记载', '无法读取', '本次未完成', '已取消', '运行已中断'].includes(label);
    }, null, {timeout: 10000});
  }

  async function createSession(title) {
    await page.locator('#session-name').fill(title);
    await page.locator('#session-mode').selectOption('file');
    await page.locator('#session-file').fill('demo-note.md');
    const before = await page.locator('#session-select option').count();
    await page.locator('#session-create').click();
    await page.waitForFunction(count => document.querySelectorAll('#session-select option').length > count,
      before);
    return page.locator('#session-select').inputValue();
  }

  async function submit(question, taskType = 'files') {
    await page.locator('#task-type').selectOption(taskType);
    await page.locator('#question').fill(question);
    await page.locator('#start').click();
    await page.waitForFunction(value => document.querySelector('#source-line')?.textContent.includes(value), question);
    await waitTerminal();
  }

  try {
    await page.goto(base, {waitUntil: 'networkidle'});
    assert.match(await page.locator('#connection').innerText(), /模拟/);
    assert.equal(await page.locator('#session-select').inputValue(), '');

    const sessionA = await createSession('会话 A');
    assert(sessionA);
    assert.match(await page.locator('#session-scope').innerText(), /demo-note\.md/);
    assert.equal(await page.locator('#file').isDisabled(), true);
    assert.equal(await page.locator('#discover').isDisabled(), true);
    await submit('第一次核对项目代号');
    assert.match(await page.locator('#answer-text').innerText(), /浏览器-481/);
    assert.equal(await page.locator('#session-history button').count(), 1);
    await submit('第二次核对项目代号');
    assert.equal(await page.locator('#session-history button').count(), 2);

    const requestsBeforeReload = runPosts;
    await page.reload({waitUntil: 'networkidle'});
    await page.waitForFunction(() => document.querySelectorAll('#session-history button').length === 2);
    assert.equal(await page.locator('#session-select').inputValue(), sessionA);
    assert.equal(runPosts, requestsBeforeReload, 'reload must only read durable history');
    assert.match(await page.locator('#answer-text').innerText(), /浏览器-481/);

    await submit('我上一次要求了什么？', 'conversation');
    assert.match(await page.locator('#answer-text').innerText(), /第二次核对项目代号/);
    assert.match(await page.locator('#citations').innerText(), /会话消息/);
    assert.equal(await page.locator('#output-file').isDisabled(), true);

    const sessionB = await createSession('会话 B');
    assert.notEqual(sessionB, sessionA);
    await submit('B 会话核对');
    assert.equal(await page.locator('#session-history button').count(), 1);
    await page.locator('#output-file').fill('session-report.md');
    await page.locator('#question').fill('B 会话生成报告');
    await page.locator('#start').click();
    await page.locator('#approval').waitFor({state: 'visible'});
    assert.equal(await page.locator('#approval-actions').isVisible(), true);
    await page.locator('#approval-allow').click();
    await waitTerminal();
    await page.waitForFunction(() => document.querySelectorAll('#session-history button').length === 2);
    await page.locator('#session-history button').first().click();
    await page.waitForFunction(() => !document.querySelector('#approval').hidden);
    assert.equal(await page.locator('#approval-actions').isVisible(), false,
      'historical approval preview must be read-only');

    await page.locator('#session-select').selectOption(sessionA);
    await page.waitForFunction(id => document.querySelector('#session-select').value === id
      && document.querySelectorAll('#session-history button').length === 3, sessionA);
    let releaseLate;
    const late = new Promise(resolve => { releaseLate = resolve; });
    await page.route(`**/api/sessions/${sessionA}/runs/*`, async route => {
      if (route.request().method() !== 'GET') return route.continue();
      const response = await route.fetch(); await late; await route.fulfill({response}).catch(() => {});
    });
    await page.locator('#session-history button').first().click();
    await page.locator('#session-select').selectOption(sessionB);
    await page.waitForFunction(id => document.querySelector('#session-select').value === id, sessionB);
    releaseLate(); await page.waitForTimeout(250);
    assert.equal(await page.locator('#session-select').inputValue(), sessionB,
      'late response from A must not replace B');
    await page.unroute(`**/api/sessions/${sessionA}/runs/*`);

    await page.locator('#session-rename-title').fill('会话 B 已改名');
    await page.locator('#session-rename').click();
    await page.waitForFunction(() => document.querySelector('#session-select').selectedOptions[0].textContent.includes('已改名'));
    await page.locator('#session-archive').click();
    await page.locator('#session-restore').waitFor({state: 'visible'});
    assert.equal(await page.locator('#start').isDisabled(), true);
    await page.locator('#session-restore').click();
    await page.locator('#session-archive').waitFor({state: 'visible'});

    for (const width of [1024, 768, 375]) {
      await page.setViewportSize({width, height: 812});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    }
    assert.equal(sessionPosts, 2);
    assert.equal(runPosts, 5);
    assert.deepEqual(errors, []);
    console.log('PASS: durable session create, fixed scope, two rounds, reload, conversation references, A/B switch, late response guard, rename, archive/restore, responsive layout');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
