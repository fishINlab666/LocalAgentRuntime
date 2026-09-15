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
  agent('directory-qa', '目录问答', 'directory', ['list_files', 'read_file', 'session_history']),
  agent('project-brief', '项目简报', 'directory', ['list_files', 'read_file', 'write_file', 'session_history']),
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

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.route('http://workbench.test/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    requests.push({path, cursor: url.searchParams.get('cursor'), archived: url.searchParams.get('archived')});
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
      workspace_available: true, provider: {simulated: true}});
    if (path === '/api/agents') return send({agents});
    if (path === '/api/capabilities') return send({skills: [], servers: []});
    if (path === '/api/sessions') {
      if (url.searchParams.get('archived') === '1') return send({sessions: [], next_cursor: null});
      return url.searchParams.get('cursor') === 'sessions-2'
        ? send({sessions: sessions.slice(20), next_cursor: null})
        : send({sessions: sessions.slice(0, 20), next_cursor: 'sessions-2'});
    }
    const sessionMatch = path.match(/^\/api\/sessions\/([^/]+)$/);
    if (sessionMatch) return send({session: sessions.find(item => item.id === sessionMatch[1])});
    const runListMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs$/);
    if (runListMatch) {
      return url.searchParams.get('cursor') === 'runs-2'
        ? send({runs: runs.slice(20), next_cursor: null})
        : send({runs: runs.slice(0, 20), next_cursor: 'runs-2'});
    }
    const runMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs\/([^/]+)$/);
    if (runMatch) {
      const item = runs.find(value => value.id === runMatch[2]);
      return send({run: {...item, events: [], approvals: [], artifacts: [],
        result: {state: 'completed', stop_reason: 'ANSWERED', model_calls: 2,
          answer: {status: 'answered', answer: `回答 ${item.question}`, citations: []},
          artifacts: []}}});
    }
    return route.fulfill({status: 404, contentType: 'application/json',
      body: JSON.stringify({error: 'NOT_FOUND'})});
  });
  try {
    await page.goto('http://workbench.test/', {waitUntil: 'networkidle'});
    assert.equal(await page.title(), 'Local Agent 工作台');
    for (const region of ['sidebar', 'thread', 'inspector']) {
      assert.equal(await page.locator(`[data-region="${region}"]`).count(), 1);
    }
    assert.equal(await page.locator('#agent-list [data-agent-id]').count(), 3);
    assert.equal(await page.locator('#session-list [data-session-id]').count(), 20);
    assert.equal(await page.locator('#session-load-more').isVisible(), true);
    await page.locator('#session-load-more').click();
    await page.waitForFunction(() => document.querySelectorAll('#session-list [data-session-id]').length === 22);
    assert(requests.some(item => item.path === '/api/sessions' && item.cursor === 'sessions-2'));
    assert.equal(await page.locator('#session-history button').count(), 20);
    assert.equal(await page.locator('#run-load-more').isVisible(), true);
    await page.locator('#run-load-more').click();
    await page.waitForFunction(() => document.querySelectorAll('#session-history button').length === 22);
    assert(requests.some(item => item.path.endsWith('/runs') && item.cursor === 'runs-2'));
    assert.match(await page.locator('#active-capabilities').innerText(), /read_file/);
    assert.deepEqual(pageErrors, []);
    console.log('PASS: workbench shell, assistant/session navigation, signed-cursor pagination');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
