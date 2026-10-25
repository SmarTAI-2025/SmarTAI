import { describe, expect, it } from "vitest";
import { normalizeMarkdownMathInput } from "./MarkdownMath";

describe("normalizeMarkdownMathInput", () => {
  it("preserves valid display closers and dollars nested in text commands", () => {
    expect(normalizeMarkdownMathInput("$$x=1\ny=2$$ where x = $1$."))
      .toBe("\n$$\nx=1\ny=2\n$$\n where x = $1$.");
    const nested = String.raw`$$\text{value $x$}$$`;
    expect(normalizeMarkdownMathInput(nested)).toBe(nested);
  });
  it("keeps literal display fences in fenced code unchanged", () => {
    const code = "```text\n$$example\nnext$$\n```";
    expect(normalizeMarkdownMathInput(code)).toBe(code);
  });
  it("repairs double-escaped model prose already stored in a task", () => {
    expect(normalizeMarkdownMathInput(
      String.raw`1. Substitute.\\n\\n2. Compute $$$\\frac{1}{2}\\left(e-1\\right)$$$.`,
    )).toBe("1. Substitute.\n\n2. Compute $$\\frac{1}{2}\\left(e-1\\right)$$.");
  });

  it("preserves legitimate nu and nabla commands", () => {
    expect(normalizeMarkdownMathInput(String.raw`$\nu$ and $\nabla f$`)).toBe(String.raw`$\nu$ and $\nabla f$`);
  });

  it("repairs overescaped group notation from persisted candidates", () => {
    expect(normalizeMarkdownMathInput(String.raw`$G=\\langle r,s\\rangle$, $x \\star y$, $r,\\dots,r^n$.`))
      .toBe(String.raw`$G=\langle r,s\rangle$, $x \star y$, $r,\dots,r^n$.`);
  });

  it("turns escaped separators before lowercase code into real line breaks", () => {
    expect(normalizeMarkdownMathInput(
      String.raw`Required signature:\ndef stable_softmax(xs):\n    return []`,
    )).toBe("Required signature:\ndef stable_softmax(xs):\n    return []");
  });
});
