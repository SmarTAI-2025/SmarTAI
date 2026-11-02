import type { Root, RootContent } from "hast";
import katex from "katex";

// A deliberately finite subset: never discard symbols/text inserted between
// columns, nested commands, or unsupported alignment declarations.
const ARRAY_COLUMNS = /(?<!\\)(\\begin\s*\{array\}\s*(?:\[[tcb]\]\s*)?)\{((?:[lcr|:\s]|@\{[^{}]*\})+)\}/g;
const SPACING_ONLY = /^(?:\s|\\(?:quad|qquad|enspace|thinspace|medspace|thickspace)(?![A-Za-z])|\\[,;:! ])*$/;
const CUSTOM_OR_LITERAL_TEX = /\\(?:[egx]?def|let|(?:re)?newcommand|providecommand|verb\*?|string|noexpand|detokenize)(?![A-Za-z])/;

function compatibleArraySpacing(tex: string, displayMode: boolean): string {
  if (!tex.includes("@{") || CUSTOM_OR_LITERAL_TEX.test(tex)) return tex;
  const candidate = tex.replace(ARRAY_COLUMNS, (original, begin: string, columns: string) => {
    const insertions = [...columns.matchAll(/@\{([^{}]*)\}/g)];
    if (!insertions.length || insertions.some(match => !SPACING_ONLY.test(match[1]))) return original;
    const plain = columns.replace(/@\{[^{}]*\}/g, "");
    return /[lcr]/.test(plain) ? `${begin}{${plain}}` : original;
  });
  if (candidate === tex) return tex;
  const options = { displayMode, strict: false, throwOnError: true } as const;
  try {
    katex.renderToString(tex, options);
    return tex; // Prefer the original if this renderer supports it.
  } catch {
    try {
      katex.renderToString(candidate, options);
      return candidate;
    } catch {
      return tex; // Keep the existing readable, nonblocking error fallback.
    }
  }
}

/** Presentation only: change math AST text, never the saved/editor source. */
export function rehypeMathCompatibility() {
  return (tree: Root) => {
    function visit(node: Root | RootContent) {
      if (node.type === "element" && node.tagName === "code") {
        const classes = node.properties.className;
        const text = node.children.length === 1 ? node.children[0] : undefined;
        if (Array.isArray(classes) && text?.type === "text"
          && classes.some(name => name === "math-inline" || name === "math-display" || name === "language-math")) {
          text.value = compatibleArraySpacing(text.value, !classes.includes("math-inline"));
        }
      }
      if ("children" in node) node.children.forEach(visit);
    }
    visit(tree);
  };
}
