'use strict';

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
let ready = false, simulated = false, activeId = null, busy = false, timer = null, renderedEvents = 0;
let viewGeneration = 0, renderedRevision = -1;
let activeSessionId = null, activeSession = null, fixedSessionSelection = false;
let workspaceAvailable = true, historyRuns = [];
let pendingApproval = null, approvalSendingId = null, approvalBlockedId = null, approvalDeadline = 0, approvalTimer = null;
let cancelling = false, cancelSendingId = null;
const errors = {
  CONFIG_MISSING: '模型尚未配置。请在已配置 DEEPSEEK_API_KEY 的终端启动页面服务。',
  CONFIG_INVALID: '模型配置有误，请检查启动服务的环境变量。',
  WORKSPACE_CHANGED: '启动时的工作区路径已被替换，请恢复原目录或重新启动服务。',
  PATH_DENIED: '文件路径不在允许范围内。请填写工作区内的相对路径，不使用链接或隐藏文件。',
  UNSUPPORTED_FILE: '仅支持 UTF-8 编码的 .md 或 .txt 文本文件。',
  FILE_NOT_FOUND: '找不到这个文件，请核对文件名和当前工作区。',
  FILE_TOO_LARGE: '文件超过 32 KiB，请选择更小的文本文件。',
  FILE_CHANGED: '读取时文件发生变化，请等编辑完成后重新提问。',
  READ_ERROR: '无法读取文件，请检查文件权限。',
  PATH_NOT_DISCOVERED: '只能读取本次列出目录后发现的资料，请重新提问。',
  FILE_COUNT_LIMIT: '本次最多读取 4 份资料，尚未检查全部文件。',
  DIRECTORY_NOT_FOUND: '找不到这个目录，请检查当前工作区。',
  DIRECTORY_TOO_LARGE: '目录条目过多，请缩小工作区范围后重试。',
  DIRECTORY_CHANGED: '列出目录时内容发生变化，请等编辑完成后重试。',
  LIST_ERROR: '无法列出目录，请检查目录权限。',
  INCOMPLETE_SEARCH: '本次尚未检查全部已发现资料，无法确认信息未记载。',
  INVALID_QUESTION: '请输入 1–4000 字的问题。',
  INVALID_TASK: '文件路径或问题不完整，请检查后重试。',
  RUN_ACTIVE: '已有任务正在运行，请等待完成或先取消。',
  RUN_NOT_FOUND: '任务已过期或服务已重启，请重新提问。',
  NOT_FOUND: '找不到这条会话记录，或它不属于当前工作区。',
  SESSION_ARCHIVED: '这个会话已归档；恢复后才能继续提交。',
  SESSION_REQUEST_CONFLICT: '同一个提交编号已经用于不同内容，请刷新后重试。',
  SESSION_SELECTION_FIXED: '当前服务固定打开一个会话，不能在这里新建其他会话。',
  WORKSPACE_UNAVAILABLE: '原工作区当前不可用。历史仍可查看，也可以选择“聊这段记录”。',
  RUN_NOT_INTERRUPTED: '这条运行不是可继续的中断状态。',
  APPROVAL_UNAVAILABLE: '旧审批仅供查看，不能在当前进程继续执行。',
  SESSION_EXPIRED: '本地服务已重启，请刷新页面重新连接。',
  AUTH_ERROR: 'DeepSeek 鉴权失败，请检查启动终端中的 API Key。',
  RATE_LIMIT: 'DeepSeek 请求频率受限，请稍后重试。',
  NETWORK_ERROR: '连接 DeepSeek 失败，请检查网络后重新提问。',
  MODEL_TIMEOUT: '等待模型回复超时，本次已停止。',
  RUN_TIMEOUT: '本次任务超过时间限制，已停止。',
  TOOL_TIMEOUT: '工具执行超时，本次已停止。',
  USER_REJECTED: '你已拒绝这次新建，文件未创建。',
  APPROVAL_EXPIRED: '确认已过期，这次新建未执行。请重新发起任务。',
  APPROVAL_NOT_FOUND: '这次确认已失效，请以最新任务状态为准。',
  APPROVAL_CONFLICT: '这次操作已收到不同的决定，请以最新任务状态为准。',
  APPROVAL_UNAVAILABLE: '无法继续等待确认，这次新建未执行。',
  FILE_EXISTS: '目标文件已存在，不能覆盖。请指定一个新的文件路径。',
  WRITE_ERROR: '文件写入失败，请检查目标目录权限。',
  WRITE_OUTCOME_UNKNOWN: '无法确认文件是否已创建，请检查目标路径；本次不会自动重试。',
  OUTPUT_NOT_CREATED: '没有取得目标文件的成功创建回执，本次未完成。',
  OS_PERMISSION_DENIED: '系统拒绝访问，请检查文件或目录权限后重试。',
  DISK_FULL: '磁盘空间不足，本次已停止。',
  INVALID_OUTPUT_FILE: '输出路径须为工作区内新建的 .md 或 .txt 文件，且不能与输入相同。',
  IO_ERROR: '本地工具出现异常，本次已停止。',
  CONTEXT_LIMIT: '问题和文件内容超出本次输入限制，请缩小内容。',
  MAX_STEPS: '已达到调用次数上限，本次未完成。',
  INVALID_ANSWER: '回答格式未通过检查，本次没有有效答案。',
  IDENTIFIER_MISMATCH: '回答中的标识连接符与已读原文不一致，本次没有有效答案。',
  INVALID_CITATION: '回答引用与文件原文不一致，请重新提问。',
  MISSING_CITATION: '回答缺少原文依据，请重新提问。',
  MISSING_READ: '模型尚未成功读取指定文件，无法确认答案。',
  CANCELLED: '已停止后续操作。已发出的模型请求可能仍在服务端处理。',
  TRACE_ERROR: '执行日志保存失败，本次不能确认为成功。',
  REQUEST_TOO_LARGE: '输入过长，请缩短问题或路径。',
  LOCAL_SERVER_ERROR: '本地服务遇到错误，请检查日志目录权限并重新启动。',
};
const eventNames = {'run.started':'任务开始', 'model.requested':'请求模型', 'model.completed':'收到模型回复',
  'tool.requested':'模型请求工具', 'tool.started':'开始执行工具', 'tool.finished':'工具执行结束',
  'tool.completed':'工具结果已回填', 'tool.described':'工具动作说明', 'approval.required':'等待确认', 'approval.resolved':'已收到确认决定',
  'approval.expired':'确认已过期', 'answer.rejected':'回答格式不合规，正在纠错', 'run.ended':'任务结束'};
