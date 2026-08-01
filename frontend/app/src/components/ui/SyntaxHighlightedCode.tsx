import { memo, useMemo, type ReactNode } from "react";

export type CodeLanguage =
  | "python"
  | "javascript"
  | "typescript"
  | "java"
  | "cpp"
  | "c"
  | "csharp"
  | "go"
  | "rust"
  | "kotlin"
  | "swift"
  | "ruby"
  | "php"
  | "sql"
  | "shell"
  | "json"
  | "html"
  | "css"
  | "plaintext";

type TokenKind = "plain" | "keyword" | "type" | "string" | "comment" | "number" | "function" | "operator";

interface CodeToken {
  kind: TokenKind;
  value: string;
}

interface LanguageSyntax {
  keywords: ReadonlySet<string>;
  types?: ReadonlySet<string>;
  lineComments?: readonly string[];
  blockComments?: readonly [string, string][];
  quotes?: readonly string[];
}

const EMPTY_WORDS = new Set<string>();
const COMMON_LITERALS = new Set(["true", "false", "null", "none", "nil", "undefined"]);

const LANGUAGE_LABELS: Record<CodeLanguage, string> = {
  python: "Python",
  javascript: "JavaScript",
  typescript: "TypeScript",
  java: "Java",
  cpp: "C++",
  c: "C",
  csharp: "C#",
  go: "Go",
  rust: "Rust",
  kotlin: "Kotlin",
  swift: "Swift",
  ruby: "Ruby",
  php: "PHP",
  sql: "SQL",
  shell: "Shell",
  json: "JSON",
  html: "HTML",
  css: "CSS",
  plaintext: "Plain Text",
};

