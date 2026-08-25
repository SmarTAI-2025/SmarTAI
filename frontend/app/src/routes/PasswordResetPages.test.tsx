import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ForgotPasswordPage } from "@/routes/ForgotPasswordPage";
import { ResetPasswordPage } from "@/routes/ResetPasswordPage";
import { LoginPage } from "@/routes/LoginPage";

vi.mock("@/api/hooks", () => ({
  useLogin: vi.fn(),
  useRequestPasswordReset: vi.fn(),
  useConfirmPasswordReset: vi.fn(),
}));

const { useLogin, useRequestPasswordReset, useConfirmPasswordReset } = await import("@/api/hooks");
const originalLocalStorage = Object.getOwnPropertyDescriptor(window, "localStorage");

afterEach(() => {
  if (originalLocalStorage) {
    Object.defineProperty(window, "localStorage", originalLocalStorage);
  } else {
    delete (window as Window & { localStorage?: Storage }).localStorage;
  }
});

function renderPage(element: React.ReactNode, initialEntries = ["/"]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <MemoryRouter initialEntries={initialEntries}>{element}</MemoryRouter>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

describe("password reset pages", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: { getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn() },
    });
    (useLogin as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
    (useRequestPasswordReset as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutateAsync: vi.fn().mockResolvedValue({ status: "reset_link_requested" }), isPending: false });
    (useConfirmPasswordReset as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutateAsync: vi.fn().mockResolvedValue({ status: "password_reset" }), isPending: false });
  });

  it("offers forgot password from login", () => {
    renderPage(<LoginPage />);
    expect(screen.getByRole("link", { name: "忘记密码" })).toHaveAttribute("href", "/forgot-password");
  });

  it("submits an email and shows the neutral request confirmation", async () => {
    const user = userEvent.setup();
    renderPage(<ForgotPasswordPage />);
    await user.type(screen.getByRole("textbox", { name: "科大邮箱" }), "teacher@ustc.edu.cn");
    await user.click(screen.getByRole("button", { name: "发送重置邮件" }));
    expect(await screen.findByText("如果该邮箱对应 SmarTAI 账号，我们会发送密码重置链接。请检查邮箱。"))
      .toBeInTheDocument();
  });

  it("requires matching passwords before confirming a token", async () => {
    const user = userEvent.setup();
    const mutateAsync = vi.fn().mockResolvedValue({ status: "password_reset" });
    (useConfirmPasswordReset as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutateAsync, isPending: false });
    renderPage(<ResetPasswordPage />, ["/reset-password#token=test-token"]);
    await user.type(screen.getByLabelText("新密码"), "new-password-123");
    await user.type(screen.getByLabelText("确认新密码"), "different-password");
    await user.click(screen.getByRole("button", { name: "重置密码" }));
    expect(screen.getByRole("alert")).toHaveTextContent("两次输入的密码不一致");
    expect(mutateAsync).not.toHaveBeenCalled();
  });
});
