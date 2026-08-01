import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { User } from "@/types/auth";
import { RequireTeacherSession } from "./RequireTeacherSession";

vi.mock("@/api/hooks", () => ({
  useCurrentUser: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  clearAuthToken: vi.fn(),
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({
    locale: "en-US",
    setLocale: vi.fn(),
    t: (key: string) => key,
  }),
}));

const { useCurrentUser } = await import("@/api/hooks");
const { clearAuthToken } = await import("@/api/client");

const teacher: User = {
  id: "teacher-1",
  username: "teacher",
  email: "teacher@example.com",
  role: "teacher",
  is_active: true,
  created_at: 1,
};

function TestApp({ client }: { client: QueryClient }) {
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/tasks/task-1/results"]}>
        <Routes>
          <Route
            path="/tasks/:taskId/results"
            element={
              <RequireTeacherSession>
                <div>Teacher workspace</div>
              </RequireTeacherSession>
            }
          />
          <Route path="/login" element={<div>Login page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function renderGuard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = render(<TestApp client={client} />);
  return { ...view, client };
}

describe("RequireTeacherSession cookie restore", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.removeItem("smartai_token");
  });

  it("waits for refresh-cookie recovery when no local access token exists", () => {
    (useCurrentUser as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    });

    renderGuard();

    expect(screen.getByRole("status")).toHaveTextContent("Restoring your session…");
    expect(screen.queryByText("Login page")).not.toBeInTheDocument();
    expect(clearAuthToken).not.toHaveBeenCalled();
  });

  it("renders the protected workspace after refresh-cookie recovery succeeds", () => {
    (useCurrentUser as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: teacher,
      isLoading: false,
      isError: false,
    });

    renderGuard();

    expect(screen.getByText("Teacher workspace")).toBeInTheDocument();
    expect(screen.queryByText("Login page")).not.toBeInTheDocument();
    expect(clearAuthToken).not.toHaveBeenCalled();
  });
});
