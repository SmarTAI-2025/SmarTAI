import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type { SourceStorageUsage } from "@/types/sourcePreview";
import { SettingsPage } from "./SettingsPage";

const mocks = vi.hoisted(() => ({
  getSourceStorageUsage: vi.fn(),
  useCurrentUser: vi.fn(),
}));

vi.mock("@/api/tasks", () => ({
  getSourceStorageUsage: mocks.getSourceStorageUsage,
}));

vi.mock("@/api/hooks", () => ({
  useCurrentUser: mocks.useCurrentUser,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "zh-CN", setLocale: vi.fn() }),
}));

vi.mock("@/theme/ThemeProvider", () => ({
  useTheme: () => ({ theme: "light", setTheme: vi.fn() }),
}));

const retryingUsage: SourceStorageUsage = {
  used_bytes: 5 * 1024 * 1024,
  limit_bytes: 512 * 1024 * 1024,
  available_bytes: 507 * 1024 * 1024,
  available_source_bytes: 3 * 1024 * 1024,
  cleanup_pending_bytes: 2 * 1024 * 1024,
  retrying_cleanup_bytes: 1024 * 1024,
  reserved_bytes: 0,
  cleanup_pending_count: 2,
  retrying_cleanup_count: 1,
  scope: "task_originals",
  knowledge_storage_included: false,
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(<SettingsPage />, { wrapper });
}

describe("SettingsPage task-original storage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getSourceStorageUsage.mockResolvedValue(retryingUsage);
    mocks.useCurrentUser.mockReturnValue({
      data: { id: "teacher-1", username: "teacher", email: "teacher@example.test", role: "teacher" },
      isLoading: false,
    });
  });

  it("shows owner-scoped usage and automatic retry without a cleanup action", async () => {
    renderPage();

    expect(await screen.findByText("5.0 MB")).toBeInTheDocument();
    expect(screen.getByText("/ 512.0 MB")).toBeInTheDocument();
    expect(screen.getByText(/1 个原文件刚才未能彻底清理/)).toHaveTextContent("无需手动操作");
    expect(screen.getByText(/知识库长期资料使用独立空间/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /清理|重试/ })).not.toBeInTheDocument();
    expect(mocks.getSourceStorageUsage).toHaveBeenCalledTimes(1);
  });

  it("does not request or expose teacher storage usage to a student", () => {
    mocks.useCurrentUser.mockReturnValue({
      data: { id: "student-1", username: "student", email: "student@example.test", role: "student" },
      isLoading: false,
    });

    renderPage();

    expect(screen.queryByRole("heading", { name: "任务原文件空间" })).not.toBeInTheDocument();
    expect(mocks.getSourceStorageUsage).not.toHaveBeenCalled();
  });
});