const sourceLabel = value => ({builtin: '内置', user: '用户工具', mcp: 'MCP'})[value] || value || '未提供';
const riskLabel = value => ({low: '低风险', medium: '中风险', high: '高风险'})[value] || value || '未提供';
function eventLabel(event) {
  if (event?.event === 'answer.rejected' && event.detail?.code === 'IDENTIFIER_MISMATCH') {
    return '标识与原文不一致，正在纠错';
  }
  if (event?.event?.startsWith('tool.') && (event.detail?.action_summary || (event.detail?.name && !['read_file', 'list_files'].includes(event.detail.name)))) {
    return {'tool.described': '工具动作说明', 'tool.requested': '模型请求工具', 'tool.started': '开始执行工具', 'tool.finished': '工具执行结束', 'tool.completed': '工具结果已回填'}[event.event] || event.event;
  }
  if (event?.detail?.name === 'list_files') {
    return {'tool.requested': '模型请求列出目录', 'tool.started': '开始列出目录'}[event.event] || eventNames[event.event];
  }
  return eventNames[event?.event] || event?.event || '正在启动';
}
const messageFor = code => errors[code] || `本次未完成（${code || 'UNKNOWN_ERROR'}），请重新提问。`;

async function api(path, body) {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
      headers: {'X-Session-Token': token, 'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal});
    const data = await response.json();
    if (!response.ok) { const error = new Error(messageFor(data.error)); error.code = data.error; throw error; }
    return data;
  } catch (error) {
    if (typeof error.code !== 'string') {
      throw new Error('与本地服务的连接中断。若任务已提交，它可能仍在运行；请保持服务开启或刷新页面确认状态。');
    }
    throw error;
  } finally { clearTimeout(deadline); }
}

