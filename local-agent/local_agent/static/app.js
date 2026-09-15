'use strict';

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
let ready = false, simulated = false, activeId = null, busy = false, timer = null, renderedEvents = 0;
let viewGeneration = 0, renderedRevision = -1;
let activeSessionId = null, activeSession = null, fixedSessionSelection = false;
let workspaceAvailable = true, historyRuns = [], runHistoryCursor = null, runPageRequest = null;
let agents = [], capabilities = {skills: [], servers: []}, selectedAgentId = '', managing = false;
let sessionListGeneration = 0, visibleSessions = [], sessionPageRequest = null;
let sessionCursors = {active: null, archived: null};
const sessionAgentIds = new Map();
let pendingApproval = null, approvalSendingId = null, approvalBlockedId = null, approvalDeadline = 0, approvalTimer = null;
let cancelling = false, cancelSendingId = null;
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

function setDrawer(name = null, returnFocus = true) {
  const wasOpen = document.body.dataset.drawer;
  if (name) {
    document.body.dataset.drawer = name;
    drawerTrigger = $(name === 'sidebar' ? 'sidebar-toggle' : 'inspector-toggle');
  } else {
    delete document.body.dataset.drawer;
  }
  $('sidebar-toggle').setAttribute('aria-expanded', String(name === 'sidebar'));
  $('inspector-toggle').setAttribute('aria-expanded', String(name === 'inspector'));
  $('workspace-backdrop').hidden = !name;
  syncDrawerAccess();
  if (!name && wasOpen && returnFocus && drawerTrigger) drawerTrigger.focus();
}

