'use strict';

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
let ready = false, simulated = false, activeId = null, busy = false, timer = null, renderedEvents = 0;
let viewGeneration = 0, renderedRevision = -1;
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
  SESSION_EXPIRED: '本地服务已重启，请刷新页面重新连接。',
  AUTH_ERROR: 'DeepSeek 鉴权失败，请检查启动终端中的 API Key。',
  RATE_LIMIT: 'DeepSeek 请求频率受限，请稍后重试。',
  NETWORK_ERROR: '连接 DeepSeek 失败，请检查网络后重新提问。',
  MODEL_TIMEOUT: '等待模型回复超时，本次已停止。',
  RUN_TIMEOUT: '本次任务超过时间限制，已停止。',
  TOOL_TIMEOUT: '读取文件超时，本次已停止。',
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
  'tool.requested':'模型请求读取文件', 'tool.started':'开始读取文件', 'tool.finished':'文件操作结束',
  'tool.completed':'读取结果已回填', 'answer.rejected':'回答格式不合规，正在纠错', 'run.ended':'任务结束'};
function eventLabel(event) {
  if (event?.event === 'answer.rejected' && event.detail.code === 'IDENTIFIER_MISMATCH') {
    return '标识与原文不一致，正在纠错';
  }
  if (event?.detail.name === 'list_files') {
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
  $('start').disabled = busy || !ready;
  $('start').textContent = busy ? '运行中…' : '开始问答 ↗';
  $('cancel').hidden = !busy || !activeId;
  $('discover').disabled = busy || !ready;
  $('file').disabled = busy || $('discover').checked;
  $('file').required = !$('discover').checked;
  for (const element of [$('question'), $('sample'), ...document.querySelectorAll('[data-question]')]) element.disabled = busy;
  $('privacy').textContent = simulated ? '演示模式：只在本机读取资料，不向模型发送内容。原文件保持只读。'
    : $('discover').checked ? '开始后，问题、目录元数据和已读取的资料内容将发送给 DeepSeek。原文件保持只读。'
    : '开始后，所选文件内容和问题将发送给 DeepSeek。原文件保持只读。';
}

function status(text, type = 'neutral') { $('run-status').textContent = text; $('run-status').className = `badge ${type}`; }
function showError(error, area = 'form-error') { $(area).textContent = error.message; $(area).hidden = false; }
function countQuestion() { $('count').textContent = `${$('question').value.length} / 4000`; }

function prepareOutput(job) {
  clearTimeout(timer);
  activeId = job.id; renderedEvents = 0; renderedRevision = -1; viewGeneration++;
  $('empty').hidden = true; $('output').hidden = false;
  for (const id of ['answer-block', 'evidence', 'failure', 'form-error', 'scope-summary']) $(id).hidden = true;
  $('events').replaceChildren(); $('citations').replaceChildren(); $('trace-path').textContent = '';
  $('source-line').textContent = `${job.mode === 'directory' ? '目录发现' : job.file || '资料'} · ${job.question}`;
  $('copy').textContent = '复制回答';
}

function render(job) {
  if (activeId !== job.id || job.revision < renderedRevision) return;
  renderedRevision = job.revision;
  for (const event of job.events.slice(renderedEvents)) {
    const li = document.createElement('li'), stamp = document.createElement('time'), label = document.createElement('span');
    stamp.textContent = `${event.elapsed.toFixed(1)}s`;
    const detail = event.detail;
    label.textContent = eventLabel(event) + (detail.path ? ` · ${detail.path}` : '') + (detail.code ? ` · ${detail.code}` : '');
    li.append(stamp, label); $('events').append(li);
  }
  renderedEvents = job.events.length; $('event-count').textContent = `${renderedEvents} 个步骤`;
  busy = job.result === null;
  $('progress').hidden = !busy;
  if (busy) {
    status(job.cancelling ? '正在取消' : '正在处理', '');
    const last = job.events.at(-1);
    $('progress-text').textContent = job.cancelling ? '正在停止后续操作…' : `${eventLabel(last)}…`;
    $('cancel').disabled = job.cancelling;
  } else {
    clearTimeout(timer);
    const result = job.result, answer = result.answer;
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
      $('answer-title').textContent = answer.status === 'not_found' ? '资料中未找到这项信息' : '资料中的答案';
      $('answer-text').textContent = answer.answer;
      $('answer-note').textContent = simulated ? '模拟演示：显示读取到的内容，未调用真实模型，也未理解问题。' : '回答通过格式与引用检查；请结合原文判断内容是否准确。';
      $('evidence').hidden = answer.citations.length === 0;
      $('citation-count').textContent = `${answer.citations.length} 处引用`;
      $('citations').replaceChildren();
      for (const citation of answer.citations) {
        const article = document.createElement('article'), heading = document.createElement('header'), quote = document.createElement('blockquote');
        article.className = 'citation';
        heading.textContent = `${citation.path} · 第 ${citation.start_line}${citation.end_line === citation.start_line ? '' : '–' + citation.end_line} 行`;
        quote.textContent = citation.quote; article.append(heading, quote); $('citations').append(article);
      }
    } else {
      const cancelled = job.state === 'cancelled';
      status(cancelled ? '已取消' : job.state === 'unable' ? '无法读取' : '本次未完成', 'warning');
      $('failure').hidden = false; $('failure-title').textContent = cancelled ? '本次运行已取消' : '没有生成有效答案';
      const toolError = [...job.events].reverse().find(e => e.event === 'tool.completed' && e.detail.code)?.detail.code;
      const reason = job.state === 'unable' ? (toolError || result.stop_reason) : result.stop_reason;
      $('failure-message').textContent = messageFor(reason) + (answer?.answer ? `\n${answer.answer}` : '');
    }
  }
  updateControls();
}

