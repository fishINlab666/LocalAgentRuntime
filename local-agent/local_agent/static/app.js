'use strict';

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
let ready = false, simulated = false, liveRunId = null, liveRunSnapshot = null;
let busy = false, timer = null;
let viewGeneration = 0, liveRunRevision = -1;
let sessionRefreshTimer = null, sessionRefreshRequest = null;
let channelSnapshot = null;
const channelRetries = new Set();
let activeSessionId = null, activeSession = null, fixedSessionSelection = false;
let workspaceAvailable = true, historyRuns = [], runHistoryCursor = null, runPageRequest = null;
let inspectedRunId = null, inspectionToken = 0, detailObserver = null;
const runDetails = new Map(), detailRequests = new Map();
let agents = [], capabilities = {skills: [], servers: []}, selectedAgentId = '', managing = false;
let appConfig = null;
let sessionListGeneration = 0, visibleSessions = [], sessionPageRequest = null;
let sessionCursors = {active: null, archived: null};
const sessionAgentIds = new Map();
let importGeneration = 0, activeImport = null;
let importSelection = {kind: 'file', supported: [], ignored: [], errors: []};
let importCapabilities = {formats: [], limits: {max_items: 500, max_files: 50,
  max_file_bytes: 20 * 1024 * 1024, max_total_bytes: 100 * 1024 * 1024}};
const importStateKey = 'local-agent-active-import';
let pendingApproval = null, approvalSendingId = null, approvalBlockedId = null, approvalDeadline = 0, approvalTimer = null;
let cancelling = false, cancelSendingId = null;
let continuableRunId = null;
let drawerTrigger = null;
const errors = {
  CONFIG_MISSING: '模型尚未配置。请在已配置 DEEPSEEK_API_KEY 的终端启动页面服务。',
  CONFIG_INVALID: '模型配置有误，请检查启动服务的环境变量。',
  WORKSPACE_CHANGED: '启动时的工作区路径已被替换，请恢复原目录或重新启动服务。',
  PATH_DENIED: '文件路径不在允许范围内。请填写工作区内的相对路径，不使用链接或隐藏文件。',
  UNSUPPORTED_FILE: '仅支持 UTF-8 编码的 .md 或 .txt 文本文件。',
  FILE_NOT_FOUND: '找不到这个文件，请核对文件名和当前工作区。',
  FILE_TOO_LARGE: '文件超过当前助手的读取限制，请选择更小的文本文件。',
  FILE_CHANGED: '读取时文件发生变化，请等编辑完成后重新提问。',
  READ_ERROR: '无法读取文件，请检查文件权限。',
  PATH_NOT_DISCOVERED: '只能读取本次列出目录后发现的资料，请重新提问。',
  FILE_COUNT_LIMIT: '已达到当前助手的文件数量限制，尚未检查全部文件。',
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
  AGENT_DISABLED: '该助手已停用；历史仍可查看，启用后才能开始新任务。',
  AGENT_CONFIG_INVALID: '助手配置不完整或超出允许范围，请检查模板、工具与预算。',
  SKILL_DISABLED: '该 Skill 已停用，请重新选择能力。',
  SKILL_NOT_BOUND: '该 Skill 没有绑定到当前会话版本，请新建会话使用新绑定。',
  SKILL_DEPENDENCIES_MISSING: 'Skill 所需的工具或环境不可用，请查看能力状态中的缺项。',
  MCP_SERVER_DISABLED: 'MCP Server 已停用，请先启用。',
  IMPORT_FORMAT_UNSUPPORTED: '包含当前版本不支持的文件格式。',
  IMPORT_LIMIT_EXCEEDED: '所选资料超过数量或大小限制，请减少后重试。',
  IMPORT_REQUEST_INVALID: '导入信息不完整，请重新选择资料。',
  IMPORT_PATH_INVALID: '文件夹中包含不安全或重复的相对路径，请重新选择。',
  IMPORT_LENGTH_MISMATCH: '文件在上传期间发生变化，请重新选择后导入。',
  IMPORT_SLOT_ALREADY_STORED: '这个文件已经保存，本次不会重复上传。',
  IMPORT_UPLOAD_INCOMPLETE: '文件上传不完整，请取消后重新导入。',
  IMPORT_STATE_CONFLICT: '当前有另一批资料正在处理，请稍后重试。',
  IMPORT_CANCELLED: '本次导入已取消，没有创建会话。',
  IMPORT_INTEGRITY_ERROR: '本地副本完整性校验失败，请重新导入。',
  IMPORT_UNAVAILABLE: '这批本地资料当前不可用。',
  DOCUMENT_PARSER_UNAVAILABLE: '本机缺少对应的 PDF 或 Word 解析器。',
  TEXT_INVALID_UTF8: 'Markdown 或文本不是有效 UTF-8。',
  TEXT_INVALID_CHARACTER: '文本包含当前版本不能处理的控制字符。',
  PDF_TEXT_NOT_FOUND: 'PDF 没有可提取的文字；当前版本不做 OCR。',
  PDF_ENCRYPTED: 'PDF 需要密码，当前版本不支持。',
  DOCUMENT_CORRUPT: 'PDF 或 Word 文件损坏或结构无效。',
  DOCUMENT_LIMIT_EXCEEDED: '文档页数、解压内容或提取文本超过限制。',
  IMPORT_STORAGE_FAILED: '本地副本未能安全保存，请检查磁盘后重试。',
  STATE_BUSY: '当前有运行、审批或导入，暂时不能开始这项操作。',
};
const eventNames = {'run.started':'任务开始', 'model.requested':'请求模型', 'model.completed':'收到模型回复',
  'tool.requested':'模型请求工具', 'tool.started':'开始执行工具', 'tool.finished':'工具执行结束',
  'tool.completed':'工具结果已回填', 'tool.described':'工具动作说明', 'approval.required':'等待确认', 'approval.resolved':'已收到确认决定',
  'approval.expired':'确认已过期', 'answer.rejected':'回答格式不合规，正在纠错', 'run.ended':'任务结束'};
const sourceLabel = value => ({builtin: '内置', user: '用户工具', skill: 'Skill', mcp: 'MCP'})[value] || value || '未提供';
const riskLabel = value => ({low: '低风险', medium: '中风险', high: '高风险'})[value] || value || '未提供';
const runStateLabel = value => ({queued: '排队中', running: '运行中', waiting_approval: '待确认',
  completed: '已完成', unable: '未完成', validation_failed: '校验失败', timed_out: '已超时',
  cancelled: '已取消', interrupted: '已中断', failed: '失败'})[value] || value || '未知状态';
function formatTimestamp(value) {
  if (value === null || value === undefined || value === '') return '';
  const parsed = typeof value === 'number' && value < 1e12 ? value * 1000 : value;
  const date = new Date(parsed);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false});
}
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

function projectRunReceipt(summary, detail, {liveRunId: liveId = null,
  continuableRunId: continuableId = null} = {}) {
  const run = detail || summary || {};
  const state = run.state || 'unknown';
  const result = detail?.result || null;
  const answer = result?.answer || null;
  const known = Boolean(detail);
  const counts = {
    citationCount: known ? (answer?.citations || answer?.references || []).length : null,
    artifactCount: known ? (result?.artifacts || detail?.artifacts || []).length : null,
    stepCount: known ? (detail?.events || []).length : null,
  };
  let label = runStateLabel(state), message = '正在载入这轮的详细结果。', tone = 'neutral';
  if (state === 'queued') {
    label = '已接收'; message = '已进入本机执行队列。';
  } else if (state === 'running') {
    label = '正在处理'; message = 'Agent 正在调用模型或工具。'; tone = 'info';
  } else if (state === 'waiting_approval') {
    label = '需要你确认'; message = '当前 Run 停在已验证的审批点。'; tone = 'warning';
  } else if (state === 'completed' && answer) {
    label = '已完成'; message = '已生成通过校验的最终回答。'; tone = 'success';
  } else if (state === 'completed') {
    label = known ? '未完成' : '已结束';
    message = known ? '这轮没有可展示的有效最终答案。' : '正在核对这轮的最终结果。';
    tone = known ? 'danger' : 'neutral';
  } else if (state === 'unable') {
    label = '未完成'; message = messageFor(result?.stop_reason || detail?.stop_reason); tone = 'danger';
  } else if (state === 'validation_failed') {
    label = '校验失败'; message = '这轮没有可展示的有效最终答案。'; tone = 'danger';
  } else if (state === 'timed_out') {
    label = '已超时'; message = '这轮在运行预算内没有完成。'; tone = 'danger';
  } else if (state === 'cancelled') {
    label = '已取消'; message = '用户或控制器停止了这轮运行。';
  } else if (state === 'interrupted') {
    label = '已中断'; message = '服务曾停止；请核对执行记录后再决定是否继续。'; tone = 'warning';
  } else if (state === 'failed') {
    label = '失败'; message = messageFor(result?.stop_reason || detail?.stop_reason); tone = 'danger';
  }
  return {runId: run.id || summary?.id || null, state, label, message, tone,
    isLive: Boolean(run.id && run.id === liveId), needsAction: state === 'waiting_approval',
    ...counts, canContinue: state === 'interrupted' && run.id === continuableId};
}

function projectSessionAttention(sessionId, liveRun) {
  if (!sessionId || !liveRun || liveRun.session_id !== sessionId) return null;
  if (['queued', 'running'].includes(liveRun.state)) return {label: '运行中', tone: 'info'};
  if (liveRun.state === 'waiting_approval') return {label: '待确认', tone: 'warning'};
  return null;
}

function projectAgentWorkspace(agentItems, sessionItems, selectedId) {
  const enabled = (agentItems || []).filter(item => item.enabled !== false);
  const selected = enabled.find(item => item.id === selectedId) || enabled[0] || null;
  return {
    agents: enabled,
    selected,
    sessions: selected
      ? (sessionItems || []).filter(item => item.agent_id === selected.id)
      : [],
  };
}

function projectModelStatus(config, selectedAgent, session, liveRun) {
  const frozen = session?.agent_snapshot || null;
  const sourceAgent = frozen || selectedAgent || null;
  const state = liveRun?.state;
  const status = !config?.ready ? '不可用'
    : state === 'queued' ? '排队中'
    : state === 'running' ? '调用中'
    : state === 'waiting_approval' ? '等待确认'
    : '就绪';
  return {
    provider: sourceAgent?.model?.provider || config?.provider?.provider || '未配置',
    model: sourceAgent?.model?.name || '未配置模型',
    status,
    source: frozen ? 'session_snapshot' : 'agent',
    revision: session?.agent_revision || sourceAgent?.revision || null,
    error: config?.error || null,
  };
}

function projectWorkRail(sessionId, liveRun, runSummaries, details) {
  const scopedLive = liveRun?.session_id === sessionId ? liveRun : null;
  const completedSummary = (runSummaries || []).find(item =>
    item.session_id === sessionId && item.state === 'completed') || null;
  let completed = null;
  if (completedSummary) {
    const detailState = details?.get?.(completedSummary.id);
    const detail = detailState?.status === 'ready' ? detailState.run : null;
    const artifacts = (detail?.result?.artifacts || detail?.artifacts || [])
      .filter(item => item && typeof item.id === 'string' && item.id);
    completed = {...completedSummary, ...(detail || {}), artifacts};
  }
  return {
    waiting: scopedLive?.state === 'waiting_approval' ? [scopedLive] : [],
    running: ['queued', 'running'].includes(scopedLive?.state) ? [scopedLive] : [],
    completed: completed ? [completed] : [],
  };
}

function setDrawer(name = null, returnFocus = true) {
  const wasOpen = document.body.dataset.drawer;
  if (name) {
    document.body.dataset.drawer = name;
    drawerTrigger = $(name === 'sidebar' ? (mobileDrawer.matches ? 'mobile-agent-switch' : 'sidebar-toggle')
      : name === 'work' ? 'work-rail-toggle' : 'inspector-toggle');
  } else {
    delete document.body.dataset.drawer;
  }
  $('sidebar-toggle').setAttribute('aria-expanded', String(name === 'sidebar'));
  $('mobile-agent-switch').setAttribute('aria-expanded', String(name === 'sidebar'));
  $('work-rail-toggle').setAttribute('aria-expanded', String(name === 'work'));
  $('inspector-toggle').setAttribute('aria-expanded', String(name === 'inspector'));
  $('workspace-backdrop').hidden = !name;
  syncDrawerAccess();
  if (!name && wasOpen && returnFocus && drawerTrigger) drawerTrigger.focus();
}

function setSessionCreateOpen(open) {
  $('session-create-fields').hidden = !open;
  $('session-create-toggle').setAttribute('aria-expanded', String(open));
  $('session-create-toggle').setAttribute('aria-label', open ? '收起新建会话' : '新建会话');
  if (open) requestAnimationFrame(() => $('session-name').focus());
}

function syncDrawerAccess() {
  const open = document.body.dataset.drawer || null;
  const sidebarHidden = inspectorDrawer.matches && open !== 'sidebar';
  const workHidden = inspectorDrawer.matches && open !== 'work';
  const inspectorHidden = open !== 'inspector';
  for (const [panel, hidden] of [[$('sidebar-panel'), sidebarHidden],
                                 [$('work-rail'), workHidden],
                                 [$('inspector-panel'), inspectorHidden]]) {
    panel.inert = hidden;
    if (hidden) panel.setAttribute('aria-hidden', 'true');
    else panel.removeAttribute('aria-hidden');
  }
}