function updateControls() {
  const sessionMode = Boolean(activeSessionId);
  const conversation = sessionMode && $('task-type').value === 'conversation';
  const archived = activeSession?.status === 'archived';
  $('start').disabled = busy || !ready || archived || (sessionMode && !conversation && !workspaceAvailable);
  $('start').textContent = busy ? '运行中…' : '开始问答 ↗';
  $('cancel').hidden = !busy || !activeId;
  $('discover').disabled = sessionMode || busy || !ready;
  $('file').disabled = sessionMode || busy || $('discover').checked;
  $('file').required = !sessionMode && !$('discover').checked;
  $('task-type').disabled = busy || !sessionMode;
  $('output-file').disabled = busy || conversation;
  for (const element of [$('question'), $('sample'), ...document.querySelectorAll('[data-question]')]) element.disabled = busy;
  $('session-create').disabled = busy || fixedSessionSelection;
  $('session-rename').disabled = busy || !activeSessionId;
  $('session-archive').disabled = busy || !activeSessionId;
  $('session-restore').disabled = busy || !activeSessionId;
  $('privacy').textContent = simulated ? '演示模式：只在本机读取资料，不向模型发送内容。原文件保持只读。'
    : conversation ? '本轮只把问题和本会话的受限历史发送给 DeepSeek，不读取工作区文件。'
    : $('discover').checked ? '开始后，问题、目录元数据和已读取的资料内容将发送给 DeepSeek。原文件保持只读。'
    : '开始后，所选文件内容和问题将发送给 DeepSeek。原文件保持只读。';
  if ($('output-file').value.trim()) $('privacy').textContent += ' 输出文件的完整内容经你确认后才新建。';
  updateApprovalControls();
}

function status(text, type = 'neutral') { $('run-status').textContent = text; $('run-status').className = `badge ${type}`; }
function showError(error, area = 'form-error') { $(area).textContent = error.message; $(area).hidden = false; }
function countQuestion() { $('count').textContent = `${$('question').value.length} / 4000`; }

function updateApprovalControls() {
  clearTimeout(approvalTimer);
  if (!pendingApproval) return;
  if (pendingApproval.historical) {
    $('approval-status').textContent = `历史审批：${pendingApproval.status}。仅供查看，不能再次执行。`;
    return;
  }
  const id = pendingApproval.approval_id || pendingApproval.id;
  const remaining = Math.max(0, Math.ceil((approvalDeadline - Date.now()) / 1000));
  const sending = approvalSendingId === id, blocked = approvalBlockedId === id;
  $('approval-allow').disabled = $('approval-deny').disabled = !ready || sending || blocked || cancelling || remaining === 0;
  $('approval-status').textContent = !ready ? '连接已失效，请刷新页面；旧确认不能继续使用。'
    : cancelling ? '正在取消，这次确认已停用。'
    : sending ? '正在提交决定，请勿重复点击…'
    : blocked ? '决定的提交状态需要重新确认，请刷新页面查看。'
    : remaining === 0 ? '确认已过期，正在获取最终状态…'
    : `请核对完整内容，剩余 ${remaining} 秒。确认仅适用于这次操作。`;
  if (remaining > 0 && ready) approvalTimer = setTimeout(updateApprovalControls, 1000);
}

function renderApproval(job) {
  const historicalRow = !busy && job.approvals?.length ? job.approvals.at(-1) : null;
  const approval = busy ? job.pending_approval : historicalRow ? {
    ...historicalRow.preview, id: historicalRow.id, status: historicalRow.decision,
    arguments: {}, historical: true
  } : null;
  const id = approval?.approval_id || approval?.id;
  const previousId = pendingApproval?.approval_id || pendingApproval?.id;
  pendingApproval = approval || null;
  $('approval').hidden = !pendingApproval;
  $('approval-actions').hidden = Boolean(approval?.historical);
  clearTimeout(approvalTimer);
  if (!pendingApproval) return;
  approvalDeadline = approval.historical ? 0
    : Date.now() + Math.max(0, Number(approval.remaining_seconds) || 0) * 1000;
  if (id !== previousId) {
    $('approval-error').hidden = true;
    $('approval-content').textContent = approval.content;
  }
  $('approval-action').textContent = `实际操作：${approval.action_summary || approval.name}`;
  $('approval-details').textContent = `目标：${approval.path} · ${approval.bytes} 字节 · ${['create', 'created'].includes(approval.operation) ? '新建，不覆盖' : approval.operation} · 来源：${sourceLabel(approval.source)} · ${riskLabel(approval.risk)}`;
  $('approval-intent').textContent = `模型意图：${approval.arguments?.intent || '未提供'}（用于说明目的）`;
  if (approval.historical) $('approval-status').textContent = `历史审批：${approval.status}。仅供查看，不能再次执行。`;
}

