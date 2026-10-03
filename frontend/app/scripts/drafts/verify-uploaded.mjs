import {chromium} from '@playwright/test';
import {expect} from '@playwright/test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
const qaApp = process.env.DRAFT_QA_APP_URL || 'http://127.0.0.1:53971';
const qaApi = process.env.DRAFT_QA_API_URL || 'http://127.0.0.1:8037';
for (const url of [qaApp, qaApi]) if (!['127.0.0.1', 'localhost', '[::1]'].includes(new URL(url).hostname)) throw new Error('Draft QA requires isolated loopback services.');
const qaOut = process.env.DRAFT_QA_OUTPUT || path.join(os.tmpdir(), 'smartai-explicit-draft-evidence');
await fs.mkdir(qaOut, { recursive: true });
const b=await chromium.launch({headless:true}),ctx=await b.newContext({viewport:{width:1440,height:1000}}),p=await ctx.newPage(),out=qaOut;let uploads=0,starts=0,referenceChecks=0;
await ctx.addInitScript(()=>{localStorage.setItem('smartai_token','demo-teacher-uploaded-explicit');localStorage.setItem('smartai_locale','zh-CN')});
p.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/question-preparation/sources/preflight'))uploads++;if(r.url().includes('/draft-reference'))referenceChecks++});
await ctx.route('**/question-preparation/jobs',r=>{starts++;return r.fulfill({status:503,json:{detail:{message:'QA intentionally prevents model job',code:'provider_unreachable'}}})});
try{
await p.goto(qaApp + '/tasks/new');await p.getByLabel('任务名称',{exact:true}).fill('已上传引用真实本地验收');await p.getByRole('link',{name:'管理模型与 BYOK',exact:true}).click();await p.getByRole('button',{name:'暂存并离开',exact:true}).click();await p.waitForURL(/\/settings\/byok(?:\?|$)/);
if(await p.getByText('qa-offline-explicit',{exact:true}).count()===0){await p.getByRole('button',{name:'添加模型配置',exact:true}).click();await p.getByLabel('模型名称',{exact:true}).fill('qa-offline-explicit');await p.getByLabel(/^API key/).fill('sk-QA-placeholder-not-a-real-key');await p.getByRole('button',{name:'保存配置',exact:true}).click();await expect(p.getByRole('dialog')).toHaveCount(0)}
await p.goBack();await expect(p.getByLabel('任务名称',{exact:true})).toHaveValue('已上传引用真实本地验收');await p.getByRole('button',{name:'创建并添加题目',exact:true}).click();await p.waitForURL('**/upload/problems');
await p.getByLabel('选择文件',{exact:true}).setInputFiles({name:'qa-uploaded.txt',mimeType:'text/plain',buffer:Buffer.from('1. Compute 2+3.\n2. Compute 3+4.')});await expect(p.getByLabel('题目识别模型',{exact:true})).not.toHaveValue('');await p.getByRole('button',{name:'识别并准备题目资料',exact:true}).click();await expect.poll(()=>starts,{timeout:15000}).toBe(1);
expect(uploads).toBe(1);expect(starts).toBe(1);await p.getByRole('button',{name:'暂存',exact:true}).click();await expect(p.getByText(/已暂存 ·/)).toBeVisible();await p.reload();await expect(p.getByText('qa-uploaded.txt',{exact:true}).first()).toBeVisible();await p.getByRole('button',{name:'识别并准备题目资料',exact:true}).click();await expect.poll(()=>starts).toBe(2);expect(uploads).toBe(1);expect(referenceChecks).toBeGreaterThan(0);
await p.getByRole('button',{name:'暂存',exact:true}).click();await expect(p.getByText(/已暂存 ·/)).toBeVisible();await ctx.route('**/problem-sources/draft-reference?**',r=>r.fulfill({status:404,json:{detail:{code:'draft_source_not_found',message:'QA reference expired'}}}));await p.reload();await expect(p.getByText(/已失效|已删除|找不到|已过期|删除或不可用/).first()).toBeVisible();expect(uploads).toBe(1);await p.screenshot({path:out+'/uploaded-expired-reference.png',fullPage:true});
await p.locator('input[type=file]').first().setInputFiles({name:'replacement.txt',mimeType:'text/plain',buffer:Buffer.from('1. Replacement question')});await expect(p.getByText('replacement.txt',{exact:true}).first()).toBeVisible();
await fs.writeFile(out+'/uploaded-browser-results.json',JSON.stringify({uploads,starts,referenceChecks,checks:['BYOK configured through UI with a fake key and no external provider','actual server upload reference survives explicit save and refresh','manual retry reuses successful upload/preflight','expired reference explains reselect; restore itself never uploads or starts a model job']},null,2));console.log('passed',{uploads,starts,referenceChecks});
}catch(e){console.log('FAILED',String(e));console.log((await p.locator('body').innerText()).slice(-1800));await p.screenshot({path:out+'/uploaded-failure.png',fullPage:true});process.exitCode=1}await b.close();
