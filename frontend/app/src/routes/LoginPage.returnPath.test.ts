import { describe, expect, it } from "vitest";
import { safeReturnPath } from "./LoginPage";

describe("safeReturnPath", () => {
  it("accepts the same-origin Frontier live path from the login query", () => {
    expect(safeReturnPath(null, "?returnTo=%2Ffrontier%2Flive")).toBe("/frontier/live");
  });

  it("rejects cross-origin and recursive auth destinations", () => {
    expect(safeReturnPath(null, "?returnTo=https%3A%2F%2Fevil.example%2Fsteal")).toBe("/");
    expect(safeReturnPath({ from: "/login" })).toBe("/");
    expect(safeReturnPath({ from: "/register" })).toBe("/");
  });
});
