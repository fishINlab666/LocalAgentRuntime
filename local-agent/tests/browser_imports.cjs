/* Actual static import page with intercepted local HTTP; never calls a model. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

const budgets = {max_steps: 6, run_timeout: 120, model_timeout: 45,
  tool_timeout: 5, max_tool_calls: 4, max_input_bytes: 65536,
  max_files: 16, max_file_bytes: 32768};
const agent = (id, name, strategy, tools, enabled = true) => ({
  id, name, strategy, tools, budgets, enabled, revision: `${id}-revision`,
  instructions: name, model: {provider: 'deepseek', name: 'deepseek-chat'},
  skills: [], mcp: [],
});
const agents = [
  agent('file-qa', '单文件问答', 'file', ['read_file', 'session_history']),
  agent('directory-qa', '目录问答', 'directory',
    ['list_files', 'read_file', 'search_documents', 'session_history']),
  agent('project-brief', '项目简报', 'combined',
    ['list_files', 'read_file', 'search_documents', 'session_history']),
  agent('legacy-directory', '旧目录助手', 'directory',
    ['list_files', 'read_file', 'session_history']),
  agent('disabled-import', '已停用导入助手', 'directory',
    ['list_files', 'read_file', 'search_documents'], false),
];
const staticRoot = path.join(__dirname, '..', 'local_agent', 'static');
const formats = {'.md': true, '.txt': true, '.pdf': true, '.docx': true};
const imports = new Map();
const sessions = [];
const observed = {begins: [], uploads: [], completes: [], cancels: [], failed: []};
let importNumber = 0, activeUploads = 0, maxActiveUploads = 0;
let holdNextUpload = false, heldUploadStarted = false, releaseHeldUpload = () => {};
let heldUploadGate = Promise.resolve();
let holdNextPoll = false, heldPollStarted = false, releaseHeldPoll = () => {};
let heldPollGate = Promise.resolve();
let failNextSessionDetail = false, omitNextReadySession = false;
let selectedSessionId = null, failSessionLists = false;

function resetUploadGate() {
  heldUploadStarted = false;
  heldUploadGate = new Promise(resolve => { releaseHeldUpload = resolve; });
}
function resetPollGate() {
  heldPollStarted = false;
  heldPollGate = new Promise(resolve => { releaseHeldPoll = resolve; });
}
async function waitFor(check, label, timeout = 4000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (check()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail(`timed out waiting for ${label}`);
}
function extension(logicalPath) {
  return path.posix.extname(logicalPath).toLowerCase();
}
function snapshot(batch, status = batch.status) {
  return {id: batch.id, status, files: batch.files, job_id: batch.jobId || null,
    session_id: batch.sessionId || null, error_code: null,
    total_files: batch.files.length,
    stored_files: batch.files.filter(item => item.upload_state === 'stored').length};
}
function sessionFor(batch) {
  return {id: batch.sessionId, title: batch.metadata.name,
    scope: {mode: 'directory'}, status: 'active', revision: 1,
    created_at: 1, updated_at: 1, agent_id: batch.metadata.agent_id,
    agent_revision: `${batch.metadata.agent_id}-revision`,
    agent_snapshot: agents.find(item => item.id === batch.metadata.agent_id),
    import: {id: batch.id, files: batch.metadata.files.map(item => ({
      logical_path: item.logical_path, format: extension(item.logical_path).slice(1),
      bytes: item.bytes, sha256: 'a'.repeat(64),
      stats: {source_bytes: item.bytes, extracted_bytes: item.bytes, unit_count: 1},
      warnings: [], chunks: 1,
    }))}};
}
function publishSession(batch, forcedId = null) {
  batch.status = 'ready';
  batch.sessionId = forcedId || `session-${batch.id}`;
  if (!sessions.some(item => item.id === batch.sessionId)) sessions.push(sessionFor(batch));
}
function json(route, value, status = 200) {
  return route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
}

async function handleRoute(route) {
  const request = route.request();
  const url = new URL(request.url()), pathname = url.pathname;
  if (['/', '/app.js', '/app.css'].includes(pathname)) {
    const file = pathname === '/' ? 'index.html' : pathname.slice(1);
    const contentType = file.endsWith('.js') ? 'text/javascript'
      : file.endsWith('.css') ? 'text/css' : 'text/html';
    return route.fulfill({status: 200, contentType,
      body: fs.readFileSync(path.join(staticRoot, file), 'utf8')});
  }
  if (pathname === '/api/config') return json(route, {ready: true,
    workspace: '/synthetic', workspace_available: true,
    provider: {simulated: true}, selected_session_id: selectedSessionId,
    imports: {formats: Object.entries(formats).map(([extension, available]) => (
      {extension, available})), limits: {max_files: 50,
      max_file_bytes: 20 * 1024 * 1024, max_total_bytes: 100 * 1024 * 1024}}});
  if (pathname === '/api/agents') return json(route, {agents});
  if (pathname === '/api/capabilities') return json(route, {skills: [], servers: []});
  if (pathname === '/api/sessions') {
    if (failSessionLists) return json(route, {error: 'NETWORK_ERROR'}, 503);
    return json(route, {sessions: url.searchParams.get('archived') === '1' ? [] : sessions,
      next_cursor: null});
  }
  const sessionMatch = pathname.match(/^\/api\/sessions\/([^/]+)$/);
  if (sessionMatch) {
    if (failNextSessionDetail) {
      failNextSessionDetail = false;
      return json(route, {error: 'NETWORK_ERROR'}, 503);
    }
    const session = sessions.find(item => item.id === sessionMatch[1]);
    return session ? json(route, {session}) : json(route, {error: 'NOT_FOUND'}, 404);
  }
  if (/^\/api\/sessions\/[^/]+\/runs$/.test(pathname)) {
    return json(route, {runs: [], next_cursor: null});
  }
  if (pathname === '/api/imports' && request.method() === 'POST') {
    const metadata = request.postDataJSON();
    const id = `import-${++importNumber}`;
    const batch = {id, metadata, status: 'uploading', pollCount: 0,
      files: metadata.files.map((item, index) => ({slot_id: `slot-${importNumber}-${index}`,
        logical_path: item.logical_path, extension: extension(item.logical_path),
        declared_bytes: item.bytes, upload_state: 'pending'}))};
    imports.set(id, batch); observed.begins.push({id, metadata,
      agent: request.headers()['x-agent-id']});
    return json(route, snapshot(batch), 201);
  }
  const fileMatch = pathname.match(/^\/api\/imports\/([^/]+)\/files\/([^/]+)$/);
  if (fileMatch && request.method() === 'POST') {
    const batch = imports.get(fileMatch[1]);
    const slot = batch.files.find(item => item.slot_id === fileMatch[2]);
    const body = request.postDataBuffer();
    const record = {importId: batch.id, slot: slot.slot_id,
      logicalPath: slot.logical_path, contentType: request.headers()['content-type'],
      body: Buffer.from(body || [])};
    observed.uploads.push(record); activeUploads++;
    maxActiveUploads = Math.max(maxActiveUploads, activeUploads);
    try {
      if (holdNextUpload) {
        holdNextUpload = false; heldUploadStarted = true; await heldUploadGate;
      } else {
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      slot.upload_state = 'stored';
      return await json(route, {import_id: batch.id, source_id: slot.slot_id,
        bytes: record.body.length, sha256: 'b'.repeat(64)});
    } catch (_) {
      return undefined;
    } finally { activeUploads--; }
  }
  const completeMatch = pathname.match(/^\/api\/imports\/([^/]+)\/complete$/);
  if (completeMatch && request.method() === 'POST') {
    const batch = imports.get(completeMatch[1]);
    batch.status = 'finalizing'; batch.jobId ||= `job-${batch.id}`;
    observed.completes.push(batch.id);
    return json(route, snapshot(batch), 202);
  }
  const cancelMatch = pathname.match(/^\/api\/imports\/([^/]+)\/cancel$/);
  if (cancelMatch && request.method() === 'POST') {
    const batch = imports.get(cancelMatch[1]);
    batch.status = 'cancelled'; observed.cancels.push(batch.id);
    return json(route, snapshot(batch));
  }
  const importMatch = pathname.match(/^\/api\/imports\/([^/]+)$/);
  if (importMatch && request.method() === 'GET') {
    const batch = imports.get(importMatch[1]);
    if (holdNextPoll) {
      holdNextPoll = false; heldPollStarted = true; await heldPollGate;
      publishSession(batch, `stale-${batch.id}`);
      return json(route, snapshot(batch));
    }
    if (batch.pollCount++ === 0) return json(route, snapshot(batch, 'finalizing'));
    publishSession(batch);
    if (omitNextReadySession) {
      omitNextReadySession = false;
      return json(route, {...snapshot(batch), session_id: null});
    }
    return json(route, snapshot(batch));
  }
  return json(route, {error: 'NOT_FOUND'}, 404);
}

const filePayloads = [
  {name: '说明.md', mimeType: 'text/markdown', buffer: Buffer.from('# 项目\n')},
  {name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('budget 42\n')},
  {name: 'two.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-test')},
  {name: 'brief.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    buffer: Buffer.from('docx-test')},
  {name: '<img src=x onerror=window.importInjected=true>.png', mimeType: 'image/png',
    buffer: Buffer.from('png')},
];

async function openImport(page) {
  if (!await page.locator('#import-files').isVisible()) {
    await page.locator('#import-trigger').click();
    await page.locator('#import-files').waitFor({state: 'visible'});
  }
}
async function captureComposerKeys(page) {
  await page.locator('#question').fill('按 Enter 发送');
  await page.evaluate(() => {
    window.__importSubmitCount = 0;
    window.__captureImportSubmit = event => {
      window.__importSubmitCount++;
      event.preventDefault(); event.stopImmediatePropagation();
    };
    document.querySelector('#question-form').addEventListener(
      'submit', window.__captureImportSubmit, {capture: true});
  });
  await page.locator('#question').press('Enter');
  assert.equal(await page.evaluate(() => window.__importSubmitCount), 1);
  await page.locator('#question').fill('第一行');
  await page.locator('#question').press('Shift+Enter');
  assert.equal(await page.locator('#question').inputValue(), '第一行\n');
  await page.evaluate(() => document.querySelector('#question-form').removeEventListener(
    'submit', window.__captureImportSubmit, {capture: true}));
}

(async () => {
  const directoryRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-import-ui-'));
  const chosenDirectory = path.join(directoryRoot, '项目资料');
  fs.mkdirSync(path.join(chosenDirectory, '子目录'), {recursive: true});
  fs.writeFileSync(path.join(chosenDirectory, 'README.md'), '# root\n');
  fs.writeFileSync(path.join(chosenDirectory, '子目录', 'facts.txt'), 'fact\n');
  fs.writeFileSync(path.join(chosenDirectory, 'ignore.png'), 'png');
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const context = await browser.newContext({viewport: {width: 1440, height: 900}});
  await context.route('http://imports.test/**', handleRoute);
  const page = await context.newPage();
  const pageErrors = [], failedRequests = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('requestfailed', request => failedRequests.push(new URL(request.url()).pathname));
  try {
    await page.goto('http://imports.test/', {waitUntil: 'networkidle'});
    assert.equal(await page.locator('#import-trigger').count(), 1,
      'the workbench must expose the document import trigger');
    await openImport(page);
    assert.deepEqual(await page.locator('#import-agent option').evaluateAll(options =>
      options.map(option => option.value)), ['directory-qa', 'project-brief']);

    await page.locator('#import-files').setInputFiles(filePayloads);
    assert.equal(await page.locator('#import-supported-count').textContent(), '4');
    assert.equal(await page.locator('#import-ignored-count').textContent(), '1');
    assert.equal(await page.locator('#import-ignored-list img').count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.importInjected)), false);

    await page.locator('#import-directory').setInputFiles(chosenDirectory);
    assert.equal(await page.locator('#import-supported-count').textContent(), '2');
    assert.equal(await page.locator('#import-ignored-count').textContent(), '1');
    const folderPaths = await page.locator('#import-directory').evaluate(input =>
      [...input.files].map(file => file.webkitRelativePath));
    assert(folderPaths.some(value => value.endsWith('/子目录/facts.txt')));
    resetUploadGate(); holdNextUpload = true;
    await page.locator('#import-start').click();
    await waitFor(() => observed.begins.length === 1, 'folder begin');
    assert.equal(observed.begins[0].metadata.kind, 'folder');
    assert(observed.begins[0].metadata.files.some(item =>
      item.logical_path.endsWith('/子目录/facts.txt')));
    await waitFor(() => heldUploadStarted, 'held folder upload');
    await page.locator('#import-cancel').click();
    await waitFor(() => observed.cancels.length === 1, 'server-side cancel');
    releaseHeldUpload();
    await waitFor(() => failedRequests.some(value => value.includes('/files/')),
      'AbortController upload cancellation');
    await waitFor(() => activeUploads === 0, 'cancelled upload cleanup');
    assert.equal(observed.completes.includes(observed.begins[0].id), false);

    await page.locator('#import-files').setInputFiles(filePayloads);
    await page.evaluate(() => {
      window.__importStatuses = [];
      const status = document.querySelector('#import-status');
      new MutationObserver(() => window.__importStatuses.push(status.textContent))
        .observe(status, {childList: true, characterData: true, subtree: true});
    });
    await page.locator('#import-start').click();
    await page.waitForFunction(() => document.querySelector('#session-select')?.value
      .startsWith('session-import-'), null, {timeout: 5000});
    const happy = observed.begins.at(-1);
    const happyUploads = observed.uploads.filter(item => item.importId === happy.id);
    assert.equal(happy.metadata.kind, 'file');
    assert.equal(happy.metadata.files.length, 4);
    assert.equal(happy.metadata.ignored.length, 1);
    assert.deepEqual(happyUploads.map(item => item.contentType),
      Array(4).fill('application/octet-stream'));
    assert.deepEqual(happyUploads.map(item => item.logicalPath),
      happy.metadata.files.map(item => item.logical_path));
    assert.deepEqual(happyUploads.map(item => item.body), filePayloads.slice(0, 4).map(item => item.buffer));
    assert.equal(maxActiveUploads, 1, 'document bytes must upload one slot at a time');
    assert.equal(observed.completes.filter(id => id === happy.id).length, 1);
    assert.equal(await page.locator('#import-status').textContent(),
      '本地副本已保存 · 4 份资料');
    assert.equal(await page.locator('#import-sources').isVisible(), true);
    assert.match(await page.locator('#import-source-list').innerText(), /说明\.md.*SHA-256/s);
    const statuses = await page.evaluate(() => window.__importStatuses);
    assert(statuses.some(value => /1\s*\/\s*4/.test(value)), 'progress must show the first upload');
    assert(statuses.some(value => /4\s*\/\s*4/.test(value)), 'progress must show the final upload');
    await page.waitForFunction(() => document.activeElement?.id === 'question');
    assert.equal(await page.locator('#question').evaluate(node => node === document.activeElement), true);
    await captureComposerKeys(page);
    const completedBeforeReload = observed.completes.length;
    await page.reload({waitUntil: 'networkidle'});
    assert.equal(observed.completes.length, completedBeforeReload,
      'reload must not repeat a completed import');

    for (const width of [320, 375, 768, 1024, 1440]) {
      await page.setViewportSize({width, height: 812});
      if (width < 768 && await page.locator('body').getAttribute('data-drawer') !== 'sidebar') {
        await page.locator('#sidebar-toggle').click();
      }
      await openImport(page);
      const box = await page.locator('#import-start').boundingBox();
      assert(box && box.x >= 0 && box.x + box.width <= width + 1 && box.height >= 44,
        `import action must remain reachable at ${width}px`);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
        `import panel must not create horizontal overflow at ${width}px`);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#import-files').isVisible(), false,
        'closed import controls must not remain keyboard-visible');
    }

    formats['.pdf'] = false; formats['.docx'] = false;
    const unavailable = await context.newPage();
    await unavailable.goto('http://imports.test/', {waitUntil: 'networkidle'});
    await openImport(unavailable);
    await unavailable.locator('#import-files').setInputFiles(filePayloads.slice(2, 4));
    assert.equal(await unavailable.locator('#import-start').isDisabled(), true);
    const unavailableText = await unavailable.locator('body').innerText();
    assert.match(unavailableText, /PDF.*(?:未安装|不可用)/s);
    assert.match(unavailableText, /(?:Word|DOCX).*(?:未安装|不可用)/s);
    await unavailable.close(); formats['.pdf'] = true; formats['.docx'] = true;

    const recovery = await context.newPage();
    await recovery.goto('http://imports.test/', {waitUntil: 'networkidle'});
    await openImport(recovery);
    await recovery.locator('#import-files').setInputFiles([
      {name: 'recover.txt', mimeType: 'text/plain', buffer: Buffer.from('recover')},
    ]);
    failNextSessionDetail = true;
    const beginsBeforeRecovery = observed.begins.length;
    await recovery.locator('#import-start').click();
    await waitFor(() => observed.begins.length === beginsBeforeRecovery + 1,
      'recoverable import begin');
    const recoveryImport = observed.begins.at(-1).id;
    await recovery.locator('#import-error').filter({hasText: '会话列表刷新失败'}).waitFor();
    assert.equal(await recovery.locator('#import-dialog').evaluate(dialog => dialog.open), true);
    await recovery.reload({waitUntil: 'networkidle'});
    await recovery.waitForFunction(expected =>
      document.querySelector('#session-select')?.value === expected,
      `session-${recoveryImport}`);
    await recovery.locator('#import-dialog').waitFor({state: 'hidden'});
    await recovery.close();

    const malformed = await context.newPage();
    await malformed.goto('http://imports.test/', {waitUntil: 'networkidle'});
    await openImport(malformed);
    await malformed.locator('#import-files').setInputFiles([
      {name: 'missing-session.txt', mimeType: 'text/plain', buffer: Buffer.from('missing')},
    ]);
    omitNextReadySession = true;
    await malformed.locator('#import-start').click();
    await malformed.locator('#import-status').filter({hasText: '无法进入会话'}).waitFor();
    assert.equal(await malformed.locator('#import-start').isDisabled(), false,
      'malformed ready snapshot must not leave the page locked');
    await malformed.close();

    selectedSessionId = 'fixed-session'; failSessionLists = true;
    const fixed = await context.newPage();
    await fixed.addInitScript(key => sessionStorage.setItem(key, JSON.stringify({
      id: 'preserved-import', agentId: 'directory-qa', phase: 'finalizing',
    })), 'local-agent-active-import');
    await fixed.goto('http://imports.test/', {waitUntil: 'networkidle'});
    assert.equal(await fixed.locator('#import-trigger').isDisabled(), true,
      'fixed-session mode must stay locked when session lists fail');
    assert.equal(await fixed.locator('#import-dialog').evaluate(dialog => dialog.open), false);
    assert.match(await fixed.evaluate(() => sessionStorage.getItem('local-agent-active-import')),
      /preserved-import/);
    await fixed.close(); selectedSessionId = null; failSessionLists = false;

    const late = await context.newPage();
    await late.goto('http://imports.test/', {waitUntil: 'networkidle'});
    await openImport(late);
    await late.locator('#import-files').setInputFiles([
      {name: 'old.txt', mimeType: 'text/plain', buffer: Buffer.from('old')},
    ]);
    resetPollGate(); holdNextPoll = true;
    await late.locator('#import-start').click();
    await waitFor(() => heldPollStarted, 'old import poll');
    const oldImport = observed.begins.at(-1).id;
    await late.locator('#import-cancel').click();
    await waitFor(() => observed.cancels.includes(oldImport), 'old import cancellation');
    if (!await late.locator('#import-files').isVisible()) await openImport(late);
    await late.locator('#import-files').setInputFiles([
      {name: 'fresh.txt', mimeType: 'text/plain', buffer: Buffer.from('fresh')},
    ]);
    releaseHeldPoll();
    await late.waitForTimeout(350);
    assert.deepEqual(await late.locator('#import-files').evaluate(input =>
      [...input.files].map(file => file.name)), ['fresh.txt']);
    assert.equal(await late.locator('#import-supported-count').textContent(), '1');
    assert.notEqual(await late.locator('#session-select').inputValue(), `stale-${oldImport}`,
      'a late ready response must not select the old import session');

    assert.deepEqual(pageErrors, []);
    console.log('PASS: safe preflight, compatible Agent, folder paths, sequential bytes, progress, cancel, ready focus/recovery, source facts, parser availability, late response, keys, responsive panel');
  } finally {
    releaseHeldUpload(); releaseHeldPoll();
    await browser.close();
    fs.rmSync(directoryRoot, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
