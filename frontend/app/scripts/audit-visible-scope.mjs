#!/usr/bin/env node

import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";

const projectRoot = process.cwd();
const srcRoot = path.join(projectRoot, "src");
const mainFile = path.join(srcRoot, "main.tsx");

const scanEntries = [
  { relativePath: "src/main.tsx", required: true },
  { relativePath: "src/components", required: true },
  { relativePath: "src/routes", required: true },
  { relativePath: "src/i18n/messages.ts", required: false },
];

const hiddenDisclaimerPattern =
  /保持隐藏|隐藏预留|暂未开放|尚未开放|未开放|不可用|不展示|不提供|not available|hidden|unavailable/i;
const futureCapabilityDisclaimerPattern =
  /前端先行|前端清单|前端预览|待后端接入|后端.*待接入|不参与当前批改|不会.*参与批改|does not.*participate|backend.*pending|preview/i;

const visibleTextRules = [
  {
    id: "visible-lms-integration",
    description: "Do not show LMS/LTI/Canvas/Moodle integration as a visible capability.",
    pattern: /\b(?:LMS|LTI|SSO|Canvas|Moodle)\b|学校\s*LMS|成绩回传/i,
    allowHiddenDisclaimer: true,
  },
  {
    id: "visible-course-management",
    description: "Do not show course management as a visible capability.",
    pattern: /课程管理|课程作业|课程入口|课程列表|\bcourses?\b|\bclassroom\b/i,
    allowHiddenDisclaimer: true,
  },
  {
    id: "visible-assignment-publishing",
    description: "Do not show assignment publishing or LMS assignment flows.",
    pattern: /作业发布|发布作业|学生提交入口|学生成绩页|\bassignments?\b|\bgradebook\b/i,
    allowHiddenDisclaimer: true,
  },
  {
    id: "visible-student-workspace",
    description: "Do not show a student workspace beyond the unavailable notice.",
    pattern: /学生端工作台|学生端入口|学生端 Dashboard|student (?:dashboard|workspace|portal)/i,
    allowHiddenDisclaimer: true,
  },
  {
    id: "visible-grading-language",
    description: "Do not show grading-language selection.",
    pattern: /批改语言|评语语言|学科语言|grading[-\s]?language|feedback[-\s]?language/i,
    allowHiddenDisclaimer: false,
  },
  {
    id: "visible-global-kb",
    description: "Do not show user/global knowledge base as implemented without a backend-pending disclaimer.",
    pattern: /全局知识库|个人知识库|我的知识库|用户级知识库|跨任务复用|global knowledge base|personal knowledge base|user-scoped knowledge base/i,
    allowHiddenDisclaimer: true,
    allowFutureCapabilityDisclaimer: true,
  },
];

const forbiddenRouteSegments = ["courses", "course", "assignments", "assignment", "lms", "lti", "canvas", "moodle"];
const allowedStudentUnavailableText = "学生端暂未开放";

function toRelative(filePath) {
  return path.relative(projectRoot, filePath).split(path.sep).join("/");
}

async function pathExists(filePath) {
  try {
    return await stat(filePath);
  } catch (error) {
    if (error?.code === "ENOENT") {
      return null;
    }
    throw error;
  }
}

async function walkFiles(entryPath) {
  const entryStat = await pathExists(entryPath);
  if (!entryStat) {
    return [];
  }
  if (entryStat.isFile()) {
    return [entryPath];
  }
  if (!entryStat.isDirectory()) {
    return [];
  }

  const files = [];
  const dirEntries = await readdir(entryPath, { withFileTypes: true });
  for (const dirEntry of dirEntries) {
    const nestedPath = path.join(entryPath, dirEntry.name);
    if (dirEntry.isDirectory()) {
      files.push(...(await walkFiles(nestedPath)));
    } else if (dirEntry.isFile() && /\.(tsx|ts)$/.test(dirEntry.name) && !dirEntry.name.endsWith(".d.ts")) {
      files.push(nestedPath);
    }
  }
  return files;
}

async function collectVisibleFiles() {
  const missingRequired = [];
  const files = new Set();

  for (const entry of scanEntries) {
    const absolutePath = path.join(projectRoot, entry.relativePath);
    const entryStat = await pathExists(absolutePath);
    if (!entryStat) {
      if (entry.required) {
        missingRequired.push(entry.relativePath);
      }
      continue;
    }
    for (const filePath of await walkFiles(absolutePath)) {
      files.add(filePath);
    }
  }

  return {
    files: [...files].sort(),
    missingRequired,
  };
}

