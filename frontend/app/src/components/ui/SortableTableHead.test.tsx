import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { SortableTableHead, sortColumnRows, toggleColumnSort, useColumnSort } from "./SortableTableHead";

const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={["/?sort=max_score_asc&page=3&status=missing"]}>{children}</MemoryRouter>;
function useHarness() {
  const [params, setParams] = useSearchParams();
  const sort = useColumnSort(["max_score", "number"], { key: "max_score", direction: "asc" }, vi.fn());
  return { ...sort, params, setParams };
}
describe("table header ordering", () => {
  it("toggles the effective natural-language order, not a separate stale default", () => {
    const { result } = renderHook(() => useHarness(), { wrapper });
    expect(result.current.current).toEqual({ key: "max_score", direction: "asc" });
    act(() => result.current.toggle("max_score"));
    expect(result.current.current).toEqual({ key: "max_score", direction: "desc" });
    act(() => result.current.toggle("max_score"));
    expect(result.current.current).toEqual({ key: "max_score", direction: "asc" });
  });
  it("resets pagination but preserves the active filter when a header changes", () => {
    const { result } = renderHook(() => useHarness(), { wrapper });
    act(() => result.current.toggle("number"));
    expect(result.current.current).toEqual({ key: "number", direction: "asc" });
    expect(result.current.params.get("page")).toBeNull();
    expect(result.current.params.get("status")).toBe("missing");
  });
  it("honors a fresh semantic order after its controller removes the header override", () => {
    const { result } = renderHook(() => useHarness(), { wrapper });
    act(() => result.current.toggle("number"));
    act(() => result.current.setParams("sort=max_score_asc&status=missing"));
    expect(result.current.current).toEqual({ key: "max_score", direction: "asc" });
  });
  it("ignores malformed or unsupported column keys in a URL", () => {
    const { result } = renderHook(() => useHarness(), { wrapper });
    act(() => result.current.setParams('column_sort=not-json'));
    expect(result.current.current?.key).toBe("max_score");
    act(() => result.current.setParams({ column_sort: JSON.stringify(["secret", "desc"]) }));
    expect(result.current.current?.key).toBe("max_score");
  });
  it("keeps the full dataset immutable and missing values last in both directions", () => {
    const rows = [{ id: "missing", value: null }, { id: "high", value: 10 }, { id: "low", value: 2 }];
    for (const direction of ["asc", "desc"] as const) {
      const sorted = sortColumnRows(rows, { key: "value", direction }, (row) => row.value);
      expect(sorted.at(-1)?.id).toBe("missing");
      expect(sorted[0].id).toBe(direction === "asc" ? "low" : "high");
    }
    expect(rows[0].id).toBe("missing");
  });
  it("uses natural numbering so Q2 precedes Q10", () => {
    expect(sortColumnRows(["Q10", "Q2", "Q1"], { key: "number", direction: "asc" }, (row) => row)).toEqual(["Q1", "Q2", "Q10"]);
    expect(toggleColumnSort({ key: "number", direction: "desc" }, "name")).toEqual({ key: "name", direction: "asc" });
  });
  it("exposes a keyboard button and the current direction without nesting a navigation link", () => {
    const onSort = vi.fn();
    render(<table><thead><tr><SortableTableHead direction="asc" onSort={onSort} locale="zh-CN" secondary={<a href="/details">详情</a>}>满分</SortableTableHead></tr></thead></table>);
    expect(screen.getByRole("columnheader", { name: "满分" })).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(screen.getByRole("button", { name: "满分，当前升序；点击降序" }));
    expect(onSort).toHaveBeenCalledOnce();
    expect(screen.getByRole("link").closest("button")).toBeNull();
  });
});
