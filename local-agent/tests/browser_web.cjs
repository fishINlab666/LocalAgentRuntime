/* Runs against tests/web_fixture.py on 8767 (synthetic) and 8768 (missing key). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  const base = 'http://127.0.0.1:8767';
  let runRequests = 0, lastRunBody;
  page.on('request', request => {
    if (request.url() === base + '/api/runs' && request.method() === 'POST') {
      runRequests++; lastRunBody = request.postDataJSON();
    }
  });
  async function submit(question, file = 'demo-note.md') {
    await page.locator('#file').fill(file);
    await page.locator('#question').fill(question);
    await page.locator('#start').click();
  }
  async function terminal() {
    await page.waitForFunction(() => !document.querySelector('#start').disabled);
  }
  async function noOverflow() {
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  }
  try {
    fs.mkdirSync('artifacts', {recursive: true});
    await page.goto(base, {waitUntil: 'networkidle'});
    assert.match(await page.locator('#connection').innerText(), /模拟/);
    await page.locator('#discover').check();
    assert.equal(await page.locator('#file').isDisabled(), true);
    assert.equal(await page.locator('#file').evaluate(element => element.required), false);
    await page.locator('#question').fill('合并项目代号、评审人和演示日期');
    await page.locator('#start').click(); await terminal();
    assert.deepEqual(lastRunBody, {mode: 'directory', question: '合并项目代号、评审人和演示日期'});
    assert.equal(await page.locator('#citations .citation').count(), 2);
    assert.match(await page.locator('#citations').innerText(), /review\.md/);
    assert.match(await page.locator('#scope-summary').innerText(), /已发现 2.*已读取 2.*未读取 0.*未列出目录 0/);
    assert.match(await page.locator('#source-line').innerText(), /目录发现/);
    assert(!((await page.locator('#source-line').innerText()).includes('null')));
    await page.locator('#trace summary').click();
    assert.match(await page.locator('#events').innerText(), /列出目录/);
    const directoryRequests = runRequests;
    await page.reload({waitUntil: 'networkidle'}); await terminal();
    assert.equal(runRequests, directoryRequests);
    assert.equal(await page.locator('#discover').isChecked(), true);
    assert.equal(await page.locator('#file').isDisabled(), true);
    assert.equal(await page.locator('#file').inputValue(), '');
    assert.equal(await page.locator('#scope-summary').isVisible(), true);
    await page.screenshot({path: 'artifacts/web-directory.png', fullPage: true});
    await page.locator('#question').fill('慢速目录发现');
    await page.locator('#start').click();
    await page.locator('#cancel').waitFor({state: 'visible'});
    const activeDirectoryRequests = runRequests;
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.locator('#cancel').waitFor({state: 'visible'});
    assert.equal(runRequests, activeDirectoryRequests);
    assert.equal(await page.locator('#discover').isChecked(), true);
    assert.equal(await page.locator('#discover').isDisabled(), true);
    assert.equal(await page.locator('#file').isDisabled(), true);
    await page.locator('#cancel').click(); await terminal();
    assert.equal(await page.locator('#discover').isChecked(), true);
    assert.equal(await page.locator('#file').isDisabled(), true);
    assert.match(await page.locator('#scope-summary').innerText(), /未列出目录 1.*检查范围尚不完整.*未列出目录：\./);
    await page.locator('#sample').click();
    assert.equal(await page.locator('#discover').isChecked(), false);
    assert.equal(await page.locator('#file').isDisabled(), false);
    assert.equal(await page.locator('#file').inputValue(), 'demo-note.md');
    await page.locator('#start').click();
    await terminal();
    assert.equal(await page.locator('#run-status').innerText(), '回答已完成');
    assert.match(await page.locator('#answer-text').innerText(), /浏览器-481/);
    assert.match(await page.locator('#citations').innerText(), /<script>/);
    assert.equal(await page.locator('#citations script').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    await page.locator('#trace summary').click();
    assert.match(await page.locator('#events').innerText(), /工具结果已回填/);
    assert(!((await page.locator('#events').innerText()).includes('浏览器-481')));
    await page.screenshot({path: 'artifacts/web-answer-desktop.png', fullPage: true});
    await noOverflow();
    await submit('标识纠错成功'); await terminal();
    assert.equal(await page.locator('#run-status').innerText(), '回答已完成');
    assert.match(await page.locator('#answer-text').innerText(), /浏览器-481/);
    assert.match(await page.locator('#events').innerText(), /标识与原文不一致，正在纠错/);
    await submit('标识纠错失败'); await terminal();
    assert.equal(await page.locator('#answer-block').isVisible(), false);
    assert.match(await page.locator('#failure-message').innerText(), /标识连接符与已读原文不一致/);
    assert(!((await page.locator('#failure-message').innerText()).includes('格式')));
    await submit('预算是多少？'); await terminal();
    assert.equal(await page.locator('#run-status').innerText(), '信息未记载');
    assert.equal(await page.locator('#evidence').isVisible(), false);
    await submit('找项目代号', 'missing.md'); await terminal();
    assert.equal(await page.locator('#run-status').innerText(), '无法读取');
    assert.match(await page.locator('#failure-message').innerText(), /找不到这个文件/);
    assert.equal(await page.locator('#answer-block').isVisible(), false);
    await submit('模拟网络失败'); await terminal();
    assert.match(await page.locator('#failure-message').innerText(), /连接 DeepSeek 失败/);
    await submit('先读错，然后鉴权失败'); await terminal();
    assert.match(await page.locator('#failure-message').innerText(), /DeepSeek 鉴权失败/, 'Terminal provider error takes precedence over historical tool error');
    await submit('慢速读取');
    await page.locator('#cancel').waitFor({state: 'visible'});
    assert.equal(await page.locator('#discover').isDisabled(), true);
    const requestsBeforeReload = runRequests;
    // Polling remains active during a run, so networkidle would wait until it ends.
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.locator('#cancel').waitFor({state: 'visible'});
    assert.equal(runRequests, requestsBeforeReload, 'Reload must not resubmit');
    await page.locator('#cancel').click(); await terminal();
    assert.equal(await page.locator('#run-status').innerText(), '已取消');
    assert.equal(await page.locator('#answer-block').isVisible(), false);
    await submit('读取文件', '../secret.md');
    await page.locator('#form-error').waitFor({state: 'visible'});
    assert.match(await page.locator('#form-error').innerText(), /不在允许范围/);
    await submit('读取代号'); await terminal();
    let releaseCancel;
    const cancelGate = new Promise(resolve => { releaseCancel = resolve; });
    await page.route('**/api/runs/*/cancel', async route => {
      const response = await route.fetch();
      await cancelGate;
      await route.fulfill({response});
    });
    await submit('慢速旧任务');
    await page.locator('#cancel').waitFor({state: 'visible'});
    await page.locator('#cancel').click();
    await terminal(); // Poll observes the cancellation before the cancel reply arrives.
    await submit('慢速新任务');
    await page.locator('#cancel').waitFor({state: 'visible'});
    releaseCancel();
    await page.waitForTimeout(300);
    assert.match(await page.locator('#source-line').innerText(), /慢速新任务/, 'A late cancel reply must not replace the new task');
    assert.equal(await page.locator('#cancel').isVisible(), true);
    await page.unroute('**/api/runs/*/cancel');
    await page.locator('#cancel').click(); await terminal();
    await submit('读取代号'); await terminal();
    for (const [width, height] of [[1024, 900], [768, 1024], [375, 812], [812, 375]]) {
      await page.setViewportSize({width, height});
      await noOverflow();
    }
    await page.setViewportSize({width: 375, height: 812});
    await page.emulateMedia({reducedMotion: 'reduce'});
    await page.screenshot({path: 'artifacts/web-answer-mobile.png', fullPage: true});
    let releaseSubmit;
    const submitGate = new Promise(resolve => { releaseSubmit = resolve; });
    await page.route('**/api/runs', async route => {
      const response = await route.fetch();
      await submitGate;
      await route.fulfill({response}).catch(() => {});
    });
    await submit('提交已成功但响应丢失');
    await page.locator('#form-error').waitFor({state: 'visible', timeout: 10000});
    assert.match(await page.locator('#form-error').innerText(), /可能仍在运行/);
    assert.equal(await page.locator('#start').isDisabled(), true, 'Ambiguous submission must not be resubmitted');
    releaseSubmit(); await page.unroute('**/api/runs');
    await page.reload({waitUntil: 'networkidle'});
    await terminal();
    // Real local writes using a synthetic provider: preview, refresh, allow, deny, and later failure.
    await page.setViewportSize({width: 1440, height: 1000});
    const config = await page.evaluate(async () => (await fetch('/api/config', {headers: {
      'X-Session-Token': document.querySelector('meta[name="session-token"]').content}})).json());
    const path = require('node:path'), crypto = require('node:crypto');
    await page.locator('#discover').check();
    await page.locator('#output-file').fill('confirmed-report.md');
    await page.locator('#question').fill('依据两份小文件生成核对报告');
    await page.locator('#start').click();
    await page.locator('#approval').waitFor({state: 'visible'});
    const expected = await page.locator('#approval-content').textContent();
    assert.match(expected, /<script>/);
    assert.equal(await page.locator('#approval-content script').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.equal(fs.existsSync(path.join(config.workspace, 'confirmed-report.md')), false);
    const reportRequests = runRequests;
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.locator('#approval').waitFor({state: 'visible'});
    assert.equal(runRequests, reportRequests);
    assert.equal(await page.locator('#approval-content').textContent(), expected);
    await page.screenshot({path: 'artifacts/tool-approval.png', fullPage: true});
    await page.locator('#approval-allow').click(); await terminal();
    const actual = fs.readFileSync(path.join(config.workspace, 'confirmed-report.md'));
    assert.equal(actual.toString('utf8'), expected);
    assert.match(await page.locator('#artifact-list').innerText(), /confirmed-report.md.*已新建/);
    assert.equal(await page.locator('#artifact-list code').textContent(), `SHA-256：${crypto.createHash('sha256').update(actual).digest('hex')}`);
    assert.equal(await page.locator('#citations .citation').count(), 2);
    await page.screenshot({path: 'artifacts/tool-created.png', fullPage: true});
    await page.locator('#output-file').fill('denied-report.md');
    await page.locator('#start').click(); await page.locator('#approval').waitFor({state: 'visible'});
    await page.locator('#approval-deny').click(); await terminal();
    assert.equal(fs.existsSync(path.join(config.workspace, 'denied-report.md')), false);
    assert.match(await page.locator('#failure-message').innerText(), /拒绝/);
    assert.equal(await page.locator('#artifacts').isVisible(), false);
    await page.locator('#output-file').fill('retained-report.md');
    await page.locator('#question').fill('产物后失败');
    await page.locator('#start').click(); await page.locator('#approval').waitFor({state: 'visible'});
    await page.locator('#approval-allow').click(); await terminal();
    assert.equal(fs.existsSync(path.join(config.workspace, 'retained-report.md')), true);
    assert.match(await page.locator('#artifact-note').innerText(), /后续步骤未完成/);
    assert.match(await page.locator('#failure-message').innerText(), /连接 DeepSeek 失败/);
    await page.goto('http://127.0.0.1:8768', {waitUntil: 'networkidle'});
    assert.equal(await page.locator('#start').isDisabled(), true);
    assert.equal(await page.locator('#connection').innerText(), '模型未配置');
    assert.equal(await page.locator('#discover').isDisabled(), true);
    assert.match(await page.locator('#mode-notice').innerText(), /DEEPSEEK_API_KEY/);
    assert.equal(pageErrors.length, 0, pageErrors.join('\n'));
    console.log('PASS: directory mode, two-file citations, coverage, completed/active refresh, answers, HTML-as-text, not-found, missing-file, network error, cancel, late replies, path denial, 5 viewports, missing-key, exact approved file bytes, denial, retained receipts, no JS errors');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
