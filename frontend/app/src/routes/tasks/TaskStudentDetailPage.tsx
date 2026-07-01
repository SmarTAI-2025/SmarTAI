import { useMemo } from "react";
import { AlertTriangle, ArrowLeft, ArrowRight, FileText, Loader2, RefreshCw, ShieldAlert } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useTask, useTaskResult } from "@/api/hooks/tasks";
import {
  buildResultsModel,
  clampPercent,
  formatConfidence,
  formatPercent,
  formatScore,
  hasReviewSignal,
  ResultsLayout,
  reviewReasonLabel,
  type StudentSummary,
} from "@/components/tasks/ResultsLayout";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import type { Correction } from "@/types";

export function TaskStudentDetailPage() {
  const { taskId, studentId } = useParams();
  const taskQuery = useTask(taskId);
  const resultQuery = useTaskResult(taskId);
  const model = useMemo(() => buildResultsModel(taskQuery.data, resultQuery.data), [taskQuery.data, resultQuery.data]);
  const studentIndex = model.students.findIndex((student) => student.id === studentId);
  const student = studentIndex >= 0 ? model.students[studentIndex] : null;
  const previousStudent = studentIndex > 0 ? model.students[studentIndex - 1] : null;
  const nextStudent = studentIndex >= 0 && studentIndex < model.students.length - 1 ? model.students[studentIndex + 1] : null;
  const firstQuestionId = student?.corrections[0]?.q_id ?? model.questions[0]?.id ?? null;

  if (!taskId) {
    return <EmptyState title="缺少任务 ID" description="请从教师工作台或任务列表进入学生详情。" />;
  }

  return (
    <ResultsLayout
      context="student-detail"
      title={student ? `${student.name} 的批改详情` : `学生详情 ${studentId ?? ""}`}
      description="查看单个学生的逐题得分、AI 评语、低置信与复核原因。"
      detailTargets={{ studentId: student?.id ?? studentId ?? null, questionId: firstQuestionId }}
    >
      {taskQuery.isLoading || resultQuery.isLoading ? <LoadingCard /> : null}
      {taskQuery.isError || resultQuery.isError ? (
        <ErrorCard
          error={taskQuery.error ?? resultQuery.error}
          onRetry={() => {
            void taskQuery.refetch();
            void resultQuery.refetch();
          }}
        />
      ) : null}
      {!taskQuery.isLoading && !resultQuery.isLoading && !taskQuery.isError && !resultQuery.isError ? (
        <StudentContent
          taskId={taskId}
          studentId={studentId}
          student={student}
          previousStudent={previousStudent}
          nextStudent={nextStudent}
          resultStatus={resultQuery.data?.status}
        />
      ) : null}
    </ResultsLayout>
  );
}

function StudentContent({
  taskId,
  studentId,
  student,
  previousStudent,
  nextStudent,
  resultStatus,
}: {
  taskId: string;
  studentId?: string;
  student: StudentSummary | null;
  previousStudent: StudentSummary | null;
  nextStudent: StudentSummary | null;
  resultStatus?: string;
}) {
  if (resultStatus !== "completed") {
    return (
      <EmptyState
        title="结果尚未生成"
        description="批改完成后才能查看学生逐题详情。"
        action={
          <Link to={`/tasks/${taskId}/results`}>
            <Button type="button" variant="secondary">
              返回结果列表
            </Button>
          </Link>
        }
      />
    );
  }

  if (!student) {
    return (
      <EmptyState
        title="未找到该学生结果"
        description={studentId ? `当前结果中没有学生 ${studentId}。` : "当前链接缺少学生 ID。"}
        action={
          <Link to={`/tasks/${taskId}/results`}>
            <Button type="button" variant="secondary">
              返回结果列表
            </Button>
          </Link>
        }
      />
    );
  }

  return (
    <div className="grid gap-4">
      <Card className="grid gap-4">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <h2 className="text-base font-semibold">{student.name}</h2>
            <p className="mt-1 text-sm text-muted-foreground">{student.id}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {previousStudent ? (
              <Link to={`/tasks/${taskId}/results/${previousStudent.id}`}>
                <Button type="button" variant="secondary">
                  <ArrowLeft className="h-4 w-4" />
                  上一位
                </Button>
              </Link>
            ) : (
              <Button type="button" variant="secondary" disabled>
                <ArrowLeft className="h-4 w-4" />
                上一位
              </Button>
            )}
            {nextStudent ? (
              <Link to={`/tasks/${taskId}/results/${nextStudent.id}`}>
                <Button type="button" variant="secondary">
                  下一位
                  <ArrowRight className="h-4 w-4" />
                </Button>
              </Link>
            ) : (
              <Button type="button" variant="secondary" disabled>
                下一位
                <ArrowRight className="h-4 w-4" />
              </Button>
            )}
            <Link to={`/tasks/${taskId}/results`}>
              <Button type="button" variant="secondary">
                返回结果列表
              </Button>
            </Link>
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="总分" value={`${formatScore(student.totalScore)} / ${formatScore(student.totalMax)}`} />
          <Metric label="得分率" value={formatPercent(student.percent)} />
          <Metric label="平均置信度" value={formatConfidence(student.avgConfidence)} />
          <Metric label="复核信号" value={`低置信 ${student.lowConfidenceCount} · 需复核 ${student.reviewCount}`} tone="warning" />
        </div>
      </Card>

      <div className="grid gap-3">
        {student.corrections.map((correction) => (
          <StudentCorrectionCard key={correction.q_id} taskId={taskId} student={student} correction={correction} />
        ))}
      </div>
    </div>
  );
}

