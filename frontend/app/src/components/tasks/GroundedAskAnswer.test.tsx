import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GroundedAskAnswer } from "./GroundedAskAnswer";
import type { GroundedAskExecution } from "@/types";
it("shows an empty result without candidate selection or a clarification dialogue",()=>{
 const execution:GroundedAskExecution={recognized:true,kind:"table",explanation:"查询完成",data:{columns:["student_id"],rows:[]},selection:null,chart:null,candidates:[{kind:"student",text:"Alex",task_id:"task",student_id:"001",student_name:"Alex Chen"}]};
 render(<GroundedAskAnswer execution={execution} locale="zh-CN" />);
 expect(screen.getByText("没有匹配记录。")).toBeInTheDocument();
 expect(screen.getAllByRole("button")).toHaveLength(1);
 expect(screen.getByRole("button", { name: /student_id/ })).toBeInTheDocument();
});

function tableExecution(rows: GroundedAskExecution["data"]["rows"]): GroundedAskExecution {
  return { recognized: true, kind: "table", explanation: "查询完成", data: { columns: ["姓名", "分数"], rows }, selection: null, chart: null };
}

function visibleRows() {
  return screen.getAllByRole("row").slice(1).map(row => within(row).getAllByRole("cell").map(cell => cell.textContent));
}

describe("GroundedAskAnswer header sorting", () => {
  it("toggles numeric and natural text order, keeps null last, and preserves the response", () => {
    const execution = tableExecution([["Student 10", 10], ["Student 2", 2], [null, null], ["Student 1", 0]]);
    const originalRows = structuredClone(execution.data.rows);
    render(<GroundedAskAnswer execution={execution} locale="zh-CN" />);

    fireEvent.click(screen.getByRole("button", { name: /分数/ }));
    expect(visibleRows().map(row => row[1])).toEqual(["0", "2", "10", "—"]);
    expect(screen.getByRole("columnheader", { name: "分数" })).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(screen.getByRole("button", { name: /分数/ }));
    expect(visibleRows().map(row => row[1])).toEqual(["10", "2", "0", "—"]);
    expect(screen.getByRole("columnheader", { name: "分数" })).toHaveAttribute("aria-sort", "descending");

    fireEvent.click(screen.getByRole("button", { name: /姓名/ }));
    expect(visibleRows().map(row => row[0])).toEqual(["Student 1", "Student 2", "Student 10", "—"]);
    fireEvent.click(screen.getByRole("button", { name: /姓名/ }));
    expect(visibleRows().map(row => row[0])).toEqual(["Student 10", "Student 2", "Student 1", "—"]);
    expect(execution.data.rows).toEqual(originalRows);
  });

  it("sorts the complete result before pagination and resets page when the header changes", () => {
    const execution = tableExecution(Array.from({ length: 30 }, (_, i) => [`Student ${30 - i}`, 30 - i]));
    render(<GroundedAskAnswer execution={execution} locale="zh-CN" />);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(screen.getByText("2/2 · 30")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /分数/ }));
    expect(screen.getByText("1/2 · 30")).toBeInTheDocument();
    expect(visibleRows().map(row => row[1])).toEqual(Array.from({ length: 25 }, (_, i) => String(i + 1)));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(visibleRows().map(row => row[1])).toEqual(["26", "27", "28", "29", "30"]);

    fireEvent.click(screen.getByRole("button", { name: /分数/ }));
    expect(screen.getByText("1/2 · 30")).toBeInTheDocument();
    expect(visibleRows()[0][1]).toBe("30");
  });

  it("restores the new query's server order and first page when execution changes", () => {
    const execution = tableExecution(Array.from({ length: 30 }, (_, i) => [`Student ${i}`, i]));
    const { rerender } = render(<GroundedAskAnswer execution={execution} locale="zh-CN" />);
    fireEvent.click(screen.getByRole("button", { name: /分数/ }));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));

    rerender(<GroundedAskAnswer execution={tableExecution([["New A", 8], ["New B", 2]])} locale="zh-CN" />);
    expect(visibleRows()).toEqual([["New A", "8"], ["New B", "2"]]);
    expect(screen.getByRole("columnheader", { name: "分数" })).toHaveAttribute("aria-sort", "none");
    expect(screen.queryByRole("button", { name: "下一页" })).not.toBeInTheDocument();
  });
});
