import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ExpertsPage } from "./ExpertsPage";

const addExpert = vi.fn();
const saveBaiduOCRCredentials = vi.fn();
const verifyBaiduOCRCredentials = vi.fn();
const deleteBaiduOCRCredentials = vi.fn();

const hookState = vi.hoisted(() => ({
  catalog: [] as Array<Record<string, unknown>>,
  experts: [] as Array<Record<string, unknown>>,
  baiduOCR: {
    credential_id: null as string | null,
    provider_type: "baidu_unlimited_ocr" as const,
    credentials_configured: false,
    verification_status: "not_configured",
    last_checked_at: null as string | null,
    verification_error_code: null as string | null,
  },
}));

vi.mock("@/api/hooks", () => ({
  useExperts: () => ({ data: hookState.experts, isLoading: false, isError: false, isFetching: false, refetch: vi.fn() }),
  useProviderCatalog: () => ({ data: hookState.catalog, isLoading: false, isError: false, refetch: vi.fn() }),
  useAddExpertKey: () => ({ isPending: false, mutateAsync: addExpert }),
  useUpdateExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useSelectExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useSetDefaultExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useVerifyExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useRemoveExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useBaiduOCRConfiguration: () => ({
    data: hookState.baiduOCR,
    error: null,
    isLoading: false,
    isError: false,
  }),
  useSaveBaiduOCRCredentials: () => ({
    isPending: false,
    mutateAsync: saveBaiduOCRCredentials,
  }),
  useVerifyBaiduOCRCredentials: () => ({
    isPending: false,
    mutateAsync: verifyBaiduOCRCredentials,
  }),
  useDeleteBaiduOCRCredentials: () => ({
    isPending: false,
    mutateAsync: deleteBaiduOCRCredentials,
  }),
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

function providerCatalog(editable = true) {
  return [
    catalogItem("gemini", "Google Gemini", "https://generativelanguage.googleapis.com", "gemini_generate_content", editable),
    catalogItem("openai", "OpenAI", "https://api.openai.com/v1", "openai_chat_completions", editable),
    catalogItem("zhipu", "Zhipu AI", "https://open.bigmodel.cn/api/paas/v4", "openai_chat_completions", editable),
    catalogItem("anthropic", "Anthropic", "https://api.anthropic.com", "anthropic_messages", editable),
    catalogItem("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "openai_chat_completions", editable),
    catalogItem("moonshot", "Moonshot (Kimi)", "https://api.moonshot.cn/v1", "openai_chat_completions", editable),
    catalogItem("qwen", "Qwen (通义千问)", "https://dashscope.aliyuncs.com/compatible-mode/v1", "openai_chat_completions", editable),
  ];
}

function catalogItem(
  providerType: string,
  displayName: string,
  defaultBaseUrl: string,
  wireProtocol: string,
  editable: boolean,
) {
  return {
    provider_type: providerType,
    display_name: displayName,
    docs_url: "https://docs.example.com",
    console_url: "https://console.example.com",
    usage_url: "https://usage.example.com",
    default_base_url: defaultBaseUrl,
    wire_protocol: wireProtocol,
    custom_base_url_supported: true,
    custom_base_url_enabled: editable,
    base_url_editable: editable,
  };
}

describe("ExpertsPage editable vendor Base URL", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    hookState.catalog = providerCatalog(true);
    hookState.experts = [];
    hookState.baiduOCR = {
      credential_id: null,
      provider_type: "baidu_unlimited_ocr",
      credentials_configured: false,
      verification_status: "not_configured",
      last_checked_at: null,
      verification_error_code: null,
    };
    addExpert.mockResolvedValue({ status: "success", provider_id: "pc-test" });
    saveBaiduOCRCredentials.mockResolvedValue({ status: "success" });
    verifyBaiduOCRCredentials.mockResolvedValue({ status: "credentials_verified" });
    deleteBaiduOCRCredentials.mockResolvedValue({ status: "success" });
  });

  it("uses the official DeepSeek URL by default and saves USTC without extra gates", async () => {
    const user = userEvent.setup();
    renderPage();
    expect(screen.getByText("Google Gemini")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "添加模型配置" }));
    expect(screen.getByLabelText("服务商")).toHaveDisplayValue("DeepSeek");
    const baseUrl = screen.getByLabelText(/API Base URL/);
    expect(baseUrl).toHaveValue("https://api.deepseek.com/v1");
    expect(screen.queryByRole("option", { name: /自定义 OpenAI-compatible/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();

    await user.clear(screen.getByLabelText("模型名称"));
    await user.type(screen.getByLabelText("模型名称"), "school-model");
    await user.type(screen.getByLabelText(/^API key/), "sk-secret-value");
    await user.clear(baseUrl);
    await user.type(baseUrl, "https://api.llm.ustc.edu.cn/v1");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(addExpert).toHaveBeenCalledTimes(1));
    expect(addExpert).toHaveBeenCalledWith(expect.objectContaining({
      provider_type: "deepseek",
      model: "school-model",
      base_url: "https://api.llm.ustc.edu.cn/v1",
    }));
    expect(addExpert.mock.calls[0]?.[0]).not.toHaveProperty("wire_protocol");
    expect(screen.queryByDisplayValue("sk-secret-value")).not.toBeInTheDocument();
    expect(window.localStorage.getItem("sk-secret-value")).toBeNull();
  });

  it("uses catalog defaults and keeps Base URL editable for native-protocol providers", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "添加模型配置" }));
    await user.selectOptions(screen.getByLabelText("服务商"), "anthropic");
    expect(screen.getByLabelText(/API Base URL/)).toHaveValue("https://api.anthropic.com");
    expect(screen.getByLabelText(/API Base URL/)).toBeEnabled();

    await user.selectOptions(screen.getByLabelText("服务商"), "gemini");
    expect(screen.getByLabelText(/API Base URL/)).toHaveValue(
      "https://generativelanguage.googleapis.com",
    );
    expect(screen.getByLabelText(/API Base URL/)).toBeEnabled();
  });

  it("sends an advanced protocol override only for a cross-protocol relay", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "添加模型配置" }));
    await user.selectOptions(screen.getByLabelText("服务商"), "anthropic");
    await user.clear(screen.getByLabelText("模型名称"));
    await user.type(screen.getByLabelText("模型名称"), "claude-relay-model");
    await user.type(screen.getByLabelText(/^API key/), "sk-relay-secret");
    await user.clear(screen.getByLabelText(/API Base URL/));
    await user.type(screen.getByLabelText(/API Base URL/), "https://relay.example.com/v1");
    await user.click(screen.getByText("高级设置"));
    await user.selectOptions(screen.getByLabelText(/API 协议/), "openai_chat_completions");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(addExpert).toHaveBeenCalledTimes(1));
    expect(addExpert).toHaveBeenCalledWith(expect.objectContaining({
      provider_type: "anthropic",
      base_url: "https://relay.example.com/v1",
      wire_protocol: "openai_chat_completions",
    }));
  });

  it("renders backend-resolved unique names for otherwise identical configurations", () => {
    hookState.experts = [
      {
        provider_id: "one",
        provider_type: "deepseek",
        model: "school-model",
        enabled: true,
        display_name: "DeepSeek · school-model · api.deepseek.com/v1 · OpenAI Chat Completions",
        resolved_display_name: "DeepSeek · school-model · api.deepseek.com/v1 · OpenAI Chat Completions",
        endpoint_descriptor: "api.deepseek.com/v1 · OpenAI Chat Completions",
        max_concurrent: 5,
        rpm: 0,
      },
      {
        provider_id: "two",
        provider_type: "deepseek",
        model: "school-model",
        enabled: true,
        display_name: "DeepSeek · school-model · api.llm.ustc.edu.cn/v1 · OpenAI Chat Completions",
        resolved_display_name: "DeepSeek · school-model · api.llm.ustc.edu.cn/v1 · OpenAI Chat Completions",
        endpoint_descriptor: "api.llm.ustc.edu.cn/v1 · OpenAI Chat Completions",
        max_concurrent: 5,
        rpm: 0,
      },
    ];

    renderPage();

    expect(screen.getAllByText(/api\.deepseek\.com\/v1/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/api\.llm\.ustc\.edu\.cn\/v1/).length).toBeGreaterThan(0);
  });

  it("keeps the official URL read-only when the production gate is off", async () => {
    hookState.catalog = providerCatalog(false);
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "添加模型配置" }));

    expect(screen.getByLabelText(/API Base URL/)).toBeDisabled();
    expect(screen.getByLabelText(/API Base URL/)).toHaveValue("https://api.deepseek.com/v1");
  });

  it("saves Baidu OCR AK/SK without echoing or persisting either secret", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.type(screen.getByLabelText("AK"), "fake-baidu-ak");
    await user.type(screen.getByLabelText("SK"), "fake-baidu-sk");
    await user.click(screen.getByRole("button", { name: "保存凭据" }));

    await waitFor(() => expect(saveBaiduOCRCredentials).toHaveBeenCalledWith({
      api_key: "fake-baidu-ak",
      secret_key: "fake-baidu-sk",
    }));
    expect(screen.getByLabelText("AK")).toHaveValue("");
    expect(screen.getByLabelText("SK")).toHaveValue("");
    expect(screen.queryByDisplayValue("fake-baidu-ak")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("fake-baidu-sk")).not.toBeInTheDocument();
    expect(window.localStorage.getItem("fake-baidu-ak")).toBeNull();
    expect(window.localStorage.getItem("fake-baidu-sk")).toBeNull();
  });

  it("shows only Baidu OCR metadata and supports verify and confirmed delete", async () => {
    hookState.baiduOCR = {
      credential_id: "ocr-record-1",
      provider_type: "baidu_unlimited_ocr",
      credentials_configured: true,
      verification_status: "credentials_verified",
      last_checked_at: "2026-08-24T12:00:00Z",
      verification_error_code: null,
    };
    const user = userEvent.setup();
    renderPage();

    expect(screen.getByText("ocr-record-1")).toBeInTheDocument();
    expect(screen.getByText("AK/SK 已验证")).toBeInTheDocument();
    expect(screen.getByLabelText("替换 AK")).toHaveValue("");
    expect(screen.getByLabelText("替换 SK")).toHaveValue("");

    await user.click(screen.getByRole("button", { name: "验证 AK/SK" }));
    await waitFor(() => expect(verifyBaiduOCRCredentials).toHaveBeenCalledWith("ocr-record-1"));

    await user.click(screen.getByRole("button", { name: "删除凭据" }));
    await user.click(screen.getByRole("button", { name: "再次点击确认删除" }));
    await waitFor(() => expect(deleteBaiduOCRCredentials).toHaveBeenCalledWith("ocr-record-1"));
  });
});