function renderArtifacts(job) {
  const artifacts = job.result?.artifacts || [];
  $('artifacts').hidden = artifacts.length === 0;
  $('artifact-list').replaceChildren();
  if (!artifacts.length) return;
  $('artifact-note').textContent = job.state === 'cancelled' ? '文件已生成，后续步骤已取消。已创建文件保留。'
    : job.state === 'completed' ? '以下文件已由本地工具创建，记录来自实际执行回执。'
    : '文件已生成，后续步骤未完成。已创建文件保留。';
  for (const artifact of artifacts) {
    const article = document.createElement('article'), heading = document.createElement('p');
    const details = document.createElement('details'), summary = document.createElement('summary'), hash = document.createElement('code');
    article.className = 'artifact';
    heading.textContent = `${artifact.path} · ${artifact.bytes} 字节 · ${artifact.operation === 'created' ? '已新建' : artifact.operation}`;
    summary.textContent = '查看文件校验值'; hash.textContent = `SHA-256：${artifact.sha256}`;
    details.append(summary, hash); article.append(heading, details); $('artifact-list').append(article);
  }
}

function connectionExpired(error) {
  if (!['SESSION_EXPIRED', 'RUN_NOT_FOUND'].includes(error.code)) return false;
  ready = false; clearTimeout(timer); status('需要刷新页面', 'warning');
  showError(error, 'connection-error'); updateControls();
  return true;
}

function normalizedSessionRun(run) {
  const terminal = !['queued', 'running', 'waiting_approval'].includes(run.state);
  const result = run.result || (terminal ? {answer: null, stop_reason: run.stop_reason,
    trace_path: run.trace_path || '', artifacts: run.artifacts || []} : null);
  if (result && !result.artifacts && run.artifacts) result.artifacts = run.artifacts.map(item => ({
    path: item.path, bytes: item.bytes, sha256: item.sha256,
    operation: item.receipt?.operation || 'created'
  }));
  return {...run, mode: activeSession?.scope?.mode || 'file',
    file: activeSession?.scope?.file || null, events: run.events || [], result,
    revision: terminal ? Number.MAX_SAFE_INTEGER : Number(run.revision) || 0,
    pending_approval: run.pending_approval || null,
    cancelling: Boolean(run.cancelling)};
}

function runPath(runId, tail = '') {
  return activeSessionId ? `/api/sessions/${activeSessionId}/runs/${runId}${tail}`
    : `/api/runs/${runId}${tail}`;
}

function clearRunView() {
  clearTimeout(timer); activeId = null; busy = false; renderedEvents = 0; renderedRevision = -1;
  pendingApproval = null; cancelling = false; $('output').hidden = true; $('empty').hidden = false;
  $('continue-run').hidden = true; updateControls();
}

function renderRunHistory(runs) {
  historyRuns = runs;
  $('session-history').replaceChildren();
  for (const item of runs) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = `${item.state === 'completed' ? '✓' : '·'} ${item.question}`;
    button.title = item.question;
    button.addEventListener('click', () => openSessionRun(item.id));
    $('session-history').append(button);
  }
}

async function openSessionRun(runId) {
  if (!activeSessionId) return;
  const sessionId = activeSessionId, generation = ++viewGeneration;
  clearTimeout(timer);
  try {
    const response = await api(`/api/sessions/${sessionId}/runs/${runId}`);
    if (sessionId !== activeSessionId || generation !== viewGeneration) return;
    const job = normalizedSessionRun(response.run);
    $('question').value = job.question; countQuestion(); $('task-type').value = job.task_type;
    $('output-file').value = job.output_file || '';
    prepareOutput(job); render(job);
    if (busy) timer = setTimeout(poll, 450);
  } catch (error) {
    if (sessionId === activeSessionId && generation === viewGeneration) showError(error, 'session-error');
  }
}

