import {chromium,firefox,webkit,expect} from '@playwright/test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
const qaApp = process.env.DRAFT_QA_APP_URL || 'http://127.0.0.1:53971';
const qaApi = process.env.DRAFT_QA_API_URL || 'http://127.0.0.1:8037';
for (const url of [qaApp, qaApi]) if (!['127.0.0.1', 'localhost', '[::1]'].includes(new URL(url).hostname)) throw new Error('Draft QA requires isolated loopback services.');
const qaOut = process.env.DRAFT_QA_OUTPUT || path.join(os.tmpdir(), 'smartai-explicit-draft-evidence');
await fs.mkdir(qaOut, { recursive: true });
const out=qaOut,results=[];
for(const [name,engine] of Object.entries({chromium,firefox,webkit})){
 const b=await engine.launch({headless:true}),ctx=await b.newContext({viewport:{width:1440,height:1000}}),p=await ctx.newPage();let writes=0; const checks=[];
 await ctx.addInitScript(()=>{localStorage.setItem('smartai_token','demo-teacher-dialogs');localStorage.setItem('smartai_locale','zh-CN')});
 const group={group_id:'qa-group',name:'测试分组',course_id:null,material_count:1,created_at:1,updated_at:1,course_name:null,course_code:null};
 const material={material_id:'qa-material',filename:'sample.txt',course_id:null,group_id:null,category:'textbook',labels:[],content_type:'text/plain',size_bytes:20,sha256:'qa',created_at:1,updated_at:1,last_used_at:null,task_reference_count:0,parse_status:'ready',group_name:null,course_name:null,course_code:null,match_kind:null,match_score:null,match_reason:null};
 const task={task_id:'qa-history',name:'草稿标签测试',owner_id:'qa',status:'created',workflow_revision:1,course_id:null,created_at:1,updated_at:1,tag_ids:[],tags:[],problem_count:0,student_count:0};
 await ctx.route(`${qaApi}/**`,r=>{const req=r.request(),u=new URL(req.url());if(req.method()!=='GET'){writes++;return r.fulfill({status:503,json:{detail:'QA failed formal save'}})}let data=[];
 if(u.pathname==='/auth/me')data={id:'qa-dialog-teacher',username:'qa-dialog-teacher',role:'teacher',is_read_only:false};
 else if(u.pathname==='/course-materials/groups')data={items:[group],total:1};
 else if(u.pathname==='/course-materials/')data={items:[material],total:1,page:1,page_size:100,summary:{materials:1,groups:1,referenced:0,parsed:1},storage:'local',capabilities:{durable:true,ocr:false,accepted_types:['txt']}};
 else if(u.pathname.includes('storage/usage'))data={limit_bytes:128*1024*1024,used_bytes:20,available_bytes:128*1024*1024-20,unlimited:false};
 else if(u.pathname === '/tasks/')data={items:[task],total:1,page:1,page_size:40,has_next:false};
 return r.fulfill({json:data});});
 try{
 await p.goto(qaApp + '/knowledge-base');await p.getByRole('button',{name:'新建分组',exact:true}).click();await p.getByLabel('分组名称',{exact:true}).fill('新分组暂存');await p.getByRole('dialog').getByRole('button',{name:'关闭',exact:true}).click();await p.getByRole('alertdialog').waitFor();await p.keyboard.press('Escape');await expect(p.getByLabel('分组名称',{exact:true})).toHaveValue('新分组暂存');
 await p.getByRole('dialog').getByRole('button',{name:'关闭',exact:true}).click();await p.getByRole('button',{name:'暂存并离开',exact:true}).click();await expect(p.getByRole('dialog')).toHaveCount(0);await p.getByRole('button',{name:'新建分组',exact:true}).click();await expect(p.getByLabel('分组名称',{exact:true})).toHaveValue('新分组暂存');await p.getByRole('dialog').getByRole('button',{name:'关闭',exact:true}).click();checks.push('group X close three-option save/stay/Escape and repeat restore');
 await p.getByRole('button',{name:'上传资料',exact:true}).click();await p.locator('input[type=file]').setInputFiles({name:'local-kb.txt',mimeType:'text/plain',buffer:Buffer.from('real knowledge file bytes')});await p.getByRole('dialog').getByRole('button',{name:'暂存',exact:true}).click();await expect(p.getByRole('dialog').getByText(/已暂存 ·/)).toBeVisible();await p.reload();await p.getByRole('button',{name:'上传资料',exact:true}).click();await expect(p.getByText('local-kb.txt',{exact:true})).toBeVisible();await p.setViewportSize({width:390,height:844});expect(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);await p.screenshot({path:out+'/'+name+'-library-dialog-mobile.png',fullPage:true});await p.getByRole('dialog').getByRole('button',{name:'关闭',exact:true}).click();checks.push('library upload actual file reloads and mobile dialog footer is accessible');await p.setViewportSize({width:1440,height:1000});
 await p.getByRole('button',{name:'编辑资料 sample.txt',exact:true}).click();await p.getByLabel('文件名称',{exact:true}).fill('edited-name.txt');await p.getByRole('dialog').getByRole('button',{name:'暂存',exact:true}).click();await expect(p.getByRole('dialog').getByText(/已暂存 ·/)).toBeVisible();await p.reload();await p.getByRole('button',{name:'编辑资料 sample.txt',exact:true}).click();await expect(p.getByLabel('文件名称',{exact:true})).toHaveValue('edited-name.txt');await p.keyboard.press('Escape');checks.push('material metadata explicit draft refresh and Escape close');
 await p.goto(qaApp + '/history');await p.getByRole('button',{name:'管理标签',exact:true}).click();await p.getByPlaceholder('搜索或新建标签').fill('草稿新标签');await p.getByRole('link',{name:'新建任务',exact:true}).click();await p.getByRole('alertdialog').waitFor();await p.keyboard.press('Escape');await expect(p.getByPlaceholder('搜索或新建标签')).toHaveValue('草稿新标签');await p.getByRole('link',{name:'新建任务',exact:true}).click();await p.getByRole('button',{name:'暂存并离开',exact:true}).click();await p.waitForURL('**/tasks/new');await p.goBack();await expect(p.getByPlaceholder('搜索或新建标签')).toHaveValue('草稿新标签');checks.push('history popover sidebar resumes original target once and back restores');expect(writes).toBe(0);
 }catch(e){console.log(name,'FAILED',String(e));console.log((await p.locator('body').innerText()).slice(-1500));await p.screenshot({path:out+'/'+name+'-dialogs-failure.png',fullPage:true});results.push({name,checks,failure:String(e),writes});await b.close();continue;}
 results.push({name,checks,writes});await b.close();console.log(name,'passed');
}
await fs.writeFile(out+'/dialog-browser-results.json',JSON.stringify(results,null,2));if(results.some(x=>x.failure))process.exitCode=1;