async function api(path, body) {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), 8000);
  try {
    const agentId = selectedAgentId || activeSession?.agent_id || sessionAgentIds.get(activeSessionId);
    const headers = {'X-Session-Token': token, 'Content-Type': 'application/json'};
    if (agentId) headers['X-Agent-ID'] = agentId;
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', headers,
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

function compatibleImportAgents() {
  const required = ['list_files', 'read_file', 'search_documents'];
  return agents.filter(agent => agent.enabled !== false
    && ['directory', 'combined'].includes(agent.strategy)
    && required.every(name => (agent.tools || []).includes(name)));
}

function importFormatName(extension) {
  return ({'.md': 'Markdown', '.txt': '文本', '.pdf': 'PDF', '.docx': 'Word / DOCX'})[extension]
    || extension || '未知格式';
}

function importPathSafe(value) {
  if (typeof value !== 'string' || !value || value.includes('\\')
      || value.startsWith('/') || value.endsWith('/')) return false;
  if (value.split('/').some(part => !part || part === '.' || part === '..')) return false;
  return ![...value].some(character => /\p{C}/u.test(character));
}

function importNameError() {
  const value = $('import-name').value.trim();
  if (!value) return importSelection.supported.length ? '请填写会话名称。' : '';
  if ([...value].some(character => /\p{C}/u.test(character))) {
    return '会话名称不能包含控制字符。';
  }
  if (new TextEncoder().encode(value).length > 256) {
    return '会话名称过长，请缩短后重试。';
  }
  return '';
}

function importIsCurrent(run) {
  return Boolean(run && activeImport === run && run.generation === importGeneration);
}

function persistImport(run) {
  if (!run?.id) return;
  try {
    sessionStorage.setItem(importStateKey, JSON.stringify({
      id: run.id, agentId: run.agentId, phase: run.phase,
    }));
  } catch (_) {}
}

function clearPersistedImport() {
  try { sessionStorage.removeItem(importStateKey); } catch (_) {}
}

async function importFetch(path, {body, agentId, signal, binary = false} = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener('abort', abort, {once: true});
  let timedOut = false;
  const deadline = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, binary ? 30000 : 8000);
  const headers = {'X-Session-Token': token, 'X-Agent-ID': agentId};
  if (body !== undefined) headers['Content-Type'] = binary
    ? 'application/octet-stream' : 'application/json';
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST', headers,
      body: body === undefined ? undefined : binary ? body : JSON.stringify(body),
      signal: controller.signal,
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(messageFor(data.error));
      error.code = data.error;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError' && signal?.aborted) throw error;
    if (error.name === 'AbortError' && timedOut) {
      throw new Error('本地导入请求超时。请保持服务开启后重试，或取消本批导入。');
    }
    if (error.name === 'AbortError') throw error;
    if (typeof error.code === 'string') throw error;
    throw new Error('与本地导入服务的连接中断。请保持服务开启，并取消后重新选择资料。');
  } finally {
    clearTimeout(deadline);
    signal?.removeEventListener('abort', abort);
  }
}

const importJson = (path, body, agentId, signal) => importFetch(
  path, {body, agentId, signal});
const importBinary = (path, file, agentId, signal) => importFetch(
  path, {body: file, agentId, signal, binary: true});

function setImportStatus(text, {summary = false} = {}) {
  $('import-status').textContent = text;
  if (summary) {
    $('import-summary').textContent = text;
    $('import-summary').hidden = false;
  }
}

function renderImportAgents() {
  const choices = compatibleImportAgents();
  const wanted = activeImport?.agentId || $('import-agent').value
    || (choices.some(agent => agent.id === selectedAgentId) ? selectedAgentId : '')
    || choices[0]?.id || '';
  selectOptions($('import-agent'), choices.map(agent => ({
    value: agent.id, label: `${agent.name} · ${agent.strategy === 'combined' ? '组合' : '目录'}`,
  })), choices.length ? null : '没有可用的导入助手', wanted);
}

function renderImportList(element, entries) {
  element.replaceChildren();
  for (const item of entries) {
    const row = document.createElement('li');
    row.textContent = `${item.logicalPath} · ${item.file.size} bytes${item.reason ? ` · ${item.reason}` : ''}`;
    element.append(row);
  }
}

function updateImportControls() {
  const importing = Boolean(activeImport);
  const available = importCapabilities.formats.length > 0;
  const readyToStart = !importing && importSelection.supported.length > 0
    && importSelection.errors.length === 0 && Boolean($('import-agent').value)
    && !importNameError() && !fixedSessionSelection && available;
  $('import-files').disabled = importing;
  $('import-directory').disabled = importing;
  $('import-name').disabled = importing;
  $('import-agent').disabled = importing || compatibleImportAgents().length === 0;
  $('import-start').disabled = !readyToStart;
  $('import-start').textContent = importing ? '正在导入…' : '保存副本并创建会话';
  $('import-cancel').disabled = !activeImport?.id
    || ['cancelling', 'handoff'].includes(activeImport.phase);
  $('import-close').disabled = importing;
  $('import-trigger').disabled = fixedSessionSelection || busy || managing || importing
    || compatibleImportAgents().length === 0 || !available;
}

function renderImportPreflight() {
  $('import-supported-count').textContent = String(importSelection.supported.length);
  $('import-ignored-count').textContent = String(importSelection.ignored.length);
  renderImportList($('import-supported-list'), importSelection.supported);
  renderImportList($('import-ignored-list'), importSelection.ignored);
  $('import-ignored-block').hidden = importSelection.ignored.length === 0;
  if (!activeImport) {
    const validationErrors = [...importSelection.errors];
    const nameError = importNameError();
    if (nameError) validationErrors.push(nameError);
    $('import-error').hidden = validationErrors.length === 0;
    $('import-error').textContent = validationErrors.join(' ');
    setImportStatus(importSelection.supported.length
      ? `已选 ${importSelection.supported.length} 份支持资料，可以保存本地副本。`
      : '请选择资料。');
    $('import-progress').hidden = true;
    $('import-progress').value = 0;
  }
  updateImportControls();
}

function chooseImportFiles(kind, fileList) {
  if (activeImport) return;
  importGeneration++;
  const files = [...fileList];
  const limits = importCapabilities.limits || {};
  const availability = new Map((importCapabilities.formats || []).map(
    item => [String(item.extension || '').toLowerCase(), item.available !== false]));
  const supportedExtensions = new Set(['.md', '.txt', '.pdf', '.docx']);
  const supported = [], ignored = [], errors = [], seen = new Set();
  let totalBytes = 0;
  for (const file of files) {
    const logicalPath = kind === 'folder' ? file.webkitRelativePath : file.name;
    if (!importPathSafe(logicalPath) || seen.has(logicalPath)) {
      errors.push(`“${logicalPath || file.name}”的相对路径不安全或重复。`);
      continue;
    }
    seen.add(logicalPath);
    const basename = logicalPath.split('/').at(-1);
    const dot = basename.lastIndexOf('.');
    const suffix = dot > 0 ? basename.slice(dot).toLowerCase() : '';
    const item = {file, logicalPath, extension: suffix};
    if (!supportedExtensions.has(suffix)) {
      item.reason = '格式不支持'; ignored.push(item); continue;
    }
    if (availability.has(suffix) && !availability.get(suffix)) {
      errors.push(`${importFormatName(suffix)} 解析器未安装或不可用。`);
    }
    if (file.size > (limits.max_file_bytes || 20 * 1024 * 1024)) {
      errors.push(`“${logicalPath}”超过单文件大小限制。`);
    }
    totalBytes += file.size;
    supported.push(item);
  }
  if (files.length > (limits.max_items || 500)) errors.push('所选文件总数超过 500。');
  if (supported.length > (limits.max_files || 50)) errors.push('支持文件超过 50 份。');
  if (totalBytes > (limits.max_total_bytes || 100 * 1024 * 1024)) {
    errors.push('支持文件总大小超过 100 MiB。');
  }
  const uniqueErrors = [...new Set(errors)];
  importSelection = {kind, supported, ignored, errors: uniqueErrors};
  if (!$('import-name').value.trim() && supported.length) {
    const first = supported[0].logicalPath;
    $('import-name').value = kind === 'folder' ? first.split('/')[0]
      : supported.length === 1 ? first.replace(/\.[^.]+$/u, '') : `${supported.length} 份本地资料`;
  }
  renderImportPreflight();
}

function openImportDialog() {
  renderImportAgents();
  renderImportPreflight();
  if (!$('import-dialog').open) $('import-dialog').showModal();
  requestAnimationFrame(() => $('import-files').focus());
}

function closeImportDialog() {
  if (activeImport) {
    setImportStatus('导入仍在进行，请先取消本次导入。');
    return;
  }
  importGeneration++;
  if ($('import-dialog').open) $('import-dialog').close();
  requestAnimationFrame(() => $('import-trigger').focus());
}

function importDelay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const timerId = setTimeout(done, milliseconds);
    function aborted() {
      clearTimeout(timerId);
      signal?.removeEventListener('abort', aborted);
      reject(new DOMException('Aborted', 'AbortError'));
    }
    function done() {
      signal?.removeEventListener('abort', aborted);
      resolve();
    }
    signal?.addEventListener('abort', aborted, {once: true});
  });
}

function renderImportSources(session) {
  const sources = session?.import?.files || [];
  $('import-source-list').replaceChildren();
  $('import-sources').hidden = sources.length === 0;
  if (!sources.length) return;
  $('import-source-count').textContent = `本地副本已保存 · ${sources.length} 份资料`;
  for (const source of sources) {
    const card = document.createElement('article');
    const title = document.createElement('strong');
    const details = document.createElement('span');
    const hash = document.createElement('code');
    card.className = 'import-source-card';
    title.textContent = source.logical_path;
    const stats = source.stats || {};
    const units = stats.page_count ? `${stats.page_count} 页`
      : stats.unit_count !== undefined ? `${stats.unit_count} 个内容单元` : '';
    details.textContent = `${String(source.format || '').toUpperCase()} · ${source.bytes} bytes · ${source.chunks} 个块`
      + (units ? ` · ${units}` : '')
      + ((source.warnings || []).length ? ` · ${(source.warnings || []).length} 条提取警告` : '');
    hash.textContent = `SHA-256 ${source.sha256}`;
    card.append(title, details, hash); $('import-source-list').append(card);
  }
}

async function finishImport(run, snapshot) {
  if (!importIsCurrent(run) || snapshot.status !== 'ready' || !snapshot.session_id) return;
  run.phase = 'handoff';
  updateImportControls();
  selectedAgentId = run.agentId;
  $('agent-select').value = run.agentId;
  try {
    const loaded = await loadSessions(snapshot.session_id, fixedSessionSelection);
    if (!loaded || activeSession?.id !== snapshot.session_id
        || activeSession?.agent_id !== run.agentId) {
      throw new Error('本地副本已经保存，但新会话未能打开；请刷新页面重试。');
    }
  } catch (error) {
    if (importIsCurrent(run)) {
      $('import-error').textContent = '本地副本已经保存，但会话列表刷新失败；请刷新页面。';
      $('import-error').hidden = false;
    }
    return;
  }
  if (!importIsCurrent(run)) return;
  const count = snapshot.total_files || run.files.length;
  clearPersistedImport();
  setImportStatus(`本地副本已保存 · ${count} 份资料`, {summary: true});
  $('import-error').hidden = true;
  $('import-progress').hidden = true;
  activeImport = null;
  importGeneration++;
  updateControls();
  if ($('import-dialog').open) $('import-dialog').close();
  if (document.body.dataset.drawer) setDrawer(null, false);
  requestAnimationFrame(() => $('question').focus());
}

async function pollImport(run, snapshot = null) {
  let current = snapshot;
  while (importIsCurrent(run)) {
    if (current?.status === 'ready') {
      if (typeof current.session_id !== 'string' || !current.session_id) {
        clearPersistedImport();
        activeImport = null;
        $('import-progress').hidden = true;
        $('import-error').textContent = '服务端已完成导入，但没有返回可打开的会话。请刷新页面检查。';
        $('import-error').hidden = false;
        setImportStatus('本次导入无法进入会话。');
        updateControls();
        return;
      }
      return finishImport(run, current);
    }
    if (['failed', 'cancelled'].includes(current?.status)) {
      clearPersistedImport();
      activeImport = null;
      $('import-progress').hidden = true;
      const code = current.error_code || (current.status === 'cancelled' ? 'IMPORT_CANCELLED' : 'IMPORT_STORAGE_FAILED');
      $('import-error').textContent = messageFor(code);
      $('import-error').hidden = false;
      setImportStatus(current.status === 'cancelled' ? '本次导入已取消。' : '本次导入未完成。');
      updateControls();
      return;
    }
    try {
      setImportStatus('正在解析、建立索引并创建会话…');
      await importDelay(250, run.controller.signal);
      if (!importIsCurrent(run)) return;
      current = await importJson(`/api/imports/${encodeURIComponent(run.id)}`,
        undefined, run.agentId, run.controller.signal);
    } catch (error) {
      if (!importIsCurrent(run) || error.name === 'AbortError') return;
      $('import-error').textContent = error.message;
      $('import-error').hidden = false;
      setImportStatus('暂时无法取得导入进度，正在重试…');
      try { await importDelay(1000, run.controller.signal); }
      catch (_) { return; }
    }
  }
}

