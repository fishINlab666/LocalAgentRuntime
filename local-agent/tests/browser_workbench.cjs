/* Actual static page with local synthetic HTTP responses; never calls a model. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.route('http://workbench.test/**', async route => {
    const url = new URL(route.request().url());
    if (['/', '/app.js', '/app.css'].includes(url.pathname)) {
      const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      return route.fulfill({status: 200,
        contentType: file.endsWith('.js') ? 'text/javascript'
          : file.endsWith('.css') ? 'text/css' : 'text/html',
        body: fs.readFileSync('local_agent/static/' + file, 'utf8')});
    }
    const send = value => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify(value)});
    if (url.pathname === '/api/config') return send({ready: true, workspace: '/synthetic',
      workspace_available: true, provider: {simulated: true}});
    if (url.pathname === '/api/agents') return send({agents: []});
    if (url.pathname === '/api/capabilities') return send({skills: [], servers: []});
    if (url.pathname === '/api/sessions') return send({sessions: [], next_cursor: null});
    return route.fulfill({status: 404, contentType: 'application/json',
      body: JSON.stringify({error: 'NOT_FOUND'})});
  });
  try {
    await page.goto('http://workbench.test/', {waitUntil: 'networkidle'});
    assert.equal(await page.title(), 'Local Agent 工作台');
    for (const region of ['sidebar', 'thread', 'inspector']) {
      assert.equal(await page.locator(`[data-region="${region}"]`).count(), 1);
    }
    assert.equal(await page.locator('.workbench-shell').count(), 1);
    assert.deepEqual(pageErrors, []);
    console.log('PASS: workbench shell');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
