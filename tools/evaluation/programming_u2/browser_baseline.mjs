// Local U2 observation harness. API responses are fixed fixtures, never a live backend.
// Usage: node browser_baseline.mjs APP_DIR OUTPUT_DIR [FRONTEND_URL]
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve, join } from 'node:path';
import { execFileSync } from 'node:child_process';

const appDir = resolve(process.argv[2]);
const outputDir = resolve(process.argv[3]);
const frontend = process.argv[4] || 'http://127.0.0.1:5188';
assert.equal(new URL(frontend).hostname, '127.0.0.1', 'Only the local audit frontend is allowed');
const backend = 'http://127.0.0.1:8188';
const { chromium } = createRequire(join(appDir, 'package.json'))('@playwright/test');
await mkdir(outputDir, { recursive: true });
const evidence = {
  kind: 'real Chromium UI with fixed API responses; no actual persistence, model, or OCI',
  source_sha: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: appDir, encoding: 'utf8' }).trim(),
  frontend, backend, started_at: new Date().toISOString(),
  checks: [], requests: [], unexpected_requests: [], external_attempts: [], console: [], page_errors: [],
};
// Verify that the page and its executable bundle are the specified checkout's
// built files, rather than trusting that another server on this port is ours.
const sha256 = value => createHash('sha256').update(value).digest('hex');
const index = await readFile(join(appDir, 'dist/index.html'));
const servedIndex = Buffer.from(await (await fetch(frontend)).arrayBuffer());
assert.equal(sha256(servedIndex), sha256(index), 'Served page differs from APP_DIR/dist');
const scripts = [...index.toString().matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)].map(match => match[1]);
assert.ok(scripts.length, 'No built script in index.html');
const assets = [];
for (const script of scripts) {
  assert.ok(script.startsWith('/assets/') && !script.includes('..'), 'Unexpected bundle path');
  const local = await readFile(join(appDir, 'dist', script));
  const served = Buffer.from(await (await fetch(frontend + script)).arrayBuffer());
  assert.equal(sha256(served), sha256(local), 'Served script differs from APP_DIR/dist');
  assets.push({ path: script, sha256: sha256(local) });
}
evidence.served_build = { app_dir: appDir, index_sha256: sha256(index), scripts: assets,
  package_lock_sha256: sha256(await readFile(join(appDir, 'package-lock.json'))),
  verification: 'local dist index and scripts byte-identical to HTTP responses before browser launch' };
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' });
await context.addInitScript(() => {
  localStorage.setItem('smartai_token', 'synthetic-u2-token');
  localStorage.setItem('smartai_locale', 'zh-CN');
});

const initial = (cases) => ({
  q_id: 'q1', number: '1', type: 'programming', max_score: 10,
  stem: 'U2 合成：实现 fib(n)，返回第 n 项。',
  criterion: 'U2 合成评分规则，尚无教师确认。', reference_answer: '动态规划。',
  review_status: 'confirmed', max_score_review_status: 'confirmed', preparation_issues: [],
  test_cases: cases,
});
let problem = initial([{ input: '1\n2\n', expected_output: '3', source: 'teacher', visibility: 'example', sandbox_feasible: true, description: 'U2 synthetic stdin' }]);
const task = { task_id: 'u2-browser', name: 'U2 local synthetic', status: 'problems_ready', workflow_revision: 1,
  problem_data: { q1: problem }, student_data: {}, problem_count: 1, student_count: 0,
  created_at: 1, updated_at: 1, kb_docs: {}, kb_doc_count: 0 };
