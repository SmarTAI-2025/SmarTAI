import type { ReactNode } from "react";
import { BarChart3, ClipboardList, FileText, Layers3, UserRound } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { SectionHeader } from "@/components/ui/Card";
import { cn } from "@/lib/cn";
import type { Correction, ProblemInfo, StudentAnswerInfo, StudentResult, StudentSubmission, Task, TaskResultResponse } from "@/types";
import { TaskStepper } from "./TaskStepper";

export type ResultContext = "overview" | "by-question" | "visualization" | "student-detail" | "question-detail";

export const LOW_CONFIDENCE_THRESHOLD = 0.65;

export interface StudentSummary {
  id: string;
  name: string;
  corrections: Correction[];
  answers: StudentAnswerInfo[];
  answerByQuestion: Map<string, StudentAnswerInfo>;
  totalScore: number;
  totalMax: number;
  percent: number | null;
  avgConfidence: number | null;
  lowConfidenceCount: number;
  reviewCount: number;
}

export interface QuestionEntry {
  student: StudentSummary;
  correction: Correction;
  answer?: StudentAnswerInfo;
}

export interface QuestionSummary {
  id: string;
  problem?: ProblemInfo;
  label: string;
  type?: string;
  stem?: string;
  criterion?: string;
  entries: QuestionEntry[];
  count: number;
  avgScore: number;
  maxScore: number;
  avgPercent: number | null;
  minScore: number | null;
  maxObservedScore: number | null;
  lowConfidenceCount: number;
  reviewCount: number;
}

export interface ResultsModel {
  task?: Task;
  result?: TaskResultResponse;
  problems: ProblemInfo[];
  students: StudentSummary[];
  questions: QuestionSummary[];
  classAverageScore: number;
  classAverageMax: number;
  classAveragePercent: number | null;
  lowConfidenceCount: number;
  reviewCount: number;
  timestamp?: number;
}

const baseTabs = [
  {
    key: "overview",
    label: "总览",
    description: "班级概况",
    icon: ClipboardList,
    href: (taskId: string) => `/tasks/${taskId}/results`,
  },
  {
    key: "by-question",
    label: "按题分析",
    description: "题目维度",
    icon: Layers3,
    href: (taskId: string) => `/tasks/${taskId}/results?view=questions`,
  },
  {
    key: "visualization",
    label: "可视化",
    description: "图表探索",
    icon: BarChart3,
    href: (taskId: string) => `/tasks/${taskId}/results?view=charts`,
  },
] as const;

export function ResultsLayout({
  context,
  title,
  description,
  detailTargets,
  children,
}: {
  context: ResultContext;
  title: string;
  description: string;
  detailTargets?: {
    studentId?: string | null;
    questionId?: string | null;
  };
  children: ReactNode;
}) {
  const { taskId = "draft", studentId, questionId } = useParams();
  const targetStudentId = detailTargets?.studentId ?? studentId;
  const targetQuestionId = detailTargets?.questionId ?? questionId;

  const tabs = [
    ...baseTabs,
    {
      key: "student-detail",
      label: "学生详情",
      description: "跨题查看",
      icon: UserRound,
      href: () => (targetStudentId ? `/tasks/${taskId}/results/${targetStudentId}` : null),
    },
    {
      key: "question-detail",
      label: "题目详情",
      description: "全班作答",
      icon: FileText,
      href: () => (targetQuestionId ? `/tasks/${taskId}/questions/${targetQuestionId}` : null),
    },
  ];

  return (
    <div className="grid gap-5">
      <TaskStepper current="results" />
      <SectionHeader title={title} description={description} />
      <nav aria-label="结果分析视图" className="rounded-lg border bg-card p-2">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
          {tabs.map((tab) => {
            const Icon = tab.icon;
            const active = tab.key === context;
            const href = tab.href(taskId);
            const className = cn(
              "flex min-h-16 items-center gap-3 rounded-md px-3 py-2 transition",
              active
                ? "bg-primary text-primary-foreground"
                : href
                  ? "text-muted-foreground hover:bg-muted hover:text-foreground"
                  : "cursor-not-allowed text-muted-foreground/60",
            );
            const content = (
              <>
                <Icon className="h-4 w-4 shrink-0" />
                <span className="grid gap-0.5">
                  <span className="text-sm font-semibold">{tab.label}</span>
                  <span
                    className={cn(
                      "text-xs leading-4",
                      active ? "text-primary-foreground/80" : "text-muted-foreground",
                    )}
                  >
                    {tab.description}
                  </span>
                </span>
              </>
            );
            if (!href) {
              return (
                <span key={tab.key} className={className} aria-disabled="true">
                  {content}
                </span>
              );
            }
            return (
              <Link
                key={tab.key}
                to={href}
                className={className}
                aria-current={active ? "page" : undefined}
              >
                {content}
              </Link>
            );
          })}
        </div>
      </nav>
      {children}
    </div>
  );
}