async function poll() {
  if (!activeId) return;
  const requestedId = activeId, generation = viewGeneration;
  try {
    const job = await api(`/api/runs/${requestedId}`);
    if (requestedId !== activeId || generation !== viewGeneration) return;
    $('connection-error').hidden = true;
    render(job);
    if (busy) timer = setTimeout(poll, 450);
  } catch (error) {
    if (requestedId !== activeId || generation !== viewGeneration) return;
    showError(error, 'connection-error');
    if (error.code === 'SESSION_EXPIRED' || error.code === 'RUN_NOT_FOUND') {
      status('需要刷新页面', 'warning'); ready = false; updateControls();
    } else timer = setTimeout(poll, 2000);
  }
}

async function initialize() {
  try {
    const config = await api('/api/config');
    $('workspace').textContent = config.workspace;
    ready = config.ready; simulated = Boolean(config.provider?.simulated);
    $('connection').textContent = simulated ? '模拟演示 · 未调用模型' : ready ? 'DeepSeek 已配置' : '模型未配置';
    $('connection').className = `badge ${simulated || !ready ? 'warning' : ''}`;
    if (simulated || !ready) {
      $('mode-notice').hidden = false;
      $('mode-notice').textContent = simulated ? '这是页面演示模式：文件会真实读取，回答由测试替身生成。真实使用请从已配置 API Key 的终端启动，不加 --demo。' : messageFor(config.error);
    }
    $('sample').hidden = !config.example_file;
    $('sample').onclick = () => {
      $('discover').checked = false; updateControls();
      $('file').value = config.example_file;
      $('question').value = '项目代号、评审人和演示日期分别是什么？引用原文。';
      countQuestion(); $('question').focus();
    };
    if (config.latest_run_id) {
      busy = true; updateControls();
      const job = await api(`/api/runs/${config.latest_run_id}`);
      $('discover').checked = job.mode === 'directory';
      $('file').value = job.file || ''; $('question').value = job.question; countQuestion(); prepareOutput(job); render(job);
      if (busy) timer = setTimeout(poll, 450);
    } else updateControls();
  } catch (error) { ready = false; busy = false; updateControls(); showError(error, 'connection-error'); }
}

$('question').addEventListener('input', countQuestion);
$('discover').addEventListener('change', updateControls);
document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => {
  $('question').value = button.dataset.question; countQuestion(); $('question').focus();
}));
$('question-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy || !ready) return;
  $('form-error').hidden = true; busy = true; activeId = null; viewGeneration++; clearTimeout(timer); updateControls();
  try {
    const question = $('question').value.trim();
    const body = $('discover').checked ? {mode: 'directory', question} : {file: $('file').value.trim(), question};
    const job = await api('/api/runs', body);
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
  const requestedId = activeId, generation = viewGeneration;
  $('cancel').disabled = true;
  try {
    const job = await api(`/api/runs/${requestedId}/cancel`, {});
    if (requestedId === activeId && generation === viewGeneration) render(job);
  } catch (error) {
    if (requestedId === activeId && generation === viewGeneration) { $('cancel').disabled = false; showError(error); }
  }
});
$('copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('answer-text').textContent); $('copy').textContent = '已复制'; }
  catch { $('copy').textContent = '请选中文字复制'; }
});
initialize();