async function startImport() {
  if (activeImport) return;
  renderImportPreflight();
  if ($('import-start').disabled) return;
  const generation = ++importGeneration;
  const agentId = $('import-agent').value;
  const files = importSelection.supported.map(item => ({...item}));
  const ignored = importSelection.ignored.map(item => ({...item}));
  const controller = new AbortController();
  const run = {generation, agentId, files, ignored, controller, id: null,
    phase: 'begin', completeSent: false};
  activeImport = run;
  $('import-error').hidden = true;
  $('import-progress').hidden = false;
  $('import-progress').max = Math.max(1, files.length);
  $('import-progress').value = 0;
  setImportStatus('正在建立安全的本地副本…');
  updateControls();
  const metadata = {
    kind: importSelection.kind,
    name: $('import-name').value.trim(),
    agent_id: agentId,
    files: files.map(item => ({logical_path: item.logicalPath, bytes: item.file.size})),
    ignored: ignored.map(item => ({logical_path: item.logicalPath, bytes: item.file.size})),
  };
  try {
    const batch = await importJson('/api/imports', metadata, agentId, controller.signal);
    if (!importIsCurrent(run)) return;
    run.id = batch.id;
    run.phase = 'uploading';
    persistImport(run);
    updateImportControls();
    const selected = new Map(files.map(item => [item.logicalPath, item]));
    if (!Array.isArray(batch.files) || batch.files.length !== files.length) {
      const error = new Error(messageFor('IMPORT_REQUEST_INVALID'));
      error.code = 'IMPORT_REQUEST_INVALID'; throw error;
    }
    for (let index = 0; index < batch.files.length; index++) {
      const slot = batch.files[index];
      const item = selected.get(slot.logical_path);
      if (!item || item.file.size !== slot.declared_bytes) {
        const error = new Error(messageFor('IMPORT_LENGTH_MISMATCH'));
        error.code = 'IMPORT_LENGTH_MISMATCH'; throw error;
      }
      setImportStatus(`正在复制 ${index + 1} / ${files.length} · ${item.logicalPath}`);
      await importBinary(
        `/api/imports/${encodeURIComponent(run.id)}/files/${encodeURIComponent(slot.slot_id)}`,
        item.file, agentId, controller.signal);
      if (!importIsCurrent(run)) return;
      $('import-progress').value = index + 1;
      setImportStatus(`已复制 ${index + 1} / ${files.length} · ${item.logicalPath}`);
    }
    run.phase = 'finalizing';
    run.completeSent = true;
    persistImport(run);
    setImportStatus(`已复制 ${files.length} / ${files.length}，正在开始解析…`);
    const snapshot = await importJson(
      `/api/imports/${encodeURIComponent(run.id)}/complete`, {}, agentId, controller.signal);
    if (!importIsCurrent(run)) return;
    await pollImport(run, snapshot);
  } catch (error) {
    if (!importIsCurrent(run) || error.name === 'AbortError') return;
    if (run.completeSent) {
      $('import-error').textContent = error.message;
      $('import-error').hidden = false;
      setImportStatus('无法确认解析请求结果，正在查询服务端状态…');
      await pollImport(run);
      return;
    }
    $('import-error').textContent = error.message;
    $('import-error').hidden = false;
    setImportStatus(run.completeSent
      ? '无法确认解析状态；请取消后重新导入。'
      : run.id ? '导入中断；请取消本批后重新选择资料。'
        : '导入没有开始，请检查后重试。');
    run.phase = 'upload-error';
    persistImport(run);
    if (!run.id) {
      activeImport = null;
      clearPersistedImport();
      $('import-progress').hidden = true;
    }
    updateControls();
  }
}

async function cancelImport() {
  const previous = activeImport;
  if (!previous) return;
  previous.controller?.abort();
  const run = {...previous, generation: ++importGeneration, controller: null,
    phase: 'cancelling'};
  activeImport = run;
  setImportStatus('正在取消并清理本次临时副本…');
  updateControls();
  try {
    let snapshot = null;
    if (run.id) {
      try {
        snapshot = await importJson(
          `/api/imports/${encodeURIComponent(run.id)}/cancel`, {}, run.agentId);
      } catch (error) {
        if (error.code !== 'IMPORT_STATE_CONFLICT') throw error;
        snapshot = await importJson(
          `/api/imports/${encodeURIComponent(run.id)}`, undefined, run.agentId);
      }
    }
    if (!importIsCurrent(run)) return;
    if (snapshot?.status === 'ready') {
      run.controller = new AbortController();
      return finishImport(run, snapshot);
    }
    if (snapshot?.status === 'finalizing') {
      run.phase = 'finalizing';
      run.controller = new AbortController();
      persistImport(run);
      updateControls();
      return pollImport(run, snapshot);
    }
    clearPersistedImport();
    activeImport = null;
    $('import-progress').hidden = true;
    $('import-error').hidden = true;
    setImportStatus('本次导入已取消，可以重新选择资料。');
    updateControls();
  } catch (error) {
    if (!importIsCurrent(run)) return;
    $('import-error').textContent = error.message;
    $('import-error').hidden = false;
    setImportStatus('取消未完成，请保持服务开启后重试。');
    run.phase = 'error';
    updateControls();
  }
}

async function resumePersistedImport() {
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(importStateKey) || 'null'); }
  catch (_) { clearPersistedImport(); return; }
  if (!saved || typeof saved.id !== 'string' || typeof saved.agentId !== 'string') return;
  const run = {id: saved.id, agentId: saved.agentId, phase: saved.phase,
    generation: ++importGeneration, controller: new AbortController(), files: [], ignored: []};
  activeImport = run;
  renderImportAgents();
  openImportDialog();
  if (['uploading', 'begin', 'upload-error', 'error'].includes(saved.phase)) {
    run.controller = null;
    $('import-error').textContent = '页面刷新后无法重新取得浏览器文件，请取消本批并重新选择。';
    $('import-error').hidden = false;
    setImportStatus('这批文件尚未上传完成。');
    updateImportControls();
    return;
  }
  setImportStatus('正在恢复导入进度…');
  try {
    const snapshot = await importJson(
      `/api/imports/${encodeURIComponent(run.id)}`, undefined, run.agentId, run.controller.signal);
    if (importIsCurrent(run)) await pollImport(run, snapshot);
  } catch (error) {
    if (!importIsCurrent(run) || error.name === 'AbortError') return;
    clearPersistedImport();
    activeImport = null;
    $('import-error').textContent = error.message;
    $('import-error').hidden = false;
    setImportStatus('无法恢复上一批导入。');
    updateControls();
  }
}

function currentAgent() {
  // An old session without a recorded snapshot must not inherit today's bindings.
  if (activeSessionId) return activeSession?.agent_snapshot || null;
  if (selectedAgentId) return agents.find(agent => agent.id === selectedAgentId) || null;
  return agents.find(agent => agent.id === ($('discover').checked ? 'directory-qa' : 'file-qa')) || null;
}

function renderAgentList() {
  const list = $('agent-list');
  list.replaceChildren();
  const currentId = activeSession?.agent_id || selectedAgentId || currentAgent()?.id;
  const workspace = projectAgentWorkspace(agents, visibleSessions, currentId);
  for (const agent of workspace.agents) {
    const button = document.createElement('button');
    const name = document.createElement('strong'), details = document.createElement('span');
    button.type = 'button'; button.className = 'agent-nav-item'; button.dataset.agentId = agent.id;
    button.setAttribute('aria-pressed', String(agent.id === currentId));
    button.setAttribute('aria-label', `切换到${agent.name}`);
    button.title = agent.name;
    button.disabled = busy || managing || Boolean(activeImport);
    name.textContent = [...agent.name.trim()][0] || 'A';
    details.textContent = `${agent.name} · ${agent.strategy === 'file' ? '单文件' : '目录'}`;
    button.addEventListener('click', () => {
      $('agent-select').value = agent.id;
      selectAgent(agent.id);
      if (inspectorDrawer.matches) setDrawer();
    });
    button.append(name, details); list.append(button);
  }
}

function renderModelStatus() {
  const selected = agents.find(agent => agent.id === selectedAgentId) || currentAgent();
  const view = projectModelStatus(appConfig, selected, activeSession, liveRunSnapshot);
  $('model-status').dataset.state = view.status;
  $('model-name').textContent = view.model;
  $('model-state').textContent = view.status;
  $('model-provider').textContent = `Provider：${view.provider}`;
  $('model-source').textContent = view.source === 'session_snapshot'
    ? '来源：当前会话创建时冻结的助手配置'
    : '来源：当前助手配置（新会话生效）';
  $('model-revision').textContent = `修订：${view.revision ? String(view.revision).slice(0, 12) : '未提供'}`;
  $('model-status').title = view.error ? messageFor(view.error) : `${view.model} · ${view.status}`;
}

function workItem(run, kind) {
  const button = document.createElement('button');
  const title = document.createElement('strong'), details = document.createElement('span');
  button.type = 'button'; button.className = 'work-item'; button.dataset.runId = run.id || '';
  title.textContent = run.question || (kind === 'waiting' ? '有一项操作需要确认'
    : kind === 'running' ? '正在处理当前任务' : '最近完成的任务');
  const artifacts = run.artifacts || [];
  details.textContent = kind === 'waiting' ? '打开对话中的确认卡后决定是否继续'
    : kind === 'running' ? (run.state === 'queued' ? '已接收，正在排队' : 'Agent 正在调用模型或工具')
    : artifacts.length ? `已完成 · ${artifacts.length} 个文件` : '已完成 · 可查看回答和依据';
  button.addEventListener('click', () => {
    if (run.id && activeSessionId) openSessionRun(run.id).then(() => setDrawer('inspector'));
    else if (kind === 'waiting') $('approval-title').focus();
  });
  button.append(title, details);
  return button;
}

function renderWorkRail() {
  const view = projectWorkRail(activeSessionId, liveRunSnapshot, historyRuns, runDetails);
  for (const [name, items] of Object.entries(view)) {
    const target = $(`work-${name}`);
    target.replaceChildren();
    if (!items.length) {
      const empty = document.createElement('p');
      empty.className = 'work-empty';
      empty.textContent = name === 'completed' ? '还没有交付' : '暂无';
      target.append(empty);
      continue;
    }
    target.append(...items.map(item => workItem(item, name)));
  }
  $('work-detail-button').disabled = !inspectedRunId;
}

function renderActiveCapabilities() {
  const list = $('active-capabilities'), snapshot = currentAgent();
  list.replaceChildren();
  if (!snapshot) {
    const empty = document.createElement('p');
    empty.className = 'field-help'; empty.textContent = '选择一个助手后查看它可以调用的能力。';
    list.append(empty); return;
  }
  const entries = [
    ...(snapshot.tools || []).map(name => ({kind: '工具', name})),
    ...(snapshot.skills || []).map(item => ({kind: 'Skill', name: item.id})),
    ...(snapshot.mcp || []).map(item => ({kind: 'MCP', name: item.id})),
  ];
  if (!entries.length) {
    const empty = document.createElement('p');
    empty.className = 'field-help'; empty.textContent = '这个助手当前没有已绑定能力。';
    list.append(empty); return;
  }
  for (const entry of entries) {
    const item = document.createElement('span');
    item.className = 'capability-chip'; item.textContent = `${entry.kind} · ${entry.name}`;
    list.append(item);
  }
}

function selectOptions(element, entries, emptyLabel, wanted = element.value) {
  element.replaceChildren();
  const options = emptyLabel === null ? entries : [{value: '', label: emptyLabel}, ...entries];
  for (const item of options) {
    const option = document.createElement('option');
    option.value = item.value; option.textContent = item.label; option.disabled = Boolean(item.disabled);
    element.append(option);
  }
  element.value = options.some(item => item.value === wanted && !item.disabled)
    ? wanted : options.find(item => !item.disabled)?.value || '';
}

function renderRunChoices() {
  const snapshot = currentAgent();
  const skills = (snapshot?.skills || []).map(binding => {
    const installed = capabilities.skills.find(item => item.id === binding.id && item.version === binding.version);
    return {value: binding.id, label: `${installed?.name || binding.id} · ${binding.version.slice(0, 8)}`,
      disabled: installed?.enabled === false};
  });
  const prompts = (snapshot?.mcp || []).flatMap(binding => {
    const server = capabilities.servers.find(item => item.id === binding.id && item.version === binding.version);
    return (binding.prompts || []).map(name => ({value: JSON.stringify({server_id: binding.id, name}),
      label: `${binding.id} / ${name}`, disabled: server?.enabled === false}));
  });
  selectOptions($('run-skill'), skills, '由模型选择');
  selectOptions($('run-prompt'), prompts, '不指定模板');
  $('run-skill').disabled = busy || !skills.length;
  $('run-prompt').disabled = busy || !prompts.length;
  renderActiveCapabilities();
}

function updateAgentControls() {
  const selected = agents.find(agent => agent.id === selectedAgentId);
  const snapshot = currentAgent(), limits = snapshot?.budgets;
  const importing = Boolean(activeImport);
  const displayName = snapshot?.name || selected?.name || '选择助手';
  $('current-agent-name').textContent = displayName;
  $('thread-agent-name').textContent = displayName;
  $('mobile-agent-name').textContent = displayName;
  $('thread-session-name').textContent = activeSession?.title || '新任务';
  const miniMark = document.querySelector('.agent-mini-mark');
  if (miniMark) miniMark.textContent = [...displayName][0] || 'A';
  $('agent-select').disabled = busy || managing || importing || fixedSessionSelection;
  $('agent-status').textContent = activeSessionId
    ? `会话助手：${activeSession?.agent_id || '正在加载'} · 修订 ${activeSession?.agent_revision || '未知'}。旧会话保持此修订。`
    : selected ? `${selected.name} · 修订 ${(selected.revision || '').slice(0, 12)}${selected.enabled === false ? ' · 已停用' : ''}`
      : '按单文件 / 目录模式选择内置助手；选择具体助手可管理其会话与能力。';
  $('discover-help').textContent = '从当前工作区列出的 .md / .txt 资料中选择读取'
    + (limits?.max_files ? `，最多 ${limits.max_files} 份。` : '。') + '勾选后只需填写问题。';
  $('file-help').textContent = '填写工作区内的相对路径 · .md / .txt'
    + (limits?.max_file_bytes ? ` · 最大 ${Number((limits.max_file_bytes / 1024).toFixed(2))} KiB` : '');
  if (selected) {
    const mode = selected.strategy === 'file' ? 'file' : 'directory';
    $('session-mode').value = mode;
    if (!activeSessionId) $('discover').checked = mode === 'directory';
  }
  $('session-mode').disabled = busy || importing || Boolean(selected) || fixedSessionSelection;
  $('session-file').disabled = busy || importing || $('session-mode').value === 'directory';
  $('session-select').disabled = busy || managing || importing;
  $('discover').disabled = busy || importing || Boolean(activeSessionId) || Boolean(selected) || !ready;
  $('file').disabled = busy || importing || Boolean(activeSessionId) || $('discover').checked;
  $('file').required = !activeSessionId && !$('discover').checked;
  const current = agents.find(agent => agent.id === (activeSession?.agent_id || selectedAgentId));
  if (current?.enabled === false) $('start').disabled = true;
  if (selected?.enabled === false) $('session-create').disabled = true;
  for (const element of document.querySelectorAll('[data-management]')) element.disabled = busy || managing || importing;
  $('agent-toggle').disabled = busy || managing || importing || !selected;
  $('agent-toggle').textContent = selected?.enabled === false ? '启用当前助手' : '停用当前助手';
  for (const button of document.querySelectorAll('[data-bind-kind]')) {
    button.disabled = busy || managing || importing || !selected || selected.enabled === false || button.dataset.bound === 'true';
  }
  for (const button of document.querySelectorAll('[data-agent-id],[data-session-id]')) {
    button.disabled = busy || managing || importing;
  }
  renderRunChoices();
}

