import { Link } from "react-router-dom";
import { useAssignments, useMyStudentResult } from "@/api/hooks/education";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import type { GradeResult } from "@/types/education";

/**
 * Student results overview: lists published assignments with a quick score
 * summary pulled from the released per-question results. A run that has not
 * been released returns nothing, so the row shows "未发布". Detail lives on the
 * assignment page (/student/assignments/:id) where the full per-question view
 * is rendered.
 */
export function StudentResultPage() {
  const assignments = useAssignments();
  const published = (assignments.data ?? []).filter((a) => a.status === "published" || a.status === "closed");

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader title="我的成绩" description="已发布作业的成绩一览。点击作业查看每题得分与评语。" />
      {assignments.isLoading ? (
        <Card>
          <div className="text-sm text-muted-foreground">加载中...</div>
        </Card>
      ) : published.length === 0 ? (
        <Card>
          <EmptyState title="暂无成绩" description="教师发布成绩后，作业成绩会出现在这里。" />
        </Card>
      ) : (
        published.map((a) => <ResultRow key={a.id} assignmentId={a.id} name={a.name} />)
      )}
    </div>
  );
}

function ResultRow({ assignmentId, name }: { assignmentId: string; name: string }) {
  const result = useMyStudentResult(assignmentId);
  const { total, max } = summarizeReleasedResults(result.data ?? []);
  const released = (result.data?.length ?? 0) > 0;

  return (
    <Card>
      <div className="flex items-center justify-between">
        <Link to={`/student/assignments/${assignmentId}`} className="font-medium text-primary hover:underline">
          {name}
        </Link>
        <span className="text-sm text-muted-foreground">
          {released ? `${total} / ${max}` : "未发布"}
        </span>
      </div>
    </Card>
  );
}

export function summarizeReleasedResults(results: GradeResult[]) {
  const scoreable = results.flatMap((result) => {
    const score = result.effective_score ?? result.score ?? result.ai_score;
    return result.result_status === "failed" || typeof score !== "number" || !Number.isFinite(score)
      ? []
      : [{ score, maxScore: result.ai_max_score }];
  });
  return {
    total: scoreable.reduce((sum, result) => sum + result.score, 0),
    max: scoreable.reduce((sum, result) => sum + result.maxScore, 0),
  };
}
