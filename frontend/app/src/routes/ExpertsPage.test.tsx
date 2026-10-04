import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ExpertsPage } from "./ExpertsPage";

const addExpert = vi.fn();
const updateExpert = vi.fn();
const verifyImage = vi.fn();
const saveBaiduOCRCredentials = vi.fn();
const verifyBaiduOCRCredentials = vi.fn();
const deleteBaiduOCRCredentials = vi.fn();

const hookState = vi.hoisted(() => ({
  textPending: false,
  imagePending: false,
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
  useVerifyExpertImage: () => ({ isPending: hookState.imagePending, mutateAsync: verifyImage }),
  useExperts: () => ({ data: hookState.experts, isLoading: false, isError: false, isFetching: false, refetch: vi.fn() }),
  useProviderCatalog: () => ({ data: hookState.catalog, isLoading: false, isError: false, refetch: vi.fn() }),
  useAddExpertKey: () => ({ isPending: false, mutateAsync: addExpert }),
  useUpdateExpert: () => ({ isPending: false, mutateAsync: updateExpert }),
  useSelectExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useSetDefaultExpert: () => ({ isPending: false, mutateAsync: vi.fn() }),
  useVerifyExpert: () => ({ isPending: hookState.textPending, mutateAsync: vi.fn() }),
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
    hookState.textPending = false;
    hookState.imagePending = false;
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
    updateExpert.mockResolvedValue({ status: "success", provider_id: "pc-test" });
    verifyImage.mockResolvedValue({ image_capability_status: "passed" });
    saveBaiduOCRCredentials.mockResolvedValue({ status: "success" });
    verifyBaiduOCRCredentials.mockResolvedValue({ status: "credentials_verified" });
    deleteBaiduOCRCredentials.mockResolvedValue({ status: "success" });
  });

  it("uses the verified catalog default for new configurations and the requested provider order", async () => {
    hookState.catalog.find((item) => item.provider_type === "openai")!.default_model = "gpt-6-luna";
    const user = userEvent.setup(); renderPage();
    await user.click(screen.getByRole("button", { name: "添加模型配置" }));
    const dialog = screen.getByRole("dialog");
    const provider = within(dialog).getByRole("combobox", { name: "服务商" });
    expect(within(provider).getAllByRole("option").map((item) => (item as HTMLOptionElement).value))
      .toEqual(["gemini", "openai", "anthropic", "deepseek", "zhipu", "moonshot", "qwen"]);
    await user.selectOptions(provider, "openai");
    expect(within(dialog).getByDisplayValue("gpt-6-luna")).toBeInTheDocument();
    expect(within(dialog).getByDisplayValue("https://api.openai.com/v1")).toBeInTheDocument();
    expect(addExpert).not.toHaveBeenCalled();
  });

  it("orders official links consistently and includes the Baidu OCR console and API documentation", () => {
    renderPage();
    const section = screen.getByRole("heading", { name: "服务商官方入口" }).closest("section")!;
    const labels = [...section.querySelectorAll("span.font-semibold")].map((item) => item.textContent);
    expect(labels).toEqual(["Google Gemini", "OpenAI", "Anthropic", "DeepSeek", "Zhipu AI", "Moonshot (Kimi)", "Qwen (通义千问)", "百度 Unlimited-OCR"]);
    expect(within(section).getByRole("link", { name: "控制台" })).toHaveAttribute("href", "https://console.bce.baidu.com/ai-engine/ocr/overview/index");
    expect([...section.querySelectorAll("a")].some((link) => link.href === "https://ai.baidu.com/ai-doc/OCR/fmr1p39gb")).toBe(true);
  });

  it("only sends an independent image probe after an explicit click on that configuration", async () => {
    hookState.experts = [{ provider_id: "pc-image", provider_type: "qwen", model: "arbitrary-model", enabled: true, rpm: 0, max_concurrent: 1 }];
    const user = userEvent.setup(); renderPage();
    expect(screen.getAllByText("图像测试").length).toBeGreaterThan(0);
    expect(document.querySelector('[data-image-provider="pc-image"]')).toHaveTextContent("未验证");
    expect(verifyImage).not.toHaveBeenCalled();
    expect(screen.getAllByRole("button", { name: "验证（可选）" }).length).toBeGreaterThan(0);
    await user.click(screen.getAllByRole("button", { name: "验证视觉能力" })[0]!);
    expect(verifyImage).toHaveBeenCalledTimes(1);
    expect(verifyImage).toHaveBeenCalledWith("pc-image");
  });

  it.each([
    ["passed", "已通过"], ["unsupported", "明确不支持"], ["inconclusive", "本次未能确认"],
  ])("displays the evidence state %s with its time and reason", (status, label) => {
    hookState.experts = [{ provider_id: "pc-image", provider_type: "moonshot", model: "arbitrary", enabled: true, rpm: 0, max_concurrent: 1, image_capability_status: status, image_checked_at: "2026-10-03T13:50:00Z", image_reason: "image_probe_answer_incorrect" }];
    renderPage();
    expect(document.querySelector('[data-image-provider="pc-image"]')).toHaveTextContent(label);
    expect(document.querySelector('[data-image-provider="pc-image"]')).toHaveTextContent("10/03");
    expect(verifyImage).not.toHaveBeenCalled();
  });

  it("keeps shared configuration read-only while allowing opt-in image verification", async () => {
    hookState.experts = [{ provider_id: "qwen:shared", provider_type: "qwen", model: "shared", enabled: true, editable: false, is_shared: true, rpm: 0, max_concurrent: 1 }];
    const user = userEvent.setup(); renderPage();
    expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
    expect(verifyImage).not.toHaveBeenCalled();
    await user.click(screen.getAllByRole("button", { name: "验证视觉能力" })[0]!);
    expect(verifyImage).toHaveBeenCalledWith("qwen:shared");
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

  it("saves blank RPM without a manual concurrency limit", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "添加模型配置" }));
    expect(screen.getByLabelText(/^RPM/)).toHaveValue(null);
    expect(screen.queryByRole("spinbutton", { name: /并发/ })).not.toBeInTheDocument();
    expect(screen.getByText("自动 · 上限 50")).toBeInTheDocument();

    await user.type(screen.getByLabelText(/^API key/), "sk-test-only");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(addExpert).toHaveBeenCalledTimes(1));
    expect(addExpert.mock.calls[0]?.[0]).toMatchObject({ rpm: 0 });
    expect(addExpert.mock.calls[0]?.[0]).not.toHaveProperty("max_concurrent");
  });

  it("lets the backend derive concurrency when saving RPM 10", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "添加模型配置" }));
    await user.type(screen.getByLabelText(/^API key/), "sk-test-only");
    await user.type(screen.getByLabelText(/^RPM/), "10");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(addExpert).toHaveBeenCalledTimes(1));
    expect(addExpert.mock.calls[0]?.[0]).toMatchObject({ rpm: 10 });
    expect(addExpert.mock.calls[0]?.[0]).not.toHaveProperty("max_concurrent");
  });

  it("updates an existing configuration without resubmitting its legacy concurrency", async () => {
    hookState.experts = [{
      provider_id: "pc-test", provider_type: "deepseek", model: "test-model",
      enabled: true, max_concurrent: 5, rpm: 0,
    }];
    const user = userEvent.setup();
    renderPage();

    expect(screen.getAllByText("并发自动 · 上限 50").length).toBeGreaterThan(0);
    await user.click(screen.getAllByRole("button", { name: "编辑" })[0]!);
    expect(screen.getByLabelText(/^RPM/)).toHaveValue(null);
    await user.type(screen.getByLabelText(/^RPM/), "10");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(updateExpert).toHaveBeenCalledTimes(1));
    const saved = updateExpert.mock.calls[0]?.[0];
    expect(saved).toMatchObject({ providerId: "pc-test", request: { rpm: 10, api_key: null } });
    expect(saved.request).not.toHaveProperty("max_concurrent");
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

  it("keeps a safe verification failure reason visible after the toast is gone", () => {
    hookState.experts = [{
      provider_id: "gemini-test", provider_type: "gemini", model: "test-model",
      enabled: true, verification_status: "failed", max_concurrent: 1, rpm: 15,
      verification_error_code: "expert_verification_region_unsupported",
    }];
    renderPage();
    expect(screen.getAllByText(/服务商不支持当前网络出口所在地区/).length).toBeGreaterThan(0);
    expect(screen.queryByText("expert_verification_region_unsupported")).not.toBeInTheDocument();
  });

  it("does not display an unknown persisted upstream error as raw text", () => {
    hookState.experts = [{
      provider_id: "gemini-test", provider_type: "gemini", model: "test-model",
      enabled: true, verification_status: "failed", max_concurrent: 1, rpm: 15,
      verification_error_code: "private-upstream-body",
    }];
    renderPage();
    expect(screen.queryByText("private-upstream-body")).not.toBeInTheDocument();
    expect(screen.getAllByText("服务商拒绝了验证请求。").length).toBeGreaterThan(0);
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
  it.each(["textPending", "imagePending"] as const)("keeps configuration actions available while %s serializes probes", async (pending) => {
    hookState[pending] = true;
    hookState.experts = [{ provider_id: "pc-busy", provider_type: "gemini", model: "saved-model", enabled: true, rpm: 0, max_concurrent: 1 }];
    const user = userEvent.setup(); renderPage();
    for (const name of ["验证（可选）", "验证视觉能力"]) {
      for (const button of screen.getAllByRole("button", { name })) expect(button).toBeDisabled();
    }
    for (const name of ["编辑", "设为默认", "停用", "删除"]) {
      for (const button of screen.getAllByRole("button", { name })) expect(button).toBeEnabled();
    }
    await user.click(screen.getAllByRole("button", { name: "编辑" })[0]!);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText("模型名称")).toHaveValue("saved-model");
    expect(verifyImage).not.toHaveBeenCalled();
  });


});

vi.mock("@/components/ModelQuotaCard", () => ({ ModelQuotaCard: () => null }));