let rejectNextSave = false;
let correction = null;
function setProblem(value) { problem = value; task.problem_data = { q1: value }; }
await context.route('**/*', async route => {
  const request = route.request();
  const url = new URL(request.url());
  if (url.origin === frontend) return route.continue();
  if (url.origin !== backend) { evidence.external_attempts.push(request.url()); return route.abort(); }
  const path = url.pathname;
  const body = request.postDataJSON();
  evidence.requests.push({ method: request.method(), path, ...(body ? { body } : {}) });
  const json = data => route.fulfill({ json: data });
  if (request.method() === 'OPTIONS') return route.fulfill({ status: 204 });
  if (path === '/auth/me' && request.method() === 'GET') return json({ id: 'u2-teacher', username: 'U2 Synthetic', role: 'teacher', is_active: true, auth_scope: 'user' });
  if (path === '/auth/activity' && request.method() === 'POST') return json({ token: 'synthetic-u2-token' });
  if (path === '/tasks/u2-browser/problems/q1' && request.method() === 'PUT') {
    if (rejectNextSave) { rejectNextSave = false; return route.fulfill({ status: 409, json: { detail: { code: 'stale_revision', message: 'Synthetic stale save' } } }); }
    assert.equal(body.expected_workflow_revision, task.workflow_revision);
    setProblem({ ...problem, ...body });
    task.workflow_revision += 1;
    return json({ status: 'ok', q_id: 'q1', problem, workflow_revision: task.workflow_revision });
  }
  if (request.method() === 'GET') {
    if (path === '/tasks/u2-browser/result') return json({ task_id: task.task_id, problem_data: task.problem_data, student_data: task.student_data, results: [{ student_id: 'S1', student_name: 'U2 synthetic student', corrections: [correction] }], timestamp: 1 });
    if (path === '/tasks/u2-browser/source-files') return json({ task_id: task.task_id, workflow_revision: task.workflow_revision, problem_source: null, submission_sources: {} });
    if (path === '/tasks/u2-browser/finalization') return json({ task_id: task.task_id, task_status: task.status, workflow_revision: task.workflow_revision, remaining_review_count: 0, required_review_count: 0, confirmed_required_count: 0, remaining_reviews: [], ready_for_confirmation: true });
    if (path === '/experts/available') return json([]);
    if (path === '/tasks/u2-browser') return json(task);
  }
  evidence.unexpected_requests.push({ method: request.method(), path });
  return route.fulfill({ status: 501, json: { detail: 'Unexpected U2 audit request' } });
});
const page = await context.newPage();
page.on('console', msg => { if (['error', 'warning'].includes(msg.type())) evidence.console.push({ type: msg.type(), text: msg.text() }); });
page.on('pageerror', error => evidence.page_errors.push(error.message));
async function snapshot(name) {
  await writeFile(join(outputDir, name + '.txt'), await page.locator('body').innerText());
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: join(outputDir, name + '.png'), fullPage: true });
}
function panel() { return page.getByRole('heading', { name: '测试样例', exact: true }).locator('xpath=ancestor::section[1]'); }
const editorPath = frontend + '/tasks/u2-browser/questions/q1/content';
try {
  await page.goto(editorPath);
  await page.getByRole('heading', { name: '测试样例', exact: true }).waitFor();
  assert.equal(await page.title(), 'SmarTAI');
  assert.equal(await page.locator('vite-error-overlay').count(), 0);
  const tests = panel();
  await tests.getByRole('button', { name: '修改', exact: true }).click();
  await tests.getByRole('textbox', { name: '输入', exact: true }).fill('5\n7\n');
  await tests.getByRole('textbox', { name: '期望输出', exact: true }).fill('12');
  await tests.getByRole('button', { name: '保存测试样例', exact: true }).click();
  await tests.getByRole('button', { name: '修改', exact: true }).waitFor();
  assert.equal(problem.test_cases[0].input, '5\n7\n');
  assert.equal(problem.test_cases[0].expected_output, '12');
  await page.reload();
  await tests.getByRole('button', { name: '修改', exact: true }).click();
  assert.equal(await tests.getByRole('textbox', { name: '输入', exact: true }).inputValue(), '5\n7\n');
  assert.equal(await tests.getByRole('textbox', { name: '期望输出', exact: true }).inputValue(), '12');
  await snapshot('stdin-saved-reopened');
  evidence.checks.push({ id: 'UI-01', result: 'pass', observation: 'stdin editor sends exact input/output and reads fixed API saved state after reload/reopen', persisted_in_real_backend: false });
  rejectNextSave = true;
  await tests.getByRole('textbox', { name: '期望输出', exact: true }).fill('13');
  await tests.getByRole('button', { name: '保存测试样例', exact: true }).click();
  await tests.getByText('保存失败，请重试。', { exact: true }).waitFor();
  assert.equal(await tests.getByRole('textbox', { name: '期望输出', exact: true }).inputValue(), '13');
  assert.equal(problem.test_cases[0].expected_output, '12');
  await snapshot('save-rejected-edit-preserved');
  evidence.checks.push({ id: 'UI-02', result: 'pass', observation: 'Synthetic 409 save rejected; editor keeps unsaved value and fixed API saved value remains unchanged' });

  setProblem(initial([{ input: '', expected_output: '', function_name: 'fib', function_args: [0], expected_return: '0', io_mode: 'function', source: 'teacher', sandbox_feasible: true }]));
  await page.reload();
  await tests.getByRole('button', { name: '修改', exact: true }).click();
  const functionInitial = { input: await tests.getByRole('textbox', { name: '输入', exact: true }).inputValue(), expected_output: await tests.getByRole('textbox', { name: '期望输出', exact: true }).inputValue() };
  await tests.getByRole('textbox', { name: '输入', exact: true }).fill('[5]');
  await tests.getByRole('textbox', { name: '期望输出', exact: true }).fill('5');
  await snapshot('function-input-edited-before-save');
  await tests.getByRole('button', { name: '保存测试样例', exact: true }).click();
  await tests.getByRole('button', { name: '修改', exact: true }).waitFor();
  assert.deepEqual(problem.test_cases[0].function_args, [0]);
  assert.equal(problem.test_cases[0].expected_return, '0');
  await page.reload();
  await tests.getByRole('button', { name: '修改', exact: true }).waitFor();
  const functionViewer = await tests.locator('pre').allTextContents();
  assert.deepEqual(functionViewer, ['[\n  0\n]', '0']);
  await snapshot('function-after-input-edit');
  evidence.checks.push({ id: 'UI-03', result: 'existing_gap_reproduced', observation: 'Function viewer uses function_args/expected_return, editor only changes input/expected_output; saved function fields remain original', function_editor_initial: functionInitial, saved_case: problem.test_cases[0], viewer_after_reload: functionViewer, real_execution: 'not tested here' });

  setProblem(initial([]));
  await page.reload();
  await tests.getByText('测试样例生成失败或尚未完成，请在风险总览查看。', { exact: true }).waitFor();
  await snapshot('missing-tests');
  evidence.checks.push({ id: 'UI-04', result: 'pass', observation: 'Empty test list is visibly incomplete; does not display successful execution evidence' });

  task.status = 'graded';
  task.student_count = 1;
  task.student_data = { S1: { stu_id: 'S1', stu_name: 'U2 synthetic student', identity_status: 'matched', stu_ans: [{ q_id: 'q1', content: 'def fib(n): return n', review_status: 'confirmed', flag: [] }] } };
  setProblem(initial([{ input: '5', expected_output: '5', source: 'teacher', sandbox_feasible: true }]));
  for (const [name, score, confidence, comment, errorKind] of [
    ['normal', 10, .95, 'U2 模拟结果：正常作答，固定响应10分。', null],
    ['partial', 6, .8, 'U2 模拟结果：边界错误，固定响应6分。', null],
    ['student-error', 0, .8, 'U2 模拟结果：学生代码运行异常。', null],
    ['execution-failure', null, 0, 'U2 模拟结果：执行平台不可用，不能据此扣分。', 'programming_execution_unavailable'],
  ]) {
    correction = { q_id: 'q1', score, provisional_score: score, max_score: 10, confidence, comment, steps: [], requires_human_review: !!errorKind, review_reasons: errorKind ? ['llm_failed'] : [], initial_review_reasons: [], review_status: 'pending', teacher_score: null, teacher_comment: '', synthesis_method: errorKind ? 'all_failed' : 'single', expert_results: [{ provider: 'u2-fixed', score: score ?? 0, max_score: 10, confidence, comment, steps: [], ...(errorKind ? { error_kind: errorKind } : {}) }] };
    await page.goto(frontend + '/tasks/u2-browser/results/questions/q1');
    await page.getByRole('heading', { name: '代码 / 测试资料', exact: true }).waitFor();
    const resultText = await page.locator('body').innerText();
    assert.ok(resultText.includes('U2 synthetic student'));
    assert.ok(resultText.includes(comment));
    await snapshot('result-' + name);
    await page.goto(frontend + '/tasks/u2-browser/review/S1/q1');
    const resultRegion = page.getByRole('region', { name: 'SmarTAI 批改结果', exact: true });
    await resultRegion.waitFor();
    assert.ok((await resultRegion.innerText()).includes(comment));
    await snapshot('review-' + name);
    evidence.checks.push({ id: 'UI-result-' + name, result: 'rendered_fixture', score_from_fixture: score, comment_from_fixture: comment, observed_body: ['result-' + name + '.txt', 'review-' + name + '.txt'], implication: 'Only display verified, not scoring correctness or actual failure attribution' });
  }
  assert.deepEqual(evidence.external_attempts, []);
  assert.deepEqual(evidence.unexpected_requests, []);
  assert.deepEqual(evidence.page_errors, []);
  evidence.console_classification = 'Expected single HTTP 409 console error from deliberately rejected save; no framework/runtime page errors';
  assert.ok(evidence.console.every(item => item.text.includes('409 (Conflict)')));
  evidence.completed = true;
} catch (error) {
  evidence.failure = String(error.stack || error);
  await snapshot('failure').catch(() => {});
  process.exitCode = 1;
} finally {
  evidence.finished_at = new Date().toISOString();
  await writeFile(join(outputDir, 'browser-observations.json'), JSON.stringify(evidence, null, 2) + '\n');
  await browser.close();
}
