import ReactMarkdown, { type Components } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import { cn } from "@/lib/cn";

const markdownMathComponents: Components = {
  p: ({ children }) => <p className="mb-2 whitespace-pre-wrap last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="mb-2 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>,
  li: ({ children }) => <li>{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-foreground">{children}</strong>,
  code: ({ children }) => <code className="rounded bg-muted px-1 py-0.5 text-xs">{children}</code>,
};

export function MarkdownMath({ children, className }: { children?: string | null; className?: string }) {
  const content = normalizeMarkdownMathInput(children ?? "").trim();
  if (!content) {
    return null;
  }

  return (
    <div
      className={cn(
        "min-w-0 break-words text-sm leading-6 [&_.katex-display]:overflow-x-auto [&_.katex-display]:overflow-y-hidden",
        className,
      )}
    >
      <ReactMarkdown
        components={markdownMathComponents}
        remarkPlugins={[remarkMath]}
        rehypePlugins={[[rehypeKatex, { strict: false, throwOnError: false }]]}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

const DOUBLE_ESCAPED_LATEX = /\\\\(?=(?:int|sum|prod|lim|frac|dfrac|tfrac|sqrt|ker|rank|sin|cos|tan|log|ln|exp|det|max|min|alpha|beta|gamma|delta|epsilon|theta|lambda|mu|nu|pi|rho|sigma|tau|phi|psi|omega|infty|partial|nabla|ell|lVert|rVert|Vert|text|mathrm|mathbf|mathit|operatorname|left|right|begin|end|times|cdot|div|pm|mp|leq?|geq?|neq|approx|equiv|in|notin|subseteq|supseteq|to|mapsto|circ)(?![A-Za-z]))/g;
const OVERESCAPED_NEWLINE = /\\{1,2}n(?=(?:\\{1,2}n|[\s\-*#>0-9(A-Z]|[\u3400-\u9fff]|$))/g;
const OVERESCAPED_CODE_NEWLINE = /\\{1,2}n(?=(?:(?:async\s+)?def|class|from|import|return|if|elif|else|for|while|function|const|let|var|public|private|protected|#include)\b)/g;

/** Presentation fallback for already-persisted over-escaped model prose. */
export function normalizeMarkdownMathInput(value: string): string {
  return value
    .replace(/\\{1,2}r\\{1,2}n/g, "\n")
    .replace(OVERESCAPED_NEWLINE, "\n")
    .replace(OVERESCAPED_CODE_NEWLINE, "\n")
    .replace(DOUBLE_ESCAPED_LATEX, "\\")
    .replace(/\\\\(?=[\[\]()])/g, "\\")
    .replace(/(?<!\$)\${3,}(?!\$)/g, () => "$$")
    .replace(/\n{3,}/g, "\n\n");
}
