/* Real workbench assets with synthetic HTTP responses; no model or Feishu calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

const budgets = {max_steps: 6, run_timeout: 120, model_timeout: 45, tool_timeout: 5,
  max_tool_calls: 4, max_input_bytes: 65536, max_files: 7, max_file_bytes: 16384};
const agents = ['file-qa', 'directory-qa'].map(id => ({id, name: id,
  strategy: id === 'file-qa' ? 'file' : 'directory', tools: ['read_file', 'write_file'],
  budgets, revision: id + '-revision', enabled: true, instructions: id,
  model: {provider: 'deepseek', name: 'deepseek-chat'}, skills: [], mcp: []}));
const session = (id, agentId) => ({id, title: id, scope: {mode: 'file', file: 'note.md'},
  status: 'active', agent_id: agentId, agent_revision: agentId + '-revision',
  agent_snapshot: agents.find(item => item.id === agentId), created_at: 1, updated_at: 2});
const sessions = [session('session-default', 'file-qa'), session('session-other', 'file-qa'),
  session('session-target', 'directory-qa')];
const run = (id, sessionId, state = 'completed') => ({id, session_id: sessionId,
  question: id, task_type: 'files', output_file: null, state,
  phase: state === 'completed' ? 'ended' : 'approval', revision: 1,
  started_at: 1, finished_at: state === 'completed' ? 2 : null,
  events: [], artifacts: [], approvals: [],
  result: state === 'completed' ? {answer: {status: 'answered', answer: '回答 ' + id,
    citations: []}, stop_reason: 'ANSWERED', artifacts: [], trace_path: 'trace-' + id} : null,
  pending_approval: state === 'waiting_approval' ? {approval_id: 'approval-' + id,
    name: 'write_file', path: 'report.md', bytes: 9, operation: 'create', source: 'builtin',
    risk: 'medium', content: '# report', action_summary: '新建 report.md',
    arguments: {intent: '生成报告'}, remaining_seconds: 120} : null});

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const context = await browser.newContext({viewport: {width: 1440, height: 900}});
  const requests = [], pageErrors = [];
  let deliveryState = 'unknown';
  let runs = [], heldSummary = null, holdNextSummary = false;
  const reset = () => { runs = [run('old-default', 'session-default'),
    run('old-other', 'session-other'), run('old-target', 'session-target')]; };
  reset(); runs.unshift(run('live-target', 'session-target', 'waiting_approval'));
  await context.route('http://channel.test/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    requests.push({path, method: request.method(), headers: request.headers(),
      body: request.method() === 'POST' ? request.postDataJSON() : null});
    const send = (value, status = 200) => route.fulfill({status,
      contentType: 'application/json', body: JSON.stringify(value)});
    if (['/', '/app.js', '/app.css'].includes(path)) {
      const file = path === '/' ? 'index.html' : path.slice(1);
      return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript'
        : file.endsWith('.css') ? 'text/css' : 'text/html',
      body: fs.readFileSync('local_agent/static/' + file, 'utf8')});
    }
    if (path === '/api/config') return send({ready: true, workspace: '/synthetic',
      workspace_available: true, provider: {simulated: true}});
    if (path === '/api/agents') return send({agents});
    if (path === '/api/capabilities') return send({skills: [], servers: []});
    if (path === '/api/channels/feishu') return send({channel:
      request.headers()['x-agent-id'] === 'directory-qa' ? {enabled: true,
        binding: {agent_id: 'directory-qa', session_id: 'session-target', revision: 'r1'},
        transport: {state: 'connected'}, events: [], outbox: [{id: 'notice-target',
          run_id: 'live-target', kind: 'approval', state: deliveryState, attempts: 1,
          error_code: null}]} : {enabled: false}});
    if (path === '/api/channels/feishu/retry') {
      assert.deepEqual(request.postDataJSON(), {outbox_id: 'notice-target'});
      deliveryState = 'pending'; return send({ok: true});
    }
    if (path === '/api/sessions') return send({sessions: url.searchParams.has('archived') ? []
      : sessions.filter(item => item.agent_id === request.headers()['x-agent-id']
        && item.id !== 'session-target'), next_cursor: null});
    const sessionMatch = path.match(/^\/api\/sessions\/([^/]+)$/);
    if (sessionMatch) {
      if (sessionMatch[1] === 'wrong-agent') return send({session: session('wrong-agent', 'file-qa')});
      const item = sessions.find(item => item.id === sessionMatch[1]
        && item.agent_id === request.headers()['x-agent-id']);
      return item ? send({session: item}) : send({error: 'NOT_FOUND'}, 404);
    }
    const listMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs$/);
    if (listMatch) {
      assert.equal(request.method(), 'GET', 'observing external runs must never submit one');
      const snapshot = {runs: runs.filter(item => item.session_id === listMatch[1]), next_cursor: null};
      if (holdNextSummary && listMatch[1] === 'session-default') {
        holdNextSummary = false;
        await new Promise(resolve => { heldSummary = resolve; });
      }
      return send(snapshot);
    }
    const actionMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs\/([^/]+)\/(cancel|approvals\/[^/]+)$/);
    if (actionMatch) {
      const item = runs.find(item => item.id === actionMatch[2]);
      item.state = 'cancelled'; item.pending_approval = null; item.revision++;
      item.result = {answer: null, stop_reason: 'CANCELLED', artifacts: [], trace_path: ''};
      return send({run: item});
    }
    const runMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs\/([^/]+)$/);
    if (runMatch) {
      if (runMatch[2] === 'wrong-session') return send({run: run('wrong-session', 'session-default')});
      const item = runs.find(item => item.id === runMatch[2] && item.session_id === runMatch[1]);
      return item ? send({run: item}) : send({error: 'NOT_FOUND'}, 404);
    }
    return send({error: 'NOT_FOUND'}, 404);
  });
  const open = async hash => {
    const page = await context.newPage();
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto('http://channel.test/' + hash, {waitUntil: 'domcontentloaded'});
    return page;
  };
  const waitLive = (page, id) => page.waitForFunction(id =>
    document.querySelector(`#session-history [data-run-id="${id}"][data-live="true"]`)
      && !document.querySelector('#approval').hidden, id, {timeout: 7000});
  try {
    const page = await open('#agent=directory-qa&session=session-target&run=live-target');
    await page.waitForFunction(() => document.querySelector('#session-scope').textContent.length > 0);
    assert.match(await page.locator('#session-scope').innerText(), /directory-qa/,
      'deep link must choose its agent/session, even outside the first session page');
    await waitLive(page, 'live-target');
    assert.equal(await page.locator('#cancel').isVisible(), true);
    await page.locator('#channel-status').waitFor();
    assert.match(await page.locator('#channel-status').innerText(), /送达情况未知，补发可能重复通知/);
    await page.getByRole('button', {name: '仅补发通知', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('#channel-status').textContent.includes('等待发送'));
    assert(requests.filter(item => item.path.startsWith('/api/sessions/session-target'))
      .every(item => item.headers['x-agent-id'] === 'directory-qa'));
    await page.reload({waitUntil: 'domcontentloaded'});
    await waitLive(page, 'live-target');
    await page.locator('[data-run-id="old-target"] .run-inspect').click();
    assert.equal(await page.locator('#session-history [data-run-id="old-target"]').getAttribute('aria-current'), 'true');
    await page.locator('#approval-deny').click();
    await page.waitForFunction(() => document.querySelector('#cancel').hidden);
    assert.equal(requests.filter(item => item.method === 'POST').at(-1).path,
      '/api/sessions/session-target/runs/live-target/approvals/approval-live-target',
      'historical inspection must not change the active approval target');
    await page.close();

    reset(); runs.unshift(run('live-default', 'session-default', 'waiting_approval'));
    const restored = await open('');
    await waitLive(restored, 'live-default');
    await restored.reload({waitUntil: 'domcontentloaded'});
    await waitLive(restored, 'live-default');
    await restored.locator('#cancel').click();
    await restored.waitForFunction(() => document.querySelector('#cancel').hidden);
    assert.equal(requests.filter(item => item.method === 'POST').at(-1).path,
      '/api/sessions/session-default/runs/live-default/cancel');
    await restored.close();

    for (const state of ['queued', 'running']) {
      reset(); runs.unshift(run('active-default', 'session-default', state));
      const progressing = await open('');
      await progressing.locator('#cancel').waitFor();
      assert.equal(await progressing.locator('#start').isDisabled(), true);
      runs[0] = {...run('active-default', 'session-default', 'waiting_approval'), revision: 2};
      await waitLive(progressing, 'active-default');
      await progressing.close();
    }

    reset();
    const external = await open('');
    await external.locator('[data-run-id="old-default"] .run-inspect').waitFor();
    runs.unshift(run('external-run', 'session-default', 'waiting_approval'));
    await waitLive(external, 'external-run');
    assert.equal(await external.locator('#session-history [data-run-id="old-default"]').getAttribute('aria-current'), 'true',
      'discovering a new run must preserve the inspected history');
    await external.locator('#cancel').click();
    await external.waitForFunction(() => document.querySelector('#cancel').hidden);
    await external.close();

    // A summary already in flight must not attach its run after switching sessions.
    reset();
    const stale = await open('');
    await stale.locator('[data-run-id="old-default"] .run-inspect').waitFor();
    runs.unshift(run('late-run', 'session-default', 'waiting_approval'));
    holdNextSummary = true;
    for (let i = 0; i < 70 && !heldSummary; i++) await new Promise(resolve => setTimeout(resolve, 100));
    assert(heldSummary, 'current session should periodically refresh its summary');
    await stale.locator('#session-list [data-session-id="session-other"]').click();
    await stale.locator('[data-run-id="old-other"] .run-inspect').waitFor();
    heldSummary(); heldSummary = null;
    await stale.waitForTimeout(200);
    assert.equal(await stale.locator('[data-run-id="late-run"]').count(), 0);
    assert.equal(await stale.locator('#cancel').isVisible(), false);
    await stale.close();

    reset();
    const background = await open('');
    await background.locator('[data-run-id="old-default"] .run-inspect').waitFor();
    await background.waitForLoadState('networkidle');
    await background.clock.install();
    await background.evaluate(() => {
      Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
      document.dispatchEvent(new Event('visibilitychange'));
    });
    const summaryCount = () => requests.filter(item => item.path === '/api/sessions/session-default/runs').length;
    const beforeHidden = summaryCount();
    await background.clock.runFor(5000);
    assert.equal(summaryCount(), beforeHidden, 'hidden pages must reduce summary request frequency');
    const hiddenRequest = background.waitForRequest(request =>
      new URL(request.url()).pathname === '/api/sessions/session-default/runs');
    const hiddenResponse = background.waitForResponse(response =>
      new URL(response.url()).pathname === '/api/channels/feishu');
    await background.clock.runFor(10100); await hiddenRequest;
    await hiddenResponse;
    assert.equal(summaryCount(), beforeHidden + 1, 'hidden pages still refresh within the bounded interval');
    await background.evaluate(() => {
      Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
      document.dispatchEvent(new Event('visibilitychange'));
    });
    const visibleRequest = background.waitForRequest(request =>
      new URL(request.url()).pathname === '/api/sessions/session-default/runs');
    const visibleResponse = background.waitForResponse(response =>
      new URL(response.url()).pathname === '/api/sessions/session-default/runs');
    await background.clock.runFor(200); await visibleRequest;
    await visibleResponse;
    assert.equal(summaryCount(), beforeHidden + 2, 'returning to the foreground refreshes promptly');
    await background.close();

    for (const hash of ['#agent=missing&session=session-target&run=old-target',
      '#agent=directory-qa&session=wrong-agent&run=old-target',
      '#agent=directory-qa&session=session-target&run=missing',
      '#agent=directory-qa&session=session-target&run=wrong-session',
      '#agent=directory-qa&session=session-target',
      '#agent=directory-qa&agent=file-qa&session=session-target&run=old-target']) {
      const invalid = await open(hash);
      await invalid.waitForFunction(() => !document.querySelector('#session-error').hidden);
      assert.match(await invalid.locator('#session-error').innerText(), /链接|目标/);
      assert.equal(await invalid.locator('#session-history [data-run-id]').count(), 0,
        'an invalid deep link must not silently display an unrelated run');
      assert.equal(await invalid.locator('#cancel').isVisible(), false);
      await invalid.close();
    }
    assert.deepEqual(pageErrors, []);
    assert(requests.filter(item => item.method === 'POST').every(item =>
      /\/(cancel|approvals\/[^/]+)$/.test(item.path) || item.path === '/api/channels/feishu/retry'),
    'no viewing or notification flow may POST a new run');
    console.log('PASS browser_channel: deep link, ownership, refresh, external discovery, stale summary, approval/cancel isolation');
  } finally {
    if (heldSummary) heldSummary();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