async function selectSession(sessionId) {
  const generation = ++viewGeneration;
  clearTimeout(timer); activeSessionId = sessionId || null; activeSession = null;
  $('session-error').hidden = true;
  if (!activeSessionId) {
    $('session-actions').hidden = true; $('session-scope').hidden = true;
    renderRunHistory([]); $('task-type').value = 'files'; clearRunView(); return;
  }
  try {
    const [sessionResponse, runsResponse] = await Promise.all([
      api(`/api/sessions/${activeSessionId}`), api(`/api/sessions/${activeSessionId}/runs`)
    ]);
    if (generation !== viewGeneration || sessionId !== activeSessionId) return;
    activeSession = sessionResponse.session;
    $('session-select').value = activeSession.id;
    $('session-actions').hidden = false;
    $('session-rename-title').value = activeSession.title;
    $('session-archive').hidden = activeSession.status === 'archived';
    $('session-restore').hidden = activeSession.status !== 'archived';
    updateControls();
    const directory = activeSession.scope.mode === 'directory';
    $('discover').checked = directory; $('file').value = activeSession.scope.file || '';
    $('session-scope').hidden = false;
    $('session-scope').textContent = `固定资料范围：${directory ? '当前工作区目录发现' : activeSession.scope.file}`
      + (workspaceAvailable ? '' : ' · 原工作区当前不可用');
    renderRunHistory(runsResponse.runs);
    if (runsResponse.runs.length) await openSessionRun(runsResponse.runs[0].id);
    else clearRunView();
  } catch (error) {
    if (generation === viewGeneration) showError(error, 'session-error');
  }
  updateControls();
}

async function loadSessions(selectedId = null, fixed = fixedSessionSelection) {
  const [active, archived] = await Promise.all([api('/api/sessions'), api('/api/sessions?archived=1')]);
  const all = [...active.sessions, ...archived.sessions];
  const select = $('session-select'), temporary = select.firstElementChild;
  select.replaceChildren(temporary);
  for (const session of all) {
    const option = document.createElement('option'); option.value = session.id;
    option.textContent = session.title + (session.status === 'archived' ? '（已归档）' : '');
    select.append(option);
  }
  fixedSessionSelection = Boolean(fixed);
  temporary.hidden = fixedSessionSelection;
  $('session-create').hidden = fixedSessionSelection;
  const wanted = selectedId || active.sessions[0]?.id || null;
  if (wanted) { select.value = wanted; await selectSession(wanted); }
}

function prepareOutput(job) {
  clearTimeout(timer);
  activeId = job.id; renderedEvents = 0; renderedRevision = -1; viewGeneration++;
  pendingApproval = null; approvalBlockedId = null; cancelling = false; clearTimeout(approvalTimer);
  $('empty').hidden = true; $('output').hidden = false;
  for (const id of ['answer-block', 'evidence', 'failure', 'form-error', 'scope-summary', 'approval', 'artifacts']) $(id).hidden = true;
  $('continue-run').hidden = true;
  $('events').replaceChildren(); $('citations').replaceChildren(); $('trace-path').textContent = '';
  $('source-line').textContent = `${job.task_type === 'conversation' ? '会话记录'
    : job.mode === 'directory' ? '目录发现' : job.file || '资料'} · ${job.question}`;
  $('copy').textContent = '复制回答';
}