const SYNTAX: Record<CodeLanguage, LanguageSyntax> = {
  python: syntax(
    "and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case",
    "bool bytes complex dict float frozenset int list object range set str tuple type",
    ["#"],
    [],
    ["'", "\""],
  ),
  javascript: syntax(
    "async await break case catch class const continue debugger default delete do else export extends finally for from function get if import in instanceof let new of return set static super switch this throw try typeof var void while with yield",
    "array bigint boolean date error map math number object promise regexp set string symbol weakmap weakset",
    ["//"],
    [["/*", "*/"]],
    ["'", "\"", "`"],
  ),
  typescript: syntax(
    "abstract as asserts async await break case catch class const constructor continue declare default delete do else enum export extends finally for from function get if implements import in infer instanceof interface is keyof let module namespace never new of override private protected public readonly require return satisfies set static super switch this throw try type typeof undefined unique unknown var void while with yield",
    "any array bigint boolean date error map never number object promise record set string symbol tuple unknown void",
    ["//"],
    [["/*", "*/"]],
    ["'", "\"", "`"],
  ),
  java: syntax(
    "abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for goto if implements import instanceof int interface long native new package private protected public return short static strictfp super switch synchronized this throw throws transient try void volatile while",
    "boolean byte char double float int long short string object integer",
    ["//"],
    [["/*", "*/"]],
    ["'", "\""],
  ),
  cpp: syntax(
    "alignas alignof and asm auto bitand bitor break case catch class compl concept const consteval constexpr constinit const_cast continue co_await co_return co_yield decltype default delete do dynamic_cast else enum explicit export extern for friend goto if include inline mutable namespace new noexcept not nullptr operator or private protected public register reinterpret_cast requires return signed sizeof static static_assert static_cast struct switch template this thread_local throw try typedef typeid typename union unsigned using virtual void volatile while xor",
    "bool char double float int long short size_t string vector map set unordered_map",
    ["//"],
    [["/*", "*/"]],
    ["'", "\""],
  ),
  c: syntax(
    "auto break case const continue default do else enum extern for goto if include inline register restrict return signed sizeof static struct switch typedef union unsigned void volatile while",
    "bool char double float int long short size_t",
    ["//"],
    [["/*", "*/"]],
    ["'", "\""],
  ),
  csharp: syntax(
    "abstract as async await base bool break byte case catch char checked class const continue decimal default delegate do double else enum event explicit extern false finally fixed float for foreach goto if implicit in int interface internal is lock long namespace new null object operator out override params private protected public readonly ref return sbyte sealed short sizeof stackalloc static string struct switch this throw true try typeof uint ulong unchecked unsafe ushort using virtual void volatile while yield",
    "bool byte char decimal double float int long object sbyte short string uint ulong ushort void",
    ["//"],
    [["/*", "*/"]],
    ["'", "\""],
  ),
  go: syntax(
    "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var",
    "bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune string uint uint8 uint16 uint32 uint64 uintptr",
    ["//"],
    [["/*", "*/"]],
    ["'", "\"", "`"],
  ),
  rust: syntax(
    "as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self static struct super trait true type unsafe use where while",
    "bool char f32 f64 i8 i16 i32 i64 i128 isize str string u8 u16 u32 u64 u128 usize vec",
    ["//"],
    [["/*", "*/"]],
    ["'", "\""],
  ),
  kotlin: syntax(
    "as break class continue do else false for fun if in interface is null object package return super this throw true try typealias typeof val var when while",
    "any boolean byte char double float int long nothing short string unit",
    ["//"],
    [["/*", "*/"]],
    ["'", "\""],
  ),
  swift: syntax(
    "as associatedtype break case catch class continue convenience default defer deinit do dynamic else enum extension fallthrough false fileprivate final for func get guard if import in indirect init inout internal is lazy let mutating nil nonmutating open operator override private protocol public repeat required rethrows return self set static struct subscript super switch throw throws true try typealias unowned var weak where while",
    "any anyobject bool character double float int string uint void",
    ["//"],
    [["/*", "*/"]],
    ["'", "\""],
  ),
  ruby: syntax(
    "alias and begin break case class def defined do else elsif end ensure false for if in module next nil not or redo rescue retry return self super then true undef unless until when while yield",
    "array class float hash integer object string symbol",
    ["#"],
    [],
    ["'", "\""],
  ),
  php: syntax(
    "abstract and array as break callable case catch class clone const continue declare default die do echo else elseif empty enddeclare endfor endforeach endif endswitch endwhile eval exit extends final finally fn for foreach function global goto if implements include include_once instanceof insteadof interface isset list match namespace new or print private protected public readonly require require_once return static switch throw trait try unset use var while xor yield",
    "array bool float int iterable mixed object string void",
    ["//", "#"],
    [["/*", "*/"]],
    ["'", "\"", "`"],
  ),
  sql: syntax(
    "add all alter and as asc between by case check column constraint create database default delete desc distinct drop else end exists foreign from full group having in index inner insert into is join key left like limit not null on or order outer primary references right row select set table then union unique update values view when where with",
    "bigint boolean date decimal float int integer numeric real smallint text timestamp varchar",
    ["--"],
    [["/*", "*/"]],
    ["'", "\"", "`"],
  ),
  shell: syntax(
    "case do done elif else esac export fi for function if in local readonly return select then time until while",
    "",
    ["#"],
    [],
    ["'", "\"", "`"],
  ),
  json: syntax("", "", [], [], ["\""]),
  html: syntax(
    "html head body title meta link script style div span main header footer section article nav form label input button table thead tbody tr th td ul ol li p h1 h2 h3 h4 h5 h6 pre code svg",
    "",
    [],
    [["<!--", "-->"]],
    ["'", "\""],
  ),
  css: syntax(
    "important inherit initial revert unset auto none block inline flex grid absolute relative fixed sticky solid dashed transparent currentcolor",
    "",
    ["//"],
    [["/*", "*/"]],
    ["'", "\""],
  ),
  plaintext: syntax("", "", [], [], []),
};

const LANGUAGE_ALIASES: Record<string, CodeLanguage> = {
  py: "python", python: "python",
  js: "javascript", jsx: "javascript", javascript: "javascript", node: "javascript", nodejs: "javascript",
  ts: "typescript", tsx: "typescript", typescript: "typescript",
  java: "java",
  c: "c",
  cc: "cpp", cpp: "cpp", "c++": "cpp",
  cs: "csharp", "c#": "csharp", csharp: "csharp",
  go: "go", golang: "go",
  rs: "rust", rust: "rust",
  kt: "kotlin", kotlin: "kotlin",
  swift: "swift",
  rb: "ruby", ruby: "ruby",
  php: "php",
  sql: "sql",
  bash: "shell", sh: "shell", shell: "shell", zsh: "shell",
  json: "json",
  html: "html", xml: "html",
  css: "css",
  text: "plaintext", plaintext: "plaintext", txt: "plaintext",
};

