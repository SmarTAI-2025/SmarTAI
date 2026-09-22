import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import { PENDING_REGISTRATION_STORAGE_KEY } from "@/lib/registrationFlow";
import { RegisterPage } from "@/routes/RegisterPage";

vi.mock("@/api/hooks", () => ({ useRequestRegistration: vi.fn() }));

const { useRequestRegistration } = await import("@/api/hooks");
const mutateAsync = vi.fn();
const resetMutation = vi.fn();

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.hash}`}</div>;
}

function renderPage() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={["/register"]}>
        <Routes>
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/register/check-email" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

function fillRegistration() {
  fireEvent.change(screen.getByRole("textbox", { name: "用户名" }), { target: { value: "teacher" } });
  fireEvent.change(screen.getByRole("textbox", { name: "学校邮箱" }), { target: { value: "Teacher@USTC.edu.cn" } });
  fireEvent.change(screen.getByLabelText("设置密码"), { target: { value: "safe-password" } });
  fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "safe-password" } });
}

describe("RegisterPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
    window.localStorage.clear();
    (useRequestRegistration as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      mutateAsync,
      reset: resetMutation,
      isPending: false,
    });
  });

  afterEach(() => vi.useRealTimers());

  it("submits only the frozen fields, clears secret mutation state, and navigates with safe metadata", async () => {
    mutateAsync.mockResolvedValue({
      status: "verification_required",
      request_id: "request-1",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    });
    renderPage();
    fillRegistration();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "发送验证链接" })));

    expect(mutateAsync).toHaveBeenCalledWith({
      username: "teacher",
      email: "teacher@ustc.edu.cn",
      password: "safe-password",
    });
    expect(resetMutation).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("location")).toHaveTextContent("/register/check-email");
    const stored = window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY) ?? "";
    expect(stored).toContain("request-1");
    expect(stored).not.toContain("teacher@ustc.edu.cn");
    expect(stored).not.toContain("safe-password");
    expect(window.localStorage.length).toBe(0);
  });

  it("prioritizes a stable domain error over its HTTP status and clears passwords", async () => {
    mutateAsync.mockRejectedValue(new APIError(
      503,
      "registration_email_domain_not_allowed",
      { detail: { code: "registration_email_domain_not_allowed" } },
    ));
    renderPage();
    fillRegistration();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "发送验证链接" })));
    expect(screen.getByRole("alert")).toHaveTextContent("请改用允许的学校邮箱");
    expect(screen.getByLabelText("设置密码")).toHaveValue("");
    expect(screen.getByLabelText("确认密码")).toHaveValue("");
    expect(resetMutation).toHaveBeenCalledTimes(1);
  });

  it("shows the frozen requirements and blocks mismatched confirmation locally", async () => {
    renderPage();
    expect(screen.getByText(/首期支持 ustc\.edu\.cn/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "用户名" }), { target: { value: "teacher" } });
    fireEvent.change(screen.getByRole("textbox", { name: "学校邮箱" }), { target: { value: "teacher@ustc.edu.cn" } });
    fireEvent.change(screen.getByLabelText("设置密码"), { target: { value: "safe-password" } });
    fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "different-password" } });
    fireEvent.submit(screen.getByRole("button", { name: "发送验证链接" }).closest("form")!);

    expect(screen.getByRole("alert")).toHaveTextContent("两次输入的密码不一致");
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it.each([
    [0, "暂时无法连接注册服务"],
    [404, "注册服务暂不可用"],
    [503, "注册服务暂不可用"],
  ])("keeps the user on the form for a real service failure (%s)", async (status, expected) => {
    mutateAsync.mockRejectedValue(new APIError(status, "service failure"));
    renderPage();
    fillRegistration();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "发送验证链接" })));

    expect(screen.getByRole("alert")).toHaveTextContent(expected);
    expect(screen.queryByTestId("location")).not.toBeInTheDocument();
    expect(screen.getByLabelText("设置密码")).toHaveValue("");
  });

  it("enforces the authoritative registration Retry-After cooldown", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-30T00:00:00Z"));
    mutateAsync.mockRejectedValue(new APIError(
      429,
      "registration_rate_limited",
      { detail: { code: "registration_rate_limited" } },
      60,
    ));
    renderPage();
    fillRegistration();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "发送验证链接" })));

    const blocked = screen.getByRole("button", { name: "01:00 后可重试" });
    expect(blocked).toBeDisabled();
    fireEvent.click(blocked);
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTime(60_000));
    expect(screen.getByRole("button", { name: "发送验证链接" })).toBeEnabled();
  });

  it("guards a synchronous double submit", async () => {
    let resolveRequest!: (value: unknown) => void;
    mutateAsync.mockReturnValue(new Promise((resolve) => { resolveRequest = resolve; }));
    renderPage();
    fillRegistration();
    const submit = screen.getByRole("button", { name: "发送验证链接" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    resolveRequest({ status: "verification_required", request_id: "request-1", expires_in_seconds: 1800, resend_after_seconds: 60 });
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/register/check-email"));
  });
});
