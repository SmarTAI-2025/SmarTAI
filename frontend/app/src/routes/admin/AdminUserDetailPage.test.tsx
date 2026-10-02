import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AdminUserDetailPage } from "./AdminUserDetailPage";
const mocks = vi.hoisted(() => ({ get: vi.fn(), request: vi.fn() }));
vi.mock("@/api/client", () => ({ apiClient: mocks, normalizeAPIError: (e: { status?: number }) => ({ status: e.status ?? 0 }) }));
vi.mock("@/api/hooks", () => ({ useCurrentUser: () => ({ data: { id: "manager" } }) }));
const account = { id: "teacher", username: "alice", email: "alice@example.edu", role: "teacher", is_active: true, is_read_only: false };
function mount() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><MemoryRouter initialEntries={["/admin/users/teacher"]}><Routes><Route path="/admin/users/:userId" element={<AdminUserDetailPage />} /><Route path="/admin/closures/:id" element={<p>销户进度页</p>} /></Routes></MemoryRouter></QueryClientProvider>);
}
beforeEach(() => { vi.resetAllMocks(); mocks.get.mockResolvedValue({ data: account }); });
describe("administrator account actions", () => {
  it("cancel does not mutate and destructive confirmation requires exact username", async () => {
    mount(); const user = userEvent.setup(); await user.click(await screen.findByRole("button", { name: "正常销户" }));
    await user.type(screen.getByLabelText(/操作原因/), "test closure");
    expect(screen.getByRole("button", { name: "确认操作" })).toBeDisabled();
    await user.type(screen.getByLabelText(/输入用户名/), "wrong");
    expect(screen.getByRole("button", { name: "确认操作" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(mocks.request).not.toHaveBeenCalled();
  });
  it("failed requests retry with the same operation ID and no duplicate clicks", async () => {
    mount(); const user = userEvent.setup(); await user.click(await screen.findByRole("button", { name: "停用业务（只读）" }));
    await user.type(screen.getByLabelText(/操作原因/), "review access");
    mocks.request.mockRejectedValueOnce({ status: 503 });
    await user.click(screen.getByRole("button", { name: "确认操作" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("操作结果尚未确认");
    const first = mocks.request.mock.calls[0][0];
    let finish!: (v: unknown) => void;
    mocks.request.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const confirm = screen.getByRole("button", { name: "确认操作" });
    fireEvent.click(confirm); fireEvent.click(confirm);
    expect(mocks.request).toHaveBeenCalledTimes(2);
    expect(mocks.request.mock.calls[1][0].headers).toEqual(first.headers);
    expect(screen.getByRole("button", { name: "正在处理…" })).toBeDisabled();
    finish({ data: {} }); await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
  it("blacklist closure has explicit identity effect and navigates to durable progress", async () => {
    mocks.request.mockResolvedValue({ data: { closure_id: "record-1" } });
    mount(); const user = userEvent.setup(); await user.click(await screen.findByRole("button", { name: "拉黑销户" }));
    expect(screen.getByText(/用户名释放，原邮箱仍禁止注册/)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/输入用户名/), "alice");
    await user.type(screen.getByLabelText(/操作原因/), "abuse confirmed");
    await user.click(screen.getByRole("button", { name: "确认操作" }));
    expect(await screen.findByText("销户进度页")).toBeInTheDocument();
    expect(mocks.request.mock.calls[0][0].data).toMatchObject({ mode: "blacklist", confirm_username: "alice" });
  });
  it("pending closure cannot be restored or closed again", async () => {
    mocks.get.mockResolvedValue({ data: { ...account, closure: { id: "pending-1", status: "pending", mode: "normal" } } });
    mount(); expect(await screen.findByRole("link", { name: /查看销户进度/ })).toHaveAttribute("href", "/admin/closures/pending-1");
    expect(screen.getByRole("button", { name: "恢复正常使用" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "正常销户" })).toBeDisabled();
  });
});
