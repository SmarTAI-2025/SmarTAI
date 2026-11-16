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

it("weekly mode ignores exhausted legacy counters and separates reservations from spending", async () => {
  vi.spyOn(apiClient,"get").mockResolvedValue({data:{shared_quota_mode:"weekly_usd",shared_pool_enabled:true,
    shared:{requests:100,request_limit:100,estimated_input_tokens:100000,estimated_token_limit:100000},
    shared_sol:{requests:50,request_limit:50,estimated_input_tokens:50000,estimated_token_limit:50000},
    shared_weekly:{limit_usd:5,spent_usd:3.12,reserved_usd:0.55,remaining_usd:1.33,unknown_requests:1,resets_at:"2026-11-23T00:00:00+08:00"},
    history:{requests:1,request_limit:10}}});
  mount();
  expect(await screen.findByText(/本周已用/)).toHaveTextContent("$3.12 / $5.00");
  expect(screen.getByText(/待结算预留/)).toHaveTextContent("$0.55");
  expect(screen.getByText(/三个共享模型共用/)).toHaveTextContent("每周一新加坡时间 00:00");
  expect(screen.queryByText(/最多一半|估算输入|共享模型日额度已用完/)).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("用量尚未确认");
});

it("missing weekly data shows an error instead of falling back to daily", async () => {
  vi.spyOn(apiClient,"get").mockResolvedValue({data:{shared_quota_mode:"weekly_usd",shared_pool_enabled:true,
    shared:{requests:0,request_limit:100,estimated_input_tokens:0,estimated_token_limit:100000},history:{requests:0,request_limit:10}}});
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法读取周额度");
  expect(screen.queryByText(/估算输入/)).not.toBeInTheDocument();
});