function isCommentOnlyLine(line) {
  const trimmed = line.trim();
  return trimmed.startsWith("//") || trimmed.startsWith("/*") || trimmed.startsWith("*") || trimmed.startsWith("*/");
}

function isAllowedVisibleText(relativePath, line, rule) {
  if (relativePath === "src/routes/StudentUnavailablePage.tsx" && line.includes(allowedStudentUnavailableText)) {
    return true;
  }

  if (
    rule.id === "visible-global-kb" &&
    (relativePath === "src/routes/KnowledgeBasePage.tsx" || relativePath === "src/routes/tasks/TaskSetupPage.tsx")
  ) {
    return true;
  }

  if (rule.allowHiddenDisclaimer && hiddenDisclaimerPattern.test(line)) {
    return true;
  }

  if (rule.allowFutureCapabilityDisclaimer && futureCapabilityDisclaimerPattern.test(line)) {
    return true;
  }

  return false;
}

function auditVisibleText(relativePath, content) {
  const findings = [];
  const lines = content.split(/\r?\n/);

  lines.forEach((line, index) => {
    if (isCommentOnlyLine(line)) {
      return;
    }

    for (const rule of visibleTextRules) {
      if (rule.pattern.test(line) && !isAllowedVisibleText(relativePath, line, rule)) {
        findings.push({
          type: "visible-text",
          file: relativePath,
          line: index + 1,
          rule: rule.id,
          message: rule.description,
          excerpt: line.trim().replace(/\s+/g, " "),
        });
      }
    }
  });

  return findings;
}

function routeSegments(routePath) {
  return routePath
    .toLowerCase()
    .split("/")
    .map((segment) => segment.trim())
    .filter((segment) => segment && segment !== "*" && !segment.startsWith(":"))
    .map((segment) => segment.replace(/[^a-z0-9-]/g, ""));
}

function lineNumberAt(content, index) {
  return content.slice(0, index).split(/\r?\n/).length;
}

function auditRouter(content) {
  const findings = [];
  const routePattern = /\bpath\s*:\s*(["'`])([^"'`]+)\1/g;
  let match;

  while ((match = routePattern.exec(content)) !== null) {
    const routePath = match[2];
    const segments = routeSegments(routePath);
    const line = lineNumberAt(content, match.index);

    for (const forbiddenSegment of forbiddenRouteSegments) {
      if (segments.some((segment) => segment === forbiddenSegment || segment.includes(forbiddenSegment))) {
        findings.push({
          type: "route",
          file: "src/main.tsx",
          line,
          rule: "forbidden-route-segment",
          message: `Router path must not expose ${forbiddenSegment}.`,
          excerpt: `path: "${routePath}"`,
        });
      }
    }

    if (segments.includes("student") || segments.includes("students")) {
      const hasAllowedStudentRoute = /\{\s*path\s*:\s*(["'`])\/student\1\s*,\s*element\s*:\s*<StudentUnavailablePage\s*\/>\s*\}/s.test(
        content,
      );
      if (routePath !== "/student" || !hasAllowedStudentRoute) {
        findings.push({
          type: "route",
          file: "src/main.tsx",
          line,
          rule: "student-route-must-be-unavailable",
          message: "Student routes are only allowed for the unavailable notice page.",
          excerpt: `path: "${routePath}"`,
        });
      }
    }
  }

  return findings;
}

function printFindings(findings) {
  for (const finding of findings) {
    console.error(`${finding.file}:${finding.line} [${finding.rule}] ${finding.message}`);
    console.error(`  ${finding.excerpt}`);
  }
}

const { files, missingRequired } = await collectVisibleFiles();
const findings = [];

for (const relativePath of missingRequired) {
  findings.push({
    type: "missing-file",
    file: relativePath,
    line: 1,
    rule: "missing-required-visible-source",
    message: "Required visible source file was not found.",
    excerpt: relativePath,
  });
}

for (const filePath of files) {
  const relativePath = toRelative(filePath);
  const content = await readFile(filePath, "utf8");
  findings.push(...auditVisibleText(relativePath, content));
}

const mainContent = await pathExists(mainFile).then((mainStat) => (mainStat ? readFile(mainFile, "utf8") : null));
if (mainContent) {
  findings.push(...auditRouter(mainContent));
}

if (findings.length > 0) {
  console.error("FAIL visible scope audit");
  console.error(`Scanned ${files.length} user-visible source files.`);
  printFindings(findings);
  process.exitCode = 1;
} else {
  console.log("PASS visible scope audit");
  console.log(`Scanned ${files.length} user-visible source files.`);
  console.log("Checked Router paths for hidden LMS/course/assignment integrations.");
  console.log("No visible LMS, course, assignment publishing, grading-language, or unsupported KB claims found.");
}