function fillAgentTemplate() {
  const template = agents.find(agent => agent.id === $('agent-template').value);
  if (!template) return;
  $('agent-model').value = template.model?.name || '';
  $('agent-instructions').value = template.instructions || '';
  $('agent-advanced').value = JSON.stringify({tools: template.tools, budgets: template.budgets}, null, 2);
}

function capabilityState(item) {
  const missing = [...(item.missing || []), ...(item.dependencies?.unsupported || [])];
  if (item.dependencies?.scripts) missing.push('script_execution_unsupported');
  if (item.enabled === false) return '已停用';
  const state = {ready: '已就绪', imported: '已导入', unavailable: '不可用', disabled: '已停用', error: '错误'}[item.status]
    || (missing.length ? '依赖不满足' : '已导入');
  return state + (missing.length ? ` · 缺项：${missing.join('、')}` : '')
    + (item.error ? ` · ${item.error}` : '') + (item.reason ? ` · ${item.reason}` : '');
}

function managementButton(label, handler) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'secondary'; button.textContent = label;
  button.dataset.management = '';
  button.addEventListener('click', handler);
  return button;
}

function renderCapabilities() {
  $('capability-list').replaceChildren();
  const selected = agents.find(agent => agent.id === selectedAgentId);
  for (const [kind, items] of [['skill', capabilities.skills], ['mcp', capabilities.servers]]) {
    for (const item of items) {
      const article = document.createElement('article'), title = document.createElement('p');
      const details = document.createElement('p'), actions = document.createElement('div');
      article.className = 'capability-item'; actions.className = 'actions';
      title.textContent = `${kind === 'skill' ? 'Skill' : 'MCP'} · ${item.name || item.id} · ${(item.version || '').slice(0, 8)}`;
      details.textContent = capabilityState(item)
        + (item.description ? ` · ${item.description}` : '')
        + (typeof item.compatibility === 'string' ? ` · 兼容说明：${item.compatibility}` : '');
      const bound = (selected?.[kind === 'skill' ? 'skills' : 'mcp'] || []).some(
        binding => binding.id === item.id && binding.version === item.version);
      const bind = managementButton(bound ? '已绑定当前修订' : '绑定到所选助手', () => manage(async () => {
        if (!selectedAgentId) return;
        await api(`/api/agents/${encodeURIComponent(selectedAgentId)}/bind`, {kind, id: item.id, version: item.version});
        await loadExtensions();
        $('extension-status').textContent = '绑定已保存。请新建会话使用新修订；旧会话仍使用原版本。';
      }));
      bind.dataset.bindKind = kind; bind.dataset.capabilityId = item.id; bind.dataset.bound = String(bound);
      const toggle = managementButton(item.enabled === false ? '启用' : '停用', () => manage(async () => {
        await api(`/api/${kind === 'skill' ? 'skills' : 'mcp'}/${encodeURIComponent(item.id)}/enabled`, {enabled: item.enabled === false});
        await loadExtensions(); $('extension-status').textContent = '能力状态已更新。';
      }));
      actions.append(bind, toggle);
      if (kind === 'mcp') {
        const probe = managementButton('测试连接', () => manage(async () => {
          const response = await api(`/api/mcp/${encodeURIComponent(item.id)}/probe`, {});
          const catalog = response.catalog || {};
          $('extension-status').textContent = `连接检查完成：工具 ${catalog.tools?.length || 0}、资源 ${catalog.resources?.length || 0}、模板 ${catalog.prompts?.length || 0}。`;
        }));
        probe.dataset.probeId = item.id; actions.append(probe);
      }
      article.append(title, details, actions); $('capability-list').append(article);
    }
  }
}

async function loadExtensions() {
  const [agentData, capabilityData] = await Promise.all([api('/api/agents'), api('/api/capabilities')]);
  agents = agentData.agents || [];
  capabilities = {skills: capabilityData.skills || [], servers: capabilityData.servers || []};
  if (!selectedAgentId && !activeSessionId) {
    const preferredId = $('discover').checked ? 'directory-qa' : 'file-qa';
    selectedAgentId = agents.find(agent => agent.enabled !== false && agent.id === preferredId)?.id
      || agents.find(agent => agent.enabled !== false)?.id || '';
  }
  const templateWas = $('agent-template').value;
  selectOptions($('agent-select'), agents.map(agent => ({value: agent.id,
    label: `${agent.name}${agent.enabled === false ? '（已停用）' : ''}`})), '自动选择（按单文件 / 目录模式）', selectedAgentId);
  selectOptions($('agent-template'), agents.map(agent => ({value: agent.id, label: agent.name})), null);
  if (!templateWas) fillAgentTemplate();
  renderAgentList(); renderCapabilities(); renderImportAgents(); updateControls();
}

async function manage(action) {
  if (busy || managing || activeImport) return;
  managing = true; $('extension-error').hidden = true; $('extension-status').textContent = '正在处理…'; updateControls();
  try { await action(); }
  catch (error) { $('extension-status').textContent = ''; showError(error, 'extension-error'); }
  finally { managing = false; updateControls(); }
}

async function selectAgent(agentId) {
  if (busy || activeImport) return;
  selectedAgentId = agentId;
  activeSessionId = null; activeSession = null; viewGeneration++;
  resetTranscriptState(); historyRuns = []; runHistoryCursor = null;
  $('session-select').value = ''; $('session-actions').hidden = true; $('session-scope').hidden = true;
  $('question').value = ''; $('output-file').value = ''; countQuestion();
  renderRunHistory([]); clearRunView(); renderAgentList(); renderCapabilities();
  try { await loadSessions(); }
  catch (error) { showError(error, 'session-error'); }
  updateControls();
}

function selectedRunCapabilities() {
  const selected = {};
  if ($('run-skill').value) selected.skill_id = $('run-skill').value;
  if ($('run-prompt').value) selected.mcp_prompt = JSON.parse($('run-prompt').value);
  return selected;
}

function updateControls() {
  const sessionMode = Boolean(activeSessionId);
  const conversation = sessionMode && $('task-type').value === 'conversation';
  const archived = activeSession?.status === 'archived';
  const importing = Boolean(activeImport);
  const missingScope = !sessionMode && !$('discover').checked && !$('file').value.trim();
  $('start').disabled = busy || importing || !ready || archived
    || (sessionMode && !conversation && !workspaceAvailable);
  $('start').setAttribute('aria-label', busy ? '任务处理中' : '发送任务');
  $('start').title = busy ? '任务处理中' : '发送任务';
  $('thread-panel').setAttribute('aria-busy', String(busy));
  $('cancel').hidden = !busy || !liveRunId;
  $('discover').disabled = sessionMode || busy || importing || !ready;
  $('file').disabled = sessionMode || busy || importing || $('discover').checked;
  $('file').required = !sessionMode && !$('discover').checked;
  $('task-type').disabled = busy || importing || !sessionMode;
  $('output-file').disabled = busy || importing || conversation;
  for (const element of [$('question'), $('sample'), ...document.querySelectorAll('[data-question]')]) element.disabled = busy || importing;
  $('session-create').disabled = busy || importing || fixedSessionSelection;
  $('session-rename').disabled = busy || importing || !activeSessionId;
  $('session-archive').disabled = busy || importing || !activeSessionId;
  $('session-restore').disabled = busy || importing || !activeSessionId;
  $('privacy').textContent = simulated ? '演示模式：只在本机读取资料，不向模型发送内容。原文件保持只读。'
    : conversation ? '本轮只把问题和本会话的受限历史发送给 DeepSeek，不读取工作区文件。'
    : $('discover').checked ? '开始后，问题、目录元数据和已读取的资料内容将发送给 DeepSeek。原文件保持只读。'
    : '开始后，所选文件内容和问题将发送给 DeepSeek。原文件保持只读。';
  if ($('output-file').value.trim()) $('privacy').textContent += ' 输出文件的完整内容经你确认后才新建。';
  $('composer-settings').classList.toggle('needs-attention', missingScope);
  $('run-settings-summary').textContent = missingScope ? '需要先选择文件'
    : sessionMode ? '使用当前会话的固定范围'
    : $('discover').checked ? '当前工作区目录发现'
    : `文件：${$('file').value.trim()}`;
  updateAgentControls();
  renderModelStatus();
  renderWorkRail();
  updateApprovalControls();
  updateImportControls();
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

function renderDecisionCard(approval, mode, handlers = {}) {
  if (!['live', 'historical'].includes(mode)) throw new Error(`Unknown decision-card mode: ${mode}`);
  const live = mode === 'live';
  const root = $(live ? 'approval' : 'approval-history');
  root.dataset.decisionMode = mode; root.classList.toggle('decision-card', true);
  root.hidden = !approval;
  if (!approval) {
    const ids = live ? ['approval-action', 'approval-details', 'approval-intent', 'approval-content']
      : ['approval-history-action', 'approval-history-details', 'approval-history-intent',
        'approval-history-status', 'approval-history-content'];
    for (const id of ids) $(id).textContent = '';
    return root;
  }
  const view = live ? approval : {...(approval.preview || {}), ...approval};
  const prefix = live ? 'approval' : 'approval-history';
  const preview = live ? view : approval.preview || view;
  const bytes = Number.isFinite(Number(preview.bytes)) ? `${Number(preview.bytes)} 字节` : '大小未记录';
  const operation = ['create', 'created'].includes(preview.operation)
    ? '新建，不覆盖' : preview.operation || '操作未记录';
  $(`${prefix}-action`).textContent = `实际操作：${preview.action_summary || preview.name || '文件操作'}`;
  $(`${prefix}-details`).textContent = `目标：${preview.path || '未记录'} · ${bytes} · ${operation} · 来源：${sourceLabel(preview.source)} · ${riskLabel(preview.risk)}`;
  $(`${prefix}-intent`).textContent = `模型意图：${preview.arguments?.intent || '未提供'}（用于说明目的）`;
  $(`${prefix}-content`).textContent = preview.content || '';
  if (live) {
    $('approval-actions').hidden = false;
    if (!root.dataset.actionsBound && handlers.onAllow && handlers.onDeny) {
      $('approval-allow').addEventListener('click', handlers.onAllow);
      $('approval-deny').addEventListener('click', handlers.onDeny);
      root.dataset.actionsBound = 'true';
    }
  } else {
    const decision = {allow: '已批准', deny: '已拒绝', expired: '已过期'}[approval.decision]
      || approval.decision || '已处理';
    $('approval-history-status').textContent = `审批结果：${decision}。仅供查看，不能再次执行。`;
  }
  return root;
}

function renderApproval(job) {
  const approval = busy ? job.pending_approval : null;
  const id = approval?.approval_id || approval?.id;
  const previousId = pendingApproval?.approval_id || pendingApproval?.id;
  pendingApproval = approval || null;
  renderDecisionCard(approval, 'live', {
    onAllow: () => decideApproval('allow'), onDeny: () => decideApproval('deny')
  });
  clearTimeout(approvalTimer);
  if (!pendingApproval) return;
  approvalDeadline = Date.now() + Math.max(0, Number(approval.remaining_seconds) || 0) * 1000;
  if (id !== previousId) {
    $('approval-error').hidden = true;
    requestAnimationFrame(() => {
      const currentId = pendingApproval?.approval_id || pendingApproval?.id;
      if (currentId === id) $('approval-title').focus();
    });
  }
}

async function fetchArtifactBlob({sessionId, runId, artifactId, agentId, signal}) {
  const path = `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}`
    + `/artifacts/${encodeURIComponent(artifactId)}/download`;
  const response = await fetch(path, {method: 'GET', headers: {
    'X-Session-Token': token, 'X-Agent-ID': agentId
  }, signal});
  if (!response.ok) {
    let code = 'LOCAL_SERVER_ERROR';
    try { code = (await response.json()).error || code; } catch (_) {}
    const error = new Error(messageFor(code)); error.code = code; throw error;
  }
  return response.blob();
}

async function downloadArtifact(runId, artifact, button, note) {
  const sessionId = activeSessionId;
  const agentId = activeSession?.agent_id || sessionAgentIds.get(sessionId);
  if (!sessionId || !agentId || !artifact?.id) return;
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), 8000);
  button.disabled = true; note.textContent = '正在准备下载…';
  try {
    const blob = await fetchArtifactBlob({sessionId, runId, artifactId: artifact.id,
      agentId, signal: controller.signal});
    const url = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = artifact.path.split('/').at(-1); anchor.hidden = true;
      document.body.append(anchor); anchor.click(); anchor.remove();
    } finally { URL.revokeObjectURL(url); }
    note.textContent = '下载已开始。';
  } catch (error) {
    note.textContent = error.name === 'AbortError' ? '下载超时，请重试。' : error.message;
  } finally {
    clearTimeout(deadline); button.disabled = false;
  }
}