export function buildResultsModel(task?: Task, result?: TaskResultResponse): ResultsModel {
  const rawProblems = result?.problem_data ?? task?.problem_data ?? {};
  const rawStudentData = result?.student_data ?? task?.student_data ?? {};
  const problems = Object.values(rawProblems).sort(compareProblems);
  const problemOrder = new Map(problems.map((problem, index) => [problem.q_id, index]));
  const submissions = Object.values(rawStudentData);
  const submissionById = new Map(submissions.map((submission) => [submission.stu_id, submission]));
  const students = (result?.results ?? []).map((studentResult) =>
    buildStudentSummary(studentResult, submissionById.get(studentResult.student_id), problemOrder),
  );

  students.sort((a, b) => compareStudentNames(a.name, b.name) || a.id.localeCompare(b.id, undefined, { numeric: true }));

  const qIds = new Set(problems.map((problem) => problem.q_id));
  for (const student of students) {
    for (const correction of student.corrections) {
      qIds.add(correction.q_id);
    }
  }

  const entriesByQuestion = new Map<string, QuestionEntry[]>();
  for (const student of students) {
    for (const correction of student.corrections) {
      const entries = entriesByQuestion.get(correction.q_id) ?? [];
      entries.push({
        student,
        correction,
        answer: student.answerByQuestion.get(correction.q_id),
      });
      entriesByQuestion.set(correction.q_id, entries);
    }
  }

  const questions = Array.from(qIds)
    .sort((a, b) => compareQuestionIds(a, b, problemOrder))
    .map((qId) => buildQuestionSummary(qId, rawProblems[qId], entriesByQuestion.get(qId) ?? []));

  const classAverageScore = average(students.map((student) => student.totalScore));
  const classAverageMax = average(students.map((student) => student.totalMax));
  const classAveragePercent = averageOrNull(students.map((student) => student.percent));
  const lowConfidenceCount = students.reduce((sum, student) => sum + student.lowConfidenceCount, 0);
  const reviewCount = students.reduce((sum, student) => sum + student.reviewCount, 0);

  return {
    task,
    result,
    problems,
    students,
    questions,
    classAverageScore,
    classAverageMax,
    classAveragePercent,
    lowConfidenceCount,
    reviewCount,
    timestamp: result?.timestamp,
  };
}

export function compareProblems(a: ProblemInfo, b: ProblemInfo) {
  return compareProblemLabels(problemLabel(a), problemLabel(b)) || a.q_id.localeCompare(b.q_id, undefined, { numeric: true });
}

export function problemLabel(problem?: Pick<ProblemInfo, "q_id" | "number"> | null, fallbackId?: string) {
  const number = String(problem?.number ?? "").trim();
  if (number) {
    return `Q${number}`;
  }
  return String(problem?.q_id ?? fallbackId ?? "题目");
}

export function formatScore(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  if (Number.isInteger(value)) {
    return String(value);
  }
  return value.toFixed(1);
}

export function formatPercent(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  return `${Math.round(value)}%`;
}

export function formatConfidence(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  const percent = value <= 1 ? value * 100 : value;
  return `${Math.round(percent)}%`;
}

export function isLowConfidence(correction: Correction) {
  return safeNumber(correction.confidence) < LOW_CONFIDENCE_THRESHOLD;
}

