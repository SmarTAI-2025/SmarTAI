import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, MemoryRouter } from "react-router-dom";
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
  it.each([["/maintenance", "维护返回正确"], ["/admin/users", "管理返回正确"], ["https://evil.invalid/maintenance", "默认管理页"]])("preserves safe administrator return path %s", async (from, text) => {
    (useLogin as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ mutateAsync: vi.fn().mockResolvedValue({ id: "synthetic-manager", role: "admin" }), isPending: false });
    const router=createMemoryRouter([
      {path:"/login",element:<LoginPage admin/>},
      {path:"/maintenance",element:<p>维护返回正确</p>},
      {path:"/admin/users",element:<p>管理返回正确</p>},
      {path:"/admin",element:<p>默认管理页</p>},
    ],{initialEntries:[{pathname:"/login",state:{from}}]});
    render(<QueryClientProvider client={new QueryClient()}><I18nProvider><RouterProvider router={router}/></I18nProvider></QueryClientProvider>);
    const user=userEvent.setup();
    await user.type(screen.getByLabelText("用户名"),"synthetic-manager");
    await user.type(screen.getByLabelText("密码",{exact:true}),"synthetic-password");
    await user.click(screen.getByRole("button",{name:/^登录$/}));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

});
