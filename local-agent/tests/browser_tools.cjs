/* Offline page-script checks with synthetic DOM and API snapshots; no browser or model calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let focusedElement = null;

class Element {
  constructor(tagName = 'div') { this.tagName = tagName.toUpperCase(); this.children = []; this.listeners = {}; this.attributes = {}; this.dataset = {};
    this.value = ''; this.hidden = false; this.inert = false; this.open = false; this.disabled = false;
    this.classList = {toggle() {}}; }
  get firstElementChild() { return this.children[0] || null; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ''; this.children = children; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelectorAll() { return []; }
  contains(node) { return node === this || this.children.some(child => child?.contains?.(node)); }
  focus() { focusedElement = this; }
  click() { if (this.listeners.click) return this.listeners.click(); }
  showModal() { this.open = true; }
  close() { this.open = false; }
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
    get activeElement() { return focusedElement; },
    contains: element => Object.values(elements).includes(element) || body.contains(element),
    getElementById: id => elements[id],
    querySelector: selector => selector === 'meta[name="session-token"]'
      ? {content: 'synthetic-session'} : selector === '.composer-settings' ? composerSettings : null,
    querySelectorAll: () => [],
    createElement: tagName => new Element(tagName),
    addEventListener() {},
  },
  AbortController,
  Blob,
  TextDecoder,
  TextEncoder,
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
const descendants = element => element.children.flatMap(child => [child, ...descendants(child)]);
const snapshot = (id, extra = {}) => ({id, revision: 1, mode: 'single', file: 'note.md', question: '生成摘要',
  output_file: 'report.md', result: null, state: 'waiting_approval', cancelling: false, events: [], ...extra});
const approval = {id: 'approval-1', run_id: 'run-1', call_id: 'call-1', name: 'write_file',
  action_summary: '新建 report.md，36 字节', risk: 'medium', source: 'builtin', operation: 'create',
  path: 'report.md', bytes: 36, content: '<script>window.injected = 1</script>\n全文末尾',
  arguments: {intent: '保存摘要'}, remaining_seconds: 60};
const receipt = {path: 'report.md', bytes: 36, operation: 'created', sha256: 'a'.repeat(64)};
const result = {answer: null, artifacts: [receipt], stop_reason: 'CANCELLED'};

(async () => {
  for (const id of ['output-file', 'approval', 'approval-content', 'approval-allow', 'approval-deny',
    'artifacts', 'artifact-preview-dialog', 'artifact-preview-title', 'artifact-preview-meta',
    'artifact-preview-status', 'artifact-preview-error', 'artifact-preview-content',
    'artifact-preview-download', 'artifact-preview-close']) {
    assert(elements[id], `Missing tool page element: ${id}`);
  }
  vm.runInContext(source, context);
  await flush();

  const receiptCases = [
    ['queued', '已接收', 'neutral'],
    ['running', '正在处理', 'info'],
    ['waiting_approval', '需要你确认', 'warning'],
    ['unable', '未完成', 'danger'],
    ['validation_failed', '校验失败', 'danger'],
    ['timed_out', '已超时', 'danger'],
    ['cancelled', '已取消', 'neutral'],
    ['interrupted', '已中断', 'warning'],
    ['failed', '失败', 'danger'],
  ];
  for (const [state, label, tone] of receiptCases) {
    const summary = {id: `receipt-${state}`, state, question: '核对状态'};
    const detail = state === 'queued' ? null : {...summary, events: [],
      result: ['running', 'waiting_approval'].includes(state) ? null
        : {answer: null, artifacts: [], stop_reason: state.toUpperCase()}};
    const view = context.projectRunReceipt(summary, detail, {
      liveRunId: state === 'running' ? summary.id : null,
      continuableRunId: state === 'interrupted' ? summary.id : null,
    });
    assert.equal(view.label, label, `${state} receipt label`);
    assert.equal(view.tone, tone, `${state} receipt tone`);
    if (!detail) {
      assert.equal(view.citationCount, null, 'summary-only receipt cannot invent citation count');
      assert.equal(view.artifactCount, null, 'summary-only receipt cannot invent artifact count');
      assert.equal(view.stepCount, null, 'summary-only receipt cannot invent step count');
    }
  }
  const completedSummary = {id: 'receipt-completed', state: 'completed', question: '完成状态'};
  const completed = context.projectRunReceipt(completedSummary, {...completedSummary, events: [{event: 'run.ended'}],
    result: {answer: {status: 'answered', answer: '完成', citations: [{quote: '依据'}]},
      artifacts: [{id: 'artifact-1'}], stop_reason: 'ANSWERED'}}, {});
  assert.equal(completed.label, '已完成');
  assert.equal(completed.tone, 'success');
  assert.equal(completed.citationCount, 1);
  assert.equal(completed.artifactCount, 1);
  assert.equal(completed.stepCount, 1);
  const emptyCompleted = context.projectRunReceipt(completedSummary, {...completedSummary, events: [],
    result: {answer: null, artifacts: [], stop_reason: 'INVALID_ANSWER'}}, {});
  assert.equal(emptyCompleted.label, '未完成', 'completed without a validated answer is not success');
  assert.equal(emptyCompleted.tone, 'danger');

  for (const [state, label, tone] of [
    ['queued', '运行中', 'info'], ['running', '运行中', 'info'],
    ['waiting_approval', '待确认', 'warning'],
  ]) {
    const attention = context.projectSessionAttention('session-1', {
      id: 'live-run', session_id: 'session-1', state});
    assert.equal(attention.label, label, `${state} session attention label`);
    assert.equal(attention.tone, tone, `${state} session attention tone`);
  }
  assert.equal(context.projectSessionAttention('session-2', {
    id: 'live-run', session_id: 'session-1', state: 'running'}), null,
  'another session cannot inherit the live marker');
  assert.equal(context.projectSessionAttention('session-1', {
    id: 'live-run', session_id: 'session-1', state: 'completed'}), null,
  'terminal state must clear the live marker');

  const fileAgent = {id: 'file-qa', name: '文件助手', enabled: true, revision: 'file-r1',
    model: {provider: 'deepseek', name: 'deepseek-file'}};
  const directoryAgent = {id: 'directory-qa', name: '目录助手', enabled: true,
    revision: 'directory-r1', model: {provider: 'deepseek', name: 'deepseek-directory'}};
  const disabledAgent = {id: 'disabled', name: '停用助手', enabled: false,
    model: {provider: 'deepseek', name: 'deepseek-disabled'}};
  const agentWorkspace = context.projectAgentWorkspace(
    [fileAgent, directoryAgent, disabledAgent],
    [
      {id: 'file-session', agent_id: 'file-qa'},
      {id: 'directory-session', agent_id: 'directory-qa'},
    ],
    'file-qa',
  );
  assert.equal(agentWorkspace.agents.map(item => item.id).join(','), 'file-qa,directory-qa',
    'daily agent dock must omit disabled agents');
  assert.equal(agentWorkspace.sessions.map(item => item.id).join(','), 'file-session',
    'session pane must only show the selected agent sessions');

  const frozenModel = context.projectModelStatus(
    {ready: true, provider: {provider: 'deepseek', model: 'runtime-model'}},
    directoryAgent,
    {agent_revision: 'file-r1', agent_snapshot: fileAgent},
    {state: 'waiting_approval'},
  );
  assert.equal(frozenModel.model, 'deepseek-file',
    'durable session must display its frozen agent model');
  assert.equal(frozenModel.source, 'session_snapshot');
  assert.equal(frozenModel.status, '等待确认');
  for (const [liveState, expected] of [
    [null, '就绪'], ['queued', '排队中'], ['running', '调用中'],
    ['waiting_approval', '等待确认'],
  ]) {
    const statusView = context.projectModelStatus(
      {ready: true, provider: {provider: 'deepseek'}}, fileAgent, null,
      liveState ? {state: liveState} : null);
    assert.equal(statusView.status, expected, `${liveState || 'idle'} model status`);
    assert.equal(statusView.model, 'deepseek-file');
    assert.equal(statusView.source, 'agent');
  }
  assert.equal(context.projectModelStatus(
    {ready: false, provider: null, error: 'CONFIG_MISSING'}, fileAgent, null,
    {state: 'running'}).status, '不可用', 'provider failure must outrank live run wording');

  const workCompleted = {id: 'completed-run', session_id: 'session-1', state: 'completed',
    question: '生成报告', finished_at: 20};
  const otherCompleted = {id: 'other-run', session_id: 'session-2', state: 'completed',
    question: '其他会话', finished_at: 30};
  const workRail = context.projectWorkRail('session-1',
    {id: 'live-run', session_id: 'session-1', state: 'waiting_approval', question: '写入报告'},
    [otherCompleted, workCompleted],
    new Map([['completed-run', {status: 'ready', run: {...workCompleted,
      result: {artifacts: [{id: 'artifact-1', path: 'report.md'}, {path: 'transient.md'}]}}}]]));
  assert.equal(workRail.waiting.length, 1);
  assert.equal(workRail.running.length, 0);
  assert.equal(workRail.completed[0].id, 'completed-run');
  assert.equal(workRail.completed[0].artifacts.length, 1,
    'work rail must only expose persisted artifacts');
  assert.equal(context.projectWorkRail('session-2',
    {id: 'foreign-live', session_id: 'session-1', state: 'running'}, [], new Map()).running.length, 0,
  'another session live run must not leak into the work rail');

  const job = snapshot('run-1', {pending_approval: approval});
  context.prepareOutput(job); context.render(job);
  assert.equal(typeof context.renderDecisionCard, 'function',
    'approval rendering must have one decision-card module interface');
  assert.equal(elements.approval.hidden, false);
  assert.equal(elements.approval.dataset.decisionMode, 'live');
  assert.equal(elements['run-status'].textContent, '等待确认');
  assert.equal(elements['approval-content'].textContent, approval.content, 'Full body stays plain text');
  assert.match(elements['approval-action'].textContent, /新建 report\.md/);
  assert.match(elements['approval-intent'].textContent, /保存摘要/);
  assert.match(elements['approval-details'].textContent, /report\.md.*36.*新建.*内置.*中风险/);
  assert.equal(elements['output-file'].disabled, true);

  context.renderApprovalHistory({approvals: [{id: 'history-approval', decision: 'allow',
    preview: {...approval, operation: 'created'}}]});
  assert.equal(elements['approval-history'].dataset.decisionMode, 'historical');
  assert.match(elements['approval-history-status'].textContent, /仅供查看/);

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
  assert.equal(descendants(elements['artifact-list'].children[0])
    .filter(element => element.tagName === 'BUTTON').length, 0,
  'transient receipt without durable artifact ID must not expose actions');

  vm.runInContext("activeSessionId = 'session-1'; activeSession = {agent_id: 'directory-qa'}", context);
  const downloadable = {...receipt, id: 'artifact-1'};
  respond = async () => ({ok: true, blob: async () => new Blob([approval.content], {type: 'text/plain'})});
  const downloadJob = snapshot('download-run', {state: 'completed', result: {...result,
    artifacts: [downloadable]}});
  context.prepareOutput(downloadJob); context.render(downloadJob);
  const artifactCard = elements['artifact-list'].children[0];
  const artifactButtons = descendants(artifactCard).filter(child => child.tagName === 'BUTTON');
  const previewButton = artifactButtons.find(button => button.textContent === '预览');
  const downloadButton = artifactButtons.find(button => button.textContent === '下载文件');
  assert(previewButton, 'Persisted text artifact must expose a preview button');
  assert(downloadButton, 'Persisted session artifact must expose an authenticated download button');
  await previewButton.listeners.click();
  assert.equal(elements['artifact-preview-dialog'].open, true);
  assert.equal(elements['artifact-preview-content'].textContent, approval.content,
    'preview body stays literal plain text');
  assert.match(elements['artifact-preview-meta'].textContent, /完整性校验通过/);
  assert.equal(context.injected, undefined, 'script-looking preview text must not execute');
  const previewRequest = requests.at(-1);
  assert.equal(previewRequest.path,
    '/api/sessions/session-1/runs/download-run/artifacts/artifact-1/download');
  assert.equal(previewRequest.headers['X-Session-Token'], 'synthetic-session');
  assert.equal(previewRequest.headers['X-Agent-ID'], 'directory-qa');
  await downloadButton.listeners.click();
  const downloadRequest = requests.at(-1);
  assert.equal(downloadRequest.path,
    '/api/sessions/session-1/runs/download-run/artifacts/artifact-1/download');
  assert.equal(downloadRequest.method, 'GET');
  assert.equal(downloadRequest.headers['X-Session-Token'], 'synthetic-session');
  assert.equal(downloadRequest.headers['X-Agent-ID'], 'directory-qa');
  assert.equal(objectUrls.length, 1);
  assert.deepEqual(revokedUrls, ['blob:artifact']);

  const binaryArtifact = {...downloadable, id: 'artifact-bin', path: 'report.bin'};
  context.renderArtifacts(snapshot('binary-run', {state: 'completed', result: {...result,
    artifacts: [binaryArtifact]}}));
  const binaryButtons = descendants(elements['artifact-list'].children[0])
    .filter(child => child.tagName === 'BUTTON');
  assert.equal(binaryButtons.some(button => button.textContent === '预览'), false,
    'unknown artifact type must keep download but not guess a preview');
  assert.equal(binaryButtons.some(button => button.textContent === '下载文件'), true);

  respond = async () => ({ok: true, blob: async () => new Blob([
    Uint8Array.from([0xc3, 0x28])], {type: 'text/plain'})});
  context.renderArtifacts(downloadJob);
  const invalidPreview = descendants(elements['artifact-list'].children[0])
    .find(button => button.tagName === 'BUTTON' && button.textContent === '预览');
  await invalidPreview.listeners.click();
  assert.match(elements['artifact-preview-error'].textContent, /有效 UTF-8/);
  assert.equal(elements['artifact-preview-content'].textContent, '');

  let releasePreview;
  respond = async () => {
    await new Promise(resolve => { releasePreview = resolve; });
    return {ok: true, blob: async () => new Blob(['迟到正文'], {type: 'text/plain'})};
  };
  const latePreview = invalidPreview.listeners.click();
  assert.match(elements['artifact-preview-meta'].textContent, /等待服务端完整性校验/);
  assert.doesNotMatch(elements['artifact-preview-meta'].textContent, /校验通过/,
    'loading state must not claim that integrity verification already passed');
  vm.runInContext('viewGeneration += 1', context);
  releasePreview(); await latePreview;
  assert.notEqual(elements['artifact-preview-content'].textContent, '迟到正文',
    'a response from an old view generation must not enter the current preview');

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
