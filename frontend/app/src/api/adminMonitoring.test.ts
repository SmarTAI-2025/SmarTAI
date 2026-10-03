import { describe, expect, it, vi } from "vitest";
import { getAdminMonitoring, queryAdminAdoption } from "./adminMonitoring";
import { getJSON, postJSON } from "./client";

vi.mock("./client", () => ({ getJSON: vi.fn(), postJSON: vi.fn() }));

describe("private observation clients", () => {
  it("uses the dedicated endpoints and propagates cancellation", async () => {
    const signal = new AbortController().signal;
    await getAdminMonitoring(signal);
    await queryAdminAdoption({ start: 100, timezone: "UTC" }, signal);
    expect(getJSON).toHaveBeenCalledWith("/admin/monitoring", { signal });
    expect(postJSON).toHaveBeenCalledWith("/admin/analytics/query", { start: 100, timezone: "UTC" }, { signal });
  });
});
