import { useParams } from "react-router-dom";
import {
  useAssignment,
  useGradingRuns,
  useReleaseGradingRun,
  useReviewGradeResult,
  useReviewQueue,
  useStartGradingRun,
} from "@/api/hooks/education";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Assignment grading: start a run, show the review queue (failed / needs_review
 * results, never real zeros), resolve a result with an override score, and
 * release. Release is blocked while unresolved failures remain — the backend
 * returns result_not_releasable, surfaced here as a disabled button + hint.
 */
export function AssignmentGradingPage() {
  const { assignmentId } = useParams<{ assignmentId: string }>();
  const assignment = useAssignment(assignmentId);
  const runs = useGradingRuns(assignmentId);
  const reviewQueue = useReviewQueue(assignmentId);
  const startRun = useStartGradingRun();
  const review = useReviewGradeResult();
  const release = useReleaseGradingRun();

  if (assignment.isLoading) return <Card>加载中...</Card>;
  if (assignment.isError || !assignment.data) return <Card className="text-danger">作业不存在或无权访问。</Card>;

  const latestRun = runs.data?.[runs.data.length - 1];
  const released = latestRun?.released_at != null;

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <SectionHeader
        title={`批改：${assignment.data.name}`}
        description="启动批改运行、复核异常结果、发布成绩。未解决异常前无法发布。"
        action={
          <div className="flex gap-2">
            <Button
              onClick={() => assignmentId && startRun.mutate(assignmentId)}
              disabled={startRun.isPending}
            >
              启动批改
            </Button>
            <Button
              variant="secondary"
              onClick={() => latestRun && release.mutate(latestRun.id)}
              disabled={release.isPending || !latestRun || reviewQueue.data?.length ? true : false}
            >
              发布成绩
            </Button>
          </div>
        }
      />

      {latestRun ? (
        <Card className="text-sm">
          <div className="flex justify-between">
            <span>最近运行：{latestRun.status}</span>
            <span>完成 {latestRun.completed_submissions}/{latestRun.total_submissions}</span>
          </div>
          {released ? <p className="mt-2 text-emerald-600">已发布给学生。</p> : null}
        </Card>
      ) : null}

      <div>
        <h2 className="mb-2 text-sm font-semibold">复核队列</h2>
        {reviewQueue.isLoading ? (
          <Card>加载中...</Card>
        ) : !reviewQueue.data || reviewQueue.data.length === 0 ? (
          <EmptyState title="无待复核项" description="没有失败或需人工复核的结果。" />
        ) : (
          <Card className="p-0">
            <table className="w-full text-sm">
              <thead className="border-b text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-4 py-2">题号</th>
                  <th className="px-4 py-2">状态</th>
                  <th className="px-4 py-2">原因</th>
                  <th className="px-4 py-2 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {reviewQueue.data.map((r) => (
                  <tr key={r.id} className="border-b last:border-0">
                    <td className="px-4 py-2 font-mono">{r.q_id}</td>
                    <td className="px-4 py-2">{r.result_status}</td>
                    <td className="px-4 py-2 text-muted-foreground">{r.review_reason ?? "—"}</td>
                    <td className="px-4 py-2 text-right">
                      <Button
                        variant="secondary"
                        className="h-8"
                        disabled={review.isPending}
                        onClick={() =>
                          review.mutate({
                            gradeResultId: r.id,
                            newScore: r.ai_max_score ?? 10,
                            newComment: "已复核通过",
                          })
                        }
                      >
                        复核通过
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
      </div>
    </div>
  );
}
