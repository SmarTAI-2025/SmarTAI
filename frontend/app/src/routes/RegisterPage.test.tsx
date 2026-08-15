import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { REGISTRATION_FLOW_STORAGE_KEY } from "@/lib/registrationFlow";
import { RegisterPage } from "./RegisterPage";

const mutateAsync = vi.fn();

vi.mock("@/api/hooks/registration", () => ({
  useRequestRegistration: () => ({ isPending: false, mutateAsync }),
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "en-US", setLocale: vi.fn(), t: (key: string) => key }),
}));

describe("RegisterPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
    window.localStorage.clear();
    mutateAsync.mockResolvedValue({
      status: "verification_required",
      request_id: "request-1",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
      transport: "development_mock",
    });
  });

  it("requests a link, clears secrets, and moves to the email screen without authenticating", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/register"]}>
        <Routes>
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/register/check-email" element={<div>Check email route</div>} />
        </Routes>
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Username"), " teacher ");
    await user.type(screen.getByLabelText(/School email/), "Teacher@Example.edu");
    await user.type(screen.getByLabelText("Password"), "safe-password");
    await user.type(screen.getByLabelText("Confirm password"), "safe-password");
    await user.click(screen.getByRole("button", { name: "Send verification link" }));

    await waitFor(() => expect(screen.getByText("Check email route")).toBeInTheDocument());
    expect(mutateAsync).toHaveBeenCalledWith({
      username: "teacher",
      email: "teacher@example.edu",
      password: "safe-password",
    });
    const stored = window.sessionStorage.getItem(REGISTRATION_FLOW_STORAGE_KEY) ?? "";
    expect(stored).not.toContain("safe-password");
    expect(window.localStorage.getItem("smartai_token")).toBeNull();
  });

  it("enforces the eight-character password before sending", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <RegisterPage />
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Username"), "teacher");
    await user.type(screen.getByLabelText(/School email/), "teacher@example.edu");
    await user.type(screen.getByLabelText("Password"), "short");
    await user.type(screen.getByLabelText("Confirm password"), "short");
    await user.click(screen.getByRole("button", { name: "Send verification link" }));

    expect(screen.getByRole("alert")).toHaveTextContent("at least 8 characters");
    expect(mutateAsync).not.toHaveBeenCalled();
  });
});
