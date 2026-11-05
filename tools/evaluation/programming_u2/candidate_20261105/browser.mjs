import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const { origin, task, scenario, token } = JSON.parse(readFileSync(0, 'utf8'));
assert.equal(new URL(origin).hostname, '127.0.0.1');
const { chromium } = createRequire(join(process.argv[2], 'package.json'))('@playwright/test');
const browser = await chromium.launch();
try {
  const context = await browser.newContext({ serviceWorkers: 'block' });
  const unexpected = [], writes = [], errors = [];
  const base = '/tasks/' + task;
  // Only the local build and mounted API should serve this page.
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === origin) return route.continue();
    unexpected.push(new URL(route.request().url()).origin);
    return route.abort();
  });
  await context.addInitScript(key => {
    localStorage.setItem('smartai_locale', 'zh-CN');
    localStorage.setItem('smartai_token', key);
  }, token);
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  page.on('request', r => {
    if (new URL(r.url()).pathname.startsWith('/api' + base) && !['GET', 'HEAD', 'OPTIONS'].includes(r.method()))
      writes.push([r.method(), new URL(r.url()).pathname]);
  });
  page.on('pageerror', e => errors.push(e.message));
  const button = name => page.getByRole('button', { name, exact: true });
  async function api(path) {
    return page.evaluate(async p => {
      const r = await fetch('/api' + p, { headers: { Authorization: 'Bearer ' + localStorage.getItem('smartai_token') } });
      if (!r.ok) throw new Error('API returned ' + r.status);
      return r.json();
    }, base + path);
  }

  if (scenario === 'edit') {
    await page.goto(origin + base + '/questions/q1/content');
    const panel = page.getByRole('heading', { name: '测试样例', exact: true }).locator('xpath=ancestor::section[1]');
    const edit = panel.getByRole('button', { name: '修改', exact: true });
    const input = panel.getByRole('textbox', { name: '输入', exact: true });
    const expected = panel.getByRole('textbox', { name: '期望输出', exact: true });
    await edit.click();
    assert.equal(await input.inputValue(), '[0]');
    await input.fill('[5]');
    await expected.fill('5');
    await panel.getByRole('button', { name: '保存测试样例', exact: true }).click();
    await edit.waitFor();
    await page.reload();
    await edit.click();
    assert.equal(await input.inputValue(), '[5]');
    assert.equal(await expected.inputValue(), '5');
    const saved = await api('');
    await input.fill('[9]');
    await expected.fill('34');
    await panel.getByRole('button', { name: '取消', exact: true }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: '不暂存并离开' }).click();
    await edit.waitFor();
    await page.reload();
    await edit.click();
    assert.equal(await input.inputValue(), '[5]');
    assert.equal(await expected.inputValue(), '5');
    const reopened = await api('');
    assert.equal(reopened.workflow_revision, saved.workflow_revision);
    assert.deepEqual(reopened.problem_data.q1.test_cases, saved.problem_data.q1.test_cases);
    assert.deepEqual(writes, [['PUT', '/api' + base + '/problems/q1']]);
  } else {
    await page.goto(origin + base + '/results/questions/q1');
    const correction = (await api('/result')).results[0].corrections[0];
    assert.equal(correction.score, { platform: null, tentative: 6, zero: 0 }[scenario]);
    assert.equal((await api('/finalization')).ready_for_confirmation, scenario === 'zero');
    if (scenario === 'zero') {
      await page.getByRole('heading', { name: '代码 / 测试资料', exact: true }).waitFor();
      assert.equal(new URL(page.url()).pathname, base + '/results/questions/q1');
    } else {
      if (scenario === 'platform') {
        await page.waitForURL(origin + base + '/grading/progress');
        await button('人工继续处理').click();
      }
      await page.waitForURL(origin + base + '/review');
      await button('查看结果分析').click();
      const dialog = page.getByRole('alertdialog');
      await dialog.waitFor();
      assert.match(await dialog.innerText(), /暂不能查看结果分析/);
      await dialog.getByRole('button', { name: '留在此页' }).click();
      await page.goto(origin + base + '/review/S1/q1');
      const region = page.getByRole('region', { name: 'SmarTAI 批改结果' });
      await region.waitFor();
      const field = page.getByRole('textbox', { name: 'Q1 最终得分', exact: true });
      assert.equal(await field.inputValue(), scenario === 'platform' ? '' : '6');
      const before = writes.length;
      if (scenario === 'platform') {
        assert.match(await region.innerText(), /无有效分数/);
        await button('全部确认').click();
        await page.getByText('SmarTAI 没有生成有效分数，请先手动输入最终分。', { exact: true }).first().waitFor();
        assert.equal((await api('/finalization')).ready_for_confirmation, false);
        assert.equal(writes.length, before);
      } else {
        assert.ok(correction.review_reasons.includes('programming_ai_only'));
        const saved = page.waitForResponse(r => new URL(r.url()).pathname === '/api' + base + '/reviews/S1/q1'
          && r.request().method() === 'PUT');
        await button('全部确认').click();
        assert.equal((await saved).status(), 200);
        assert.equal((await api('/finalization')).ready_for_confirmation, true);
        assert.equal(writes.length, before + 1);
      }
    }
  }
  assert.deepEqual(unexpected, []);
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
}