function render(job) {
  if (activeId !== job.id || job.revision < renderedRevision) return;
  renderedRevision = job.revision;
  for (const event of job.events.slice(renderedEvents)) {
    const li = document.createElement('li'), stamp = document.createElement('time'), label = document.createElement('span');
    stamp.textContent = `${event.elapsed.toFixed(1)}s`;
    const detail = event.detail || {};
    label.textContent = eventLabel(event) + (detail.action_summary ? ` · 实际操作：${detail.action_summary}` : detail.path ? ` · ${detail.path}` : detail.name ? ` · ${detail.name}` : '')
      + (detail.source ? ` · 来源：${sourceLabel(detail.source)}` : '') + (detail.risk ? ` · ${riskLabel(detail.risk)}` : '')
      + (detail.intent ? ` · 模型意图：${detail.intent}` : '') + (detail.code ? ` · ${detail.code}` : '');
    li.append(stamp, label); $('events').append(li);
  }
  renderedEvents = job.events.length; $('event-count').textContent = `${renderedEvents} 个步骤`;
  busy = job.result === null;
  cancelling = Boolean(job.cancelling) || cancelSendingId === job.id;
  renderApproval(job);
  $('progress').hidden = !busy;
  if (busy) {
    status(cancelling ? '正在取消' : pendingApproval ? '等待确认' : '正在处理', pendingApproval ? 'warning' : '');
    const last = job.events.at(-1);
    $('progress-text').textContent = cancelling ? '正在停止后续操作…' : pendingApproval ? '核对下方操作及完整内容后，确认或拒绝。' : `${eventLabel(last)}…`;
    $('cancel').disabled = cancelling;
  } else {
    clearTimeout(timer);
    const result = job.result, answer = result.answer;
    renderArtifacts(job);
    $('trace-path').textContent = result.trace_path || '';
    if (result.scope) {
      const scope = result.scope;
      $('scope-summary').hidden = false;
      $('scope-summary').textContent = `检查范围：已发现 ${scope.discovered_files.length} 份 · 已读取 ${scope.read_files.length} 份 · 未读取 ${scope.unread_files.length} 份 · 未列出目录 ${scope.unlisted_directories.length} 个。`
        + (scope.complete ? ' 已检查全部已发现资料。' : ' 检查范围尚不完整。')
        + (scope.unread_files.length ? ` 未读取：${scope.unread_files.join('、')}。` : '')
        + (scope.unlisted_directories.length ? ` 未列出目录：${scope.unlisted_directories.join('、')}。` : '');
    }
    if (job.state === 'completed' && answer) {
      status(answer.status === 'not_found' ? '信息未记载' : '回答已完成', answer.status === 'not_found' ? 'warning' : '');
      $('answer-block').hidden = false;
      $('answer-title').textContent = answer.status === 'not_found' ? '没有找到这项信息'
        : job.task_type === 'conversation' ? '会话中的答案' : '资料中的答案';
      $('answer-text').textContent = answer.answer;
      $('answer-note').textContent = simulated ? '模拟演示：显示读取到的内容，未调用真实模型，也未理解问题。' : '回答通过格式与引用检查；请结合原文判断内容是否准确。';
      const citations = answer.citations || answer.references || [];
      $('evidence').hidden = citations.length === 0;
      $('citation-count').textContent = `${citations.length} 处引用`;
      $('citations').replaceChildren();
      for (const citation of citations) {
        const article = document.createElement('article'), heading = document.createElement('header'), quote = document.createElement('blockquote');
        article.className = 'citation';
        heading.textContent = citation.message_id
          ? `会话消息 ${citation.message_id} · 字符 ${citation.start}–${citation.end}`
          : `${citation.path} · 第 ${citation.start_line}${citation.end_line === citation.start_line ? '' : '–' + citation.end_line} 行`;
        quote.textContent = citation.quote; article.append(heading, quote); $('citations').append(article);
      }
    } else {
      const cancelled = job.state === 'cancelled';
      const interrupted = job.state === 'interrupted';
      status(cancelled ? '已取消' : interrupted ? '运行已中断'
        : job.state === 'unable' && !job.output_file ? '无法读取' : '本次未完成', 'warning');
      $('failure').hidden = false; $('failure-title').textContent = cancelled ? '本次运行已取消' : '没有生成有效答案';
      const toolError = [...job.events].reverse().find(e => e.event === 'tool.completed' && e.detail.code)?.detail.code;
      const reason = job.state === 'unable' ? (toolError || result.stop_reason) : result.stop_reason;
      $('failure-message').textContent = interrupted
        ? '服务曾在这次运行中停止。旧运行不会自动重做；可以新建一次继续运行。'
        : messageFor(reason) + (answer?.answer ? `\n${answer.answer}` : '');
      $('continue-run').hidden = !interrupted || !activeSessionId;
    }
  }
  updateControls();
}

