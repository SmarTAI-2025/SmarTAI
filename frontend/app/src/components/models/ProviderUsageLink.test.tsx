import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ProviderUsageLink, selectedProviderUsageUrl } from "./ProviderUsageLink";
import type { ProviderCatalogItem } from "@/types";

const catalog: ProviderCatalogItem[] = [
  { provider_type: "openai", display_name: "OpenAI", default_base_url: "https://api.openai.com/v1", usage_url: "https://platform.openai.com/usage", wire_protocol: "openai_chat_completions", custom_base_url_supported: true, custom_base_url_enabled: true },
  { provider_type: "qwen", display_name: "Qwen", default_base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", usage_url: "https://bailian.console.aliyun.com/", wire_protocol: "openai_chat_completions", custom_base_url_supported: true, custom_base_url_enabled: true },
];
const providers = [
  { provider_id: "a", provider_type: "openai", model: "luna" },
  { provider_id: "b", provider_type: "qwen", model: "qwen" },
  { provider_id: "c", provider_type: "openai", model: "another-model" },
];
vi.mock("@/api/hooks/experts", () => ({ useProviderCatalog: () => ({ data: catalog }) }));

it("shows one catalog link and changes it with the selected model", () => {
  const { rerender } = render(<ProviderUsageLink locale="zh-CN" context={{ providerIds: ["a"], providers }} />);
  expect(screen.getAllByRole("link")).toHaveLength(1);
  expect(screen.getByRole("link", { name: "查看用量" })).toHaveAttribute("href", catalog[0].usage_url);
  rerender(<ProviderUsageLink locale="en-US" context={{ providerIds: ["b"], providers }} />);
  expect(screen.getAllByRole("link")).toHaveLength(1);
  expect(screen.getByRole("link", { name: "View usage" })).toHaveAttribute("href", catalog[1].usage_url);
});

it.each([
  { ...providers[0], base_url: "https://relay.example/v1" },
  { ...providers[0], base_url: "https://api.openai.com.relay.example/v1" },
  { ...providers[0], base_url: "http://api.openai.com/v1" },
  { ...providers[0], is_shared: true },
  { ...providers[0], provider_type: "unknown" },
])("does not send relayed, shared or unknown models to an unrelated console", provider => {
  render(<ProviderUsageLink locale="zh-CN" context={{ providerIds: ["a"], providers: [provider] }} />);
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
});

it("only links multi-model grading when every selection has the same known console", () => {
  expect(selectedProviderUsageUrl({ providerIds: ["a", "c"], providers }, catalog)).toBe(catalog[0].usage_url);
  expect(selectedProviderUsageUrl({ providerIds: ["a", "b"], providers }, catalog)).toBeNull();
  expect(selectedProviderUsageUrl({ providerIds: ["a", "missing"], providers }, catalog)).toBeNull();
  expect(selectedProviderUsageUrl({ providerIds: [], providers }, catalog)).toBeNull();
});
