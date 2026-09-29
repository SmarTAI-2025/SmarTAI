import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { MarkdownMath } from "./MarkdownMath";

it("preserves source exercise numbers rather than renumbering retrieved text", () => {
  render(<MarkdownMath>{"8. First exercise\n20. Inverse exercise\n20. Continued label\n31. Last exercise"}</MarkdownMath>);
  expect(screen.getByRole("list")).toHaveAttribute("start", "8");
  expect(screen.getAllByRole("listitem").map((item) => item.getAttribute("value"))).toEqual(["8", "20", "20", "31"]);
});

it("does not add ordinal values to unordered source lists", () => {
  render(<MarkdownMath>{"- First\n- Second"}</MarkdownMath>);
  for (const item of screen.getAllByRole("listitem")) expect(item).not.toHaveAttribute("value");
});

it("renders compact multiline math fences without swallowing the next paragraph", () => {
  const { container } = render(<MarkdownMath>{String.raw`Before.
$$\begin{aligned}
x &= 1 \\
y &= 2
\end{aligned}$$

After: $x+y=3$.`}</MarkdownMath>);
  expect(container.querySelector(".katex-display .katex")).not.toBeNull();
  expect(container.querySelector(".katex-error")).toBeNull();
  expect(screen.getByText(/After:/).tagName).toBe("P");
  expect(container.querySelectorAll(".katex")).toHaveLength(2);
});