async function poll() {
  if (!activeId) return;
  const requestedId = activeId, requestedSession = activeSessionId, generation = viewGeneration;
  try {
    const response = await api(runPath(requestedId));
    if (requestedId !== activeId || requestedSession !== activeSessionId || generation !== viewGeneration) return;
    const job = activeSessionId ? normalizedSessionRun(response.run) : response;
    $('connection-error').hidden = true;
    render(job);
    if (busy) timer = setTimeout(poll, 450);
    else if (activeSessionId) {
      const runs = await api(`/api/sessions/${activeSessionId}/runs`);
      if (requestedSession === activeSessionId && generation === viewGeneration) renderRunHistory(runs.runs);
    }
  } catch (error) {
    if (requestedId !== activeId || requestedSession !== activeSessionId || generation !== viewGeneration) return;
    showError(error, 'connection-error');
    if (!connectionExpired(error)) timer = setTimeout(poll, 2000);
  }
}

async function initialize() {
  try {
    const config = await api('/api/config');
    $('workspace').textContent = config.workspace;
    ready = config.ready; simulated = Boolean(config.provider?.simulated);
    workspaceAvailable = config.workspace_available !== false;
    $('connection').textContent = simulated ? '模拟演示 · 未调用模型' : ready ? 'DeepSeek 已配置' : '模型未配置';
    $('connection').className = `badge ${simulated || !ready ? 'warning' : ''}`;
    if (simulated || !ready) {
      $('mode-notice').hidden = false;
      $('mode-notice').textContent = simulated ? '这是页面演示模式：文件会真实读取，回答由测试替身生成。真实使用请从已配置 API Key 的终端启动，不加 --demo。' : messageFor(config.error);
    }
    $('sample').hidden = !config.example_file;
    $('sample').onclick = () => {
      $('discover').checked = false; $('output-file').value = ''; updateControls();
      $('file').value = config.example_file;
      $('question').value = '项目代号、评审人和演示日期分别是什么？引用原文。';
      countQuestion(); $('question').focus();
    };
    await loadSessions(config.selected_session_id, Boolean(config.selected_session_id));
    if (config.latest_run_id && !activeSessionId) {
      busy = true; updateControls();
      const job = await api(`/api/runs/${config.latest_run_id}`);
      $('discover').checked = job.mode === 'directory';
      $('output-file').value = job.output_file || '';
      $('file').value = job.file || ''; $('question').value = job.question; countQuestion(); prepareOutput(job); render(job);
      if (busy) timer = setTimeout(poll, 450);
    } else updateControls();
  } catch (error) { ready = false; busy = false; updateControls(); showError(error, 'connection-error'); }
}

