import { beforeEach, describe, expect, it, vi } from "vitest";
import { addExpertKey, updateExpert } from "./experts";

const clientMocks = vi.hoisted(() => ({
  postJSON: vi.fn(),
  putJSON: vi.fn(),
}));

vi.mock("./client", () => ({
  postJSON: clientMocks.postJSON,
  putJSON: clientMocks.putJSON,
  getJSON: vi.fn(),
  deleteJSON: vi.fn(),
}));

describe("automatic model concurrency API", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clientMocks.postJSON.mockResolvedValue({ status: "success" });
    clientMocks.putJSON.mockResolvedValue({ status: "success" });
  });

  it("defaults omitted RPM to zero without injecting a concurrency limit", async () => {
    await addExpertKey({
      provider_type: "deepseek",
      api_key: "sk-test-only",
      model: "test-model",
    });

    expect(clientMocks.postJSON).toHaveBeenCalledWith("/experts/keys", {
      provider_type: "deepseek",
      api_key: "sk-test-only",
      model: "test-model",
      rpm: 0,
    });
  });

  it("passes RPM 10 to the backend on update without a concurrency default", async () => {
    await updateExpert("pc-test", { model: "test-model", rpm: 10 });

    expect(clientMocks.putJSON).toHaveBeenCalledWith("/experts/pc-test", {
      model: "test-model",
      rpm: 10,
    });
  });
});
