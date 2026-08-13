import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ExpertsPage } from "./ExpertsPage";

const addExpert = vi.fn();

const hookState = vi.hoisted(() => ({
  catalog: [] as Array<Record<string, unknown>>,
}));

vi.mock("@/api/hooks", () => ({
  useExperts: () => ({ data: [], isLoading: false, isError: false, isFetching: false, refetch: vi.fn() }),
  useProviderCatalog: () => ({ data: hookState.catalog, isLoading: false, isError: false, refetch: vi.fn() }),
  useAddExpertKey: () => ({ isPending: false, mutateAsync: addExpert }),
  useUpdateExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useSelectExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useVerifyExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useVerifyExpertVision: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useRemoveExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));

function renderPage() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={["/settings/byok"]}>
        <ExpertsPage />
      </MemoryRouter>
    </I18nProvider>,
  );
}

describe("ExpertsPage custom endpoint gate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    addExpert.mockResolvedValue({ status: "success", provider_id: "pc-test" });
  });

  it("does not expose a custom provider option while the backend flag is off", async () => {
    hookState.catalog = [];
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "添加模型配置" }));
    expect(screen.queryByRole("option", { name: "自定义 OpenAI-compatible" })).not.toBeInTheDocument();
  });

  it("requires risk confirmation and submits the versioned acknowledgement", async () => {
    hookState.catalog = [{
      provider_type: "openai_compatible",
      display_name: "Custom OpenAI-compatible service",
      custom: true,
      risk_ack_version: "2026-08-12.v1",
    }];
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "添加模型配置" }));
    await user.selectOptions(screen.getByLabelText("服务商"), "openai_compatible");
    await user.type(screen.getByLabelText("模型名称"), "school-model");
    await user.type(screen.getByLabelText(/^API key/), "sk-secret-value");
    await user.type(screen.getByLabelText(/^API Base URL/), "https://relay.example.com/v1");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    expect(screen.getByRole("alert")).toHaveTextContent("请阅读并确认");
    expect(addExpert).not.toHaveBeenCalled();

    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(addExpert).toHaveBeenCalledTimes(1));
    expect(addExpert).toHaveBeenCalledWith(expect.objectContaining({
      provider_type: "openai_compatible",
      model: "school-model",
      base_url: "https://relay.example.com/v1",
      risk_ack_version: "2026-08-12.v1",
    }));
    expect(screen.queryByDisplayValue("sk-secret-value")).not.toBeInTheDocument();
    expect(window.localStorage.getItem("sk-secret-value")).toBeNull();
  });
});