$('question').addEventListener('input', countQuestion);
$('discover').addEventListener('change', updateControls);
$('output-file').addEventListener('input', updateControls);
$('task-type').addEventListener('change', () => {
  if ($('task-type').value === 'conversation') $('output-file').value = '';
  updateControls();
});
$('session-mode').addEventListener('change', () => {
  $('session-file').disabled = $('session-mode').value === 'directory';
});
$('session-select').addEventListener('change', () => selectSession($('session-select').value));
$('session-create').addEventListener('click', async () => {
  $('session-error').hidden = true;
  const scope = $('session-mode').value === 'directory' ? {mode: 'directory'}
    : {mode: 'file', file: $('session-file').value.trim()};
  try {
    const response = await api('/api/sessions', {title: $('session-name').value.trim(), scope});
    await loadSessions(response.session.id, fixedSessionSelection);
  } catch (error) { showError(error, 'session-error'); }
});
$('session-rename').addEventListener('click', async () => {
  if (!activeSessionId) return;
  try {
    await api(`/api/sessions/${activeSessionId}/rename`, {title: $('session-rename-title').value.trim()});
    await loadSessions(activeSessionId, fixedSessionSelection);
  } catch (error) { showError(error, 'session-error'); }
});
for (const action of ['archive', 'restore']) $("session-" + action).addEventListener('click', async () => {
  if (!activeSessionId) return;
  try { await api(`/api/sessions/${activeSessionId}/${action}`, {}); await loadSessions(activeSessionId, fixedSessionSelection); }
  catch (error) { showError(error, 'session-error'); }
});
document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => {
  $('question').value = button.dataset.question; countQuestion(); $('question').focus();
}));
$('question-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy || !ready) return;
  $('form-error').hidden = true; busy = true; activeId = null; viewGeneration++; clearTimeout(timer); updateControls();
  try {
    const question = $('question').value.trim();
    const outputFile = $('output-file').value.trim();
    let job;
    if (activeSessionId) {
      const requestedSession = activeSessionId, generation = viewGeneration;
      const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
      const response = await api(`/api/sessions/${requestedSession}/runs`, {
        client_request_id: requestId, task_type: $('task-type').value,
        question, output_file: outputFile || null
      });
      if (requestedSession !== activeSessionId || generation !== viewGeneration) return;
      job = normalizedSessionRun(response.run);
    } else {
      const body = $('discover').checked ? {mode: 'directory', question} : {file: $('file').value.trim(), question};
      if (outputFile) body.output_file = outputFile;
      job = await api('/api/runs', body);
    }
    prepareOutput(job); render(job); timer = setTimeout(poll, 100);
  } catch (error) {
    if (!error.code || error.code === 'RUN_ACTIVE') {
      // Submission may have succeeded before the connection failed; do not auto-resubmit.
      ready = false; status('请刷新确认任务状态', 'warning');
    }
    busy = false; updateControls(); showError(error);
  }
});
$('cancel').addEventListener('click', async () => {
  if (!activeId || !busy || cancelling) return;
  const requestedId = activeId, requestedSession = activeSessionId, generation = viewGeneration;
  cancelSendingId = requestedId; cancelling = true; $('cancel').disabled = true; updateApprovalControls();
  try {
    const response = await api(runPath(requestedId, '/cancel'), {});
    const job = requestedSession ? normalizedSessionRun(response.run) : response;
    if (cancelSendingId === requestedId) cancelSendingId = null;
    if (requestedId === activeId && requestedSession === activeSessionId && generation === viewGeneration) render(job);
  } catch (error) {
    if (cancelSendingId === requestedId) cancelSendingId = null;
    if (requestedId === activeId && requestedSession === activeSessionId && generation === viewGeneration) {
      cancelling = false; $('cancel').disabled = false; showError(error);
      if (!connectionExpired(error)) updateApprovalControls();
    }
  }
});
async function decideApproval(decision) {
  if (!pendingApproval || !ready || cancelling || Date.now() >= approvalDeadline) return;
  const approvalId = pendingApproval.approval_id || pendingApproval.id;
  if (approvalSendingId === approvalId || approvalBlockedId === approvalId) return;
  const requestedId = activeId, requestedSession = activeSessionId, generation = viewGeneration;
  approvalSendingId = approvalId; $('approval-error').hidden = true; updateApprovalControls();
  try {
    const response = await api(runPath(requestedId, `/approvals/${approvalId}`), {decision});
    const job = requestedSession ? normalizedSessionRun(response.run) : response;
    if (requestedId === activeId && requestedSession === activeSessionId && generation === viewGeneration) render(job);
  } catch (error) {
    if (requestedId !== activeId || requestedSession !== activeSessionId || generation !== viewGeneration) return;
    showError(error, 'approval-error');
    if (!connectionExpired(error)) {
      approvalBlockedId = approvalId;
      clearTimeout(timer); timer = setTimeout(poll, 100);
    }
  } finally {
    if (approvalSendingId === approvalId) approvalSendingId = null;
    if (requestedId === activeId && requestedSession === activeSessionId && generation === viewGeneration) updateApprovalControls();
  }
}
$('approval-allow').addEventListener('click', () => decideApproval('allow'));
$('approval-deny').addEventListener('click', () => decideApproval('deny'));
$('continue-run').addEventListener('click', async () => {
  if (!activeSessionId || !activeId || busy) return;
  const sessionId = activeSessionId, parentId = activeId, generation = viewGeneration;
  busy = true; updateControls();
  try {
    const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
    const response = await api(`/api/sessions/${sessionId}/continue`, {
      run_id: parentId, client_request_id: requestId
    });
    if (sessionId !== activeSessionId || generation !== viewGeneration) return;
    const job = normalizedSessionRun(response.run);
    prepareOutput(job); render(job); timer = setTimeout(poll, 100);
  } catch (error) {
    if (sessionId === activeSessionId && generation === viewGeneration) {
      busy = false; updateControls(); showError(error);
    }
  }
});
$('copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('answer-text').textContent); $('copy').textContent = '已复制'; }
  catch { $('copy').textContent = '请选中文字复制'; }
});
initialize();
