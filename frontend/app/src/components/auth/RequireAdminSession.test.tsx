import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RequireAdminSession } from "./RequireAdminSession";
vi.mock("@/api/hooks", () => ({ useCurrentUser: vi.fn(), useLogout: () => ({ isPending: false, mutateAsync: vi.fn() }) }));
vi.mock("@/api/client", () => ({ clearAuthToken: vi.fn(), normalizeAPIError: (error: { status: number }) => error }));
vi.mock("@/lib/sessionExpiry", () => ({ useSessionExpired: () => false }));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN", t: (k: string) => k, setLocale: vi.fn() }) }));
const { useCurrentUser } = await import("@/api/hooks");
const { clearAuthToken } = await import("@/api/client");
function Destination() { const location = useLocation(); return <p>登录返回：{location.state?.from}</p>; }
function setup(state: Record<string, unknown>) {
  vi.mocked(useCurrentUser).mockReturnValue({ isLoading: false, isError: false, ...state } as ReturnType<typeof useCurrentUser>);
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  cache.setQueryData(["retained"], "keep");
  render(<QueryClientProvider client={cache}><MemoryRouter initialEntries={["/admin/users?search=a#row"]}><Routes><Route path="/admin/users" element={<RequireAdminSession><p>管理内容</p></RequireAdminSession>} /><Route path="/login" element={<Destination />} /></Routes></MemoryRouter></QueryClientProvider>);
  return cache;
}
describe("administrator session guard", () => {
  beforeEach(() => vi.clearAllMocks());
  it.each([0, 500, 503])("retains login/cache and offers retry for status %s", async status => {
    const refetch = vi.fn(); const cache = setup({ isError: true, error: { status }, refetch });
    await userEvent.click(screen.getByRole("button", { name: /重试/ }));
    expect(refetch).toHaveBeenCalledOnce(); expect(clearAuthToken).not.toHaveBeenCalled();
    expect(cache.getQueryData(["retained"])).toBe("keep");
  });
  it("returns expired sessions to their original private page", async () => {
    setup({ isError: true, error: { status: 401 } });
    expect(await screen.findByText("登录返回：/admin/users?search=a#row")).toBeInTheDocument();
    expect(clearAuthToken).toHaveBeenCalled();
  });
  it("shows permission refusal without presenting it as an expired session", () => {
    setup({ data: { role: "teacher" } }); expect(screen.getByText("没有管理权限")).toBeInTheDocument(); expect(clearAuthToken).not.toHaveBeenCalled();
  });
  it("renders admin content only for a writable administrator", () => {
    setup({ data: { role: "admin", is_read_only: false } }); expect(screen.getByText("管理内容")).toBeInTheDocument();
  });
});