const artifactPreviewController = {
  request: null,
  context: null,
  close(restoreFocus = true) {
    const current = this.request || this.context;
    if (this.request) {
      clearTimeout(this.request.deadline); this.request.controller.abort();
    }
    this.request = null; this.context = null;
    $('artifact-preview-content').textContent = '';
    $('artifact-preview-status').textContent = '';
    $('artifact-preview-error').textContent = ''; $('artifact-preview-error').hidden = true;
    $('artifact-preview-download').disabled = true;
    if ($('artifact-preview-dialog').open) $('artifact-preview-dialog').close();
    if (restoreFocus && current?.trigger && document.contains(current.trigger)) current.trigger.focus();
  },
  async open({sessionId, runId, artifact, agentId, viewGeneration: generation, trigger}) {
    this.close(false);
    const dialog = $('artifact-preview-dialog');
    const controller = new AbortController();
    const request = {sessionId, runId, artifact, agentId, generation, trigger, controller,
      timedOut: false, deadline: null};
    this.request = request; this.context = request;
    $('artifact-preview-title').textContent = artifact.path.split('/').at(-1);
    $('artifact-preview-meta').textContent = `${artifact.path} · ${artifact.bytes} 字节 · 等待服务端完整性校验`;
    $('artifact-preview-content').textContent = '';
    $('artifact-preview-error').textContent = ''; $('artifact-preview-error').hidden = true;
    $('artifact-preview-status').textContent = '正在读取并校验文件…';
    $('artifact-preview-download').disabled = true;
    if (!dialog.open) dialog.showModal();
    request.deadline = setTimeout(() => {
      request.timedOut = true; controller.abort();
    }, 8000);
    try {
      const blob = await fetchArtifactBlob({sessionId, runId, artifactId: artifact.id,
        agentId, signal: controller.signal});
      const bytes = await blob.arrayBuffer();
      let content;
      try { content = new TextDecoder('utf-8', {fatal: true}).decode(bytes); }
      catch (_) {
        const error = new Error('文件不是有效 UTF-8，无法预览；你仍可下载。');
        error.code = 'TEXT_INVALID_UTF8'; throw error;
      }
      const currentAgent = activeSession?.agent_id || sessionAgentIds.get(activeSessionId);
      if (this.request !== request || activeSessionId !== sessionId || currentAgent !== agentId
          || viewGeneration !== generation) return;
      $('artifact-preview-content').textContent = content;
      $('artifact-preview-meta').textContent = `${artifact.path} · ${artifact.bytes} 字节 · SHA-256 完整性校验通过`;
      $('artifact-preview-status').textContent = '纯文本预览 · 文件完整性校验通过';
      $('artifact-preview-download').disabled = false;
    } catch (error) {
      if (this.request !== request) return;
      if (error.name === 'AbortError' && !request.timedOut) return;
      const message = request.timedOut ? '预览超时，请重试或下载文件。'
        : error.code === 'IMPORT_INTEGRITY_ERROR'
        ? '文件与执行回执不再一致，不能预览或下载。'
        : error.message;
      $('artifact-preview-error').textContent = message;
      $('artifact-preview-error').hidden = false;
      $('artifact-preview-status').textContent = '';
      $('artifact-preview-download').disabled = error.code === 'IMPORT_INTEGRITY_ERROR';
      if (connectionExpired(error)) return;
    } finally {
      clearTimeout(request.deadline);
    }
  }
};

function artifactPreviewable(artifact) {
  const path = String(artifact?.path || '').toLowerCase();
  return Boolean(artifact?.id && (path.endsWith('.md') || path.endsWith('.txt')));
}

function renderArtifactCard(runId, artifact, handlers = {}) {
  const article = document.createElement('article'), heading = document.createElement('p');
  const path = document.createElement('p'), details = document.createElement('details');
  const summary = document.createElement('summary'), hash = document.createElement('code');
  article.className = 'artifact artifact-card';
  const name = String(artifact.path || '').split('/').at(-1) || '未命名文件';
  heading.className = 'artifact-name';
  heading.textContent = `${name} · ${artifact.bytes} 字节 · ${artifact.operation === 'created' ? '已新建' : artifact.operation}`;
  path.className = 'artifact-path'; path.textContent = artifact.path;
  summary.textContent = '查看文件校验值'; hash.textContent = `SHA-256：${artifact.sha256}`;
  details.append(summary, hash); article.append(heading, path, details);
  if (artifact.id) {
    const actions = document.createElement('div'), note = document.createElement('p');
    actions.className = 'artifact-actions'; note.className = 'field-help';
    note.setAttribute('aria-live', 'polite');
    if (artifactPreviewable(artifact)) {
      const preview = document.createElement('button');
      preview.type = 'button'; preview.className = 'secondary artifact-preview'; preview.textContent = '预览';
      preview.addEventListener('click', () => handlers.onPreview?.(preview, note));
      actions.append(preview);
    }
    const download = document.createElement('button');
    download.type = 'button'; download.className = 'secondary artifact-download'; download.textContent = '下载文件';
    download.addEventListener('click', () => handlers.onDownload?.(download, note));
    actions.append(download); article.append(actions, note);
  }
  return article;
}

function artifactHandlers(runId, artifact) {
  const sessionId = activeSessionId;
  const agentId = activeSession?.agent_id || sessionAgentIds.get(sessionId);
  const generation = viewGeneration;
  if (!sessionId || !agentId) return {};
  return {
    onPreview: trigger => artifactPreviewController.open({sessionId, runId,
      artifact, agentId, viewGeneration: generation, trigger}),
    onDownload: (button, note) => downloadArtifact(runId, artifact, button, note),
  };
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
    const article = renderArtifactCard(job.id, artifact, artifactHandlers(job.id, artifact));
    $('artifact-list').append(article);
  }
}

function connectionExpired(error) {
  if (!['SESSION_EXPIRED', 'RUN_NOT_FOUND'].includes(error.code)) return false;
  ready = false; liveRunSnapshot = null; clearTimeout(timer); clearTimeout(sessionRefreshTimer);
  status('需要刷新页面', 'warning');
  artifactPreviewController.close(false);
  renderSessionList();
  showError(error, 'connection-error'); updateControls();
  return true;
}

