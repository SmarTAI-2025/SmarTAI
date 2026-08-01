import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { detectCodeLanguage, SyntaxHighlightedCode } from "./SyntaxHighlightedCode";

describe("SyntaxHighlightedCode", () => {
  it("infers Python, renders highlighted tokens, and exposes reviewed English language copy", () => {
    const code = [
      "def fibonacci(n):",
      "    if n <= 0: return 0",
      "    return fibonacci(n - 1) + fibonacci(n - 2)",
    ].join("\n");
    const { container } = render(<SyntaxHighlightedCode code={code} locale="en-US" />);

    expect(container.querySelector("[data-code-language='python']")).toBeInTheDocument();
    expect(screen.getByLabelText("Code language: Python")).toHaveTextContent("Python");
    expect(container.querySelector("pre")).toHaveTextContent("def fibonacci(n):");
    expect(Array.from(container.querySelectorAll("[data-code-token='keyword']")).map((node) => node.textContent))
      .toEqual(expect.arrayContaining(["def", "if", "return"]));
    expect(container.querySelectorAll("[data-code-token='function']").length).toBeGreaterThan(0);
  });

  it("uses fenced or question language hints before falling back to code detection", () => {
    expect(detectCodeLanguage("```cpp\n#include <iostream>\nint main() {}\n```", "Write it in Python"))
      .toBe("cpp");
    expect(detectCodeLanguage("function solve(value) { return value; }", "Implement the solution in TypeScript."))
      .toBe("typescript");
    expect(detectCodeLanguage("public static void main(String[] args) {}"))
      .toBe("java");
  });
});
