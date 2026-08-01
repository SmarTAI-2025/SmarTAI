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

// These normalized workspaces are intentionally retained as dormant source so
// they can continue to typecheck while the Figma presentation remains the only
// production surface. They are audited by the router/import checks below, not
// as user-visible copy, because the default entry point must never import them.
const dormantVisibleSourcePrefixes = [
  "src/routes/admin/",
  "src/routes/student/",
  "src/routes/teacher/",
];
const dormantVisibleSourceFiles = new Set([
  "src/components/auth/RequireRoleSession.tsx",
  "src/components/layout/nav.ts",
]);

const hiddenDisclaimerPattern =
  /保持隐藏|隐藏预留|暂未开放|尚未开放|未开放|不可用|不展示|不提供|not available|hidden|unavailable/i;
const futureCapabilityDisclaimerPattern =
  /前端先行|前端清单|前端预览|待后端接入|后端.*待接入|不参与当前批改|不会.*参与批改|does not.*participate|backend.*pending|preview/i;
const rawChineseJsxTextPattern = />\s*[\u3400-\u9fff][^<{]*</;
const rawChineseLiteralPropPattern =
  /\b(?:aria-label|title|description|placeholder|label|hint|message|alt)\s*=\s*(["'`])[^"'`\n]*[\u3400-\u9fff][^"'`\n]*\1/;

const visibleTextRules = [
  {
    id: "visible-developer-jargon",
    description: "Do not expose implementation-pending or in-memory developer language to teachers.",
    pattern: /待后端|后端.{0,12}待接入|前端先行|前端预览|内存索引|in-memory (?:index|state)|backend.{0,30}pending|server persistence.{0,30}pending/i,
  },
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
      const relativePath = toRelative(filePath);
      const isTestSource = /\.test\.(?:tsx?|jsx?)$/.test(relativePath);
      const isDormantSource =
        dormantVisibleSourceFiles.has(relativePath) ||
        dormantVisibleSourcePrefixes.some((prefix) => relativePath.startsWith(prefix));
      if (!isTestSource && !isDormantSource) {
        files.add(filePath);
      }
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
    rule.id === "visible-course-management" &&
    relativePath === "src/i18n/messages.ts" &&
    /^\s*(?:(?:knowledgeBase|courseLibrary):\s*"Course Library"|dashboardColumnCourseTags:\s*"Course \/ Tags"|history(?:SearchPlaceholder|Course|AllCourses|CourseUnset):),?/.test(line)
  ) {
    return true;
  }

  if (
    rule.id === "visible-course-management" &&
    (
      relativePath === "src/routes/tasks/AddProblemsPage.tsx"
      || relativePath === "src/routes/tasks/QuestionMaterialImportPage.tsx"
      || relativePath === "src/routes/tasks/GradingSetupPage.tsx"
      || relativePath.startsWith("src/api/problemSources")
      || relativePath.startsWith("src/api/hooks/problemSources")
      || relativePath === "src/types/problemSources.ts"
      || (relativePath === "src/i18n/messages.ts" && /^\s*addProblems/.test(line))
    )
  ) {
    // Q-01, Q-08, and grading setup may select real owner-scoped course-library
    // material for the current task. They do not expose enrollment or publishing.
    return true;
  }

  if (
    rule.id === "visible-course-management" &&
    (relativePath.startsWith("src/components/history/") || relativePath === "src/routes/HistoryPage.tsx")
  ) {
    // History exposes course metadata and filtering backed by the task API. It
    // does not expose course CRUD, publishing, enrollment, or a course route.
    return true;
  }

  if (
    rule.id === "visible-course-management" &&
    (
      relativePath.startsWith("src/components/knowledge-base/")
      || relativePath === "src/routes/KnowledgeBasePage.tsx"
      || relativePath.startsWith("src/components/new-task/")
      || relativePath === "src/routes/NewTaskPage.tsx"
      || (relativePath === "src/i18n/messages.ts" && /^\s*newTask/.test(line))
    )
  ) {
    // New Task may select/create course metadata, and Course Library may use
    // that metadata to organize materials. Neither exposes enrollment,
    // assignment publishing, or a standalone course-management route.
    return true;
  }

  if (
    rule.id === "visible-global-kb" &&
    (
      relativePath === "src/routes/KnowledgeBasePage.tsx"
      || relativePath.startsWith("src/components/knowledge-base/")
      || relativePath === "src/routes/tasks/TaskSetupPage.tsx"
    )
  ) {
    return true;
  }

  if (
    rule.id === "visible-lms-integration" &&
    relativePath === "src/routes/tasks/results/VisualizationAnalysisPage.tsx" &&
    /(?:createElement\(["']canvas["']\)|\bcanvas\.(?:width|height|toBlob|getContext))/.test(line)
  ) {
    // Browser Canvas is used only to rasterize an already-rendered SVG for
    // PNG download. It is unrelated to the Canvas LMS product capability.
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

    if (rawChineseJsxTextPattern.test(line) || rawChineseLiteralPropPattern.test(line)) {
      findings.push({
        type: "visible-text",
        file: relativePath,
        line: index + 1,
        rule: "raw-unlocalized-chinese",
        message: "User-visible Chinese JSX text must select a locale-aware alternative.",
        excerpt: line.trim().replace(/\s+/g, " "),
      });
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

function auditInteractionMarkup(relativePath, content) {
  const findings = [];
  const nestedInteractivePattern =
    /<(?:Link|a)\b[^>]*>\s*<(?:Button|button)\b|<(?:Button|button)\b[^>]*>\s*<(?:Link|a)\b/g;
  const nativeButtonPattern = /<button\b[^>]*>/gs;
  const interactiveTagPattern = /<(?:button|a|Link|input|select|textarea)\b[^>]*>/gs;
  let match;

  while ((match = nestedInteractivePattern.exec(content)) !== null) {
    findings.push({
      type: "interaction",
      file: relativePath,
      line: lineNumberAt(content, match.index),
      rule: "nested-interactive-control",
      message: "Links and buttons must not be nested inside one another.",
      excerpt: match[0].replace(/\s+/g, " "),
    });
  }

  while ((match = nativeButtonPattern.exec(content)) !== null) {
    if (/\btype\s*=/.test(match[0])) continue;
    findings.push({
      type: "interaction",
      file: relativePath,
      line: lineNumberAt(content, match.index),
      rule: "native-button-type",
      message: "Native buttons must declare an explicit type.",
      excerpt: match[0].replace(/\s+/g, " "),
    });
  }

  while ((match = interactiveTagPattern.exec(content)) !== null) {
    if (!match[0].includes("outline-none") || /\bfocus(?:-visible)?:/.test(match[0])) continue;
    findings.push({
      type: "interaction",
      file: relativePath,
      line: lineNumberAt(content, match.index),
      rule: "missing-focus-indicator",
      message: "Interactive controls that remove the default outline must add an explicit focus indicator.",
      excerpt: match[0].replace(/\s+/g, " "),
    });
  }

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
      // This nested task route is a teacher-only review surface protected by
      // RequireTeacherSession, not a student portal or student workspace.
      const isTeacherTaskReviewRoute = [
        "tasks/:taskId/students/:studentId",
        "tasks/:taskId/results/students",
        "tasks/:taskId/results/students/:studentId",
      ].includes(routePath);
      if ((routePath !== "/student" || !hasAllowedStudentRoute) && !isTeacherTaskReviewRoute) {
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

function auditProductionComposition(mainContent, appShellContent) {
  const findings = [];
  const forbiddenMainImportPattern =
    /(?:from\s+["']@\/routes\/(?:admin|student|teacher)\/|import\(\s*["'][^"']*\/routes\/(?:admin|student|teacher)\/|RequireRoleSession|@\/components\/layout\/nav)/g;
  let match;

  while ((match = forbiddenMainImportPattern.exec(mainContent)) !== null) {
    findings.push({
      type: "composition",
      file: "src/main.tsx",
      line: lineNumberAt(mainContent, match.index),
      rule: "dormant-workspace-imported",
      message: "The production entry point must not import a dormant normalized workspace.",
      excerpt: match[0].replace(/\s+/g, " "),
    });
  }

  const normalizedNavPattern = /@\/components\/layout\/nav|\.\/nav/g;
  while ((match = normalizedNavPattern.exec(appShellContent)) !== null) {
    findings.push({
      type: "composition",
      file: "src/components/layout/AppShell.tsx",
      line: lineNumberAt(appShellContent, match.index),
      rule: "normalized-nav-in-figma-shell",
      message: "The Figma AppShell must not import the dormant normalized navigation.",
      excerpt: match[0],
    });
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
  findings.push(...auditInteractionMarkup(relativePath, content));
}

const mainContent = await pathExists(mainFile).then((mainStat) => (mainStat ? readFile(mainFile, "utf8") : null));
if (mainContent) {
  findings.push(...auditRouter(mainContent));
  const appShellFile = path.join(srcRoot, "components/layout/AppShell.tsx");
  const appShellContent = await readFile(appShellFile, "utf8");
  findings.push(...auditProductionComposition(mainContent, appShellContent));
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
  console.log("No visible LMS, unsupported course management, assignment publishing, or unsupported KB claims found.");
}
