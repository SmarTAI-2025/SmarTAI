import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { getAdminMonitoring, queryAdminAdoption, type AdoptionResult, type MonitoringResult } from "@/api/adminMonitoring";
import { AdminAnalyticsPage, adoptionWindowStart } from "./AdminAnalyticsPage";
import { AdminMonitoringPage, capacityChartData } from "./AdminMonitoringPage";
import { bytes } from "./observationUi";

vi.mock("@/api/adminMonitoring", () => ({ getAdminMonitoring: vi.fn(), queryAdminAdoption: vi.fn() }));
// Chart geometry is verified in the browser; jsdom has no layout engine.
vi.mock("recharts", () => ({ ResponsiveContainer: () => <div data-testid="chart" />, CartesianGrid: () => null, Legend: () => null, Line: () => null, LineChart: () => null, Tooltip: () => null, XAxis: () => null, YAxis: () => null }));

function mount(element: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

const adoption: AdoptionResult = {
  metric_version: "test", as_of: 100, start: 10, end: 100, timezone: "UTC",
  coverage: { status: "partial", first_retained_event_at: 30, last_retained_event_at: 90, event_count: 4, notes: ["历史未回填"] },
  filters: { event_role: "teacher", success: true, account_cohort: "all_unclassified", environment: "current_database" },
  summary: { current_teacher_accounts: 5, new_users: 3, login_users: 2, business_users: 1, business_actions: 4, actions_per_business_user: 4 },
  series: [], frequency: [{ label: "1 天", users: 1 }],
  retention: [{ date: "2026-01-01", users: 2, w1: { numerator: 1, denominator: 2, pending: 0, rate: .5 }, w4: { numerator: 0, denominator: 0, pending: 2, rate: null } }],
  definitions: { growth: "新增口径", activity: "业务活动口径", frequency: "频率口径", retention: "留存口径", business_events: [] },
};
const monitoring: MonitoringResult = {
  metric_version: "test", as_of: 100,
  health: { application: { status: "ok", check: "private_admin_request" }, database: { status: "ok", check: "select_1" }, storage: { status: "error", check: "directory_access_only", backend: "local" }, public_api: { status: "unavailable", check: "not_probed" }, providers: { status: "unavailable", check: "not_probed" } },
  application_storage: { status: "available", bytes: 0, objects: 0, by_backend: [], knowledge_reserved_bytes: 0 },
  disk: { status: "available", scope: "application_mount", total_bytes: 10240, used_bytes: 5120, free_bytes: 5120, used_ratio: .5, host_status: "unavailable" },
  history: { status: "unavailable", coverage_start: null, sample_interval_seconds: 300, retention_days: 90, samples: [], projection: { status: "unavailable", reason: "insufficient_samples", bytes_per_day: null, days_remaining: null } },
  recommendation: { level: "warning", message: "请规划容量" }, notes: ["不等于对象存储账单"],
};

beforeEach(() => vi.resetAllMocks());

describe("analytics page", () => {
  it("renders denominators and distinguishes immature retention from zero", async () => {
    vi.mocked(queryAdminAdoption).mockResolvedValue(adoption);
    mount(<AdminAnalyticsPage />);
    expect(await screen.findByText("50.0%")).toBeInTheDocument();
    expect(screen.getByText("1 / 2 人")).toBeInTheDocument();
    expect(screen.getByText("尚未成熟")).toBeInTheDocument();
    expect(screen.getByText(/所有未分类账号/)).toBeInTheDocument();
  });

  it("changes range and timezone in the request and refreshes", async () => {
    vi.mocked(queryAdminAdoption).mockResolvedValue(adoption);
    mount(<AdminAnalyticsPage />);
    await screen.findByText("50.0%");
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("时间区间"), "90");
    await waitFor(() => expect(queryAdminAdoption).toHaveBeenLastCalledWith(expect.objectContaining({ start: adoptionWindowStart(90, "Asia/Singapore") }), expect.any(AbortSignal)));
    await user.selectOptions(screen.getByLabelText("日历时区"), "UTC");
    await waitFor(() => expect(queryAdminAdoption).toHaveBeenLastCalledWith(expect.objectContaining({ timezone: "UTC", start: adoptionWindowStart(90, "UTC") }), expect.any(AbortSignal)));
    await user.click(screen.getByRole("button", { name: "刷新统计" }));
    await waitFor(() => expect(queryAdminAdoption).toHaveBeenCalledTimes(4));
  });

  it("does not fabricate a chart or numbers before collection", async () => {
    vi.mocked(queryAdminAdoption).mockResolvedValue({ ...adoption, coverage: { ...adoption.coverage, status: "unavailable", first_retained_event_at: null }, summary: { current_teacher_accounts: 2, new_users: null, login_users: null, business_users: null, business_actions: null, actions_per_business_user: null }, retention: [], frequency: [] });
    mount(<AdminAnalyticsPage />);
    expect(await screen.findByText("尚无可用事件")).toBeInTheDocument();
    expect(screen.getAllByText("暂无数据")).toHaveLength(4);
    expect(screen.queryByTestId("chart")).not.toBeInTheDocument();
  });

  it("retries a failure without exposing raw exception details", async () => {
    vi.mocked(queryAdminAdoption).mockRejectedValueOnce(new Error("private connection detail")).mockResolvedValue(adoption);
    mount(<AdminAnalyticsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("数据加载失败");
    expect(screen.queryByText("private connection detail")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("50.0%")).toBeInTheDocument();
  });
});

describe("monitoring page", () => {
  it("keeps application and host capacity distinct and shows failed health", async () => {
    vi.mocked(getAdminMonitoring).mockResolvedValue(monitoring);
    mount(<AdminMonitoringPage />);
    expect(await screen.findByText("检查失败")).toBeInTheDocument();
    expect(screen.getByText(/宿主磁盘：不可用/)).toBeInTheDocument();
    expect(screen.getByText("0 B")).toBeInTheDocument();
    expect(screen.getByText("暂无估计")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "刷新检查" }));
    await waitFor(() => expect(getAdminMonitoring).toHaveBeenCalledTimes(2));
  });

  it("shows loading and error states without stale success", async () => {
    let reject!: (reason: Error) => void;
    vi.mocked(getAdminMonitoring).mockImplementation(() => new Promise((_, fail) => { reject = fail; }));
    mount(<AdminMonitoringPage />);
    expect(screen.getByRole("status")).toHaveTextContent("正在检查");
    reject(new Error("unreachable"));
    expect(await screen.findByRole("alert")).toHaveTextContent("数据加载失败");
    expect(screen.queryByText("检查通过")).not.toBeInTheDocument();
  });

  it("breaks the curve over missing samples", () => {
    const base = { total_bytes: 100, used_bytes: 50, free_bytes: 50, application_bytes: null };
    const points = capacityChartData([{ at: 100, ...base }, { at: 2000, ...base }]);
    expect(points[1]).toEqual({ at: 1999, free_bytes: null, application_bytes: null });
    expect(bytes(null)).toBe("不可用");
    expect(bytes(0)).toBe("0 B");
    expect(bytes(1024)).toBe("1 KiB");
  });
});
