import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, Link, RouterProvider } from "react-router-dom";
import { adminListUsers } from "@/api/admin";
import { APIError } from "@/api/client";
import { getBusinessConfig, saveBusinessConfig, type BusinessConfiguration, type BusinessConfigKey } from "@/api/adminBusinessConfig";
import { AdminBusinessConfigPage } from "./AdminBusinessConfigPage";

vi.mock("@/api/admin", () => ({ adminListUsers: vi.fn() }));
vi.mock("@/api/adminBusinessConfig", () => ({ getBusinessConfig: vi.fn(), saveBusinessConfig: vi.fn() }));

const globalConfig: BusinessConfiguration = {
  scope: "global", owner_id: null, version: 0, global_version: 0,
  fields: {
    allowed_email_domains: { effective: "example.edu", override: null, source: "settings", bounds: null },
    email_verification_resend_seconds: { effective: 60, override: null, source: "default", bounds: [1, 3600] },
    email_verification_hourly_email_limit: { effective: 5, override: null, source: "default", bounds: [1, 100] },
    email_verification_hourly_ip_limit: { effective: 20, override: null, source: "default", bounds: [1, 1000] },
    unfinished_source_quota_bytes: { effective: 536870912, override: null, source: "default", bounds: [0, 1099511627776] },
    knowledge_storage_quota_bytes: { effective: 536870912, override: null, source: "default", bounds: [0, 1099511627776] },
  },
  read_only: { email_verification_expiry_seconds: 1800, password_recovery_independent_of_registration: false, shared_pool_daily_request_limit: 5, shared_pool_daily_estimated_token_limit: 100, history_query_llm_daily_limit: 20, model_quota_note: "模型额度按进程计数，只读。" },
};
const userConfig: BusinessConfiguration = {
  scope: "user", owner_id: "teacher", version: 2, global_version: 0,
  fields: {
    unfinished_source_quota_bytes: { effective: 10, override: 10, source: "user_override", bounds: [0, 1099511627776] },
    knowledge_storage_quota_bytes: { effective: 536870912, override: null, source: "default", bounds: [0, 1099511627776] },
  },
  usage: { unfinished_source_quota_bytes: { used_bytes: 20, limit_bytes: 10, available_bytes: 0, reserved_bytes: 5 } },
};
function updated(config: BusinessConfiguration, key: BusinessConfigKey, value: number | string | null) {
  const copy = structuredClone(config); copy.version++;
  copy.fields[key] = { ...copy.fields[key]!, effective: value ?? 536870912, override: value, source: value === null ? "default" : copy.scope === "user" ? "user_override" : "global_override" };
  return copy;
}
function mount(path = "/admin/business-config") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const router = createMemoryRouter([{ path: "/admin/business-config", element: <><Link to="/elsewhere">其他页面</Link><AdminBusinessConfigPage /></> }, { path: "/elsewhere", element: <p>已离开</p> }], { initialEntries: [path] });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return router;
}
async function edit(label: string, value: string, form = screen.getByRole("form", { name: "全局配置表单" })) {
  const user = userEvent.setup();
  const input = within(form).getByLabelText(label) as HTMLInputElement;
  if (input.disabled) await user.click(within(input.closest("label")!.parentElement!).getByRole("button", { name: /设置.*覆盖/ }));
  await user.clear(input);
  if (value) await user.type(input, value);
  return user;
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getBusinessConfig).mockImplementation(async owner => structuredClone(owner ? userConfig : globalConfig));
  vi.mocked(adminListUsers).mockResolvedValue({ items: [{ id: "teacher", username: "teacher", email: "teacher@example.edu", role: "teacher", is_active: true, created_at: 0 }], page: 1, page_size: 25, total: 1, has_next: false });
});

it("shows grouped effective values, sources and explicit empty/zero semantics", async () => {
  mount();
  expect(await screen.findByRole("heading", { name: "注册规则" })).toBeInTheDocument();
  expect(screen.getByText("邮件发送限制")).toBeInTheDocument();
  expect(screen.getByText("用户存储配额")).toBeInTheDocument();
  expect(screen.getByText(/来源：部署设置/)).toBeInTheDocument();
  expect(screen.getByText(/空字符串明确表示全部拒绝/)).toBeInTheDocument();
  expect(screen.getByText(/0 表示禁止新增占用/)).toBeInTheDocument();
  expect(screen.getByText(/@badexample.edu/)).toBeInTheDocument();
  expect(screen.getByText("模型额度（只读）")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存全局配置" })).toBeDisabled();
});

it("requires a reason and saves explicit deny-all with version and idempotency", async () => {
  vi.mocked(saveBusinessConfig).mockResolvedValue(updated(globalConfig, "allowed_email_domains", ""));
  mount(); await screen.findByRole("heading", { name: "注册规则" });
  const user = await edit("允许注册的邮箱域名", "");
  expect(screen.getByText("有未保存的修改。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存全局配置" })).toBeDisabled();
  await user.type(screen.getByLabelText("修改原因"), "暂停注册");
  await user.click(screen.getByRole("button", { name: "保存全局配置" }));
  expect(await screen.findByText("配置已保存，后续请求使用新设置。")).toBeInTheDocument();
  expect(saveBusinessConfig).toHaveBeenCalledWith({ expected_version: 0, changes: { allowed_email_domains: "" }, reason: "暂停注册" }, expect.any(String), undefined);
  expect(screen.queryByText("有未保存的修改。")).not.toBeInTheDocument();
});

