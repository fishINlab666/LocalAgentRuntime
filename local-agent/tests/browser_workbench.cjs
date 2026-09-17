/* Actual static page with local synthetic HTTP responses; never calls a model. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');

const budgets = {max_steps: 6, run_timeout: 120, model_timeout: 45, tool_timeout: 5,
  max_tool_calls: 4, max_input_bytes: 65536, max_files: 7, max_file_bytes: 16384};
const agent = (id, name, strategy, tools) => ({id, name, strategy, tools, budgets,
  revision: id + '-revision', enabled: true, instructions: name,
  model: {provider: 'deepseek', name: 'deepseek-chat'}, skills: [], mcp: []});
const agents = [
  agent('file-qa', '单文件问答', 'file', ['read_file', 'write_file', 'session_history']),
  agent('directory-qa', '目录问答', 'directory', ['list_files', 'read_file', 'search_documents', 'session_history']),
  agent('project-brief', '项目简报', 'directory', ['list_files', 'read_file', 'search_documents', 'write_file', 'session_history']),
];
const makeSession = number => ({id: `session-${String(number).padStart(2, '0')}`,
  title: `项目会话 ${number}`, scope: {mode: 'file', file: 'demo-note.md'}, status: 'active',
  agent_id: 'file-qa', agent_revision: agents[0].revision, agent_snapshot: agents[0],
  created_at: 100 - number, updated_at: 200 - number});
const sessions = Array.from({length: 22}, (_, index) => makeSession(index + 1));
const makeRun = number => ({id: `run-${String(number).padStart(2, '0')}`,
  session_id: sessions[0].id, question: `第 ${number} 轮问题`, task_type: 'files',
  output_file: null, state: 'completed', phase: 'ended', stop_reason: 'ANSWERED',
  started_at: 100 - number, finished_at: 101 - number});
const runs = Array.from({length: 22}, (_, index) => makeRun(index + 1));
const requests = [];
let delayNextRunPage = false, releaseRunPage;
let delayNextSessionPage = false, releaseSessionPage;
const runPageGate = new Promise(resolve => { releaseRunPage = resolve; });
const sessionPageGate = new Promise(resolve => { releaseSessionPage = resolve; });
const runDetailGates = new Map();
let releaseRun03 = () => {}, releaseRun04 = () => {};
let failRun05Once = false;

function gateRunDetail(runId) {
  let release, markRequested;
  const waiting = new Promise(resolve => { release = resolve; });
  const requested = new Promise(resolve => { markRequested = resolve; });
  runDetailGates.set(runId, {waiting, markRequested});
  return {release, requested};
}

async function assertComposerVisible(page, width, height) {
  await page.setViewportSize({width, height});
  const geometry = await page.evaluate(() => {
    const box = id => {
      const rect = document.querySelector(id).getBoundingClientRect();
      return {top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right};
    };
    return {question: box('#question'), send: box('#start'), width: innerWidth, height: innerHeight};
  });
  for (const [name, box] of Object.entries({question: geometry.question, send: geometry.send})) {
    assert(box.top >= 0 && box.bottom <= geometry.height,
      `${name} must stay vertically visible at ${width}x${height}: ${JSON.stringify(box)}`);
    assert(box.left >= 0 && box.right <= geometry.width,
      `${name} must stay horizontally visible at ${width}x${height}: ${JSON.stringify(box)}`);
  }
}

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.route('http://workbench.test/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const requestedAgent = request.headers()['x-agent-id'];
    const scopedSession = item => requestedAgent ? {...item,
      title: `${item.title} · ${requestedAgent}`, agent_id: requestedAgent,
      agent_revision: `${requestedAgent}-revision`,
      agent_snapshot: agents.find(agent => agent.id === requestedAgent) || item.agent_snapshot} : item;
    let body = null;
    if (request.method() === 'POST') {
      try { body = request.postDataJSON(); } catch (_) {}
    }
    requests.push({method: request.method(), path, cursor: url.searchParams.get('cursor'),
      archived: url.searchParams.get('archived'), body});
    if (['/', '/app.js', '/app.css'].includes(path)) {
      const file = path === '/' ? 'index.html' : path.slice(1);
      return route.fulfill({status: 200,
        contentType: file.endsWith('.js') ? 'text/javascript'
          : file.endsWith('.css') ? 'text/css' : 'text/html',
        body: fs.readFileSync('local_agent/static/' + file, 'utf8')});
    }
    const send = value => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify(value)});
    if (path === '/api/config') return send({ready: true, workspace: '/synthetic',
      workspace_available: true, provider: {simulated: true}, imports: {
        formats: ['.md', '.txt', '.pdf', '.docx'].map(extension => ({extension, available: true})),
        limits: {max_items: 500, max_files: 50, max_file_bytes: 20 * 1024 * 1024,
          max_total_bytes: 100 * 1024 * 1024}}});
    if (path === '/api/agents') return send({agents});
    if (path === '/api/capabilities') return send({skills: [], servers: []});
    if (path === '/api/sessions') {
      if (url.searchParams.get('archived') === '1') return send({sessions: [], next_cursor: null});
      if (url.searchParams.get('cursor') === 'sessions-2' && delayNextSessionPage) {
        delayNextSessionPage = false; await sessionPageGate;
      }
      return url.searchParams.get('cursor') === 'sessions-2'
        ? send({sessions: sessions.slice(20).map(scopedSession), next_cursor: null})
        : send({sessions: sessions.slice(0, 20).map(scopedSession), next_cursor: 'sessions-2'});
    }
    const sessionMatch = path.match(/^\/api\/sessions\/([^/]+)$/);
    if (sessionMatch) return send({session: scopedSession(sessions.find(item => item.id === sessionMatch[1]))});
    const runListMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs$/);
    if (runListMatch) {
      if (request.method() === 'POST') {
        const body = request.postDataJSON();
        return send({run: {id: 'approval-run', session_id: runListMatch[1],
          question: body.question, task_type: body.task_type, output_file: body.output_file,
          state: 'running', phase: 'model', revision: 0}});
      }
      if (url.searchParams.get('cursor') === 'runs-2' && delayNextRunPage) {
        delayNextRunPage = false; await runPageGate;
      }
      return url.searchParams.get('cursor') === 'runs-2'
        ? send({runs: runs.slice(20), next_cursor: null})
        : send({runs: runs.slice(0, 20), next_cursor: 'runs-2'});
    }
    const continueMatch = path.match(/^\/api\/sessions\/([^/]+)\/continue$/);
    if (continueMatch && request.method() === 'POST') {
      return send({run: {id: 'continued-run', session_id: continueMatch[1],
        question: '继续中断任务', task_type: 'files', output_file: null,
        state: 'running', phase: 'model', revision: 0}});
    }
    const approvalMatch = path.match(
      /^\/api\/sessions\/([^/]+)\/runs\/(approval-run)\/approvals\/(approval-1)$/);
    if (approvalMatch && request.method() === 'POST') {
      return send({run: {id: approvalMatch[2], session_id: approvalMatch[1],
        question: '请生成一份报告', task_type: 'files', output_file: 'generated.md',
        state: 'completed', phase: 'ended', revision: 2, events: [], approvals: [], artifacts: [],
        result: {stop_reason: 'ANSWERED', trace_path: 'trace-approval-run', artifacts: [],
          answer: {status: 'answered', answer: '合成报告已生成', citations: []}}}});
    }
    const cancelMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs\/(approval-run)\/cancel$/);
    if (cancelMatch && request.method() === 'POST') {
      return send({run: {id: cancelMatch[2], session_id: cancelMatch[1],
        question: '请生成一份报告', task_type: 'files', output_file: 'generated.md',
        state: 'cancelled', phase: 'ended', revision: 2, events: [], approvals: [], artifacts: [],
        result: {stop_reason: 'CANCELLED', trace_path: 'trace-approval-run', artifacts: [], answer: null}}});
    }
    const runMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs\/([^/]+)$/);
    if (runMatch) {
      if (runMatch[2] === 'continued-run') {
        return send({run: {id: 'continued-run', session_id: runMatch[1],
          question: '继续中断任务', task_type: 'files', output_file: null,
          state: 'completed', phase: 'ended', revision: 1, events: [], approvals: [], artifacts: [],
          result: {stop_reason: 'ANSWERED', trace_path: 'trace-continued-run', artifacts: [],
            answer: {status: 'answered', answer: '中断任务已继续完成', citations: []}}}});
      }
      if (runMatch[2] === 'approval-run') {
        return send({run: {id: 'approval-run', session_id: runMatch[1],
          question: '请生成一份报告', task_type: 'files', output_file: 'generated.md',
          state: 'waiting_approval', phase: 'approval', revision: 1,
          events: [{elapsed: 0.1, event: 'approval.required', detail: {name: 'write_file'}}],
          approvals: [], artifacts: [], result: null,
          pending_approval: {approval_id: 'approval-1', name: 'write_file', path: 'generated.md',
            bytes: 12, operation: 'create', source: 'builtin', risk: 'medium',
            action_summary: '新建 generated.md', arguments: {intent: '保存报告'},
            content: '合成报告内容', remaining_seconds: 60}}});
      }
      if (runMatch[2] === 'run-05' && failRun05Once) {
        failRun05Once = false;
        return route.fulfill({status: 500, contentType: 'application/json',
          body: JSON.stringify({error: 'LOCAL_SERVER_ERROR'})});
      }
      const gate = runDetailGates.get(runMatch[2]);
      if (gate) {
        gate.markRequested(); await gate.waiting;
        if (runDetailGates.get(runMatch[2]) === gate) runDetailGates.delete(runMatch[2]);
      }
      const item = runs.find(value => value.id === runMatch[2]);
      return send({run: {...item, events: [], approvals: [], artifacts: [],
        result: {state: 'completed', stop_reason: 'ANSWERED', model_calls: 2,
          trace_path: `trace-${item.id}`,
          answer: {status: 'answered', answer: `回答 ${item.question}`, citations: []},
          artifacts: []}}});
    }
    return route.fulfill({status: 404, contentType: 'application/json',
      body: JSON.stringify({error: 'NOT_FOUND'})});
  });
  try {
    await page.goto('http://workbench.test/', {waitUntil: 'networkidle'});
    assert.equal(await page.title(), 'Local Agent 工作台');
    assert.equal(await page.locator('.composer-settings').evaluate(element => element.open), false,
      'secondary run settings must start collapsed');
    for (const [width, height] of [[1440, 900], [1280, 800], [1024, 768]]) {
      await assertComposerVisible(page, width, height);
    }
    await page.setViewportSize({width: 1440, height: 900});
    await page.locator('#question').fill('按 Enter 发送');
    await page.evaluate(() => {
      window.__composerSubmitCount = 0;
      window.__captureComposerSubmit = event => {
        window.__composerSubmitCount += 1;
        event.preventDefault(); event.stopImmediatePropagation();
      };
      document.querySelector('#question-form').addEventListener(
        'submit', window.__captureComposerSubmit, {capture: true});
    });
    await page.locator('#question').press('Enter');
    assert.equal(await page.evaluate(() => window.__composerSubmitCount), 1,
      'Enter in the question box must submit');
    assert.equal(await page.locator('#question').inputValue(), '按 Enter 发送');
    await page.evaluate(() => document.querySelector('#question-form').removeEventListener(
      'submit', window.__captureComposerSubmit, {capture: true}));
    await page.locator('#question').fill('第一行');
    await page.locator('#question').press('Shift+Enter');
    assert.equal(await page.locator('#question').inputValue(), '第一行\n',
      'Shift+Enter must insert a newline');
    for (const region of ['sidebar', 'thread', 'inspector']) {
      assert.equal(await page.locator(`[data-region="${region}"]`).count(), 1);
    }
    assert.equal(await page.locator('#session-create-fields').isVisible(), false);
    await page.locator('#session-create-toggle').click();
    assert.equal(await page.locator('#session-create-toggle').getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('#session-create-fields').isVisible(), true);
    await page.locator('#session-create-toggle').click();
    assert.equal(await page.locator('#agent-list [data-agent-id]').count(), 3);
    assert.equal(await page.locator('#session-list [data-session-id]').count(), 20);
    assert.match(await page.locator('#session-list [data-session-id]').first().innerText(), /1970/);
    assert.equal(await page.locator('#session-load-more').isVisible(), true);
    await page.locator('#session-load-more').click();
    await page.waitForFunction(() => document.querySelectorAll('#session-list [data-session-id]').length === 22);
    assert(requests.some(item => item.path === '/api/sessions' && item.cursor === 'sessions-2'));
    const transcriptCards = page.locator('#session-history article[data-run-id]');
    assert.equal(await transcriptCards.count(), 20,
      'the first run page must render as one transcript card per run');
    assert.equal(await transcriptCards.first().getAttribute('data-run-id'), 'run-20');
    assert.equal(await transcriptCards.last().getAttribute('data-run-id'), 'run-01',
      'transcript cards must read from oldest to newest');
    for (const index of [17, 18, 19]) {
      await transcriptCards.nth(index).scrollIntoViewIfNeeded();
    }
    await page.waitForFunction(() => [...document.querySelectorAll(
      '#session-history article[data-run-id]')].slice(-3).every(card =>
      card.textContent.includes('问题') && card.textContent.includes('回答')));
    const newestTranscript = await transcriptCards.last().innerText();
    assert.match(newestTranscript, /第 1 轮问题/);
    assert.match(newestTranscript, /回答 第 1 轮问题/);
    assert.equal(await page.locator('#run-load-more').isVisible(), true);
    await page.locator('#run-load-more').scrollIntoViewIfNeeded();
    const anchorBefore = await page.evaluate(() => {
      const top = document.querySelector('#conversation-scroll').getBoundingClientRect().top;
      const cards = [...document.querySelectorAll('#session-history article[data-run-id]')];
      const card = cards.find(element => element.getBoundingClientRect().top >= top)
        || cards.find(element => element.getBoundingClientRect().bottom >= top);
      return {runId: card.dataset.runId, top: card.getBoundingClientRect().top};
    });
    await page.locator('#run-load-more').click();
    await page.waitForFunction(() => document.querySelectorAll(
      '#session-history article[data-run-id]').length === 22);
    await page.waitForTimeout(100);
    const anchorAfter = await page.locator(
      `#session-history article[data-run-id="${anchorBefore.runId}"]`)
      .evaluate(element => element.getBoundingClientRect().top);
    assert(Math.abs(anchorAfter - anchorBefore.top) <= 2,
      `loading older runs must preserve the visible anchor: before=${anchorBefore.top}, after=${anchorAfter}`);
    assert.equal(await transcriptCards.first().getAttribute('data-run-id'), 'run-22');
    assert.equal(await transcriptCards.last().getAttribute('data-run-id'), 'run-01');
    assert(requests.some(item => item.path.endsWith('/runs') && item.cursor === 'runs-2'));
    assert.match(await page.locator('#active-capabilities').innerText(), /read_file/);
    assert.equal(await transcriptCards.last().getAttribute('aria-current'), 'true');
    const inspectedCard = page.locator('#session-history article[data-run-id="run-02"]');
    await inspectedCard.click();
    await page.waitForFunction(() => document.querySelector(
      '#session-history article[data-run-id="run-02"]')?.textContent.includes('回答 第 2 轮问题'));
    assert.equal(await inspectedCard.getAttribute('aria-current'), 'true');
    assert.equal(await transcriptCards.count(), 22, 'inspecting one run must keep the full transcript');
    for (const id of ['scope-summary', 'artifacts', 'evidence', 'trace']) {
      assert.equal(await page.locator('#' + id).evaluate(element =>
        Boolean(element.closest('[data-region="inspector"]'))), true, `${id} must live in inspector`);
    }
    assert.equal(await page.locator('#trace').isVisible(), true);
    await page.locator('#session-select').selectOption('');
    await page.waitForFunction(() => document.querySelector('#output').hidden);
    assert.equal(await page.locator('#trace').isVisible(), false);
    await page.locator('#file').evaluate(element => { element.value = ''; });
    await page.locator('#question').fill('缺少资料范围时给出提示');
    await page.locator('#question').press('Enter');
    await page.waitForFunction(() => document.querySelector('.composer-settings').open);
    assert.match(await page.locator('#form-error').innerText(), /选择.*文件/);
    await page.waitForFunction(() => document.activeElement?.id === 'file');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'file');
    await page.locator('#file').fill('demo-note.md');
    await page.locator('.composer-settings summary').click();
    await page.evaluate(() => {
      window.__nativeIntersectionObserver = window.IntersectionObserver;
      window.IntersectionObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      };
    });
    const run03Gate = gateRunDetail('run-03'), run04Gate = gateRunDetail('run-04');
    releaseRun03 = run03Gate.release; releaseRun04 = run04Gate.release;
    await page.locator('#session-select').selectOption(sessions[0].id);
    await page.waitForFunction(() => document.querySelectorAll(
      '#session-history article[data-run-id]').length === 20);
    const run03Card = page.locator('#session-history article[data-run-id="run-03"]');
    const run04Card = page.locator('#session-history article[data-run-id="run-04"]');
    assert.equal(await run04Card.getAttribute('role'), 'button');
    await run03Card.click();
    await run04Card.click();
    await Promise.all([run03Gate.requested, run04Gate.requested]);
    releaseRun04();
    await page.waitForFunction(() => document.querySelector(
      '#session-history article[data-run-id="run-04"]')?.textContent.includes('回答 第 4 轮问题'));
    assert.equal(await run04Card.getAttribute('aria-current'), 'true');
    assert.equal(await page.locator('#trace-path').textContent(), 'trace-run-04');
    releaseRun03();
    await page.waitForFunction(() => document.querySelector(
      '#session-history article[data-run-id="run-03"]')?.textContent.includes('回答 第 3 轮问题'));
    assert.equal(await run04Card.getAttribute('aria-current'), 'true',
      'an earlier detail response must not replace the last inspected run');
    assert.equal(await page.locator('#trace-path').textContent(), 'trace-run-04');
    failRun05Once = true;
    const run05Card = page.locator('#session-history article[data-run-id="run-05"]');
    await run05Card.click();
    await run05Card.locator('.run-detail-retry').waitFor({state: 'visible'});
    assert.match(await run05Card.innerText(), /暂时无法载入这轮/);
    assert.equal(await transcriptCards.count(), 20, 'one failed detail must keep the rest of the transcript');
    await run04Card.focus();
    await page.keyboard.press('Enter');
    assert.equal(await run04Card.getAttribute('aria-current'), 'true',
      'Enter on a run card must inspect that run');
    await page.evaluate(() => {
      const retry = document.querySelector(
        '#session-history article[data-run-id="run-05"] .run-detail-retry');
      retry.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
      retry.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
      retry.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    });
    await page.waitForFunction(() => document.querySelector(
      '#session-history article[data-run-id="run-05"]')?.textContent.includes('回答 第 5 轮问题'));
    assert.equal(await run04Card.getAttribute('aria-current'), 'true',
      'keyboard retry inside a card must not inspect that card');
    assert.equal(requests.filter(item => item.method === 'GET'
      && item.path === '/api/sessions/session-01/runs/run-05').length, 2,
    'an initial failure plus repeated retry input must issue only one retry GET');
    runs.find(item => item.id === 'run-06').state = 'interrupted';
    await page.locator('#session-history article[data-run-id="run-06"]').click();
    await page.locator('#continue-run').waitFor({state: 'visible'});
    assert.match(await page.locator(
      '#session-history article[data-run-id="run-06"]').innerText(), /已中断/);
    await page.locator('#continue-run').click();
    await page.waitForFunction(() => document.querySelector(
      '#session-history article[data-run-id="continued-run"]')?.textContent.includes('中断任务已继续完成'));
    const continueRequest = requests.find(item => item.method === 'POST'
      && item.path === '/api/sessions/session-01/continue');
    assert.equal(continueRequest?.body?.run_id, 'run-06',
      'continue must post the inspected interrupted run as its parent');
    await run04Card.click();
    assert.equal(await page.locator('#continue-run').isVisible(), false,
      'continuation must follow the inspected interrupted run only');
    await page.evaluate(() => {
      window.IntersectionObserver = window.__nativeIntersectionObserver;
      delete window.__nativeIntersectionObserver;
    });

    delayNextRunPage = true;
    await page.locator('#run-load-more').click();
    await page.waitForFunction(() => document.querySelector('#run-load-more').disabled);
    await page.locator('[data-session-id="session-02"]').click();
    await page.waitForFunction(() => document.querySelector('[data-session-id="session-02"]')
      ?.getAttribute('aria-current') === 'true');
    assert.equal(await page.locator('#run-load-more').isDisabled(), false,
      'an old run page must not disable the new session page');
    releaseRunPage();

    await page.locator('[data-agent-id="file-qa"]').click();
    await page.waitForFunction(() => {
      const button = document.querySelector('#session-load-more');
      return button && !button.hidden && !button.disabled
        && document.querySelectorAll('#session-list [data-session-id]').length === 20;
    });
    delayNextSessionPage = true;
    await page.locator('#session-load-more').click();
    await page.waitForFunction(() => document.querySelector('#session-load-more').disabled);
    await page.locator('[data-agent-id="directory-qa"]').click();
    await page.waitForFunction(() => document.querySelector('#session-list [data-session-id] strong')
      ?.textContent.includes('directory-qa'));
    assert.equal(await page.locator('#session-load-more').isDisabled(), false,
      'an old session page must not disable the new agent page');
    releaseSessionPage();

    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.locator('.composer-settings').evaluate(element => element.open), false);
    assert.equal(await page.locator('#sidebar-panel').evaluate(element => element.inert), true);
    assert.equal(await page.locator('#inspector-panel').evaluate(element => element.inert), true);
    await page.locator('#sidebar-toggle').focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'inspector-toggle',
      'Tab must skip both hidden drawers');
    assert(Number.parseFloat(await page.locator('.transcript-answer').first().evaluate(element =>
      getComputedStyle(element).fontSize)) >= 16);
    for (const selector of ['.suggestions button', '#session-history article[data-run-id]']) {
      assert((await page.locator(selector).first().boundingBox()).height >= 44,
        `${selector} must remain a 44px touch target`);
    }
    await page.locator('#sidebar-toggle').click();
    assert.equal(await page.locator('body').getAttribute('data-drawer'), 'sidebar');
    assert.equal(await page.locator('#sidebar-toggle').getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('#sidebar-panel').evaluate(element => element.inert), false);
    assert.equal(await page.locator('#inspector-panel').evaluate(element => element.inert), true);
    assert.equal(await page.locator('#workspace-backdrop').isVisible(), true);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('body').getAttribute('data-drawer'), null);
    assert.equal(await page.locator('#sidebar-toggle').getAttribute('aria-expanded'), 'false');
    assert.equal(await page.locator('#sidebar-panel').evaluate(element => element.inert), true);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'sidebar-toggle');
    await page.locator('#inspector-toggle').click();
    assert.equal(await page.locator('body').getAttribute('data-drawer'), 'inspector');
    assert.equal(await page.locator('#inspector-panel').evaluate(element => element.inert), false);
    await page.waitForTimeout(250);
    await page.locator('#inspector-close').click();
    assert.equal(await page.locator('body').getAttribute('data-drawer'), null);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));

    await page.locator('.composer-settings summary').click();
    await page.locator('#output-file').fill('generated.md');
    await page.locator('#question').fill('请生成一份报告');
    await page.locator('#start').click();
    await page.locator('#approval').waitFor({state: 'visible'});
    assert.equal(await page.locator('#thread-panel').getAttribute('aria-busy'), 'true');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'approval-title');
    assert((await page.locator('#approval-allow').boundingBox()).height >= 44);
    const oldRunCard = page.locator('#session-history article[data-run-id="run-02"]');
    await oldRunCard.click();
    await page.waitForFunction(() => document.querySelector(
      '#session-history article[data-run-id="run-02"]')?.getAttribute('aria-current') === 'true');
    assert.equal(await page.locator('#approval').isVisible(), true,
      'inspecting an old run must keep the live approval visible');
    assert.equal(await page.locator('#approval-actions').isVisible(), true);
    await page.locator('#approval-allow').click();
    await page.waitForFunction(() => document.querySelector('#thread-panel')?.getAttribute('aria-busy') === 'false');
    assert(requests.some(item => item.method === 'POST'
      && item.path === '/api/sessions/session-01/runs/approval-run/approvals/approval-1'),
    'approval must remain bound to the live approval-run while an old run is inspected');
    await page.locator('#start').click();
    await page.locator('#approval').waitFor({state: 'visible'});
    await oldRunCard.click();
    await page.locator('#cancel').click();
    await page.waitForFunction(() => document.querySelector('#thread-panel')?.getAttribute('aria-busy') === 'false');
    assert(requests.some(item => item.method === 'POST'
      && item.path === '/api/sessions/session-01/runs/approval-run/cancel'),
    'cancel must remain bound to the live approval-run while an old run is inspected');
    assert.deepEqual(pageErrors, []);
    console.log('PASS: workbench navigation, scoped pagination races, responsive inert drawers, result/approval focus');
  } finally {
    releaseRunPage(); releaseSessionPage(); releaseRun03(); releaseRun04(); await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
