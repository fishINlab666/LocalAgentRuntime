/* Three real-material page trials. Requires a live, explicitly configured DeepSeek service. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {chromium} = require('playwright');

const base = process.env.TRIAL_URL || 'http://127.0.0.1:8765';
const file = 'meeting02-excerpt.md';
const workspace = fs.realpathSync(path.join(__dirname, 'workspace'));
const source = fs.readFileSync(path.join(workspace, file), 'utf8').replace(/\r\n?/g, '\n');
const lines = source.trimEnd().split('\n');
const cases = [
  {name: '事实查询', status: 'answered', question: '会议建议先验证 API 的什么输入输出过程，再做什么工具？请引用原文。'},
  {name: '步骤整理', status: 'answered', question: '按会议原文整理接下来要做的步骤，以及当时明确说先不管的内容；区分会议建议和已经完成的事实，引用原文。'},
  {name: '缺失信息', status: 'not_found', question: '这段会议是否明确给出了项目验收的日历日期和具体负责人？若未说明，请明确指出，不要根据录制时间或说话人推断。'},
];

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const report = {created_at: new Date().toISOString(), source: file,
    source_sha256: crypto.createHash('sha256').update(source).digest('hex'),
    gate: 'NOT_RUN', semantic_review: 'pending', user_experience: 'pending', trials: []};
  let output;
  try {
    await page.goto(base, {waitUntil: 'networkidle'});
    const token = await page.locator('meta[name="session-token"]').getAttribute('content');
    const configResponse = await page.request.get(base + '/api/config', {headers: {'X-Session-Token': token}});
    assert.equal(configResponse.status(), 200, 'Cannot access current service');
    const config = await configResponse.json();
    assert.equal(config.ready, true, 'Real provider has no usable environment configuration');
    assert.equal(config.provider.simulated, false, 'Demo mode cannot be used for this trial');
    assert.equal(config.provider.provider, 'deepseek', 'Unexpected provider');
    assert.equal(config.workspace, workspace, 'Service must use trial/workspace');
    assert.equal(await page.locator('#start').isEnabled(), true, 'Another run is active');
    report.provider = config.provider;
    output = path.join(__dirname, 'results', 'real-web-' + crypto.randomUUID());
    fs.mkdirSync(output, {recursive: true, mode: 0o700});
    for (const item of cases) {
      await page.locator('#file').fill(file);
      await page.locator('#question').fill(item.question);
      const posted = page.waitForResponse(r => r.url() === base + '/api/runs' && r.request().method() === 'POST');
      const started = Date.now();
      await page.locator('#start').click();
      const response = await posted;
      assert.equal(response.status(), 202, 'Run was not accepted');
      const accepted = await response.json();
      await page.waitForFunction(() => !document.getElementById('start').disabled, null, {timeout: 150000});
      const runResponse = await page.request.get(base + '/api/runs/' + accepted.id,
        {headers: {'X-Session-Token': token}});
      assert.equal(runResponse.status(), 200);
      const run = await runResponse.json();
      const answer = run.result?.answer;
      const citationsOK = Array.isArray(answer?.citations) && answer.citations.every(c =>
        c.path === file && Number.isInteger(c.start_line) && Number.isInteger(c.end_line) &&
        c.start_line >= 1 && c.end_line >= c.start_line && c.end_line <= lines.length &&
        c.quote === lines.slice(c.start_line - 1, c.end_line).join('\n'));
      const checks = {
        completed: run.state === 'completed',
        expected_status: answer?.status === item.status,
        exact_citations: citationsOK && (item.status !== 'answered' || answer.citations.length > 0),
        tool_result_recorded: run.events.some(e => e.event === 'tool.completed' && e.detail.ok),
        real_provider: run.result?.provider?.simulated === false,
        answer_visible: answer ? await page.locator('#answer-text').innerText() === answer.answer : false,
      };
      await page.locator('#trace').evaluate(el => {el.open = true;});
      const screenshot = path.join(output, item.name + '.png');
      await page.screenshot({path: screenshot, fullPage: true});
      report.trials.push({...item, checks, automated_pass: Object.values(checks).every(Boolean),
        page_elapsed_seconds: (Date.now() - started) / 1000, run, screenshot});
      fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2), {mode: 0o600});
      console.log(item.name + ': ' + run.state + ', checks=' + JSON.stringify(checks));
    }
    report.gate = report.trials.every(t => t.automated_pass) ? 'PENDING_SEMANTIC_REVIEW' : 'FAILED';
    const lastId = report.trials.at(-1).run.id;
    await page.reload({waitUntil: 'networkidle'});
    await page.waitForFunction(() => !document.getElementById('start').disabled);
    report.refresh_restores_answer = await page.locator('#answer-text').innerText() === report.trials.at(-1).run.result?.answer?.answer;
    report.last_run_id = lastId;
    console.log('Report: ' + path.join(output, 'report.json'));
  } catch (error) {
    report.gate = report.trials.length ? 'FAILED' : 'NOT_RUN';
    console.error('Trial stopped: ' + error.message);
    process.exitCode = 1;
  } finally {
    if (output) fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2), {mode: 0o600});
    await browser.close();
  }
})();
