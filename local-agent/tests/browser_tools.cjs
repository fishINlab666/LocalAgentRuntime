/* Offline page-script checks with synthetic DOM and API snapshots; no browser or model calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tagName = 'div') { this.tagName = tagName.toUpperCase(); this.children = []; this.listeners = {}; this.attributes = {}; this.dataset = {};
    this.value = ''; this.hidden = false; this.inert = false;
    this.classList = {toggle() {}}; }
  get firstElementChild() { return this.children[0] || null; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ''; this.children = children; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelectorAll() { return []; }
  focus() {}
  click() { if (this.listeners.click) return this.listeners.click(); }
  remove() {}
}

const html = fs.readFileSync('local_agent/static/index.html', 'utf8');
const source = fs.readFileSync('local_agent/static/app.js', 'utf8');
const elements = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element()]));
elements['session-select'].append(new Element());
const body = new Element(), composerSettings = new Element();
composerSettings.open = true;
const requests = [], timers = new Map(), objectUrls = [], revokedUrls = [];
let nextTimer = 0, respond;
const config = {workspace: '/synthetic', ready: true, provider: {simulated: true}};
const context = vm.createContext({
  document: {
    body,
    getElementById: id => elements[id],
    querySelector: selector => selector === 'meta[name="session-token"]'
      ? {content: 'synthetic-session'} : selector === '.composer-settings' ? composerSettings : null,
    querySelectorAll: () => [],
    createElement: tagName => new Element(tagName),
    addEventListener() {},
  },
  AbortController,
  setTimeout: (fn, delay) => { timers.set(++nextTimer, {fn, delay}); return nextTimer; },
  clearTimeout: id => timers.delete(id),
  requestAnimationFrame: callback => callback(),
  matchMedia: () => ({matches: false, addEventListener() {}, addListener() {}}),
  URL: {createObjectURL: blob => { objectUrls.push(blob); return 'blob:artifact'; },
    revokeObjectURL: value => revokedUrls.push(value)},
  fetch: async (path, options) => {
    requests.push({path, ...options});
    if (path === '/api/config') return {ok: true, json: async () => config};
    if (path === '/api/sessions' || path === '/api/sessions?archived=1') {
      return {ok: true, json: async () => ({sessions: [], next_cursor: null})};
    }
    return respond(path, options);
  },
});
const flush = () => new Promise(resolve => setImmediate(resolve));
const snapshot = (id, extra = {}) => ({id, revision: 1, mode: 'single', file: 'note.md', question: '生成摘要',
  output_file: 'report.md', result: null, state: 'waiting_approval', cancelling: false, events: [], ...extra});
const approval = {id: 'approval-1', run_id: 'run-1', call_id: 'call-1', name: 'write_file',
  action_summary: '新建 report.md，36 字节', risk: 'medium', source: 'builtin', operation: 'create',
  path: 'report.md', bytes: 36, content: '<script>window.injected = 1</script>\n全文末尾',
  arguments: {intent: '保存摘要'}, remaining_seconds: 60};
const receipt = {path: 'report.md', bytes: 36, operation: 'created', sha256: 'a'.repeat(64)};
const result = {answer: null, artifacts: [receipt], stop_reason: 'CANCELLED'};

(async () => {
  for (const id of ['output-file', 'approval', 'approval-content', 'approval-allow', 'approval-deny', 'artifacts']) {
    assert(elements[id], `Missing tool page element: ${id}`);
  }
  vm.runInContext(source, context);
  await flush();
  const job = snapshot('run-1', {pending_approval: approval});
  context.prepareOutput(job); context.render(job);
  assert.equal(elements.approval.hidden, false);
  assert.equal(elements['run-status'].textContent, '等待确认');
  assert.equal(elements['approval-content'].textContent, approval.content, 'Full body stays plain text');
  assert.match(elements['approval-action'].textContent, /新建 report\.md/);
  assert.match(elements['approval-intent'].textContent, /保存摘要/);
  assert.match(elements['approval-details'].textContent, /report\.md.*36.*新建.*内置.*中风险/);
  assert.equal(elements['output-file'].disabled, true);

  let release;
  respond = async () => {
    await new Promise(resolve => { release = resolve; });
    return {ok: true, json: async () => snapshot('run-1', {revision: 2, pending_approval: null, state: 'running'})};
  };
  const allow = elements['approval-allow'].listeners.click();
  elements['approval-deny'].listeners.click();
  context.render(job); // A concurrent poll must not re-enable the buttons.
  assert.equal(elements['approval-allow'].disabled, true);
  assert.equal(elements['approval-deny'].disabled, true);
  const sent = requests.filter(request => request.path.includes('/approvals/'));
  assert.equal(sent.length, 1);
  assert.deepEqual(JSON.parse(sent[0].body), {decision: 'allow'});
  assert.equal(sent[0].headers['X-Session-Token'], 'synthetic-session');
  release(); await allow;
  assert.equal(elements.approval.hidden, true);
  context.render(snapshot('run-1', {revision: 3, pending_approval: null, state: 'cancelled', result}));
  assert.equal(elements.artifacts.hidden, false, 'Created file remains visible after cancellation');
  assert.match(elements['artifact-list'].textContent, /report\.md.*36/);
  assert.match(elements['artifact-note'].textContent, /后续步骤已取消/);

  vm.runInContext("activeSessionId = 'session-1'; activeSession = {agent_id: 'directory-qa'}", context);
  const downloadable = {...receipt, id: 'artifact-1'};
  respond = async () => ({ok: true, blob: async () => ({kind: 'artifact-blob'})});
  const downloadJob = snapshot('download-run', {state: 'completed', result: {...result,
    artifacts: [downloadable]}});
  context.prepareOutput(downloadJob); context.render(downloadJob);
  const artifactCard = elements['artifact-list'].children[0];
  const downloadButton = artifactCard.children.find(child => child.tagName === 'BUTTON');
  assert(downloadButton, 'Persisted session artifact must expose an authenticated download button');
  await downloadButton.listeners.click();
  const downloadRequest = requests.at(-1);
  assert.equal(downloadRequest.path,
    '/api/sessions/session-1/runs/download-run/artifacts/artifact-1/download');
  assert.equal(downloadRequest.method, 'GET');
  assert.equal(downloadRequest.headers['X-Session-Token'], 'synthetic-session');
  assert.equal(downloadRequest.headers['X-Agent-ID'], 'directory-qa');
  assert.equal(objectUrls.length, 1);
  assert.deepEqual(revokedUrls, ['blob:artifact']);

  const normalized = context.normalizedSessionRun({id: 'stored-run', state: 'completed',
    question: 'q', task_type: 'files', result: {answer: null, artifacts: [receipt]},
    artifacts: [{id: 'stored-artifact', path: receipt.path, bytes: receipt.bytes,
      sha256: receipt.sha256, receipt: {operation: 'created'}}]});
  assert.equal(normalized.result.artifacts[0].id, 'stored-artifact',
    'Durable artifact ID must replace the transient receipt before rendering');
  vm.runInContext("activeSessionId = null; activeSession = null", context);

  const importedJob = snapshot('imported-citations', {state: 'completed', result: {
    answer: {status: 'answered', answer: '已核对导入资料。', citations: [
      {path: 'documents/source-pdf/chunk-0001.md', start_line: 1, end_line: 2,
        quote: '合同原文', source: {kind: 'imported_document', name: '合同.pdf',
          logical_path: '项目资料/合同.pdf', locations: [{kind: 'pdf_page', page: 2}]}},
      {path: 'documents/source-docx/chunk-0003.md', start_line: 4, end_line: 6,
        quote: '会议原文', source: {kind: 'imported_document', name: '会议纪要.docx',
          logical_path: '会议/会议纪要.docx', locations: [
            {kind: 'docx_paragraph', paragraph: 3},
            {kind: 'docx_table_row', table: 2, row: 4},
          ]}},
    ]}, artifacts: []},
  });
  context.prepareOutput(importedJob); context.render(importedJob);
  assert.equal(elements.citations.children.length, 2);
  const pdfCitation = elements.citations.children[0].textContent;
  assert.match(pdfCitation, /项目资料\/合同\.pdf/);
  assert.match(pdfCitation, /PDF 第 2 页/);
  assert.doesNotMatch(pdfCitation, /documents\/source-pdf\/chunk-0001\.md/,
    'Normalized PDF chunk path must not replace the imported source path');
  const wordCitation = elements.citations.children[1].textContent;
  assert.match(wordCitation, /会议\/会议纪要\.docx/);
  assert.match(wordCitation, /Word 第 3 段/);
  assert.match(wordCitation, /Word 表格 2 第 4 行/);
  assert.doesNotMatch(wordCitation, /documents\/source-docx\/chunk-0003\.md/,
    'Normalized Word chunk path must not replace the imported source path');

  const failedJob = snapshot('run-1', {revision: 4, pending_approval: null, state: 'failed',
    result: {...result, stop_reason: 'INVALID_ANSWER'}});
  context.prepareOutput(failedJob); context.render(failedJob);
  assert.equal(elements.artifacts.hidden, false, 'Created file remains visible after answer validation fails');
  assert.match(elements['artifact-note'].textContent, /后续步骤未完成/);

  const denyJob = snapshot('deny-run', {pending_approval: {...approval, id: 'deny-approval', run_id: 'deny-run'}});
  context.prepareOutput(denyJob); context.render(denyJob);
  respond = async () => ({ok: true, json: async () => snapshot('deny-run', {revision: 2, state: 'unable',
    pending_approval: null, result: {...result, artifacts: [], stop_reason: 'USER_REJECTED'}})});
  await elements['approval-deny'].listeners.click();
  assert.deepEqual(JSON.parse(requests.at(-1).body), {decision: 'deny'});
  assert.equal(elements.artifacts.hidden, true);
  assert.match(elements['failure-message'].textContent, /拒绝/);

  // Reload restores the same pending run without submitting the task again.
  config.latest_run_id = 'restore-run';
  const restored = snapshot('restore-run', {mode: 'directory', file: null, pending_approval: {...approval, id: 'restore-approval', run_id: 'restore-run'}});
  respond = async () => ({ok: true, json: async () => restored});
  const beforeRestore = requests.filter(request => request.method === 'POST').length;
  await context.initialize();
  assert.equal(elements['output-file'].value, 'report.md');
  assert.equal(elements.discover.checked, true);
  assert.equal(elements.approval.hidden, false);
  assert.equal(requests.filter(request => request.method === 'POST').length, beforeRestore);

  respond = async () => {
    await new Promise(resolve => { release = resolve; });
    return {ok: true, json: async () => snapshot('restore-run', {revision: 2, pending_approval: null, state: 'cancelled', result: {...result, artifacts: []}})};
  };
  const cancel = elements.cancel.listeners.click();
  context.render(restored);
  assert.equal(elements['approval-allow'].disabled, true, 'Polling cannot re-enable approval during cancellation');
  release(); await cancel;

  for (const [outputFile, directory] of [['', false], ['summary.md', true]]) {
    elements['output-file'].value = outputFile; elements.discover.checked = directory;
    elements.file.value = 'note.md'; elements.question.value = '概括';
    respond = async () => ({ok: true, json: async () => snapshot('submit-run', {state: 'completed',
      pending_approval: null, result: {answer: {status: 'answered', answer: '已创建 summary.md', citations: []}, artifacts: []}})});
    await elements['question-form'].listeners.submit({preventDefault() {}});
    const body = JSON.parse(requests.at(-1).body);
    assert.deepEqual(body, directory ? {mode: 'directory', question: '概括', output_file: 'summary.md'} : {file: 'note.md', question: '概括'});
    assert.equal(elements.artifacts.hidden, true, 'Model text cannot create a receipt');
  }

  const expired = snapshot('run-2', {pending_approval: {...approval, run_id: 'run-2', remaining_seconds: 0}});
  context.prepareOutput(expired); context.render(expired);
  assert.equal(elements['approval-allow'].disabled, true);
  assert.match(elements['approval-status'].textContent, /已过期/);
  const pending = snapshot('run-3', {pending_approval: {...approval, run_id: 'run-3'}});
  context.prepareOutput(pending); context.render(pending);
  respond = async () => ({ok: false, json: async () => ({error: 'SESSION_EXPIRED'})});
  await elements['approval-allow'].listeners.click();
  assert.equal(elements['approval-allow'].disabled, true);
  assert.match(elements['connection-error'].textContent, /刷新/);

  console.log('PASS: preview, allow/deny body, duplicate and cancel guards, restore, optional output, terminal receipts, expiry, stale session');
})().catch(error => { console.error(error); process.exitCode = 1; });
