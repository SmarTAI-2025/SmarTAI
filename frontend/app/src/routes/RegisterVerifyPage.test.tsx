import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { APIError } from "@/api/client";
import { RegisterVerifyPage } from "./RegisterVerifyPage";

const mutateAsync = vi.fn();

vi.mock("@/api/hooks/registration", () => ({
  useVerifyRegistration: () => ({ isPending: false, mutateAsync }),
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "en-US", setLocale: vi.fn(), t: (key: string) => key }),
}));

function renderVerify(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/register/verify" element={<RegisterVerifyPage />} />
        <Route path="/login" element={<div>Login route</div>} />
        <Route path="/register" element={<div>Register route</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function VerifyNavigationHarness() {
  const navigate = useNavigate();
  return (
    <>
      <button onClick={() => navigate("/register/verify#token=expired-token")}>
        Open another link
      </button>
      <RegisterVerifyPage />
    </>
  );
}

describe("RegisterVerifyPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
  });

  it("does not consume a link until the teacher explicitly confirms", async () => {
    mutateAsync.mockResolvedValue({ status: "registered" });
    const user = userEvent.setup();
    renderVerify("/register/verify#token=high-entropy-token");

    expect(screen.getByText("Complete email verification")).toBeInTheDocument();
    expect(mutateAsync).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Confirm and create account" }));

    await waitFor(() => expect(screen.getByText("Registration complete")).toBeInTheDocument());
    expect(mutateAsync).toHaveBeenCalledWith({ token: "high-entropy-token" });
    expect(window.localStorage.getItem("smartai_token")).toBeNull();
  });

  it("renders an actionable expired-link state", async () => {
    mutateAsync.mockRejectedValue(new APIError(410, "expired", {
      detail: { code: "verification_link_expired" },
    }));
    const user = userEvent.setup();
    renderVerify("/register/verify#token=expired-token");

    await user.click(screen.getByRole("button", { name: "Confirm and create account" }));
    await waitFor(() => expect(screen.getByText("Request a new verification link")).toBeInTheDocument());
  });

  it("does not claim success when opened without a token", () => {
    renderVerify("/register/verify");
    expect(screen.getByText("We cannot verify this link")).toBeInTheDocument();
    expect(screen.queryByText("Registration complete")).not.toBeInTheDocument();
  });

  it("resets a completed page when a different verification link is opened", async () => {
    mutateAsync
      .mockResolvedValueOnce({ status: "registered" })
      .mockRejectedValueOnce(new APIError(410, "expired", {
        detail: { code: "verification_link_expired" },
      }));
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/register/verify#token=first-token"]}>
        <Routes>
          <Route path="/register/verify" element={<VerifyNavigationHarness />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Confirm and create account" }));
    await screen.findByText("Registration complete");
    await user.click(screen.getByRole("button", { name: "Open another link" }));
    await user.click(await screen.findByRole("button", { name: "Confirm and create account" }));

    await screen.findByText("Request a new verification link");
    expect(mutateAsync).toHaveBeenNthCalledWith(2, { token: "expired-token" });
  });
});
