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

it("repairs a mismatched display opener without swallowing later proof paragraphs", () => {
  const { container } = render(<MarkdownMath>{String.raw`Proof:
$$ (x * y) * z = x + y + z - [x + y + z]$（说明 $x * (y * z)$）。

**Identity:** $0 * x = x$.

**Inverse:** $x+y=1$.`}</MarkdownMath>);
  expect(container.querySelector(".katex-error")).toBeNull();
  expect(screen.getByText("Identity:").tagName).toBe("STRONG");
  expect(screen.getByText("Inverse:").tagName).toBe("STRONG");
  expect(container.querySelectorAll(".katex")).toHaveLength(4);
});

it("renders standard TeX delimiters while keeping code examples literal", () => {
  const { container } = render(<MarkdownMath>{String.raw`Inline \(x^2=1\).

\[\frac{1}{2}\]

Code: ` + '`\\(x=1\\)`'}</MarkdownMath>);
  expect(container.querySelectorAll(".katex")).toHaveLength(2);
  expect(container.querySelector(".katex-error")).toBeNull();
  expect(container.querySelector("code")?.textContent).toBe(String.raw`\(x=1\)`);
});

it.each([String.raw`\unknowncommand{x}`, String.raw`\frac{1}{`, String.raw`\left(x`, String.raw`\begin{matrix}a\end{cases}`])(
  "keeps invalid or unsupported TeX readable without a blocking warning: %s", tex => {
  const { container } = render(<MarkdownMath>{`Before $${tex}$.\n\nAfter the formula.`}</MarkdownMath>);
  expect(screen.getByText("After the formula.")).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(container.textContent).toContain(tex);
  const fallback = container.querySelector<HTMLElement>(".katex-error, .katex [style*='color']");
  expect(fallback).not.toBeNull();
  expect(fallback?.style.color).toBe("inherit");
});