function syncDrawerAccess() {
  const open = document.body.dataset.drawer || null;
  const sidebarHidden = mobileDrawer.matches && open !== 'sidebar';
  const inspectorHidden = inspectorDrawer.matches && open !== 'inspector';
  for (const [panel, hidden] of [[$('sidebar-panel'), sidebarHidden],
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

function currentAgent() {
  // An old session without a recorded snapshot must not inherit today's bindings.
  if (activeSessionId) return activeSession?.agent_snapshot || null;
  if (selectedAgentId) return agents.find(agent => agent.id === selectedAgentId) || null;
  return agents.find(agent => agent.id === ($('discover').checked ? 'directory-qa' : 'file-qa')) || null;
}

function renderAgentList() {
  const list = $('agent-list');
  list.replaceChildren();
  const currentId = activeSession?.agent_id || selectedAgentId;
  for (const agent of agents) {
    const button = document.createElement('button');
    const name = document.createElement('strong'), details = document.createElement('span');
    button.type = 'button'; button.className = 'agent-nav-item'; button.dataset.agentId = agent.id;
    button.setAttribute('aria-pressed', String(agent.id === currentId));
    button.disabled = busy || managing;
    name.textContent = agent.name;
    details.textContent = `${agent.strategy === 'file' ? '单文件' : '目录'} · ${agent.enabled === false ? '已停用' : '可用'}`;
    button.addEventListener('click', () => {
      $('agent-select').value = agent.id;
      selectAgent(agent.id);
    });
    button.append(name, details); list.append(button);
  }
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
  $('agent-select').disabled = busy || managing || fixedSessionSelection;
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
  $('session-mode').disabled = busy || Boolean(selected) || fixedSessionSelection;
  $('session-file').disabled = busy || $('session-mode').value === 'directory';
  $('session-select').disabled = busy || managing;
  $('discover').disabled = busy || Boolean(activeSessionId) || Boolean(selected) || !ready;
  $('file').disabled = busy || Boolean(activeSessionId) || $('discover').checked;
  $('file').required = !activeSessionId && !$('discover').checked;
  const current = agents.find(agent => agent.id === (activeSession?.agent_id || selectedAgentId));
  if (current?.enabled === false) $('start').disabled = true;
  if (selected?.enabled === false) $('session-create').disabled = true;
  for (const element of document.querySelectorAll('[data-management]')) element.disabled = busy || managing;
  $('agent-toggle').disabled = busy || managing || !selected;
  $('agent-toggle').textContent = selected?.enabled === false ? '启用当前助手' : '停用当前助手';
  for (const button of document.querySelectorAll('[data-bind-kind]')) {
    button.disabled = busy || managing || !selected || selected.enabled === false || button.dataset.bound === 'true';
  }
  for (const button of document.querySelectorAll('[data-agent-id],[data-session-id]')) {
    button.disabled = busy || managing;
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
  const templateWas = $('agent-template').value;
  selectOptions($('agent-select'), agents.map(agent => ({value: agent.id,
    label: `${agent.name}${agent.enabled === false ? '（已停用）' : ''}`})), '自动选择（按单文件 / 目录模式）', selectedAgentId);
  selectOptions($('agent-template'), agents.map(agent => ({value: agent.id, label: agent.name})), null);
  if (!templateWas) fillAgentTemplate();
  renderAgentList(); renderCapabilities(); updateControls();
}

async function manage(action) {
  if (busy || managing) return;
  managing = true; $('extension-error').hidden = true; $('extension-status').textContent = '正在处理…'; updateControls();
  try { await action(); }
  catch (error) { $('extension-status').textContent = ''; showError(error, 'extension-error'); }
  finally { managing = false; updateControls(); }
}

async function selectAgent(agentId) {
  if (busy) return;
  selectedAgentId = agentId;
  activeSessionId = null; activeSession = null; viewGeneration++;
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
  $('start').disabled = busy || !ready || archived || (sessionMode && !conversation && !workspaceAvailable);
  $('start').textContent = busy ? 'Agent 工作中…' : '发送任务 ↑';
  $('thread-panel').setAttribute('aria-busy', String(busy));
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
  updateAgentControls();
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
    if (!approval.historical) requestAnimationFrame(() => {
      const currentId = pendingApproval?.approval_id || pendingApproval?.id;
      if (currentId === id) $('approval-title').focus();
    });
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
  $('inspector-run-empty').hidden = false;
  for (const id of ['scope-summary', 'artifacts', 'evidence', 'trace']) $(id).hidden = true;
  $('continue-run').hidden = true; updateControls();
}

function markCurrentRun(runId) {
  for (const button of $('session-history').querySelectorAll('[data-run-id]')) {
    if (button.dataset.runId === runId) button.setAttribute('aria-current', 'true');
    else button.removeAttribute('aria-current');
  }
}

function currentRunPageLoading() {
  return Boolean(runPageRequest && runPageRequest.sessionId === activeSessionId
    && runPageRequest.generation === viewGeneration);
}

function renderRunHistory(runs, append = false) {
  const merged = append ? [...historyRuns, ...runs] : [...runs];
  historyRuns = [...new Map(merged.map(item => [item.id, item])).values()];
  $('session-history').replaceChildren();
  for (const item of historyRuns) {
    const button = document.createElement('button');
    button.type = 'button'; button.dataset.runId = item.id;
    const when = formatTimestamp(item.started_at || item.created_at);
    button.textContent = `${runStateLabel(item.state)} · ${item.question}${when ? ` · ${when}` : ''}`;
    button.title = item.question;
    button.addEventListener('click', () => openSessionRun(item.id));
    $('session-history').append(button);
  }
  markCurrentRun(activeId);
  $('run-load-more').hidden = !runHistoryCursor;
  $('run-load-more').disabled = currentRunPageLoading() || busy;
}

async function loadMoreRuns() {
  if (!activeSessionId || !runHistoryCursor || currentRunPageLoading()) return;
  const sessionId = activeSessionId, generation = viewGeneration, cursor = runHistoryCursor;
  const request = {sessionId, generation, cursor};
  runPageRequest = request; $('run-load-more').disabled = true;
  try {
    const response = await api(`/api/sessions/${sessionId}/runs?cursor=${encodeURIComponent(cursor)}`);
    if (sessionId !== activeSessionId || generation !== viewGeneration) return;
    runHistoryCursor = response.next_cursor || null;
    renderRunHistory(response.runs || [], true);
  } catch (error) {
    if (sessionId === activeSessionId && generation === viewGeneration) showError(error, 'session-error');
  } finally {
    if (runPageRequest === request) runPageRequest = null;
    if (sessionId === activeSessionId && generation === viewGeneration) renderRunHistory([], true);
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
    markCurrentRun(job.id);
    focusRunResult(job);
    if (busy) timer = setTimeout(poll, 450);
  } catch (error) {
    if (sessionId === activeSessionId && generation === viewGeneration) showError(error, 'session-error');
  }
}

function focusRunResult(job) {
  const target = job.state === 'completed' && !$('answer-block').hidden
    ? $('answer-title') : !$('failure').hidden ? $('failure-title') : null;
  if (!target || document.activeElement === $('question')) return;
  requestAnimationFrame(() => {
    if (activeId === job.id && document.activeElement !== $('question')) target.focus();
  });
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
  for (const session of visibleSessions) {
    if (session.agent_id) sessionAgentIds.set(session.id, session.agent_id);
    const option = document.createElement('option'); option.value = session.id;
    option.textContent = session.title + (session.status === 'archived' ? '（已归档）' : '');
    select.append(option);
  }
  temporary.hidden = fixedSessionSelection;
  if (activeSessionId && visibleSessions.some(item => item.id === activeSessionId)) select.value = activeSessionId;
}

function renderSessionList() {
  const list = $('session-list'); list.replaceChildren();
  if (!visibleSessions.length) {
    const empty = document.createElement('p'); empty.className = 'field-help session-empty';
    empty.textContent = '还没有持久会话。'; list.append(empty);
  }
  for (const session of visibleSessions) {
    const button = document.createElement('button'), title = document.createElement('strong');
    const details = document.createElement('span');
    button.type = 'button'; button.className = 'session-nav-item'; button.dataset.sessionId = session.id;
    if (session.id === activeSessionId) button.setAttribute('aria-current', 'true');
    button.disabled = busy || managing;
    title.textContent = session.title;
    const scope = session.scope?.mode === 'directory' ? '目录' : session.scope?.file || '单文件';
    const when = formatTimestamp(session.updated_at || session.created_at);
    details.textContent = `${scope}${session.status === 'archived' ? ' · 已归档' : ''}${when ? ` · ${when}` : ''}`;
    button.addEventListener('click', () => {
      $('session-select').value = session.id;
      selectSession(session.id);
    });
    button.append(title, details); list.append(button);
  }
  $('session-load-more').hidden = fixedSessionSelection || (!sessionCursors.active && !sessionCursors.archived);
  $('session-load-more').disabled = currentSessionPageLoading() || busy || managing;
}

function currentSessionPageLoading() {
  return Boolean(sessionPageRequest && sessionPageRequest.generation === sessionListGeneration
    && sessionPageRequest.agentId === selectedAgentId);
}

async function loadMoreSessions() {
  if (fixedSessionSelection || currentSessionPageLoading()
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

async function selectSession(sessionId) {
  const generation = ++viewGeneration;
  clearTimeout(timer); activeSessionId = sessionId || null; activeSession = null;
  $('session-error').hidden = true;
  if (!activeSessionId) {
    $('session-actions').hidden = true; $('session-scope').hidden = true;
    runHistoryCursor = null; renderRunHistory([]); renderSessionList(); renderAgentList();
    $('task-type').value = 'files'; clearRunView(); return;
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
    renderSessionList(); renderAgentList(); updateControls();
    const directory = activeSession.scope.mode === 'directory';
    $('discover').checked = directory; $('file').value = activeSession.scope.file || '';
    $('session-scope').hidden = false;
    $('session-scope').textContent = `固定资料范围：${directory ? '当前工作区目录发现' : activeSession.scope.file}`
      + (activeSession.agent_id ? ` · 助手 ${activeSession.agent_id} · 修订 ${(activeSession.agent_revision || '未知').slice(0, 12)}（旧会话保留此版本）` : '')
      + (workspaceAvailable ? '' : ' · 原工作区当前不可用');
    runHistoryCursor = runsResponse.next_cursor || null;
    renderRunHistory(runsResponse.runs);
    if (runsResponse.runs.length) await openSessionRun(runsResponse.runs[0].id);
    else clearRunView();
  } catch (error) {
    if (generation === viewGeneration) showError(error, 'session-error');
  }
  updateControls();
}

async function loadSessions(selectedId = null, fixed = fixedSessionSelection) {
  const generation = ++sessionListGeneration, requestedAgent = selectedAgentId;
  const [active, archived] = await Promise.all([api('/api/sessions'), api('/api/sessions?archived=1')]);
  if (generation !== sessionListGeneration || requestedAgent !== selectedAgentId) return;
  fixedSessionSelection = Boolean(fixed);
  visibleSessions = [...new Map([...(active.sessions || []), ...(archived.sessions || [])]
    .map(item => [item.id, item])).values()];
  sessionCursors = {active: active.next_cursor || null, archived: archived.next_cursor || null};
  renderSessionOptions(); renderSessionList();
  $('session-create').hidden = fixedSessionSelection;
  const wanted = selectedId || active.sessions?.[0]?.id || null;
  if (wanted) { $('session-select').value = wanted; await selectSession(wanted); }
  else {
    $('session-select').value = ''; await selectSession('');
    if (document.activeElement === document.body) $('question').focus();
  }
}

function prepareOutput(job) {
  clearTimeout(timer);
  activeId = job.id; renderedEvents = 0; renderedRevision = -1; viewGeneration++;
  pendingApproval = null; approvalBlockedId = null; cancelling = false; clearTimeout(approvalTimer);
  $('empty').hidden = true; $('output').hidden = false;
  $('inspector-run-empty').hidden = true; $('trace').hidden = false;
  for (const id of ['answer-block', 'evidence', 'failure', 'form-error', 'scope-summary', 'approval', 'artifacts']) $(id).hidden = true;
  $('continue-run').hidden = true;
  $('events').replaceChildren(); $('citations').replaceChildren(); $('trace-path').textContent = '';
  $('source-line').textContent = `${job.task_type === 'conversation' ? '会话记录'
    : job.mode === 'directory' ? '目录发现' : job.file || '资料'} · ${job.question}`;
  $('copy').textContent = '复制回答';
}

function render(job) {
  if (activeId !== job.id || job.revision < renderedRevision) return;
  const focusWhenFinished = busy && renderedRevision >= 0 && job.result !== null;
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
        const source = citation.source;
        const sourceName = typeof source === 'string' ? source : source?.label || source?.name
          || (source ? `${sourceLabel(source.type)} · ${source.server_id || source.id || citation.source_id || citation.path || ''}` : null);
        heading.textContent = sourceName || citation.source_id
          ? `${sourceName || citation.source_id}${citation.start_line ? ` · 第 ${citation.start_line}${citation.end_line === citation.start_line ? '' : '–' + citation.end_line} 行` : ''}`
          : citation.message_id
          ? `会话消息 ${citation.message_id} · 字符 ${citation.start}–${citation.end}`
          : `${citation.path} · 第 ${citation.start_line}${citation.end_line === citation.start_line ? '' : '–' + citation.end_line} 行`;
        quote.textContent = citation.quote; article.append(heading, quote); $('citations').append(article);
      }
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
      $('continue-run').hidden = !interrupted || !activeSessionId;
    }
  }
  updateControls();
  if (focusWhenFinished) focusRunResult(job);
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
      if (requestedSession === activeSessionId && generation === viewGeneration) {
        runHistoryCursor = runs.next_cursor || null;
        renderRunHistory(runs.runs);
      }
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
    try { await loadExtensions(); }
    catch (error) { showError(error, 'extension-error'); }
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
$('session-create').addEventListener('click', async () => {
  $('session-error').hidden = true;
  const scope = $('session-mode').value === 'directory' ? {mode: 'directory'}
    : {mode: 'file', file: $('session-file').value.trim()};
  try {
    const response = await api('/api/sessions', {title: $('session-name').value.trim(), scope,
      ...(selectedAgentId ? {agent_id: selectedAgentId} : {})});
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
const inspectorDrawer = matchMedia('(max-width: 1179px)');
const compactComposer = mobileDrawer;
const syncComposerDensity = event => {
  document.querySelector('.composer-settings').open = !event.matches;
};
if (compactComposer.addEventListener) compactComposer.addEventListener('change', syncComposerDensity);
else compactComposer.addListener(syncComposerDensity);
for (const media of [mobileDrawer, inspectorDrawer]) {
  if (media.addEventListener) media.addEventListener('change', syncDrawerAccess);
  else media.addListener(syncDrawerAccess);
}
syncComposerDensity(compactComposer);
syncDrawerAccess();
initialize();
