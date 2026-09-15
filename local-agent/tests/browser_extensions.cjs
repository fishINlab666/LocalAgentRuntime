/* Actual static page with synthetic HTTP responses; no server or cloud model calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');

const budgets = {max_steps: 6, run_timeout: 120, model_timeout: 45, tool_timeout: 5,
  max_tool_calls: 4, max_input_bytes: 65536, max_files: 7, max_file_bytes: 16384};
const template = {schema_version: 1, id: 'project', name: '项目助手', strategy: 'directory',
  instructions: '按来源核对项目', model: {provider: 'deepseek', name: 'deepseek-chat',
    api_key_env: 'DEEPSEEK_API_KEY', use_system_proxy: false},
  tools: ['read_file', 'list_files', 'write_file', 'session_history'], budgets,
  approval: 'ask_writes', skills: [{id: 'skill-one', version: 'a'.repeat(64)}],
  mcp: [{id: 'status', version: 'c'.repeat(64), tools: ['status'], resources: [], prompts: ['weekly']}],
  revision: 'r1', enabled: true};
const agents = [template, {...structuredClone(template), id: 'file-qa', name: '文件助手',
  strategy: 'file', skills: [], mcp: []}];
const capabilities = {skills: [{id: 'skill-one', name: '第一方法', version: 'a'.repeat(64),
  description: '<img src=x onerror=alert(1)>', enabled: true},
  {id: 'skill-two', name: '第二方法', version: 'b'.repeat(64), enabled: true,
    status: 'unavailable', missing: ['tool:missing']}],
  servers: [{id: 'status', version: 'c'.repeat(64), enabled: true, allowed_tools: ['status'],
    allowed_resources: [], allowed_prompts: ['weekly']}]};
const sessions = [], requests = [], pageErrors = [];
const clone = value => JSON.parse(JSON.stringify(value));

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport: {width: 1280, height: 900}});
  page.setDefaultTimeout(5000);
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.route('http://extensions.test/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const body = request.postDataJSON();
    requests.push({path, body, method: request.method(), headers: request.headers()});
    const send = value => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(value)});
    if (path === '/' || path === '/app.js' || path === '/app.css') {
      const file = path === '/' ? 'index.html' : path.slice(1);
      return route.fulfill({status: 200, contentType: file.endsWith('.js') ? 'text/javascript'
        : file.endsWith('.css') ? 'text/css' : 'text/html',
        body: fs.readFileSync('local_agent/static/' + file, 'utf8')});
    }
    if (path === '/api/config') return send({ready: true, workspace: '/synthetic', provider: {simulated: true}});
    if (path === '/api/agents' && request.method() === 'GET') return send({agents});
    if (path === '/api/agents' && body) {
      const agent = {...body, revision: 'created', enabled: true}; agents.push(agent); return send({agent});
    }
    if (path === '/api/capabilities') return send(capabilities);
    if (path === '/api/sessions' && request.method() === 'GET') return send({sessions: url.search ? []
      : sessions.filter(item => !request.headers()['x-agent-id'] || item.agent_id === request.headers()['x-agent-id'])});
    if (path === '/api/sessions' && body) {
      const selected = agents.find(item => item.id === body.agent_id);
      const session = {id: 's' + (sessions.length + 1), title: body.title, scope: body.scope,
        agent_id: selected.id, agent_revision: selected.revision, agent_snapshot: clone(selected), status: 'active'};
      sessions.push(session); return send({session});
    }
    if (/^\/api\/sessions\/[^/]+$/.test(path)) return send({session: sessions.find(item => path.endsWith(item.id))});
    if (path.endsWith('/runs') && request.method() === 'GET') return send({runs: []});
    if (path.endsWith('/runs') && body) return send({run: {id: 'run1', question: body.question,
      task_type: 'files', state: 'completed', result: {answer: {status: 'answered', answer: '状态正常',
        citations: [{source_id: 'source-1', source: {type: 'mcp', label: '<img src=x>状态服务'},
          quote: '<script>window.injected = true</script>', start_line: 1, end_line: 1}]}, artifacts: []}}});
    if (path.endsWith('/bind')) {
      template.skills.push({id: body.id, version: body.version}); template.revision = 'r2';
      return send({agent: template});
    }
    if (path.endsWith('/probe')) return send({catalog: {tools: [{name: 'status'}], resources: [], prompts: [{name: 'weekly'}]}});
    if (path.endsWith('/enabled')) {
      const id = path.split('/').at(-2), entries = [...agents, ...capabilities.skills, ...capabilities.servers];
      entries.filter(item => item.id === id).forEach(item => { item.enabled = body.enabled; });
      return send({ok: true});
    }
    if (path.endsWith('/install')) return send({ok: true});
    return route.fulfill({status: 404, contentType: 'application/json', body: '{"error":"NOT_FOUND"}'});
  });
  try {
    await page.goto('http://extensions.test/', {waitUntil: 'networkidle'});
    assert.equal(await page.evaluate(() => document.activeElement.id), 'question');
    for (const id of ['agent-select', 'agent-create', 'skill-install', 'mcp-install', 'run-skill', 'run-prompt']) {
      assert.equal(await page.locator('#' + id).count(), 1, 'Missing extension control: ' + id);
    }
    assert.equal(await page.locator('#agent-select').inputValue(), '');
    await page.locator('#agent-select').selectOption('project');
    await page.waitForFunction(() => document.querySelector('#session-mode').value === 'directory');
    assert.equal(await page.locator('#discover').isDisabled(), true);
    assert.match(await page.locator('#discover-help').textContent(), /7 份/);
    assert.match(await page.locator('#file-help').textContent(), /16 KiB/);
    await page.locator('#session-create-toggle').click();
    await page.locator('#session-name').fill('旧版本会话');
    await page.locator('#session-create').click();
    await page.waitForFunction(() => document.querySelector('#session-select').value === 's1');
    const creation = requests.find(item => item.path === '/api/sessions' && item.body);
    assert.equal(creation.body.agent_id, 'project');
    assert.equal(creation.headers['x-agent-id'], 'project');
    assert.equal(creation.body.scope.mode, 'directory');
    await page.locator('#capability-management').evaluate(element => { element.open = true; });
    assert.equal(await page.locator('#capability-list img').count(), 0);
    assert.match(await page.locator('#capability-list').innerText(), /tool:missing/);
    await page.locator('[data-bind-kind="skill"][data-capability-id="skill-two"]').click();
    await page.waitForFunction(() => document.querySelector('#extension-status').textContent.includes('旧会话'));
    assert.equal(await page.locator('#run-skill option[value="skill-two"]').count(), 0,
      'new binding must not enter existing session snapshot');
    assert.match(await page.locator('#session-scope').innerText(), /r1/);
    await page.locator('.composer-settings summary').click();
    await page.locator('#run-skill').selectOption('skill-one');
    const promptValue = await page.locator('#run-prompt option').last().getAttribute('value');
    await page.locator('#run-prompt').selectOption(promptValue);
    await page.locator('#question').fill('查当前状态');
    await page.locator('#start').click();
    await page.waitForFunction(() => document.querySelector('#run-status').textContent === '回答已完成');
    const run = requests.find(item => item.path.endsWith('/runs') && item.body);
    assert.equal(run.body.skill_id, 'skill-one');
    assert.deepEqual(run.body.mcp_prompt, {server_id: 'status', name: 'weekly'});
    assert.match(await page.locator('#citations').innerText(), /状态服务/);
    assert.equal(await page.locator('#citations img, #citations script').count(), 0);
    await page.locator('[data-probe-id="status"]').click();
    await page.waitForFunction(() => document.querySelector('#extension-status').textContent.includes('连接'));
    await page.locator('#skill-path').fill('/local/package');
    await page.locator('#skill-install').click();
    await page.waitForFunction(() => document.querySelector('#extension-status').textContent.includes('导入'));
    assert(requests.some(item => item.path === '/api/skills/install' && item.body.path === '/local/package'));
    await page.locator('#agent-select').selectOption('file-qa');
    await page.waitForFunction(() => document.querySelector('#session-select').value === '');
    assert.equal(await page.locator('#output').isVisible(), false);
    assert.equal(await page.locator('#session-history button').count(), 0);
    assert.equal(await page.locator('#session-mode').inputValue(), 'file');
    await page.locator('#agent-template').selectOption('file-qa');
    await page.locator('#agent-id').fill('new-agent');
    await page.locator('#agent-name').fill('新助手');
    await page.locator('#agent-model').fill('deepseek-chat');
    await page.locator('#agent-instructions').fill('简明回答');
    await page.locator('#agent-create').click();
    await page.waitForFunction(() => document.querySelector('#agent-select').value === 'new-agent');
    const saved = requests.find(item => item.path === '/api/agents' && item.body).body;
    assert.equal(saved.id, 'new-agent');
    assert.equal(saved.revision, undefined); assert.equal(saved.enabled, undefined);
    assert.equal(saved.model.api_key_env, 'DEEPSEEK_API_KEY');
    for (const width of [768, 375]) {
      await page.setViewportSize({width, height: 900});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    }
    assert.deepEqual(pageErrors, []);
    console.log('PASS: assistant headers/scope, snapshot-bound choices, local capability actions, template creation, safe sources, responsive page; model calls 0');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
