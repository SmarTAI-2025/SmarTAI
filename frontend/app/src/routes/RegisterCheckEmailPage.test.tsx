import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { savePendingRegistrationFlow } from "@/lib/registrationFlow";
import { RegisterCheckEmailPage } from "./RegisterCheckEmailPage";

const mutateAsync = vi.fn();

vi.mock("@/api/hooks/registration", () => ({
  useResendRegistration: () => ({ isPending: false, mutateAsync }),
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: "en-US", setLocale: vi.fn(), t: (key: string) => key }),
}));

describe("RegisterCheckEmailPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
    const now = Date.now();
    savePendingRegistrationFlow({
      version: 1,
      requestId: "request-1",
      username: "teacher",
      email: "teacher@example.edu",
      createdAt: now,
      expiresAt: now + 1_800_000,
      resendAvailableAt: now - 1,
      transport: "api",
    });
    mutateAsync.mockResolvedValue({
      status: "verification_required",
      request_id: "request-2",
      expires_in_seconds: 1800,
      resend_after_seconds: 60,
    });
  });

  it("shows the entered address, expiry, and a resend confirmation", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><RegisterCheckEmailPage /></MemoryRouter>);

    expect(screen.getByText(/teacher@example\.edu/)).toBeInTheDocument();
    expect(screen.getByText(/expires in/)).toHaveTextContent(/29:5\d|30:00/);
    await user.click(screen.getByRole("button", { name: "Resend link" }));

    await waitFor(() => expect(screen.getByText("New link sent")).toBeInTheDocument());
    expect(mutateAsync).toHaveBeenCalledWith({ request_id: "request-1" });
    expect(screen.getByRole("button", { name: /Resend available in/ })).toBeDisabled();
  });
});