it("rejects empty numeric fields instead of treating them as inheritance or zero", async () => {
  mount(); await screen.findByRole("heading", { name: "注册规则" });
  const user = await edit("重发冷却（秒）", "");
  await user.type(screen.getByLabelText("修改原因"), "调整");
  await user.click(screen.getByRole("button", { name: "保存全局配置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("须为 1–3600 之间的整数");
  expect(saveBusinessConfig).not.toHaveBeenCalled();
});

it("preserves edits after uncertain failure and reuses the same request key", async () => {
  vi.mocked(saveBusinessConfig).mockRejectedValueOnce(new Error("private database details")).mockResolvedValue(updated(globalConfig, "unfinished_source_quota_bytes", 0));
  mount(); await screen.findByRole("heading", { name: "注册规则" });
  const user = await edit("未完成任务原件配额（字节）", "0");
  await user.type(screen.getByLabelText("修改原因"), "暂停新增占用");
  await user.click(screen.getByRole("button", { name: "保存全局配置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法确认是否保存");
  expect(screen.queryByText("private database details")).not.toBeInTheDocument();
  expect(screen.getByLabelText("未完成任务原件配额（字节）")).toHaveValue("0");
  await user.click(screen.getByRole("button", { name: "保存全局配置" }));
  await screen.findByText("配置已保存，后续请求使用新设置。");
  expect(vi.mocked(saveBusinessConfig).mock.calls[0][1]).toBe(vi.mocked(saveBusinessConfig).mock.calls[1][1]);
});

it("blocks a conflicted save until the administrator explicitly loads the latest", async () => {
  vi.mocked(saveBusinessConfig).mockRejectedValue(new APIError(409, "conflict"));
  mount(); await screen.findByRole("heading", { name: "注册规则" });
  const user = await edit("重发冷却（秒）", "120");
  await user.type(screen.getByLabelText("修改原因"), "调整");
  await user.click(screen.getByRole("button", { name: "保存全局配置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("配置发生冲突");
  expect(screen.getByRole("button", { name: "保存全局配置" })).toBeDisabled();
  vi.mocked(getBusinessConfig).mockResolvedValue(updated(globalConfig, "email_verification_resend_seconds", 90));
  await user.click(screen.getByRole("button", { name: "放弃更改并载入最新" }));
  await waitFor(() => expect(screen.getByLabelText("重发冷却（秒）")).toHaveValue("90"));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("opens a directly selected user and restores inheritance using null", async () => {
  vi.mocked(saveBusinessConfig).mockResolvedValue(updated(userConfig, "unfinished_source_quota_bytes", null));
  mount("/admin/business-config?userId=teacher");
  const form = await screen.findByRole("form", { name: "用户配额表单" });
  expect(within(form).getByText(/已超过当前配额/)).toBeInTheDocument();
  const input = within(form).getByLabelText("未完成任务原件配额（字节）");
  const user = userEvent.setup();
  await user.click(within(input.closest("label")!.parentElement!).getByRole("button", { name: "恢复继承" }));
  expect(screen.getByLabelText("选择用户")).toBeDisabled();
  await user.type(within(form).getByLabelText("修改原因"), "恢复标准配额");
  await user.click(within(form).getByRole("button", { name: "保存用户配额" }));
  await screen.findByText("配置已保存，后续请求使用新设置。");
  expect(saveBusinessConfig).toHaveBeenCalledWith({ expected_version: 2, expected_global_version: 0, changes: { unfinished_source_quota_bytes: null }, reason: "恢复标准配额" }, expect.any(String), "teacher");
});

it("searches the existing user endpoint and shows safe load failures", async () => {
  vi.mocked(getBusinessConfig).mockRejectedValueOnce(new Error("private")).mockResolvedValue(globalConfig);
  mount(); expect(await screen.findByRole("alert")).toHaveTextContent("配置加载失败");
  await userEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByRole("heading", { name: "注册规则" });
  await userEvent.type(screen.getByLabelText("搜索用户名或邮箱"), "teacher@example.edu");
  await userEvent.click(screen.getByRole("button", { name: "搜索" }));
  await waitFor(() => expect(adminListUsers).toHaveBeenLastCalledWith({ search: "teacher@example.edu", page: 1, page_size: 25 }));
});

it("protects unsaved edits on navigation and permits explicit discard", async () => {
  mount(); await screen.findByRole("heading", { name: "注册规则" });
  const user = await edit("允许注册的邮箱域名", "new.edu");
  await user.click(screen.getByRole("link", { name: "其他页面" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("还有未保存的修改");
  await user.click(screen.getByRole("button", { name: "保留编辑" }));
  expect(screen.getByLabelText("允许注册的邮箱域名")).toHaveValue("new.edu");
  await user.click(screen.getByRole("link", { name: "其他页面" }));
  await user.click(screen.getByRole("button", { name: "放弃更改并离开" }));
  expect(await screen.findByText("已离开")).toBeInTheDocument();
});
