import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { GroundedAskAnswer } from "./GroundedAskAnswer";
import type { GroundedAskExecution } from "@/types";
const execution: GroundedAskExecution = { recognized: true, kind: "students", explanation: "Executed against task data", data: { columns: ["student_id"], rows: [["001"]] }, selection: { kind: "students", ids: ["001"] }, chart: null, sql: "SELECT student_id FROM students", assumptions: ["Internal reasoning"], fingerprint: "private-snapshot" };
it.each(["zh-CN", "en-US"])("keeps accepted query evidence and duplicate tables off screen (%s)", locale => {
  const { container, rerender } = render(<GroundedAskAnswer execution={execution} locale={locale} />);
  expect(container).toBeEmptyDOMElement();
  rerender(<GroundedAskAnswer execution={{...execution, kind: "table", selection: null}} locale={locale} />);
  expect(container).toBeEmptyDOMElement();
});
it.each([["zh-CN", "请缩小范围重试。"], ["en-US", "Narrow the query and retry."]])("retains actionable errors in the selected language (%s)", (locale, text) => {
  render(<GroundedAskAnswer execution={{...execution, recognized:false, kind:"clarification", selection:null, explanation:"请缩小范围重试。 / Narrow the query and retry."}} locale={locale} />);
  expect(screen.getByRole("status")).toHaveTextContent(text);
  expect(screen.queryByText(/SELECT|Internal reasoning/)).not.toBeInTheDocument();
});
