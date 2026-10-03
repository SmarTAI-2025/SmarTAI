import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { AdminMaintenancePage } from "./AdminMaintenancePage";
vi.mock("@/api/client",()=>({backendUrl:"/api",getAuthToken:()=>"synthetic-admin-token"}));
const fingerprint="a".repeat(64)+"-"+"b".repeat(32);
const phrase="ERASE ALL DISPOSABLE BUSINESS DATA "+fingerprint.slice(0,12);
const preview={available:true,execution_available:true,environment:"production",fingerprint,confirmation:phrase,target:{database:"sqlite:masked",local_roots:["/synthetic/project-files"],object_scope:null},tables:{users:3},storage:{files:2,versions_and_markers:0,multipart_uploads:0,bytes:50}};
let fetcher: ReturnType<typeof vi.fn>;
beforeEach(()=>{fetcher=vi.fn(async(url:string)=>({ok:true,json:async()=>url.endsWith("preview")?preview:{}}));vi.stubGlobal("fetch",fetcher);});
afterEach(()=>vi.unstubAllGlobals());
function mount(standalone=false){render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false,gcTime:0}}})}><MemoryRouter><AdminMaintenancePage standalone={standalone}/></MemoryRouter></QueryClientProvider>);}
async function open(){const user=userEvent.setup();await user.click(await screen.findByRole("button",{name:"全清空并恢复全新状态"}));return user;}
it("shows exact scope and explains why ordinary service cannot execute",async()=>{
 fetcher.mockResolvedValue({ok:true,json:async()=>({...preview,execution_available:false})});mount();
 expect(await screen.findByRole("button",{name:"全清空并恢复全新状态"})).toBeDisabled();
 expect(screen.getByText(/按钮不可用的原因/)).toBeInTheDocument();
 expect(screen.getByText(/sqlite:masked/)).toBeInTheDocument();
});
it("requires password, exact confirmation and stopped-service acknowledgement; cancel clears secrets",async()=>{
 mount();const user=await open();
 expect(screen.getByRole("button",{name:"确认全清空"})).toBeDisabled();
 await user.type(screen.getByLabelText("独立维护密码"),"synthetic-secret");
 await user.type(screen.getByRole("textbox",{name:/输入确认短语/}),phrase);
 await user.click(screen.getByRole("checkbox"));
 expect(screen.getByRole("button",{name:"确认全清空"})).toBeEnabled();
 await user.click(screen.getByRole("button",{name:"取消"}));await open();
 expect(screen.getByLabelText("独立维护密码")).toHaveValue("");
 expect(fetcher.mock.calls.filter(([url])=>url.endsWith("execute"))).toHaveLength(0);
});
it("keeps the same plan and permits retry after network failure without storing the password",async()=>{
 fetcher.mockImplementation(async(url:string)=>{if(url.endsWith("execute"))throw new TypeError("network");return{ok:true,json:async()=>preview};});mount();const user=await open();
 await user.type(screen.getByLabelText("独立维护密码"),"synthetic-secret");await user.type(screen.getByRole("textbox",{name:/输入确认短语/}),phrase);await user.click(screen.getByRole("checkbox"));
 await user.click(screen.getByRole("button",{name:"确认全清空"}));
 expect(await screen.findByRole("alert")).toHaveTextContent("再次提交会复用同一维护记录");
 await user.click(screen.getByRole("button",{name:"确认全清空"}));
 const calls=fetcher.mock.calls.filter(([url])=>url.endsWith("execute"));expect(calls).toHaveLength(2);
 expect(JSON.parse(calls[0][1].body).fingerprint).toBe(JSON.parse(calls[1][1].body).fingerprint);
});
it("can recover completed status without a surviving user account",async()=>{
 fetcher.mockImplementation(async(url:string)=>({ok:!url.endsWith("preview"),json:async()=>url.endsWith("status")?{fingerprint,status:"completed",rows_deleted:20}:{detail:{code:"HTTP_401"}}}));mount(true);
 await waitFor(()=>expect(screen.getByText(/清理完成：数据库及项目存储残留检查已通过/)).toBeInTheDocument());
 expect(screen.getByText(/scripts\/create_admin.py/)).toBeInTheDocument();
});
