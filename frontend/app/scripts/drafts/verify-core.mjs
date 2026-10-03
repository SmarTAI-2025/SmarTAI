import { chromium, firefox, webkit } from '@playwright/test';
import { expect } from '@playwright/test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
const qaApp = process.env.DRAFT_QA_APP_URL || 'http://127.0.0.1:53971';
const qaApi = process.env.DRAFT_QA_API_URL || 'http://127.0.0.1:8037';
for (const url of [qaApp, qaApi]) if (!['127.0.0.1', 'localhost', '[::1]'].includes(new URL(url).hostname)) throw new Error('Draft QA requires isolated loopback services.');
const qaOut = process.env.DRAFT_QA_OUTPUT || path.join(os.tmpdir(), 'smartai-explicit-draft-evidence');
await fs.mkdir(qaOut, { recursive: true });
const origin=qaApp;const out=qaOut;const results=[];
for(const [name,engine] of Object.entries({chromium,firefox,webkit})){
 const browser=await engine.launch({headless:true, ...(name === "firefox" ? { firefoxUserPrefs: { "dom.disable_beforeunload": false } } : {})});const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage();const errors=[];const calls=[];const checks=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method())&&!/auth/.test(r.url()))calls.push(r.url());});
 await page.addInitScript((token)=>{if(!localStorage.getItem('smartai_token')){localStorage.setItem('smartai_token',token);localStorage.setItem('smartai_locale','zh-CN');}},`demo-teacher-explicit-${name}`);
 try{
 await page.goto(origin+'/tasks/new');const input=page.getByLabel('任务名称',{exact:true});await input.fill('显式暂存 '+name);
 await page.getByRole('link',{name:'管理模型与 BYOK',exact:true}).click();await page.getByRole('alertdialog').waitFor();
 await page.getByRole('button',{name:'继续编辑',exact:true}).click();await expect(input).toHaveValue('显式暂存 '+name);checks.push('stay retains current input');
 await page.getByRole('button',{name:'暂存',exact:true}).click();await expect(page.getByText(/已暂存 ·/)).toBeVisible();await input.fill('尚未暂存的修改');
 await page.getByRole('link',{name:'管理模型与 BYOK',exact:true}).click();await page.getByRole('button',{name:'不暂存并离开',exact:true}).click();await page.waitForURL('**/settings/byok**');
 await page.goBack();await expect(input).toHaveValue('显式暂存 '+name);checks.push('discard retains previous explicit snapshot');
 await input.fill('保存并继续 '+name);await page.getByRole('link',{name:'管理模型与 BYOK',exact:true}).click();await page.getByRole('button',{name:'暂存并离开',exact:true}).click();await page.waitForURL('**/settings/byok**');await page.goBack();await expect(input).toHaveValue('保存并继续 '+name);checks.push('save-and-leave resumes original intent once');
 await page.reload();await expect(input).toHaveValue('保存并继续 '+name);checks.push('clean reload has no native prompt and restores');
 await page.getByRole('button',{name:'创建并添加题目',exact:true}).click();await page.waitForURL('**/upload/problems');
 await page.getByLabel('选择文件',{exact:true}).setInputFiles({name:'real-local.txt',mimeType:'text/plain',buffer:Buffer.from('real bytes survive refresh '+name)});
 await page.getByRole('button',{name:'暂存',exact:true}).click();await expect(page.getByText(/已暂存 ·/)).toBeVisible();const before=calls.length;
 await page.reload();await expect(page.getByText('real-local.txt',{exact:true}).first()).toBeVisible();
 const bytes=await page.evaluate(async()=>{const db=await new Promise(resolve=>{const r=indexedDB.open('smartai-explicit-drafts',1);r.onsuccess=()=>resolve(r.result)});const rows=await new Promise(resolve=>{const r=db.transaction('drafts').objectStore('drafts').getAll();r.onsuccess=()=>resolve(r.result)});return Promise.all(rows.flatMap(r=>r.files.map(f=>new TextDecoder().decode(f.bytes))))});
 expect(bytes).toContain('real bytes survive refresh '+name);expect(calls.length).toBe(before);checks.push('real File bytes refresh with zero upload/recognition calls');
 await page.getByRole('button',{name:'从原文提取',exact:true}).click();const hint=page.getByLabel('补充说明（选填）');await hint.fill('原生离开保护测试');
 let native=false;page.once('dialog',async d=>{native=d.type()==='beforeunload';await d.dismiss();});await page.evaluate(()=>{setTimeout(()=>location.reload(),0)}); await page.waitForTimeout(800);await expect(hint).toHaveValue('原生离开保护测试');checks.push('native refresh stay preserves input: '+native);
 if(!native)throw new Error('Native beforeunload was not observed');
 await page.getByRole('button',{name:'暂存',exact:true}).click();await expect(page.getByText(/已暂存 ·/)).toBeVisible();
 await page.setViewportSize({width:390,height:844});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);await page.screenshot({path:`${out}/explicit-${name}-mobile.png`,fullPage:true});checks.push('mobile width and visible action feedback');
 results.push({name,checks,errors,calls});console.log(name,checks);
 }catch(e){await page.screenshot({path:`${out}/explicit-${name}-failure.png`,fullPage:true});results.push({name,checks,errors,failure:String(e)});console.log(name,'FAILED',e.message);}
 await browser.close();
}
await fs.writeFile(`${out}/explicit-browser-results.json`,JSON.stringify(results,null,2));if(results.some(r=>r.failure||r.errors.length))process.exitCode=1;