function normalizedSessionRun(run) {
  const terminal = !['queued', 'running', 'waiting_approval'].includes(run.state);
  const result = run.result || (terminal ? {answer: null, stop_reason: run.stop_reason,
    trace_path: run.trace_path || '', artifacts: run.artifacts || []} : null);
  if (result && run.artifacts?.length) result.artifacts = run.artifacts.map(item => ({
    id: item.id,
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

function resetTranscriptState() {
  if (detailObserver) detailObserver.disconnect();
  detailObserver = null; runDetails.clear(); detailRequests.clear();
  inspectedRunId = null; inspectionToken++;
}

function clearRunView() {
  clearTimeout(timer); liveRunId = null; busy = false; liveRunRevision = -1;
  liveRunSnapshot = null;
  channelSnapshot = null;
  if ($('channel-status')) $('channel-status').hidden = true;
  artifactPreviewController.close(false);
  pendingApproval = null; cancelling = false; $('output').hidden = true;
  continuableRunId = null;
  $('output').classList.toggle('session-live-output', false);
  $('empty').hidden = historyRuns.length > 0;
  $('inspector-run-empty').hidden = false;
  for (const id of ['scope-summary', 'approval-history', 'artifacts', 'evidence', 'trace']) $(id).hidden = true;
  for (const marker of $('session-list').querySelectorAll('.session-attention')) marker.remove();
  $('continue-run').hidden = true; updateControls();
}

function markCurrentRun(runId) {
  for (const card of $('session-history').querySelectorAll('[data-run-id]')) {
    if (card.dataset.runId === runId) card.setAttribute('aria-current', 'true');
    else card.removeAttribute('aria-current');
  }
}

function currentRunPageLoading() {
  return Boolean(runPageRequest && runPageRequest.sessionId === activeSessionId
    && runPageRequest.generation === viewGeneration);
}

function runDetailState(runId) {
  return runDetails.get(runId) || {status: 'idle'};
}

function transcriptStatus(receipt, state) {
  if (state.status === 'loading') return {text: '正在载入这轮回答…', tone: ''};
  if (state.status === 'error') return {text: '暂时无法载入这轮，重新加载。', tone: 'error'};
  if (receipt.state === 'completed' && receipt.tone === 'success') return null;
  return {text: receipt.message, tone: receipt.tone === 'danger' ? 'error'
    : receipt.tone === 'warning' ? 'warning' : ''};
}

function createRunCard(item) {
  const card = document.createElement('article');
  card.className = 'transcript-run'; card.dataset.runId = item.id;
  if (item.id === inspectedRunId) card.setAttribute('aria-current', 'true');
  if (item.id === liveRunId) card.dataset.live = 'true';

  const detailState = runDetailState(item.id), detail = detailState.run;
  const receiptView = projectRunReceipt(item, detail, {liveRunId, continuableRunId});
  const receipt = document.createElement('div'), receiptText = document.createElement('div');
  const receiptLabel = document.createElement('span'), receiptMessage = document.createElement('span');
  receipt.className = `run-receipt tone-${receiptView.tone}`;
  receiptText.className = 'run-receipt-text'; receiptLabel.className = 'run-receipt-label';
  receiptMessage.className = 'run-receipt-message'; receiptLabel.textContent = receiptView.label;
  receiptMessage.textContent = receiptView.message; receiptText.append(receiptLabel, receiptMessage);
  receipt.append(receiptText);
  if ([receiptView.citationCount, receiptView.artifactCount, receiptView.stepCount]
    .every(value => value !== null)) {
    const counts = document.createElement('span'); counts.className = 'run-receipt-counts';
    counts.textContent = `${receiptView.citationCount} 处依据 · ${receiptView.artifactCount} 个产物 · ${receiptView.stepCount} 个步骤`;
    receipt.append(counts);
  }

  const meta = document.createElement('div'), when = document.createElement('time');
  const inspect = document.createElement('button');
  meta.className = 'transcript-meta'; inspect.type = 'button'; inspect.className = 'run-inspect';
  inspect.textContent = '查看本轮依据与执行记录';
  inspect.setAttribute('aria-label', `查看本轮依据与执行记录：${item.question}`);
  inspect.addEventListener('click', () => openSessionRun(item.id));
  when.textContent = formatTimestamp(item.started_at || item.created_at);
  if (when.textContent) meta.append(when); meta.append(inspect);

  const user = document.createElement('div'), userLabel = document.createElement('span'), question = document.createElement('p');
  user.className = 'message transcript-user'; user.dataset.role = 'user'; userLabel.className = 'message-label';
  userLabel.textContent = '你'; question.className = 'source-line'; question.textContent = item.question;
  user.append(userLabel, question); card.append(receipt, meta, user);

  if (detail?.state === 'completed' && detail.result?.answer) {
    const assistant = document.createElement('div'), label = document.createElement('span'), answer = document.createElement('p');
    assistant.className = 'message transcript-assistant'; assistant.dataset.role = 'assistant';
    label.className = 'message-label'; label.textContent = 'Agent 回答';
    answer.className = 'transcript-answer run-answer'; answer.textContent = detail.result.answer.answer;
    assistant.append(label, answer); card.append(assistant);
  } else {
    const statusInfo = transcriptStatus(receiptView, detailState), state = document.createElement('div');
    state.className = `transcript-state${statusInfo?.tone ? ` ${statusInfo.tone}` : ''}`;
    state.textContent = statusInfo?.text || runStateLabel(item.state); card.append(state);
    if (detailState.status === 'error') {
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'run-detail-retry';
      retry.textContent = '重新加载'; retry.addEventListener('click', event => {
        event.stopPropagation(); loadRunDetail(item.id, {retry: true});
      }); state.append(document.createElement('br'), retry);
    }
  }
  const artifacts = detail?.result?.artifacts || detail?.artifacts || [];
  if (artifacts.length) {
    const delivery = document.createElement('section'), label = document.createElement('span');
    delivery.className = 'run-artifacts'; label.className = 'message-label';
    label.textContent = '交付文件'; delivery.append(label);
    for (const artifact of artifacts) {
      delivery.append(renderArtifactCard(item.id, artifact, artifactHandlers(item.id, artifact)));
    }
    card.append(delivery);
  }
  if (detail?.approvals?.length) {
    const note = document.createElement('p'); note.className = 'transcript-state warning run-approval-note';
    note.textContent = `历史审批：${detail.approvals.at(-1).decision || '已处理'}（仅供查看，不会再次执行）。`;
    card.append(note);
  }
  return card;
}

function observeRunCard(card) {
  if (runDetailState(card.dataset.runId).status !== 'idle') return;
  if (!('IntersectionObserver' in globalThis)) return;
  if (!detailObserver) detailObserver = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      detailObserver.unobserve(entry.target); loadRunDetail(entry.target.dataset.runId);
    }
  }, {root: $('conversation-scroll'), rootMargin: '0px'});
  detailObserver.observe(card);
}

function findRunCard(runId) {
  return [...$('session-history').querySelectorAll('[data-run-id]')]
    .find(card => card.dataset.runId === runId) || null;
}

function replaceRunCard(runId) {
  const current = findRunCard(runId);
  const item = historyRuns.find(run => run.id === runId);
  if (!current || !item) return;
  const anchor = visibleRunAnchor();
  const replacement = createRunCard(item);
  current.className = replacement.className;
  if (replacement.hasAttribute('aria-current')) current.setAttribute('aria-current', 'true');
  else current.removeAttribute('aria-current');
  if (replacement.dataset.live) current.dataset.live = replacement.dataset.live;
  else delete current.dataset.live;
  current.replaceChildren(...replacement.childNodes);
  if (anchor) {
    const preserved = findRunCard(anchor.runId);
    if (preserved) {
      $('conversation-scroll').scrollTop += preserved.getBoundingClientRect().top - anchor.top;
    }
  }
}

function visibleRunAnchor() {
  const containerTop = $('conversation-scroll').getBoundingClientRect().top;
  const cards = [...$('session-history').querySelectorAll('[data-run-id]')];
  const card = cards.find(item => item.getBoundingClientRect().top >= containerTop)
    || cards.find(item => item.getBoundingClientRect().bottom >= containerTop);
  return card ? {runId: card.dataset.runId, top: card.getBoundingClientRect().top} : null;
}

function renderRunHistory(runs, append = false, anchor = null) {
  const merged = append ? [...historyRuns, ...runs] : [...runs];
  historyRuns = [...new Map(merged.map(item => [item.id, item])).values()];
  if (detailObserver) detailObserver.disconnect();
  $('session-history').replaceChildren();
  for (const item of [...historyRuns].reverse()) {
    const card = createRunCard(item); $('session-history').append(card); observeRunCard(card);
  }
  $('empty').hidden = historyRuns.length > 0 || !$('output').hidden;
  markCurrentRun(inspectedRunId);
  $('run-load-more').hidden = !runHistoryCursor;
  $('run-load-more').disabled = currentRunPageLoading() || busy;
  if (anchor) {
    const preserved = findRunCard(anchor.runId);
    if (preserved) {
      $('conversation-scroll').scrollTop += preserved.getBoundingClientRect().top - anchor.top;
    }
  }
}

async function loadRunDetail(runId, {inspect = false, retry = false} = {}) {
  if (!activeSessionId) return null;
  const sessionId = activeSessionId, generation = viewGeneration;
  const key = `${sessionId}:${runId}:${generation}`;
  if (detailRequests.has(key)) {
    const job = await detailRequests.get(key);
    if (job && inspect && inspectedRunId === runId) renderInspector(job);
    return job;
  }
  const cached = runDetails.get(runId);
  if (!retry && cached?.status === 'ready') {
    if (inspect && inspectedRunId === runId) renderInspector(cached.run);
    return cached.run;
  }
  runDetails.set(runId, {status: 'loading'});
  replaceRunCard(runId);
  let request;
  request = (async () => {
    try {
      const response = await api(`/api/sessions/${sessionId}/runs/${runId}`);
      if (sessionId !== activeSessionId || generation !== viewGeneration) return null;
      if (response.run?.id !== runId || response.run?.session_id !== sessionId) {
        throw new Error('任务目标与当前会话不匹配，请重新打开链接。');
      }
      const job = normalizedSessionRun(response.run);
      runDetails.set(runId, {status: 'ready', run: job});
      historyRuns = historyRuns.map(item => item.id === runId ? {...item, ...response.run} : item);
      replaceRunCard(runId);
      if (inspect && inspectedRunId === runId) renderInspector(job);
      return job;
    } catch (error) {
      if (sessionId === activeSessionId && generation === viewGeneration) {
        runDetails.set(runId, {status: 'error', error}); replaceRunCard(runId);
      }
      return null;
    } finally {
      if (detailRequests.get(key) === request) detailRequests.delete(key);
    }
  })();
  detailRequests.set(key, request); return request;
}

async function loadMoreRuns() {
  if (!activeSessionId || !runHistoryCursor || currentRunPageLoading()) return;
  const sessionId = activeSessionId, generation = viewGeneration, cursor = runHistoryCursor;
  const anchor = visibleRunAnchor();
  const request = {sessionId, generation, cursor};
  runPageRequest = request; $('run-load-more').disabled = true;
  try {
    const response = await api(`/api/sessions/${sessionId}/runs?cursor=${encodeURIComponent(cursor)}`);
    if (sessionId !== activeSessionId || generation !== viewGeneration) return;
    runHistoryCursor = response.next_cursor || null;
    renderRunHistory(response.runs || [], true, anchor);
  } catch (error) {
    if (sessionId === activeSessionId && generation === viewGeneration) showError(error, 'session-error');
  } finally {
    if (runPageRequest === request) runPageRequest = null;
    if (sessionId === activeSessionId && generation === viewGeneration) {
      $('run-load-more').hidden = !runHistoryCursor;
      $('run-load-more').disabled = currentRunPageLoading() || busy;
    }
  }
}

async function openSessionRun(runId, {focus = true} = {}) {
  if (!activeSessionId) return;
  if (inspectedRunId !== runId) artifactPreviewController.close(false);
  inspectedRunId = runId; const token = ++inspectionToken; markCurrentRun(runId);
  const job = await loadRunDetail(runId, {inspect: true});
  if (!job || token !== inspectionToken || inspectedRunId !== runId) return;
  if (!busy) {
    continuableRunId = job.state === 'interrupted' ? job.id : null;
    $('continue-run').hidden = !continuableRunId;
  }
  if (!focus) {
    $('task-type').value = job.task_type || 'files';
    $('output-file').value = job.output_file || '';
    updateControls();
  }
  renderInspector(job);
  if (focus) requestAnimationFrame(() => {
    const control = findRunCard(runId)?.querySelector('.run-inspect');
    if (control && inspectedRunId === runId) control.focus({preventScroll: true});
  });
}

function focusRunResult(job) {
  const target = job.state === 'completed' && !$('answer-block').hidden
    ? $('answer-title') : !$('failure').hidden ? $('failure-title') : null;
  if (!target || document.activeElement === $('question')) return;
  requestAnimationFrame(() => {
    if (liveRunId === job.id && document.activeElement !== $('question')) target.focus();
  });
}

function importedLocationLabel(location) {
  if (location?.kind === 'text_lines') return `原文第 ${location.start}${location.end === location.start ? '' : '–' + location.end} 行`;
  if (location?.kind === 'pdf_page') return `PDF 第 ${location.page} 页`;
  if (location?.kind === 'docx_paragraph') return `Word 第 ${location.paragraph} 段`;
  if (location?.kind === 'docx_table_row') return `Word 表格 ${location.table} 第 ${location.row} 行`;
  return '';
}

function sessionListPath(kind, cursor = null) {
  const parameters = new URLSearchParams();
  if (kind === 'archived') parameters.set('archived', '1');
  if (cursor) parameters.set('cursor', cursor);
  const query = parameters.toString();
  return `/api/sessions${query ? `?${query}` : ''}`;
}

function renderSessionOptions() {
  sessionAgentIds.clear();
  const select = $('session-select'), temporary = select.firstElementChild;
  select.replaceChildren(temporary);
  const sessions = projectAgentWorkspace(
    agents, visibleSessions, activeSession?.agent_id || selectedAgentId).sessions;
  for (const session of sessions) {
    if (session.agent_id) sessionAgentIds.set(session.id, session.agent_id);
    const option = document.createElement('option'); option.value = session.id;
    option.textContent = session.title + (session.status === 'archived' ? '（已归档）' : '');
    select.append(option);
  }
  temporary.hidden = fixedSessionSelection;
  if (activeSessionId && sessions.some(item => item.id === activeSessionId)) select.value = activeSessionId;
}

function renderSessionList() {
  const list = $('session-list'); list.replaceChildren();
  const sessions = projectAgentWorkspace(
    agents, visibleSessions, activeSession?.agent_id || selectedAgentId).sessions;
  if (!sessions.length) {
    const empty = document.createElement('p'); empty.className = 'field-help session-empty';
    empty.textContent = '这个助手还没有会话。'; list.append(empty);
  }
  for (const session of sessions) {
    const button = document.createElement('button'), title = document.createElement('strong');
    const details = document.createElement('span');
    button.type = 'button'; button.className = 'session-nav-item'; button.dataset.sessionId = session.id;
    if (session.id === activeSessionId) button.setAttribute('aria-current', 'true');
    button.disabled = busy || managing || Boolean(activeImport);
    title.textContent = session.title;
    const scope = session.scope?.mode === 'directory' ? '目录' : session.scope?.file || '单文件';
    const when = formatTimestamp(session.updated_at || session.created_at);
    details.textContent = `${scope}${session.status === 'archived' ? ' · 已归档' : ''}${when ? ` · ${when}` : ''}`;
    const attention = projectSessionAttention(session.id, liveRunSnapshot);
    button.addEventListener('click', () => {
      $('session-select').value = session.id;
      selectSession(session.id);
      if (inspectorDrawer.matches) setDrawer();
    });
    button.append(title, details);
    if (attention) {
      const marker = document.createElement('span');
      marker.className = `session-attention ${attention.tone}`; marker.textContent = attention.label;
      button.append(marker);
    }
    list.append(button);
  }
  $('session-load-more').hidden = fixedSessionSelection || (!sessionCursors.active && !sessionCursors.archived);
  $('session-load-more').disabled = currentSessionPageLoading() || busy || managing || Boolean(activeImport);
}

function currentSessionPageLoading() {
  return Boolean(sessionPageRequest && sessionPageRequest.generation === sessionListGeneration
    && sessionPageRequest.agentId === selectedAgentId);
}

async function loadMoreSessions() {
  if (fixedSessionSelection || activeImport || currentSessionPageLoading()
      || (!sessionCursors.active && !sessionCursors.archived)) return;
  const generation = sessionListGeneration, requestedAgent = selectedAgentId;
  const cursors = {...sessionCursors};
  const request = {generation, agentId: requestedAgent, cursors};
  const pending = [];
  if (cursors.active) pending.push(api(sessionListPath('active', cursors.active)).then(data => ({kind: 'active', data})));
  if (cursors.archived) pending.push(api(sessionListPath('archived', cursors.archived)).then(data => ({kind: 'archived', data})));
  sessionPageRequest = request; $('session-load-more').disabled = true;
  try {
    const pages = await Promise.all(pending);
    if (generation !== sessionListGeneration || requestedAgent !== selectedAgentId) return;
    for (const page of pages) {
      sessionCursors[page.kind] = page.data.next_cursor || null;
      visibleSessions.push(...(page.data.sessions || []));
    }
    visibleSessions = [...new Map(visibleSessions.map(item => [item.id, item])).values()];
    renderSessionOptions(); renderSessionList();
  } catch (error) {
    if (generation === sessionListGeneration && requestedAgent === selectedAgentId) showError(error, 'session-error');
  } finally {
    if (sessionPageRequest === request) sessionPageRequest = null;
    if (generation === sessionListGeneration && requestedAgent === selectedAgentId) renderSessionList();
  }
}

async function selectSession(sessionId, {targetRun = null} = {}) {
  const generation = ++viewGeneration;
  clearTimeout(timer); clearTimeout(sessionRefreshTimer);
  activeSessionId = sessionId || null; activeSession = null;
  resetTranscriptState(); historyRuns = []; runHistoryCursor = null; clearRunView();
  $('session-error').hidden = true;
  if (!activeSessionId) {
    $('session-actions').hidden = true; $('session-scope').hidden = true;
    renderImportSources(null);
    renderRunHistory([]); renderSessionList(); renderAgentList();
    $('task-type').value = 'files'; clearRunView(); updateControls(); return true;
  }
  try {
    const [sessionResponse, runsResponse] = await Promise.all([
      api(`/api/sessions/${activeSessionId}`), api(`/api/sessions/${activeSessionId}/runs`)
    ]);
    if (generation !== viewGeneration || sessionId !== activeSessionId) return;
    if (sessionResponse.session?.id !== sessionId
      || (selectedAgentId && sessionResponse.session?.agent_id !== selectedAgentId)) {
      throw new Error('会话目标与当前助手不匹配，请重新打开链接。');
    }
    activeSession = sessionResponse.session;
    if (!visibleSessions.some(item => item.id === sessionId)) {
      visibleSessions.push(activeSession); renderSessionOptions();
    }
    renderImportSources(activeSession);
    $('session-select').value = activeSession.id;
    $('session-actions').hidden = false;
    $('session-rename-title').value = activeSession.title;
    $('session-archive').hidden = activeSession.status === 'archived';
    $('session-restore').hidden = activeSession.status !== 'archived';
    renderSessionList(); renderAgentList(); updateControls();
    const directory = activeSession.scope.mode === 'directory';
    $('discover').checked = directory; $('file').value = activeSession.scope.file || '';
    $('session-scope').hidden = false;
    const scopeLabel = activeSession.import
      ? `${activeSession.import.files?.length || 0} 份本地导入资料`
      : directory ? '当前工作区目录发现' : activeSession.scope.file;
    $('session-scope').textContent = `固定资料范围：${scopeLabel}`
      + (activeSession.agent_id ? ` · 助手 ${activeSession.agent_id} · 修订 ${(activeSession.agent_revision || '未知').slice(0, 12)}（旧会话保留此版本）` : '')
      + (workspaceAvailable ? '' : ' · 原工作区当前不可用');
    runHistoryCursor = runsResponse.next_cursor || null;
    const runs = [...runsResponse.runs];
    if (targetRun && !runs.some(item => item.id === targetRun.id)) runs.push(targetRun);
    if (targetRun) runDetails.set(targetRun.id, {status: 'ready', run: normalizedSessionRun(targetRun)});
    inspectedRunId = targetRun?.id || runs[0]?.id || null;
    renderRunHistory(runs);
    if (inspectedRunId) await openSessionRun(inspectedRunId, {focus: false});
    else clearRunView();
    if (generation !== viewGeneration || sessionId !== activeSessionId) return false;
    await adoptSessionRun(runs);
    const loaded = generation === viewGeneration && activeSession?.id === sessionId;
    if (loaded) { refreshChannelStatus(); scheduleSessionRefresh(); }
    updateControls();
    return loaded;
  } catch (error) {
    if (generation === viewGeneration) {
      showError(error, 'session-error');
      updateControls();
    }
    return false;
  }
}

async function loadSessions(selectedId = null, fixed = fixedSessionSelection, options = {}) {
  const generation = ++sessionListGeneration, requestedAgent = selectedAgentId;
  const [active, archived] = await Promise.all([api('/api/sessions'), api('/api/sessions?archived=1')]);
  if (generation !== sessionListGeneration || requestedAgent !== selectedAgentId) return false;
  fixedSessionSelection = Boolean(fixed);
  visibleSessions = [...new Map([...(active.sessions || []), ...(archived.sessions || [])]
    .map(item => [item.id, item])).values()];
  sessionCursors = {active: active.next_cursor || null, archived: archived.next_cursor || null};
  renderSessionOptions(); renderSessionList();
  $('session-create').hidden = fixedSessionSelection;
  $('session-create-toggle').hidden = fixedSessionSelection;
  if (fixedSessionSelection) setSessionCreateOpen(false);
  const wanted = selectedId || active.sessions?.[0]?.id || null;
  if (wanted) { $('session-select').value = wanted; return selectSession(wanted, options); }
  else {
    $('session-select').value = ''; const loaded = await selectSession('');
    if (document.activeElement === document.body) $('question').focus();
    return loaded;
  }
}

async function openLinkedRun() {
  const generation = ++viewGeneration, fragment = location.hash;
  clearTimeout(timer); clearTimeout(sessionRefreshTimer);
  activeSessionId = null; activeSession = null; resetTranscriptState();
  renderRunHistory([]); clearRunView();
  $('session-actions').hidden = true; $('session-scope').hidden = true;
  try {
    const values = new URLSearchParams(fragment.slice(1));
    const keys = ['agent', 'session', 'run'];
    if ([...values.keys()].length !== keys.length || keys.some(key =>
      values.getAll(key).length !== 1 || !/^[A-Za-z0-9_-]{1,128}$/.test(values.get(key) || ''))) {
      throw new Error('链接格式无效，需要完整的助手、会话和任务目标。');
    }
    const agentId = values.get('agent'), sessionId = values.get('session'), runId = values.get('run');
    if (!agents.some(item => item.id === agentId)
      || (fixedSessionSelection && appConfig.selected_session_id !== sessionId)) {
      throw new Error('链接目标不可访问，请核对助手和会话。');
    }
    selectedAgentId = agentId; $('agent-select').value = agentId; renderAgentList();
    const [sessionResponse, runResponse] = await Promise.all([
      api(`/api/sessions/${encodeURIComponent(sessionId)}`),
      api(`/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}`)
    ]);
    if (generation !== viewGeneration || fragment !== location.hash) return false;
    if (sessionResponse.session?.id !== sessionId || sessionResponse.session?.agent_id !== agentId
      || runResponse.run?.id !== runId || runResponse.run?.session_id !== sessionId) {
      throw new Error('链接目标不匹配，请核对助手、会话和任务。');
    }
    const loaded = await loadSessions(sessionId, fixedSessionSelection, {targetRun: runResponse.run});
    if (!loaded) return false;
    findRunCard(runId)?.scrollIntoView({block: 'nearest'});
    return true;
  } catch (error) {
    if (generation !== viewGeneration || fragment !== location.hash) return false;
    await selectSession('');
    showError(new Error(`无法打开链接目标：${error.message}`), 'session-error');
    return false;
  }
}

async function adoptSessionRun(runs) {
  if (busy || !activeSessionId) return;
  const candidate = runs.find(item => ['queued', 'running', 'waiting_approval'].includes(item.state));
  if (!candidate) return;
  const sessionId = activeSessionId, generation = viewGeneration;
  const job = await loadRunDetail(candidate.id);
  if (!job || busy || sessionId !== activeSessionId || generation !== viewGeneration || job.result !== null) return;
  prepareOutput(job, {preserveInspection: true}); render(job);
  timer = setTimeout(poll, document.hidden ? 4000 : 450);
}

function scheduleSessionRefresh(delay = document.hidden ? 15000 : 5000) {
  clearTimeout(sessionRefreshTimer);
  if (activeSessionId && ready) sessionRefreshTimer = setTimeout(refreshSessionSummary, delay);
}

async function refreshSessionSummary() {
  if (!activeSessionId || !ready) return;
  const sessionId = activeSessionId, generation = viewGeneration;
  if (sessionRefreshRequest?.sessionId === sessionId && sessionRefreshRequest.generation === generation) return;
  const request = {sessionId, generation}; sessionRefreshRequest = request;
  try {
    if (!busy) {
      const response = await api(`/api/sessions/${sessionId}/runs`);
      if (sessionId !== activeSessionId || generation !== viewGeneration) return;
      const fresh = response.runs || [], known = new Set(fresh.map(item => item.id));
      const changed = fresh.some(item => {
        const previous = historyRuns.find(old => old.id === item.id);
        return !previous || previous.state !== item.state;
      });
      if (changed) {
        renderRunHistory([...fresh, ...historyRuns.filter(item => !known.has(item.id))], false, visibleRunAnchor());
        renderWorkRail();
      }
      await adoptSessionRun(fresh);
    }
    if (sessionId === activeSessionId && generation === viewGeneration) await refreshChannelStatus();
  } catch (error) {
    if (sessionId === activeSessionId && generation === viewGeneration) {
      if (!connectionExpired(error)) showError(error, 'connection-error');
    }
  } finally {
    if (sessionRefreshRequest === request) sessionRefreshRequest = null;
    if (sessionId === activeSessionId && generation === viewGeneration) scheduleSessionRefresh();
  }
}

function renderChannelStatus(channel) {
  let section = $('channel-status');
  if (!channel?.enabled || channel.binding?.agent_id !== selectedAgentId
    || channel.binding?.session_id !== activeSessionId) {
    if (section) section.hidden = true;
    return;
  }
  if (!section) {
    section = document.createElement('section'); section.id = 'channel-status';
    section.className = 'work-group'; section.setAttribute('aria-label', '飞书通知');
    $('work-rail').insertBefore(section, document.querySelector('.work-environment'));
  }
  section.hidden = false; section.replaceChildren();
  const title = document.createElement('h3'), transport = document.createElement('p');
  title.textContent = '飞书通知'; transport.className = 'field-help';
  const connection = {starting: '连接中', connected: '已连接', disconnected: '连接已断开',
    error: '连接异常', stopped: '已停止'}[channel.transport?.state] || '连接状态未知';
  transport.textContent = `飞书：${connection}。任务结果以本工作台为准。`;
  section.append(title, transport);
  const labels = {pending: '等待发送', sending: '正在发送', retry: '等待重试', retry_wait: '等待重试',
    accepted: '平台已接收，不代表对方已读', sent: '平台已接收，不代表对方已读',
    unknown: '送达情况未知，补发可能重复通知', failed: '发送失败',
    invalidated: '绑定已变更，通知已失效', suppressed: '绑定已变更，通知已失效', cancelled: '通知已取消'};
  const kinds = {approval: '审批提醒', waiting_approval: '审批提醒', result: '结果通知',
    terminal: '结果通知', accepted: '接收回执', rejected: '拒绝回执'};
  for (const notice of (channel.outbox || []).slice(-5)) {
    const row = document.createElement('div'), text = document.createElement('p');
    text.className = 'field-help';
    text.textContent = `${kinds[notice.kind] || '通知'}：${labels[notice.state] || '状态未知'}`;
    row.append(text);
    if (['unknown', 'failed'].includes(notice.state)) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'secondary'; button.textContent = '仅补发通知';
      button.disabled = channelRetries.has(notice.id) || !ready;
      const sessionId = activeSessionId, agentId = selectedAgentId, generation = viewGeneration;
      button.addEventListener('click', async () => {
        if (channelRetries.has(notice.id) || !ready || sessionId !== activeSessionId
          || agentId !== selectedAgentId || generation !== viewGeneration) return;
        channelRetries.add(notice.id); button.disabled = true;
        try {
          await api('/api/channels/feishu/retry', {outbox_id: notice.id});
          if (sessionId === activeSessionId && generation === viewGeneration) await refreshChannelStatus();
        } catch (error) {
          if (sessionId === activeSessionId && generation === viewGeneration) {
            text.textContent = `补发未确认：${error.message}`;
          }
        } finally {
          channelRetries.delete(notice.id); button.disabled = !ready;
        }
      });
      row.append(button);
    }
    section.append(row);
  }
  const rejected = (channel.events || []).find(item => item.state === 'rejected' || item.state === 'failed');
  if (rejected) {
    const note = document.createElement('p'); note.className = 'field-help';
    note.textContent = '有一条飞书请求未执行，请核对通知或任务记录。'; section.append(note);
  }
}

