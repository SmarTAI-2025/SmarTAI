import { beforeEach, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { ModelQuotaCard } from "./ModelQuotaCard";
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN" }) }));
beforeEach(() => { vi.restoreAllMocks(); });
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ModelQuotaCard /></QueryClientProvider>); }
it("shows actual counts, exhaustion explanation, and refresh", async () => {
  const get = vi.spyOn(apiClient,"get").mockResolvedValue({data:{resets_at:"2026-10-04T00:00:00Z",shared_pool_enabled:true,shared:{requests:2,request_limit:2,estimated_input_tokens:4,estimated_token_limit:100},history:{requests:1,request_limit:-1}}});
  mount();
  expect(await screen.findByText(/共享模型日额度已用完/)).toBeInTheDocument();
  expect(screen.getByText(/任务 Ask 调用/)).toHaveTextContent("1 / 无限制");
  expect(screen.getByText(/失败和中断/)).toHaveTextContent("不代表实际 token 或费用");
  await userEvent.click(screen.getByRole("button",{name:"刷新用量"}));
  expect(get).toHaveBeenCalledTimes(2);
});
it("offers retry after a network failure", async () => {
  vi.spyOn(apiClient,"get").mockRejectedValue(new Error("synthetic network failure"));
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("请重试");
  expect(screen.getByRole("button",{name:"刷新用量"})).toBeEnabled();
});
