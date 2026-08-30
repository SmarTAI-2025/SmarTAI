import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { LoginPage } from "@/routes/LoginPage";

vi.mock("@/api/hooks", () => ({ useLogin: vi.fn() }));

const { useLogin } = await import("@/api/hooks");
const originalLocalStorage = Object.getOwnPropertyDescriptor(window, "localStorage");

describe("LoginPage registration entry", () => {
  beforeEach(() => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: { getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn() },
    });
    (useLogin as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    });
  });

  afterEach(() => {
    if (originalLocalStorage) {
      Object.defineProperty(window, "localStorage", originalLocalStorage);
    } else {
      Reflect.deleteProperty(window, "localStorage");
    }
  });

  it("links to email verification registration without invite wording", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <I18nProvider>
          <MemoryRouter>
            <LoginPage />
          </MemoryRouter>
        </I18nProvider>
      </QueryClientProvider>,
    );

    expect(screen.getByRole("link", { name: "邮箱验证注册" })).toHaveAttribute("href", "/register");
    expect(screen.queryByText(/邀请码/)).not.toBeInTheDocument();
  });
});
