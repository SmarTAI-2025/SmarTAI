import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import { RegisterPage } from "@/routes/RegisterPage";

vi.mock("@/api/hooks", () => ({
  useRequestRegistration: vi.fn(),
  useResendRegistration: vi.fn(),
}));

const { useRequestRegistration, useResendRegistration } = await import("@/api/hooks");
const requestRegistration = vi.fn();
const resendRegistration = vi.fn();
const originalLocalStorage = Object.getOwnPropertyDescriptor(window, "localStorage");

function renderPage() {
  return render(
    <I18nProvider>
      <MemoryRouter>
        <RegisterPage />
      </MemoryRouter>
    </I18nProvider>,
  );
}

async function submitRegistration() {
  fireEvent.change(screen.getByRole("textbox", { name: "用户名" }), { target: { value: "teacher" } });
  fireEvent.change(screen.getByRole("textbox", { name: "科大邮箱" }), { target: { value: "teacher@ustc.edu.cn" } });
  fireEvent.change(screen.getByLabelText("设置密码"), { target: { value: "safe-password" } });
  fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "safe-password" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "发送验证邮件" }));
  });
}

describe("RegisterPage", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: { getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn() },
    });
    (useRequestRegistration as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      mutateAsync: requestRegistration,
      isPending: false,
    });
    (useResendRegistration as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      mutateAsync: resendRegistration,
      isPending: false,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    if (originalLocalStorage) {
      Object.defineProperty(window, "localStorage", originalLocalStorage);
    } else {
      Reflect.deleteProperty(window, "localStorage");
    }
  });

  it("shows resend only after a request and keeps it disabled for the cooldown", async () => {
    requestRegistration.mockResolvedValue({
      status: "verification_required",
      request_id: "request-1",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    });
    renderPage();

    expect(screen.queryByRole("button", { name: /重新发送/ })).not.toBeInTheDocument();
    await submitRegistration();

    expect(screen.getByText(/验证邮件已发送/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新发送验证邮件（60 秒）" })).toBeDisabled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(screen.getByRole("button", { name: "重新发送验证邮件" })).toBeEnabled();
  });

  it("resends with the latest request id returned by the server", async () => {
    requestRegistration.mockResolvedValue({
      status: "verification_required",
      request_id: "request-1",
      expires_in_seconds: 1800,
      resend_after_seconds: 0,
    });
    resendRegistration
      .mockResolvedValueOnce({
        status: "verification_required",
        request_id: "request-2",
        expires_in_seconds: 1800,
        resend_after_seconds: 0,
      })
      .mockResolvedValueOnce({
        status: "verification_required",
        request_id: "request-3",
        expires_in_seconds: 1800,
        resend_after_seconds: 60,
    });
    renderPage();
    await submitRegistration();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "重新发送验证邮件" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "重新发送验证邮件" }));
    });

    expect(resendRegistration).toHaveBeenNthCalledWith(1, "request-1");
    expect(resendRegistration).toHaveBeenNthCalledWith(2, "request-2");
  });

  it("shows the stable registration rate-limit error", async () => {
    requestRegistration.mockResolvedValue({
      status: "verification_required",
      request_id: "request-1",
      expires_in_seconds: 1800,
      resend_after_seconds: 0,
    });
    resendRegistration.mockRejectedValue(new APIError(
      429,
      "registration_rate_limited",
      { detail: { code: "registration_rate_limited" } },
      120,
    ));
    renderPage();
    await submitRegistration();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "重新发送验证邮件" }));
    });

    expect(screen.getByRole("alert")).toHaveTextContent("请求过于频繁，请稍后再试。");
  });
});
