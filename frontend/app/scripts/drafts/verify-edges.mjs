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
const origin=qaApp,out=qaOut,results=[];
for(const [name,engine] of Object.entries({chromium,firefox,webkit})){
 const browser=await engine.launch({headless:true,...(name==='firefox'?{firefoxUserPrefs:{'dom.disable_beforeunload':false}}:{})}),context=await browser.newContext({viewport:{width:1440,height:1000}});
 await context.addInitScript(()=>{if(!localStorage.getItem('smartai_token'))localStorage.setItem('smartai_token','demo-teacher-edge-A');localStorage.setItem('smartai_locale','zh-CN')});const page=await context.newPage();const checks=[];
 try{
 await page.goto(origin+'/tasks/new');const input=page.getByLabel('任务名称',{exact:true});await input.fill('A explicit');await page.getByRole('button',{name:'暂存',exact:true}).click();await expect(page.getByText(/已暂存 ·/)).toBeVisible();
 const tab=await context.newPage();await tab.goto(origin+'/tasks/new');await expect(tab.getByLabel('任务名称',{exact:true})).toHaveValue('A explicit');
 await input.fill('A newer');await page.getByRole('button',{name:'暂存',exact:true}).click();await expect(page.getByText(/已暂存 ·/)).toBeVisible();await tab.getByLabel('任务名称',{exact:true}).fill('B competing');await tab.getByRole('button',{name:'暂存',exact:true}).click();await expect(tab.getByText(/其他标签页已暂存或删除/)).toBeVisible();await page.reload();await expect(input).toHaveValue('A newer');checks.push('multi-tab CAS keeps the newest explicit version and competing input');
 let closePrompt=false;tab.once('dialog',async d=>{closePrompt=d.type()==='beforeunload';await d.dismiss()});await tab.close({runBeforeUnload:true});await page.waitForTimeout(500);expect(closePrompt).toBe(true);expect(tab.isClosed()).toBe(false);await expect(tab.getByLabel('任务名称',{exact:true})).toHaveValue('B competing');checks.push('native tab close can be cancelled without losing input');await tab.close();
 await input.fill('quota input');await page.evaluate(()=>{globalThis.__draftPut=IDBObjectStore.prototype.put;IDBObjectStore.prototype.put=function(...args){if(this.name==='drafts')throw new DOMException('QA full','QuotaExceededError');return globalThis.__draftPut.apply(this,args)}});
 await page.getByRole('link',{name:'管理模型与 BYOK',exact:true}).click();await page.getByRole('button',{name:'暂存并离开',exact:true}).click();await expect(page.getByText(/浏览器存储空间不足/).first()).toBeVisible();await expect(input).toHaveValue('quota input');await expect(page).toHaveURL(/\/tasks\/new$/);await page.screenshot({path:`${out}/${name}-quota-dialog.png`,fullPage:true});await page.keyboard.press('Escape');await expect(page.getByRole('alertdialog')).toHaveCount(0);checks.push('quota failure preserves input, old snapshot and original navigation intent; Escape cancels');
 await page.evaluate(()=>{IDBObjectStore.prototype.put=globalThis.__draftPut});await page.getByRole('button',{name:'暂存',exact:true}).click();await expect(page.getByText(/已暂存 ·/)).toBeVisible();
 await input.fill('offsite dirty');let offsite=false;page.once('dialog',async d=>{offsite=d.type()==='beforeunload';await d.dismiss()});await page.evaluate(url=>setTimeout(()=>location.assign(url),0), qaApi + '/health');await page.waitForTimeout(500);expect(offsite).toBe(true);await expect(input).toHaveValue('offsite dirty');checks.push('native cross-origin leave can be cancelled');
 await page.getByRole('button',{name:'暂存',exact:true}).click();await expect(page.getByText(/已暂存 ·/)).toBeVisible();let cleanClose=false;page.once('dialog',async d=>{cleanClose=true;await d.dismiss()});await page.close({runBeforeUnload:true});await context.newPage();await expect.poll(()=>page.isClosed(),{timeout:3000}).toBe(true);expect(cleanClose).toBe(false);checks.push('clean tab close has no native warning');
 }catch(e){results.push({engine:name,checks,failure:String(e)});console.log(name,'FAILED',String(e));await page.screenshot({path:`${out}/${name}-edges-failure.png`,fullPage:true}).catch(()=>{});await browser.close();continue;}
 results.push({engine:name,checks});await browser.close();
}
await fs.writeFile(`${out}/explicit-edge-results.json`,JSON.stringify(results,null,2));if(results.some(x=>x.failure))process.exitCode=1;