function StudentCorrectionCard({
  taskId,
  student,
  correction,
}: {
  taskId: string;
  student: StudentSummary;
  correction: Correction;
}) {
  const answer = student.answerByQuestion.get(correction.q_id);
  const percent = correction.max_score > 0 ? (correction.score / correction.max_score) * 100 : null;

  return (
    <Card className="grid gap-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold">{answer?.number ? `Q${answer.number}` : correction.q_id}</h3>
            <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">{correction.type}</span>
            {hasReviewSignal(correction) ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning">
                <ShieldAlert className="h-3 w-3" />
                需要关注
              </span>
            ) : null}
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            <Metric label="得分" value={`${formatScore(correction.score)} / ${formatScore(correction.max_score)}`} />
            <Metric label="得分率" value={formatPercent(percent)} />
            <Metric label="置信度" value={formatConfidence(correction.confidence)} />
          </div>
        </div>
        <Link to={`/tasks/${taskId}/questions/${correction.q_id}`}>
          <Button type="button" variant="secondary">
            <FileText className="h-4 w-4" />
            该题全班分析
          </Button>
        </Link>
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
          <span>得分率</span>
          <span>{formatPercent(percent)}</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-muted">
          <div className="h-full bg-primary" style={{ width: `${clampPercent(percent)}%` }} />
        </div>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <TextBlock title="学生作答" body={answer?.content} empty="没有作答文本。" />
        <TextBlock title="AI 评语" body={correction.comment} empty="没有 AI 评语。" />
      </div>

      <ReviewReasons correction={correction} />
    </Card>
  );
}

function ReviewReasons({ correction }: { correction: Correction }) {
  const reasons = correction.review_reasons ?? [];
  if (!hasReviewSignal(correction) && !reasons.length) {
    return <div className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">无低置信或复核原因。</div>;
  }

  return (
    <div className="rounded-lg border border-warning/30 bg-warning/5 p-3">
      <div className="flex items-center gap-2 text-sm font-medium text-warning">
        <AlertTriangle className="h-4 w-4" />
        低置信 / 复核原因
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        {correction.confidence < 0.65 ? (
          <span className="rounded-full bg-warning/10 px-2 py-1 text-xs font-medium text-warning">置信度偏低</span>
        ) : null}
        {reasons.map((reason) => (
          <span key={reason} className="rounded-full bg-card px-2 py-1 text-xs text-muted-foreground">
            {reviewReasonLabel(reason)}
          </span>
        ))}
      </div>
    </div>
  );
}

function TextBlock({ title, body, empty }: { title: string; body?: string | null; empty: string }) {
  return (
    <div className="rounded-lg border bg-muted/30 p-3">
      <p className="text-xs font-medium text-muted-foreground">{title}</p>
      <p className="mt-2 whitespace-pre-wrap text-sm leading-6">{body?.trim() || empty}</p>
    </div>
  );
}

function Metric({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "warning" }) {
  return (
    <div className={tone === "warning" ? "rounded-lg border border-warning/30 bg-warning/5 p-3" : "rounded-lg border p-3"}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-semibold">{value}</p>
    </div>
  );
}

function LoadingCard() {
  return (
    <Card className="flex items-center gap-3 text-sm text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" />
      正在读取学生结果...
    </Card>
  );
}

function ErrorCard({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return (
    <Card className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 text-danger" />
        <div>
          <p className="font-medium">学生结果读取失败</p>
          <p className="mt-1 text-sm text-muted-foreground">{error instanceof Error ? error.message : "请稍后重试。"}</p>
        </div>
      </div>
      <Button type="button" variant="secondary" onClick={onRetry}>
        <RefreshCw className="h-4 w-4" />
        重试
      </Button>
    </Card>
  );
}
