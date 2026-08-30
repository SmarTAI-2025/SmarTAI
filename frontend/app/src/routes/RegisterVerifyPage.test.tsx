import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { I18nProvider } from "@/i18n/I18nProvider";
import { RegisterVerifyPage } from "@/routes/RegisterVerifyPage";

vi.mock("@/api/hooks", () => ({ useVerifyRegistration: vi.fn() }));

const { useVerifyRegistration } = await import("@/api/hooks");
const mutateAsync = vi.fn();
const resetMutation = vi.fn();

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.hash}`}</output>;
}

function renderPage(entry = "/register/verify#token=verification-secret") {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/register/verify" element={<><RegisterVerifyPage /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

describe("RegisterVerifyPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
    (useVerifyRegistration as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      mutateAsync,
      reset: resetMutation,
      isPending: false,
    });
  });

  afterEach(() => vi.useRealTimers());

  it("removes the token fragment immediately and waits for active confirmation", async () => {
    mutateAsync.mockResolvedValue({ status: "registered" });
    renderPage();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/register\/verify$/));
    expect(screen.getByTestId("location")).not.toHaveTextContent("verification-secret");
    expect(mutateAsync).not.toHaveBeenCalled();
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    expect(mutateAsync).toHaveBeenCalledWith("verification-secret");
    expect(resetMutation).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("heading", { name: "注册完成" })).toBeInTheDocument();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.getItem("verification-secret")).toBeNull();
  });

  it("treats already_verified as a completed registration", async () => {
    mutateAsync.mockResolvedValue({ status: "already_verified" });
    renderPage();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/register\/verify$/));
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    expect(await screen.findByRole("heading", { name: "注册完成" })).toBeInTheDocument();
    expect(screen.getByText("该流程此前已确认")).toBeInTheDocument();
  });

  it.each([
    ["verification_link_expired", "重置链接"],
    ["verification_link_already_used", "此链接不可再用"],
    ["verification_link_invalid", "无法验证此链接"],
  ])("maps %s without exposing the raw token", async (code, expected) => {
    mutateAsync.mockRejectedValue(new APIError(400, code, { detail: { code } }));
    renderPage();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/register\/verify$/));
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    if (code === "verification_link_expired") {
      expect(screen.getByRole("heading", { name: "需要新的验证链接" })).toBeInTheDocument();
    } else {
      expect(screen.getByRole("heading", { name: expected })).toBeInTheDocument();
    }
    expect(document.body).not.toHaveTextContent("verification-secret");
  });

  it("retains the in-memory token only for a safe network retry", async () => {
    mutateAsync
      .mockRejectedValueOnce(new APIError(0, "Network error"))
      .mockResolvedValueOnce({ status: "registered" });
    renderPage();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/register\/verify$/));
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());
    expect(screen.getByRole("alert")).toHaveTextContent("暂时无法完成验证");
    await act(async () => screen.getByRole("button", { name: "重试验证" }).click());
    expect(mutateAsync).toHaveBeenNthCalledWith(1, "verification-secret");
    expect(mutateAsync).toHaveBeenNthCalledWith(2, "verification-secret");
    expect(await screen.findByRole("heading", { name: "注册完成" })).toBeInTheDocument();
  });

  it.each([404, 408])("treats HTTP %s as a service retry, not an invalid link", async (status) => {
    mutateAsync
      .mockRejectedValueOnce(new APIError(status, "service unavailable"))
      .mockResolvedValueOnce({ status: "registered" });
    renderPage();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/register\/verify$/));
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());

    expect(screen.getByRole("alert")).toHaveTextContent("暂时无法完成验证");
    expect(screen.queryByRole("heading", { name: "无法验证此链接" })).not.toBeInTheDocument();
    await act(async () => screen.getByRole("button", { name: "重试验证" }).click());
    expect(mutateAsync).toHaveBeenNthCalledWith(2, "verification-secret");
  });

  it("enforces Retry-After before the verification token can be retried", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-30T00:00:00Z"));
    mutateAsync.mockRejectedValue(new APIError(
      429,
      "registration_rate_limited",
      { detail: { code: "registration_rate_limited" } },
      60,
    ));
    renderPage();
    await act(async () => undefined);
    await act(async () => screen.getByRole("button", { name: "确认并完成注册" }).click());

    const blocked = screen.getByRole("button", { name: "01:00 后可重试" });
    expect(blocked).toBeDisabled();
    blocked.click();
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTime(60_000));
    expect(screen.getByRole("button", { name: "重试验证" })).toBeEnabled();
  });
});
