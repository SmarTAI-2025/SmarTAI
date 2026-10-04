import ReactMarkdown, { type Components } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import { cn } from "@/lib/cn";

const markdownMathComponents: Components = {
  p: ({ children }) => <p className="mb-2 whitespace-pre-wrap last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="mb-2 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>,
  ol: ({ children, start }) => <ol start={start} className="mb-2 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="whitespace-pre-wrap">{children}</li>,
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
        components={{ ...markdownMathComponents, li: ({ node, children }) => {
          // Keep source exercise labels, including skipped or repeated numbers.
          const offset = node?.position?.start.offset;
          const marker = offset == null ? null : /^(\d{1,9})[.)][ \t]/.exec(content.slice(offset));
          return <li className="whitespace-pre-wrap" value={marker ? Number(marker[1]) : undefined}>{children}</li>;
        } }}
        remarkPlugins={[remarkMath]}
        rehypePlugins={[[rehypeKatex, { strict: false, throwOnError: false }]]}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

const DOUBLE_ESCAPED_LATEX = /\\\\(?=(?:int|sum|prod|lim|frac|dfrac|tfrac|sqrt|ker|rank|sin|cos|tan|log|ln|exp|det|max|min|alpha|beta|gamma|delta|epsilon|theta|lambda|mu|nu|pi|rho|sigma|tau|phi|psi|omega|infty|partial|nabla|ell|lVert|rVert|Vert|text|mathrm|mathbf|mathit|operatorname|left|right|begin|end|times|cdot|div|pm|mp|leq?|geq?|neq|approx|equiv|in|notin|subseteq|supseteq|to|mapsto|circ|star|langle|rangle|dots|ldots|cdots|iota|mid|forall|exists)(?![A-Za-z]))/g;
const OVERESCAPED_NEWLINE = /\\{1,2}n(?!(?:u|abla|eq|e|otin|i|exists|eg|ot|ewcommand|ewline|ewpage|olimits|onumber)\b)/g;

function normalizeDisplayMathFences(value: string): string {
  // remark-math treats text after an opening $$ line as metadata, not math.
  return value.replace(/(`{3,}[^\n]*\n[\s\S]*?`{3,}|~{3,}[^\n]*\n[\s\S]*?~{3,}|`[^`\n]*`)|\$\$([\s\S]*?)\$\$/g,
    (match, code: string | undefined, math: string | undefined) =>
      code || !math?.includes("\n") ? match : `\n$$\n${math.trim()}\n$$\n`);
}

/** Presentation fallback for already-persisted over-escaped model prose. */
function normalizeProseNewlines(value: string): string {
  return value.split(/(\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\))/g)
    .map((part, index) => index % 2 ? part : part
      .replace(/\\{1,2}r\\{1,2}n/g, "\n").replace(OVERESCAPED_NEWLINE, "\n")).join("");
}

export function normalizeMarkdownMathInput(value: string): string {
  const parts = value.split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|(?<![A-Za-z0-9_])[A-Za-z]:\\[^\s]*)/g);
  return normalizeDisplayMathFences(parts.map((part, index) => index % 2 ? part : normalizeProseNewlines(part)
    .replace(DOUBLE_ESCAPED_LATEX, "\\")
    .replace(/\\\\(?=[\[\]()])/g, "\\")
    .replace(/(?<!\$)\${3,}(?!\$)/g, () => "$$")
    .replace(/\n{3,}/g, "\n\n")).join(""));
}
