import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import { LoginPage } from "./LoginPage";

vi.mock("@/api/hooks", () => ({ useLogin: vi.fn() }));
const { useLogin } = await import("@/api/hooks");
const mutateAsync = vi.fn();

function Destination() {
  const location = useLocation();
  return <div>{location.pathname}{location.search}{location.hash}</div>;
}
function page(pending = false, from = "/history?tag=test#result") {
  vi.mocked(useLogin).mockReturnValue({ mutateAsync, isPending: pending } as unknown as ReturnType<typeof useLogin>);
  return render(<QueryClientProvider client={new QueryClient()}><I18nProvider>
    <MemoryRouter initialEntries={[{ pathname: "/login", state: { from } }]}><Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="*" element={<Destination />} />
    </Routes></MemoryRouter>
  </I18nProvider></QueryClientProvider>);
}
beforeEach(() => {
  vi.resetAllMocks();
  window.localStorage.clear();
  mutateAsync.mockResolvedValue({ id: "teacher", role: "teacher" });
});

describe("login modes", () => {
  it.each(["username", "email"])("submits an explicit unambiguous %s identity and retains the return path", async (mode) => {
    page();
    if (mode === "email") fireEvent.click(screen.getByRole("radio", { name: "邮箱登录" }));
    fireEvent.change(screen.getByRole("textbox", { name: mode === "email" ? "邮箱" : "用户名" }), { target: { value: "other@ustc.edu.cn" } });
    fireEvent.change(screen.getByPlaceholderText("输入密码"), { target: { value: "test-password" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "登录" })));
    expect(mutateAsync).toHaveBeenCalledWith(mode === "email"
      ? { login_type: "email", email: "other@ustc.edu.cn", password: "test-password" }
      : { username: "other@ustc.edu.cn", password: "test-password" });
    expect(screen.getByText("/history?tag=test#result")).toBeInTheDocument();
  });

  it("clears password and mode-specific errors on switching, preserving each identity", async () => {
    mutateAsync.mockRejectedValueOnce(new APIError(401, "bad password"));
    page();
    fireEvent.change(screen.getByRole("textbox", { name: "用户名" }), { target: { value: "teacher" } });
    fireEvent.change(screen.getByPlaceholderText("输入密码"), { target: { value: "wrong" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "登录" })));
    expect(screen.getByRole("alert")).toHaveTextContent("用户名或密码不正确");
    fireEvent.click(screen.getByRole("radio", { name: "邮箱登录" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("输入密码")).toHaveValue("");
    fireEvent.change(screen.getByRole("textbox", { name: "邮箱" }), { target: { value: "teacher@ustc.edu.cn" } });
    fireEvent.click(screen.getByRole("radio", { name: "用户名登录" }));
    expect(screen.getByRole("textbox", { name: "用户名" })).toHaveValue("teacher");
    fireEvent.click(screen.getByRole("radio", { name: "邮箱登录" }));
    expect(screen.getByRole("textbox", { name: "邮箱" })).toHaveValue("teacher@ustc.edu.cn");
  });

  it.each([[401, "邮箱或密码不正确"], [0, "暂时无法连接服务"], [503, "登录失败"]])("recovers from email login failure %s", async (status, message) => {
    mutateAsync.mockRejectedValueOnce(new APIError(Number(status), "failed"));
    page();
    fireEvent.click(screen.getByRole("radio", { name: "邮箱登录" }));
    fireEvent.change(screen.getByRole("textbox", { name: "邮箱" }), { target: { value: "teacher@ustc.edu.cn" } });
    fireEvent.change(screen.getByPlaceholderText("输入密码"), { target: { value: "test-password" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "登录" })));
    expect(screen.getByRole("alert")).toHaveTextContent(String(message));
    expect(screen.getByPlaceholderText("输入密码")).toHaveValue("");
    fireEvent.change(screen.getByPlaceholderText("输入密码"), { target: { value: "test-password" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "登录" })));
    expect(screen.getByText("/history?tag=test#result")).toBeInTheDocument();
  });

  it("blocks double submit and switching before pending state has rendered", async () => {
    let finish!: (value: unknown) => void;
    mutateAsync.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    page();
    fireEvent.change(screen.getByRole("textbox", { name: "用户名" }), { target: { value: "teacher" } });
    fireEvent.change(screen.getByPlaceholderText("输入密码"), { target: { value: "test-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    fireEvent.click(screen.getByRole("radio", { name: "邮箱登录" }));
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("radio", { name: "用户名登录" })).toBeChecked();
    await act(async () => finish({ role: "teacher" }));
  });

  it("disables mode, fields, and submit while signing in", () => {
    page(true);
    expect(screen.getByRole("radio", { name: "邮箱登录" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "用户名" })).toBeDisabled();
    expect(screen.getByPlaceholderText("输入密码")).toBeDisabled();
    expect(screen.getByRole("button", { name: "正在登录…" })).toBeDisabled();
  });

  it("still refuses external return URLs", async () => {
    page(false, "https://example.com/private");
    fireEvent.change(screen.getByRole("textbox", { name: "用户名" }), { target: { value: "teacher" } });
    fireEvent.change(screen.getByPlaceholderText("输入密码"), { target: { value: "test-password" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "登录" })));
    expect(screen.getByText("/")).toBeInTheDocument();
  });
});
