import { render, screen } from "@testing-library/react";
import katex from "katex";
import { expect, it, vi } from "vitest";
import { MarkdownMath, normalizeMarkdownMathInput } from "./MarkdownMath";

const permutation = String.raw`\begin{array}{r@{\quad}r@{\quad}r@{\quad}r@{\quad}r}
1 \mapsto 13 & 2 \mapsto 2 & 3 \mapsto 15 & 4 \mapsto 14 & 5 \mapsto 10 \\
6 \mapsto 6 & 7 \mapsto 12 & 8 \mapsto 3 & 9 \mapsto 4 & 10 \mapsto 1 \\
11 \mapsto 7 & 12 \mapsto 9 & 13 \mapsto 5 & 14 \mapsto 11 & 15 \mapsto 8
\end{array}`;

it("renders the reported permutation with default spacing while preserving every cell", () => {
  expect(() => katex.renderToString(permutation)).toThrow("Unknown column alignment: @");
  const source = `Before.\n$$\n${permutation}\n$$\n\nAfter.`;
  const { container } = render(<MarkdownMath>{source}</MarkdownMath>);
  expect(container.querySelector(".katex-error")).toBeNull();
  expect(container.querySelector(".katex-display .katex")).not.toBeNull();
  expect(container.querySelector("annotation")?.textContent).toBe(permutation.replaceAll(String.raw`@{\quad}`, ""));
  expect(container.querySelectorAll("mtable mtr")).toHaveLength(3);
  for (const row of container.querySelectorAll("mtable mtr")) expect(row.querySelectorAll("mtd")).toHaveLength(5);
  expect(screen.getByText("After.")).toBeVisible();
  // Display repair must not change the input used by editing/saving.
  expect(normalizeMarkdownMathInput(source)).toContain(permutation);
});

it.each([String.raw`\quad`, String.raw`\qquad`, String.raw`\,`, String.raw`\;`, "", " "])(
  "recovers only spacing insertions in array columns: %s", spacing => {
    const tex = String.raw`\begin{array}{l@{${spacing}}|r}a&b\end{array}`;
    const { container } = render(<MarkdownMath>{`$${tex}$`}</MarkdownMath>);
    expect(container.querySelector(".katex-error")).toBeNull();
    expect(container.querySelector("annotation")?.textContent).toBe(String.raw`\begin{array}{l|r}a&b\end{array}`);
  },
);

it.each([
  String.raw`\begin{array}{r@{=}r}a&b\end{array}`,
  String.raw`\begin{array}{r@{\text{if}}r}a&b\end{array}`,
  String.raw`\begin{array}{r@{\hspace{1em}}r}a&b\end{array}`,
  String.raw`\begin{array}{r@{\quad}r}a&\unknowncommand\end{array}`,
  String.raw`\def\quad{=}\begin{array}{r@{\quad}r}a&b\end{array}`,
  String.raw`\verb|\begin{array}{r@{\quad}r}|+\begin{array}{r@{\quad}r}a&b\end{array}`,
])("keeps unsafe or unsuccessful repairs readable and nonblocking: %s", tex => {
  const { container } = render(<MarkdownMath>{`$${tex}$\n\nNext paragraph.`}</MarkdownMath>);
  expect(container.querySelector(".katex-error")?.textContent).toBe(tex);
  expect(screen.getByText("Next paragraph.")).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("leaves literal code and prose containing array examples unchanged", () => {
  const { container } = render(<MarkdownMath>{"```tex\n" + permutation + "\n```\n\n`" + permutation.split("\n")[0] + "`"}</MarkdownMath>);
  expect(container.querySelector(".katex")).toBeNull();
  expect(container.querySelector("pre code")?.textContent).toBe(permutation + "\n");
});

it("preserves ordinary supported math without adding validation renders", () => {
  const tex = String.raw`\begin{array}{rr}a&b\end{array}`;
  const renderMath = vi.spyOn(katex, "renderToString");
  try {
    const { container } = render(<MarkdownMath>{`$${tex}$`}</MarkdownMath>);
    expect(container.querySelector("annotation")?.textContent).toBe(tex);
    expect(renderMath).toHaveBeenCalledTimes(1);
  } finally {
    renderMath.mockRestore();
  }
});