async function refreshChannelStatus() {
  const sessionId = activeSessionId, agentId = selectedAgentId, generation = viewGeneration;
  if (!sessionId) return;
  try {
    const response = await api('/api/channels/feishu');
    if (sessionId !== activeSessionId || agentId !== selectedAgentId || generation !== viewGeneration) return;
    channelSnapshot = response.channel; renderChannelStatus(channelSnapshot);
  } catch (error) {
    if (sessionId !== activeSessionId || generation !== viewGeneration) return;
    if (error.code === 'NOT_FOUND') return;
    if (connectionExpired(error)) return;
    if ($('channel-status') && channelSnapshot?.enabled) {
      $('channel-status').textContent = '飞书状态暂不可用，请等待刷新。';
    }
  }
}

function prepareOutput(job, {preserveInspection = false} = {}) {
  clearTimeout(timer);
  liveRunId = job.id; liveRunRevision = -1;
  liveRunSnapshot = activeSessionId ? {id: job.id,
    session_id: job.session_id || activeSessionId, state: job.state} : null;
  pendingApproval = null; approvalBlockedId = null; cancelling = false; clearTimeout(approvalTimer);
  continuableRunId = null;
  $('empty').hidden = true; $('output').hidden = false;
  $('output').classList.toggle('session-live-output', Boolean(activeSessionId));
  for (const id of ['answer-block', 'failure', 'form-error', 'approval']) $(id).hidden = true;
  $('continue-run').hidden = true;
  $('source-line').textContent = `${job.task_type === 'conversation' ? '会话记录'
    : job.mode === 'directory' ? '目录发现' : job.file || '资料'} · ${job.question}`;
  $('copy').textContent = '复制回答';
  if (activeSessionId) {
    historyRuns = [job, ...historyRuns.filter(item => item.id !== job.id)];
    runDetails.set(job.id, {status: 'ready', run: job});
    if (!preserveInspection) { inspectedRunId = job.id; inspectionToken++; }
    renderRunHistory([], true);
    renderSessionList();
    if (!preserveInspection) requestAnimationFrame(() => {
      $('conversation-scroll').scrollTop = $('conversation-scroll').scrollHeight;
    });
  }
}

function renderApprovalHistory(job) {
  const row = job.approvals?.at(-1) || null;
  renderDecisionCard(row, 'historical');
}

function renderInspector(job) {
  $('inspector-run-empty').hidden = true;
  for (const id of ['scope-summary', 'approval-history', 'artifacts', 'evidence']) $(id).hidden = true;
  $('trace').hidden = false; $('events').replaceChildren(); $('citations').replaceChildren();
  renderApprovalHistory(job);
  for (const event of job.events || []) {
    const li = document.createElement('li'), stamp = document.createElement('time'), label = document.createElement('span');
    stamp.textContent = `${event.elapsed.toFixed(1)}s`;
    const detail = event.detail || {};
    label.textContent = eventLabel(event) + (detail.action_summary ? ` · 实际操作：${detail.action_summary}` : detail.path ? ` · ${detail.path}` : detail.name ? ` · ${detail.name}` : '')
      + (detail.source ? ` · 来源：${sourceLabel(detail.source)}` : '') + (detail.risk ? ` · ${riskLabel(detail.risk)}` : '')
      + (detail.intent ? ` · 模型意图：${detail.intent}` : '') + (detail.code ? ` · ${detail.code}` : '');
    li.append(stamp, label); $('events').append(li);
  }
  $('event-count').textContent = `${job.events?.length || 0} 个步骤`;
  const result = job.result;
  $('trace-path').textContent = result?.trace_path || job.trace_path || '';
  renderArtifacts(job);
  if (result?.scope) {
    const scope = result.scope;
    $('scope-summary').hidden = false;
    $('scope-summary').textContent = `检查范围：已发现 ${scope.discovered_files.length} 份 · 已读取 ${scope.read_files.length} 份 · 未读取 ${scope.unread_files.length} 份 · 未列出目录 ${scope.unlisted_directories.length} 个。`
      + (scope.complete ? ' 已检查全部已发现资料。' : ' 检查范围尚不完整。')
      + (scope.unread_files.length ? ` 未读取：${scope.unread_files.join('、')}。` : '')
      + (scope.unlisted_directories.length ? ` 未列出目录：${scope.unlisted_directories.join('、')}。` : '');
  }
  const answer = result?.answer, citations = answer?.citations || answer?.references || [];
  $('evidence').hidden = citations.length === 0;
  $('citation-count').textContent = `${citations.length} 处引用`;
  for (const citation of citations) {
    const article = document.createElement('article'), heading = document.createElement('header'), quote = document.createElement('blockquote');
    article.className = 'citation';
    const source = citation.source;
    const importedLocations = source?.kind === 'imported_document'
      ? (source.locations || []).map(importedLocationLabel).filter(Boolean).join('、') : '';
    const importedName = source?.kind === 'imported_document'
      ? `${source.name || source.logical_path}${source.logical_path && source.logical_path !== source.name ? ` · ${source.logical_path}` : ''}${importedLocations ? ` · ${importedLocations}` : ''}`
      : null;
    const sourceName = importedName || (typeof source === 'string' ? source : source?.label || source?.name
      || (source ? `${sourceLabel(source.type)} · ${source.server_id || source.id || citation.source_id || citation.path || ''}` : null));
    heading.textContent = sourceName || citation.source_id
      ? `${sourceName || citation.source_id}${citation.start_line && source?.kind !== 'imported_document' ? ` · 第 ${citation.start_line}${citation.end_line === citation.start_line ? '' : '–' + citation.end_line} 行` : ''}`
      : citation.message_id
      ? `会话消息 ${citation.message_id} · 字符 ${citation.start}–${citation.end}`
      : `${citation.path} · 第 ${citation.start_line}${citation.end_line === citation.start_line ? '' : '–' + citation.end_line} 行`;
    quote.textContent = citation.quote; article.append(heading, quote); $('citations').append(article);
  }
}

function render(job) {
  if (liveRunId !== job.id || job.revision < liveRunRevision) return;
  const focusWhenFinished = busy && liveRunRevision >= 0 && job.result !== null;
  liveRunRevision = job.revision;
  busy = job.result === null;
  liveRunSnapshot = activeSessionId && busy ? {id: job.id,
    session_id: job.session_id || activeSessionId, state: job.state} : null;
  cancelling = Boolean(job.cancelling) || cancelSendingId === job.id;
  if (activeSessionId) {
    runDetails.set(job.id, {status: 'ready', run: job});
    historyRuns = historyRuns.map(item => item.id === job.id ? {...item, ...job} : item);
    replaceRunCard(job.id);
  }
  if (activeSessionId) renderSessionList();
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
    if (job.state === 'completed' && answer) {
      status(answer.status === 'not_found' ? '信息未记载' : '回答已完成', answer.status === 'not_found' ? 'warning' : '');
      $('answer-block').hidden = false;
      $('answer-title').textContent = answer.status === 'not_found' ? '没有找到这项信息'
        : job.task_type === 'conversation' ? '会话中的答案' : '资料中的答案';
      $('answer-text').textContent = answer.answer;
      $('answer-note').textContent = simulated ? '模拟演示：显示读取到的内容，未调用真实模型，也未理解问题。' : '回答通过格式与引用检查；请结合原文判断内容是否准确。';
    } else {
      const cancelled = job.state === 'cancelled';
      const interrupted = job.state === 'interrupted';
      const toolError = [...job.events].reverse().find(e => e.event === 'tool.completed' && e.detail.code)?.detail.code;
      const reason = job.state === 'unable' ? (toolError || result.stop_reason) : result.stop_reason;
      const unreadable = ['FILE_NOT_FOUND', 'UNSUPPORTED_FILE', 'READ_ERROR', 'OS_PERMISSION_DENIED',
        'DIRECTORY_NOT_FOUND', 'LIST_ERROR'].includes(reason);
      status(cancelled ? '已取消' : interrupted ? '运行已中断'
        : unreadable ? '无法读取' : '本次未完成', 'warning');
      $('failure').hidden = false; $('failure-title').textContent = cancelled ? '本次运行已取消' : '没有生成有效答案';
      $('failure-message').textContent = interrupted
        ? '服务曾在这次运行中停止。旧运行不会自动重做；可以新建一次继续运行。'
        : messageFor(reason) + (answer?.answer ? `\n${answer.answer}` : '');
      continuableRunId = interrupted && activeSessionId ? job.id : null;
      $('continue-run').hidden = !continuableRunId;
    }
  }
  if (!activeSessionId || inspectedRunId === job.id) renderInspector(job);
  if (activeSessionId && !busy) $('output').hidden = true;
  updateControls();
  if (focusWhenFinished) {
    if (activeSessionId) requestAnimationFrame(() => {
      const control = findRunCard(job.id)?.querySelector('.run-inspect');
      if (control && document.activeElement !== $('question')) control.focus({preventScroll: true});
    });
    else focusRunResult(job);
  }
}

