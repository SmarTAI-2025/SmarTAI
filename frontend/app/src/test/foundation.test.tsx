import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RequireRoleSession } from "@/components/auth/RequireRoleSession";
import { navForRole, homeForRole } from "@/components/layout/nav";
import { clearAuthToken } from "@/api/client";
import type { User, UserRole } from "@/types/auth";

// Stub the current-user hook so the guard sees a controlled identity per test
// without hitting the network. Each test installs its own return value.
vi.mock("@/api/hooks", () => ({
  useCurrentUser: vi.fn(),
}));
vi.mock("@/api/client", () => ({
  clearAuthToken: vi.fn(),
}));

const { useCurrentUser } = await import("@/api/hooks");

function wrap(user: User | null, loading = false, errored = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  (useCurrentUser as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    data: user,
    isLoading: loading,
    isError: errored,
  });
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/protected"]}>
        <Routes>
          <Route path="/protected" element={<RequireRoleSession allowed="teacher" homeFor={homeForRole}><div>protected</div></RequireRoleSession>} />
          <Route path="/teacher" element={<div>teacher home</div>} />
          <Route path="/student" element={<div>student home</div>} />
          <Route path="/admin" element={<div>admin home</div>} />
          <Route path="/login" element={<div>login</div>} />
        </Routes>
      </MemoryRouter>
      {children}
    </QueryClientProvider>
  );
}

const user = (role: UserRole): User => ({
  id: "u1", username: "x", email: "x@x", role, is_active: true, created_at: 1,
});

describe("role navigation", () => {
  it("gives teacher, student, and admin distinct nav sets", () => {
    const t = navForRole("teacher").map((n) => n.to);
    const s = navForRole("student").map((n) => n.to);
    const a = navForRole("admin").map((n) => n.to);
    expect(t).not.toEqual(s);
    expect(s).not.toEqual(a);
    expect(t.some((p) => p.startsWith("/teacher"))).toBe(true);
    expect(s.some((p) => p.startsWith("/student"))).toBe(true);
    expect(a.some((p) => p.startsWith("/admin"))).toBe(true);
  });

  it("homeForRole maps each role to its workspace root", () => {
    expect(homeForRole("teacher")).toBe("/teacher");
    expect(homeForRole("student")).toBe("/student");
    expect(homeForRole("admin")).toBe("/admin");
  });
});

describe("RequireRoleSession redirect", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("redirects a wrong-role user to their own home without clearing the token", async () => {
    const Wrapper = wrap(user("student"));
    const { container } = render(<Wrapper><></></Wrapper>);
    await waitFor(() => expect(container.textContent).toContain("student home"));
    // A valid session must NOT be cleared on a wrong-role redirect.
    expect(clearAuthToken).not.toHaveBeenCalled();
  });

  it("clears the token and bounces to /login when the session is gone", async () => {
    const Wrapper = wrap(null, false, true);
    const { container } = render(<Wrapper><></></Wrapper>);
    await waitFor(() => expect(container.textContent).toContain("login"));
    expect(clearAuthToken).toHaveBeenCalled();
  });

  it("renders children for the allowed role", async () => {
    const Wrapper = wrap(user("teacher"));
    const { container } = render(<Wrapper><></></Wrapper>);
    await waitFor(() => expect(container.textContent).toContain("protected"));
  });
});
