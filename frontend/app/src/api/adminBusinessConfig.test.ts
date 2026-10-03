import { afterEach, expect, it, vi } from "vitest";
import { apiClient } from "./client";
import { getBusinessConfig, saveBusinessConfig } from "./adminBusinessConfig";

afterEach(() => vi.restoreAllMocks());
it("uses private endpoints and forwards optimistic versions and the caller's retry key", async () => {
  const get = vi.spyOn(apiClient, "get").mockResolvedValue({ data: { version: 3 } });
  const patch = vi.spyOn(apiClient, "patch").mockResolvedValue({ data: { version: 4 } });
  expect(await getBusinessConfig()).toEqual({ version: 3 });
  expect(get).toHaveBeenCalledWith("/admin/business-config", { signal: undefined });
  const payload = { expected_version: 3, expected_global_version: 2, changes: { knowledge_storage_quota_bytes: null }, reason: "inherit" };
  await saveBusinessConfig(payload, "retry-key", "owner/one");
  expect(patch).toHaveBeenCalledWith("/admin/business-config/users/owner%2Fone", payload, { headers: { "Idempotency-Key": "retry-key" } });
});