async function poll() {
  if (!liveRunId) return;
  const requestedId = liveRunId, requestedSession = activeSessionId, generation = viewGeneration;
  try {
    const response = await api(runPath(requestedId));
    if (requestedId !== liveRunId || requestedSession !== activeSessionId || generation !== viewGeneration) return;
    const job = activeSessionId ? normalizedSessionRun(response.run) : response;
    $('connection-error').hidden = true;
    render(job);
    if (busy) timer = setTimeout(poll, document.hidden ? 4000 : 450);
    else if (activeSessionId) {
      const runs = await api(`/api/sessions/${activeSessionId}/runs`);
      if (requestedSession === activeSessionId && generation === viewGeneration) {
        runHistoryCursor = runs.next_cursor || null;
        renderRunHistory(runs.runs, true);
        await adoptSessionRun(runs.runs);
      }
    }
  } catch (error) {
    if (requestedId !== liveRunId || requestedSession !== activeSessionId || generation !== viewGeneration) return;
    showError(error, 'connection-error');
    if (!connectionExpired(error)) timer = setTimeout(poll, document.hidden ? 15000 : 2000);
  }
}

async function initialize() {
  try {
    const config = await api('/api/config');
    appConfig = config;
    $('workspace').textContent = config.workspace;
    ready = config.ready; simulated = Boolean(config.provider?.simulated);
    workspaceAvailable = config.workspace_available !== false;
    fixedSessionSelection = Boolean(config.selected_session_id);
    if (config.imports && typeof config.imports === 'object') {
      importCapabilities = {
        formats: Array.isArray(config.imports.formats) ? config.imports.formats : [],
        limits: {...importCapabilities.limits, ...(config.imports.limits || {})},
      };
    }
    $('connection').textContent = simulated ? '模拟演示 · 未调用模型' : ready ? 'DeepSeek 已配置' : '模型未配置';
    $('connection').className = `connection-state ${simulated || !ready ? 'warning' : ''}`;
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
    try { await loadExtensions(); }
    catch (error) { showError(error, 'extension-error'); }
    if (location.hash) {
      await openLinkedRun(); updateControls(); return;
    }
    await loadSessions(config.selected_session_id, Boolean(config.selected_session_id));
    if (!fixedSessionSelection) await resumePersistedImport();
    if (config.latest_run_id && !activeSessionId) {
      busy = true; updateControls();
      const job = await api(`/api/runs/${config.latest_run_id}`);
      const restoredAgentId = job.agent_snapshot?.id;
      if (agents.some(agent => agent.enabled !== false && agent.id === restoredAgentId)) {
        selectedAgentId = restoredAgentId;
        $('agent-select').value = restoredAgentId;
        renderAgentList();
      }
      $('discover').checked = job.mode === 'directory';
      $('output-file').value = job.output_file || '';
      $('file').value = job.file || ''; $('question').value = job.question; countQuestion(); prepareOutput(job); render(job);
      if (busy) timer = setTimeout(poll, 450);
    } else updateControls();
  } catch (error) { ready = false; busy = false; updateControls(); showError(error, 'connection-error'); }
}

window.addEventListener('hashchange', () => { if (appConfig && location.hash) openLinkedRun(); });
document.addEventListener('visibilitychange', () => scheduleSessionRefresh(document.hidden ? 15000 : 100));

$('question').addEventListener('input', countQuestion);
$('question').addEventListener('focus', () => {
  $('composer-settings').open = false;
  $('model-status').open = false;
});
$('question').addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (!$('start').disabled) $('question-form').requestSubmit();
});
$('file').addEventListener('input', () => {
  $('form-error').hidden = true;
  updateControls();
});
$('file').addEventListener('invalid', event => {
  event.preventDefault();
  $('composer-settings').open = true;
  showError(new Error('请先选择要核对的文件，或在本轮设置中启用目录发现。'));
  requestAnimationFrame(() => $('file').focus());
});
$('artifact-preview-close').addEventListener('click', () => artifactPreviewController.close());
$('artifact-preview-close-footer').addEventListener('click', () => artifactPreviewController.close());
$('artifact-preview-dialog').addEventListener('cancel', event => {
  event.preventDefault(); artifactPreviewController.close();
});
$('artifact-preview-download').addEventListener('click', () => {
  const current = artifactPreviewController.context;
  if (!current) return;
  downloadArtifact(current.runId, current.artifact, $('artifact-preview-download'),
    $('artifact-preview-status'));
});
$('import-trigger').addEventListener('click', openImportDialog);
$('composer-import').addEventListener('click', openImportDialog);
$('import-close').addEventListener('click', closeImportDialog);
$('import-dialog').addEventListener('cancel', event => {
  event.preventDefault();
  closeImportDialog();
});
$('import-files').addEventListener('change', event => {
  $('import-directory').value = '';
  chooseImportFiles('file', event.currentTarget.files);
});
$('import-directory').addEventListener('change', event => {
  $('import-files').value = '';
  chooseImportFiles('folder', event.currentTarget.files);
});
$('import-name').addEventListener('input', renderImportPreflight);
$('import-agent').addEventListener('change', updateImportControls);
$('import-start').addEventListener('click', startImport);
$('import-cancel').addEventListener('click', cancelImport);
$('agent-select').addEventListener('change', () => selectAgent($('agent-select').value));
$('agent-template').addEventListener('change', fillAgentTemplate);
$('agent-create').addEventListener('click', () => manage(async () => {
  const template = agents.find(agent => agent.id === $('agent-template').value);
  if (!template) throw new Error('请先选择一个助手模板。');
  let advanced;
  try { advanced = JSON.parse($('agent-advanced').value); }
  catch { throw new Error('工具与预算 JSON 格式有误，请检查后重试。'); }
  if (!advanced || typeof advanced !== 'object' || Array.isArray(advanced)
      || Object.keys(advanced).some(key => !['tools', 'budgets'].includes(key))) {
    throw new Error('高级配置只接受 tools 与 budgets。');
  }
  const {revision, enabled, ...configuration} = JSON.parse(JSON.stringify(template));
  configuration.id = $('agent-id').value.trim(); configuration.name = $('agent-name').value.trim();
  configuration.model.name = $('agent-model').value.trim();
  configuration.instructions = $('agent-instructions').value;
  if (advanced.tools !== undefined) configuration.tools = advanced.tools;
  if (advanced.budgets !== undefined) configuration.budgets = advanced.budgets;
  const response = await api('/api/agents', configuration);
  await loadExtensions(); await selectAgent(response.agent.id);
  $('agent-select').value = response.agent.id;
  $('extension-status').textContent = '助手已创建，可以新建会话。';
}));
$('agent-toggle').addEventListener('click', () => manage(async () => {
  const agent = agents.find(item => item.id === selectedAgentId);
  if (!agent) return;
  await api(`/api/agents/${encodeURIComponent(agent.id)}/enabled`, {enabled: agent.enabled === false});
  await loadExtensions(); $('extension-status').textContent = '助手状态已更新。';
}));
for (const kind of ['skill', 'mcp']) $(kind + '-install').addEventListener('click', () => manage(async () => {
  const path = $(kind + '-path').value.trim();
  if (!path) throw new Error('请填写要导入的本地路径。');
  await api(`/api/${kind === 'skill' ? 'skills' : 'mcp'}/install`, {path});
  await loadExtensions(); $('extension-status').textContent = '已导入。选择助手并绑定后，新会话可以使用。';
}));
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
$('session-load-more').addEventListener('click', loadMoreSessions);
$('run-load-more').addEventListener('click', loadMoreRuns);
$('session-create-toggle').addEventListener('click', () => setSessionCreateOpen(
  $('session-create-toggle').getAttribute('aria-expanded') !== 'true'));
$('session-create').addEventListener('click', async () => {
  $('session-error').hidden = true;
  const scope = $('session-mode').value === 'directory' ? {mode: 'directory'}
    : {mode: 'file', file: $('session-file').value.trim()};
  try {
    const response = await api('/api/sessions', {title: $('session-name').value.trim(), scope,
      ...(selectedAgentId ? {agent_id: selectedAgentId} : {})});
    await loadSessions(response.session.id, fixedSessionSelection);
    setSessionCreateOpen(false);
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
  $('composer-settings').open = false;
  $('model-status').open = false;
  $('form-error').hidden = true; busy = true; liveRunId = null; clearTimeout(timer); updateControls();
  try {
    const question = $('question').value.trim();
    const outputFile = $('output-file').value.trim();
    let job;
    if (activeSessionId) {
      const requestedSession = activeSessionId, generation = viewGeneration;
      const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
      const response = await api(`/api/sessions/${requestedSession}/runs`, {
        client_request_id: requestId, task_type: $('task-type').value,
        question, output_file: outputFile || null, ...selectedRunCapabilities()
      });
      if (requestedSession !== activeSessionId || generation !== viewGeneration) return;
      job = normalizedSessionRun(response.run);
    } else {
      const body = $('discover').checked ? {mode: 'directory', question} : {file: $('file').value.trim(), question};
      if (outputFile) body.output_file = outputFile;
      if (selectedAgentId) body.agent_id = selectedAgentId;
      Object.assign(body, selectedRunCapabilities());
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
  if (!liveRunId || !busy || cancelling) return;
  const requestedId = liveRunId, requestedSession = activeSessionId, generation = viewGeneration;
  cancelSendingId = requestedId; cancelling = true; $('cancel').disabled = true; updateApprovalControls();
  try {
    const response = await api(runPath(requestedId, '/cancel'), {});
    const job = requestedSession ? normalizedSessionRun(response.run) : response;
    if (cancelSendingId === requestedId) cancelSendingId = null;
    if (requestedId === liveRunId && requestedSession === activeSessionId && generation === viewGeneration) render(job);
  } catch (error) {
    if (cancelSendingId === requestedId) cancelSendingId = null;
    if (requestedId === liveRunId && requestedSession === activeSessionId && generation === viewGeneration) {
      cancelling = false; $('cancel').disabled = false; showError(error);
      if (!connectionExpired(error)) updateApprovalControls();
    }
  }
});
async function decideApproval(decision) {
  if (!pendingApproval || !ready || cancelling || Date.now() >= approvalDeadline) return;
  const approvalId = pendingApproval.approval_id || pendingApproval.id;
  if (approvalSendingId === approvalId || approvalBlockedId === approvalId) return;
  const requestedId = liveRunId, requestedSession = activeSessionId, generation = viewGeneration;
  approvalSendingId = approvalId; $('approval-error').hidden = true; updateApprovalControls();
  try {
    const response = await api(runPath(requestedId, `/approvals/${approvalId}`), {decision});
    const job = requestedSession ? normalizedSessionRun(response.run) : response;
    if (requestedId === liveRunId && requestedSession === activeSessionId && generation === viewGeneration) render(job);
  } catch (error) {
    if (requestedId !== liveRunId || requestedSession !== activeSessionId || generation !== viewGeneration) return;
    showError(error, 'approval-error');
    if (!connectionExpired(error)) {
      approvalBlockedId = approvalId;
      clearTimeout(timer); timer = setTimeout(poll, 100);
    }
  } finally {
    if (approvalSendingId === approvalId) approvalSendingId = null;
    if (requestedId === liveRunId && requestedSession === activeSessionId && generation === viewGeneration) updateApprovalControls();
  }
}
$('continue-run').addEventListener('click', async () => {
  if (!activeSessionId || !continuableRunId || busy) return;
  const sessionId = activeSessionId, parentId = continuableRunId, generation = viewGeneration;
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
$('agent-settings-toggle').addEventListener('click', () => {
  $('agent-settings-dialog').showModal();
  requestAnimationFrame(() => $('agent-settings-close').focus());
});
$('agent-settings-close').addEventListener('click', () => $('agent-settings-dialog').close());
$('agent-settings-dialog').addEventListener('cancel', event => {
  event.preventDefault(); $('agent-settings-dialog').close(); $('agent-settings-toggle').focus();
});
$('mobile-agent-switch').addEventListener('click', () => {
  setDrawer('sidebar');
  requestAnimationFrame(() => $('sidebar-close').focus());
});
$('work-rail-toggle').addEventListener('click', () => {
  setDrawer('work');
  requestAnimationFrame(() => $('work-rail-close').focus());
});
$('work-rail-close').addEventListener('click', () => setDrawer());
$('work-detail-button').addEventListener('click', () => {
  if (!inspectedRunId) return;
  setDrawer('inspector');
  requestAnimationFrame(() => $('inspector-close').focus());
});
for (const name of ['sidebar', 'inspector']) {
  $(name + '-toggle').addEventListener('click', () => {
    setDrawer(name);
    requestAnimationFrame(() => $(name + '-close').focus());
  });
  $(name + '-close').addEventListener('click', () => setDrawer());
}
$('workspace-backdrop').addEventListener('click', () => setDrawer());
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && document.body.dataset.drawer) setDrawer();
});
const mobileDrawer = matchMedia('(max-width: 767px)');
const inspectorDrawer = matchMedia('(max-width: 1199px)');
for (const media of [mobileDrawer, inspectorDrawer]) {
  if (media.addEventListener) media.addEventListener('change', syncDrawerAccess);
  else media.addListener(syncDrawerAccess);
}
syncDrawerAccess();
initialize();
