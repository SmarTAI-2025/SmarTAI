import type { ProblemSourcePreflightInput } from "@/types";

/** Empty fields mean no restriction. Validate before uploading or starting paid work. */
export function parseQuestionRecognitionScope(pagesText: string, targetsText: string): {
  options?: ProblemSourcePreflightInput["recognitionOptions"];
  error?: "pages" | "targets";
} {
  const tokens = (text: string) => text.trim().replace(/\s*[-–—]\s*/g, "-").split(/[,，、;；\s]+/).filter(Boolean);
  const pages = new Set<number>();
  for (const token of tokens(pagesText)) {
    if (!/^\d+(?:-\d+)?$/.test(token)) return { error: "pages" };
    const [first, last = first] = token.split("-").map(Number);
    if (first < 1 || last < first || last > 10000) return { error: "pages" };
    for (let page = first; page <= last; page++) pages.add(page);
  }
  const targets = new Set<string>();
  for (const token of tokens(targetsText)) {
    if (token.includes("-")) {
      const match = /^(\d+(?:\.\d+)*)-(\d+(?:\.\d+)*)$/.exec(token);
      if (!match) return { error: "targets" };
      const left = match[1].split(".");
      const right = match[2].split(".");
      const first = Number(left.pop());
      const last = Number(right.pop());
      if (left.join(".") !== right.join(".") || first < 1 || last < first || last > 10000 || last - first >= 64) return { error: "targets" };
      const prefix = left.length ? `${left.join(".")}.` : "";
      for (let number = first; number <= last; number++) targets.add(`${prefix}${number}`);
    } else {
      if (token.length > 80 || !/^[\p{L}\p{N}_.()（）]+$/u.test(token)) return { error: "targets" };
      targets.add(token);
    }
    if (targets.size > 64) return { error: "targets" };
  }
  return { options: { pages: [...pages].sort((a, b) => a - b), targets: [...targets] } };
}