const HINT_RULES: readonly [CodeLanguage, RegExp][] = [
  ["typescript", /(?:\btypescript\b|\btsx?\b|TypeScript)/iu],
  ["javascript", /(?:\bjavascript\b|\bjsx?\b|\bnode(?:\.js)?\b|JavaScript)/iu],
  ["cpp", /(?:c\+\+|\bcpp\b|C\s*Plus\s*Plus)/iu],
  ["csharp", /(?:c#|\bcsharp\b|C\s*Sharp|\.NET)/iu],
  ["python", /(?:\bpython\b|Python)/iu],
  ["java", /\bjava\b/iu],
  ["go", /(?:\bgolang\b|\bgo\s+(?:language|语言)\b)/iu],
  ["rust", /\brust\b/iu],
  ["kotlin", /\bkotlin\b/iu],
  ["swift", /\bswift\b/iu],
  ["ruby", /\bruby\b/iu],
  ["php", /\bphp\b/iu],
  ["sql", /\bsql\b/iu],
  ["shell", /(?:\bbash\b|\bshell\b|\bzsh\b|Shell\s*脚本)/iu],
  ["html", /\bhtml\b/iu],
  ["css", /\bcss\b/iu],
  ["c", /(?:\bc\s+(?:language|programming|语言|程序设计)\b|使用\s*C\s*(?:语言)?)/iu],
];

const CODE_SCORE_RULES: readonly [CodeLanguage, RegExp, number][] = [
  ["python", /^\s*(?:from\s+\w+\s+import|import\s+\w+|def\s+\w+\s*\(|class\s+\w+\s*[:(])/mu, 5],
  ["python", /\b(?:elif|None|range|len)\s*(?:\(|:)?/u, 2],
  ["typescript", /\b(?:interface|type)\s+\w+|:\s*(?:string|number|boolean)(?:\[\])?|\sas\s+const\b/u, 5],
  ["javascript", /\b(?:const|let|var)\s+\w+|=>|console\.log\s*\(/u, 3],
  ["cpp", /#include\s*<[^>]+>|\bstd::|\b(?:cout|cin)\s*<</u, 6],
  ["cpp", /\bvector\s*</u, 3],
  ["c", /#include\s*<(?:stdio|stdlib|string)\.h>|\b(?:printf|scanf|malloc)\s*\(/u, 5],
  ["java", /\bpublic\s+static\s+void\s+main\b|\bSystem\.out\.|\bimport\s+java\./u, 6],
  ["csharp", /\busing\s+System\s*;|\bConsole\.(?:Write|WriteLine)\s*\(|\bnamespace\s+\w+/u, 6],
  ["go", /^\s*package\s+\w+|\bfunc\s+\w+\s*\(|\bfmt\.(?:Print|Println|Printf)\s*\(/mu, 6],
  ["rust", /\bfn\s+main\s*\(|\blet\s+mut\b|\bprintln!\s*\(|\buse\s+std::/u, 6],
  ["kotlin", /\bfun\s+main\s*\(|\b(?:val|var)\s+\w+\s*(?::|=)/u, 5],
  ["swift", /\bimport\s+Foundation\b|\bfunc\s+\w+\s*\([^)]*\)\s*(?:->|\{)/u, 5],
  ["ruby", /^\s*def\s+\w+|\bputs\s+|^\s*end\s*$/mu, 4],
  ["php", /<\?php|\$[A-Za-z_]\w*|\becho\s+/u, 6],
  ["sql", /\bSELECT\b[\s\S]+\bFROM\b|\bINSERT\s+INTO\b|\bCREATE\s+TABLE\b/iu, 6],
  ["shell", /^#!\s*\/.*\b(?:ba|z|k)?sh\b|\b(?:echo|printf)\s+\$?\w+/mu, 5],
  ["json", /^\s*[\[{][\s\S]*[\]}]\s*$/u, 2],
  ["html", /<!doctype\s+html|<html\b|<(?:div|main|section|article)\b/iu, 6],
  ["css", /(?:^|\})\s*[.#]?[A-Za-z_-][\w-]*(?:\s+[.#]?[A-Za-z_-][\w-]*)*\s*\{[^}]*:[^}]*\}/mu, 5],
];

const TOKEN_CLASSES: Record<Exclude<TokenKind, "plain">, string> = {
  keyword: "font-semibold text-violet-700 dark:text-violet-300",
  type: "text-sky-700 dark:text-sky-300",
  string: "text-emerald-700 dark:text-emerald-300",
  comment: "italic text-slate-500 dark:text-slate-400",
  number: "text-amber-700 dark:text-amber-300",
  function: "text-blue-700 dark:text-blue-300",
  operator: "text-rose-600 dark:text-rose-300",
};

export const SyntaxHighlightedCode = memo(function SyntaxHighlightedCode({
  code,
  languageHint = "",
  locale,
}: {
  code: string;
  languageHint?: string;
  locale: string;
}) {
  const presentation = useMemo(() => prepareCode(code, languageHint), [code, languageHint]);
  const tokens = useMemo(
    () => tokenizeCode(presentation.code, presentation.language),
    [presentation.code, presentation.language],
  );
  const languageLabel = presentation.language === "plaintext"
    ? tx(locale, "纯文本", "Plain Text")
    : LANGUAGE_LABELS[presentation.language];

  return (
    <div data-code-language={presentation.language}>
      <div className="mb-2 flex justify-end">
        <span
          aria-label={tx(locale, `代码语言：${languageLabel}`, `Code language: ${languageLabel}`)}
          className="rounded-full border bg-background px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground"
        >
          {languageLabel}
        </span>
      </div>
      <pre className="overflow-x-auto whitespace-pre font-mono text-xs leading-6 text-foreground">
        <code>{renderTokens(tokens)}</code>
      </pre>
    </div>
  );
});

export function detectCodeLanguage(code: string, languageHint = ""): CodeLanguage {
  const fenced = unwrapCodeFence(code);
  const explicitLanguage = normalizeLanguage(fenced.language);
  if (explicitLanguage) return explicitLanguage;

  for (const [language, pattern] of HINT_RULES) {
    if (pattern.test(languageHint)) return language;
  }

  const scores = new Map<CodeLanguage, number>();
  for (const [language, pattern, score] of CODE_SCORE_RULES) {
    if (pattern.test(fenced.code)) scores.set(language, (scores.get(language) ?? 0) + score);
  }

  let bestLanguage: CodeLanguage = "plaintext";
  let bestScore = 0;
  for (const [language, score] of scores) {
    if (score > bestScore) {
      bestLanguage = language;
      bestScore = score;
    }
  }
  return bestLanguage;
}

function prepareCode(code: string, languageHint: string) {
  const fenced = unwrapCodeFence(code);
  return {
    code: fenced.code,
    language: detectCodeLanguage(code, languageHint),
  };
}

function unwrapCodeFence(value: string): { code: string; language: string } {
  const normalized = value.replace(/\r\n?/gu, "\n").trimEnd();
  const match = normalized.match(/^\s*(```|~~~)\s*([^\n`]*)\n([\s\S]*?)\n\1\s*$/u);
  if (!match) return { code: normalized, language: "" };
  return { code: match[3], language: match[2].trim().split(/\s+/u)[0] ?? "" };
}

function normalizeLanguage(value: string): CodeLanguage | null {
  const normalized = value.trim().toLocaleLowerCase().replace(/^language-/u, "");
  return LANGUAGE_ALIASES[normalized] ?? null;
}

function tokenizeCode(code: string, language: CodeLanguage): CodeToken[] {
  if (!code) return [];
  const definition = SYNTAX[language];
  const tokens: CodeToken[] = [];
  let index = 0;

  while (index < code.length) {
    const blockComment = definition.blockComments?.find(([start]) => code.startsWith(start, index));
    if (blockComment) {
      const endIndex = code.indexOf(blockComment[1], index + blockComment[0].length);
      const nextIndex = endIndex < 0 ? code.length : endIndex + blockComment[1].length;
      pushToken(tokens, "comment", code.slice(index, nextIndex));
      index = nextIndex;
      continue;
    }

    const lineComment = definition.lineComments?.find((marker) => code.startsWith(marker, index));
    if (lineComment) {
      const endIndex = code.indexOf("\n", index + lineComment.length);
      const nextIndex = endIndex < 0 ? code.length : endIndex;
      pushToken(tokens, "comment", code.slice(index, nextIndex));
      index = nextIndex;
      continue;
    }

    const character = code[index];
    if (definition.quotes?.includes(character)) {
      const tripleQuote = language === "python" && code.startsWith(character.repeat(3), index);
      const delimiter = tripleQuote ? character.repeat(3) : character;
      let nextIndex = index + delimiter.length;
      while (nextIndex < code.length) {
        if (code[nextIndex] === "\\") {
          nextIndex += 2;
          continue;
        }
        if (code.startsWith(delimiter, nextIndex)) {
          nextIndex += delimiter.length;
          break;
        }
        nextIndex += 1;
      }
      pushToken(tokens, "string", code.slice(index, nextIndex));
      index = nextIndex;
      continue;
    }

    if (/\s/u.test(character)) {
      let nextIndex = index + 1;
      while (nextIndex < code.length && /\s/u.test(code[nextIndex])) nextIndex += 1;
      pushToken(tokens, "plain", code.slice(index, nextIndex));
      index = nextIndex;
      continue;
    }

    if (/\d/u.test(character)) {
      const match = code.slice(index).match(/^(?:0[xob][\da-f]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?)/iu);
      const value = match?.[0] ?? character;
      pushToken(tokens, "number", value);
      index += value.length;
      continue;
    }

    if (/[A-Za-z_$]/u.test(character)) {
      const match = code.slice(index).match(/^[A-Za-z_$][\w$]*/u);
      const value = match?.[0] ?? character;
      const normalized = value.toLocaleLowerCase();
      const nextCharacter = code.slice(index + value.length).match(/^\s*(.)/us)?.[1] ?? "";
      const kind = definition.keywords.has(normalized)
        ? "keyword"
        : (definition.types ?? EMPTY_WORDS).has(normalized)
          ? "type"
          : COMMON_LITERALS.has(normalized)
            ? "keyword"
            : nextCharacter === "("
              ? "function"
              : "plain";
      pushToken(tokens, kind, value);
      index += value.length;
      continue;
    }

    if (/[+\-*/%=!<>&|^~?:]/u.test(character)) {
      let nextIndex = index + 1;
      while (nextIndex < code.length && /[+\-*/%=!<>&|^~?:]/u.test(code[nextIndex])) nextIndex += 1;
      pushToken(tokens, "operator", code.slice(index, nextIndex));
      index = nextIndex;
      continue;
    }

    pushToken(tokens, "plain", character);
    index += 1;
  }

  return tokens;
}

function renderTokens(tokens: CodeToken[]): ReactNode[] {
  return tokens.map((token, index) => token.kind === "plain"
    ? token.value
    : <span key={`${index}-${token.kind}`} data-code-token={token.kind} className={TOKEN_CLASSES[token.kind]}>{token.value}</span>);
}

function pushToken(tokens: CodeToken[], kind: TokenKind, value: string) {
  if (!value) return;
  const previous = tokens[tokens.length - 1];
  if (previous?.kind === kind) previous.value += value;
  else tokens.push({ kind, value });
}

function syntax(
  keywords: string,
  types: string,
  lineComments: readonly string[],
  blockComments: readonly [string, string][],
  quotes: readonly string[],
): LanguageSyntax {
  return {
    keywords: wordSet(keywords),
    types: wordSet(types),
    lineComments,
    blockComments,
    quotes,
  };
}

function wordSet(words: string) {
  return new Set(words.split(/\s+/u).filter(Boolean));
}

function tx(locale: string, zh: string, en: string) {
  return locale === "en-US" ? en : zh;
}