export function hasReviewSignal(correction: Correction) {
  return correction.requires_human_review || isLowConfidence(correction);
}

export function reviewReasonLabel(reason: string) {
  const labels: Record<string, string> = {
    high_indecisiveness: "专家分歧较大",
    low_confidence: "置信度偏低",
    score_spread_high: "评分差异较大",
    parse_failed: "解析失败",
    transient_llm: "模型临时错误",
    quota_exhausted: "额度不足",
    general: "需要人工确认",
  };
  return labels[reason] ?? reason.replaceAll("_", " ");
}

export function clampPercent(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return 0;
  }
  return Math.max(0, Math.min(100, value));
}

function buildStudentSummary(
  result: StudentResult,
  submission: StudentSubmission | undefined,
  problemOrder: Map<string, number>,
): StudentSummary {
  const corrections = [...(result.corrections ?? [])].sort((a, b) => compareQuestionIds(a.q_id, b.q_id, problemOrder));
  const answers = result.student_answers?.length ? result.student_answers : (submission?.stu_ans ?? []);
  const answerByQuestion = new Map(answers.map((answer) => [answer.q_id, answer]));
  const totalScore = sum(corrections.map((correction) => safeNumber(correction.score)));
  const totalMax = sum(corrections.map((correction) => safeNumber(correction.max_score)));
  const confidenceValues = corrections.map((correction) => safeNumber(correction.confidence));
  const lowConfidenceCount = corrections.filter(isLowConfidence).length;
  const reviewCount = corrections.filter((correction) => correction.requires_human_review).length;

  return {
    id: result.student_id,
    name: String(result.student_name || submission?.stu_name || result.student_id),
    corrections,
    answers,
    answerByQuestion,
    totalScore,
    totalMax,
    percent: totalMax > 0 ? (totalScore / totalMax) * 100 : null,
    avgConfidence: confidenceValues.length ? average(confidenceValues) : null,
    lowConfidenceCount,
    reviewCount,
  };
}

function buildQuestionSummary(qId: string, problem: ProblemInfo | undefined, entries: QuestionEntry[]): QuestionSummary {
  const scores = entries.map((entry) => safeNumber(entry.correction.score));
  const maxScores = entries.map((entry) => safeNumber(entry.correction.max_score)).filter((value) => value > 0);
  const percents = entries
    .map((entry) => {
      const maxScore = safeNumber(entry.correction.max_score);
      return maxScore > 0 ? (safeNumber(entry.correction.score) / maxScore) * 100 : null;
    })
    .filter((value): value is number => value !== null);

  return {
    id: qId,
    problem,
    label: problemLabel(problem, qId),
    type: problem?.type,
    stem: problem?.stem,
    criterion: problem?.criterion,
    entries,
    count: entries.length,
    avgScore: average(scores),
    maxScore: maxScores.length ? Math.max(...maxScores) : 0,
    avgPercent: percents.length ? average(percents) : null,
    minScore: scores.length ? Math.min(...scores) : null,
    maxObservedScore: scores.length ? Math.max(...scores) : null,
    lowConfidenceCount: entries.filter((entry) => isLowConfidence(entry.correction)).length,
    reviewCount: entries.filter((entry) => entry.correction.requires_human_review).length,
  };
}

function compareQuestionIds(a: string, b: string, problemOrder: Map<string, number>) {
  const aOrder = problemOrder.get(a);
  const bOrder = problemOrder.get(b);
  if (aOrder !== undefined && bOrder !== undefined) {
    return aOrder - bOrder;
  }
  if (aOrder !== undefined) {
    return -1;
  }
  if (bOrder !== undefined) {
    return 1;
  }
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

function compareProblemLabels(a: string, b: string) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

function compareStudentNames(a: string, b: string) {
  return a.localeCompare(b, "zh-Hans-CN", { numeric: true, sensitivity: "base" });
}

function sum(values: number[]) {
  return values.reduce((total, value) => total + value, 0);
}

function average(values: number[]) {
  return values.length ? sum(values) / values.length : 0;
}

function averageOrNull(values: Array<number | null>) {
  const clean = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  return clean.length ? average(clean) : null;
}

function safeNumber(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
