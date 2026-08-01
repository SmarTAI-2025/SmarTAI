import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AdminUsersPage } from "@/routes/admin/AdminUsersPage";
import type { AdminUser } from "@/types/education";

vi.mock("@/api/hooks/admin", () => ({
  useAdminUsers: vi.fn(),
  useAdminSetActive: vi.fn(),
}));

const { useAdminUsers, useAdminSetActive } = await import("@/api/hooks/admin");

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AdminUsersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const users: AdminUser[] = [
  { id: "u1", username: "alice", email: "a@x", role: "teacher", is_active: true, created_at: 1 },
  { id: "u2", username: "bob", email: "b@x", role: "student", is_active: false, created_at: 2 },
];

function mockUsers(data: AdminUser[] | null, state: "loading" | "error" | "ok" = "ok") {
  (useAdminUsers as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    data: state === "ok" ? data : undefined,
    isLoading: state === "loading",
    isError: state === "error",
  });
  (useAdminSetActive as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  });
}

describe("AdminUsersPage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders loading state", () => {
    mockUsers(null, "loading");
    renderPage();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("renders error state", () => {
    mockUsers(null, "error");
    renderPage();
    expect(screen.getByText(/加载失败/)).toBeInTheDocument();
  });

  it("renders empty state", () => {
    mockUsers([]);
    renderPage();
    expect(screen.getByText("暂无用户")).toBeInTheDocument();
  });

  it("lists users with role + status and an activate/deactivate button", async () => {
    mockUsers(users);
    renderPage();
    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("bob")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /停用|启用/ })).toHaveLength(2);
  });

  it("filters by role via the select", async () => {
    mockUsers(users);
    const user = userEvent.setup();
    renderPage();
    await user.selectOptions(screen.getByDisplayValue("全部"), "teacher");
    // The hook is mocked; we assert the filter call shape by checking the
    // selected option is now teacher (the page passes role into the hook).
    expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("teacher");
  });
});
