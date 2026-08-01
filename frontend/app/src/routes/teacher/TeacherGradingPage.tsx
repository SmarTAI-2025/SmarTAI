import { useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import {
  useGradingRuns,
  useReleaseGradingRun,
  useReviewGradeResult,
  useReviewQueue,
} from "@/api/hooks/education";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Teacher grading / review / release workspace. The review queue lists
 * failed / needs_review results across the assignment's runs; the teacher can
 * override a score (a review row, the AI original stays immutable) and then
 * release the run. Release is blocked while unresolved results remain, so the
 * release button reflects the latest run's terminal state.
 */
export function TeacherGradingPage() {
  const { assignmentId } = useParams<{ assignmentId: string }>();
  const [params] = useSearchParams();
  const preferredRun = params.get("run") ?? undefined;
  const runs = useGradingRuns(assignmentId);
  const reviewQueue = useReviewQueue(assignmentId);
  const review = useReviewGradeResult();
  const release = useReleaseGradingRun();

  const latestRun = useMemo(() => {
    const list = runs.data ?? [];
    if (preferredRun) {
      const found = list.find((r) => r.id === preferredRun);
      if (found) return found;
    }
    return list[list.length - 1];
  }, [runs.data, preferredRun]);

  const [edits, setEdits] = useState<Record<string, { score: string; comment: string }>>({});

  function submitReview(resultId: string) {
    const edit = edits[resultId];
    if (!edit || !assignmentId) return;
    review.mutate({
      gradeResultId: resultId,
      newScore: Number(edit.score),
      newComment: edit.comment,
    });
  }

  function doRelease() {
    if (!latestRun) return;
    release.mutate(latestRun.id);
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader title="批改与复核" description="复核失败 / 待复核结果，覆盖分数后发布成绩。" />

      {latestRun ? (
        <Card className="flex flex-wrap items-center gap-3">
          <div className="text-sm">
            当前运行 <code className="font-mono">{latestRun.id.slice(0, 10)}</code> · {latestRun.status}
            {latestRun.released_at ? " · 已发布" : " · 未发布"}
          </div>
          <Button
            onClick={doRelease}
            disabled={
              release.isPending ||
              !["completed", "partial_failed"].includes(latestRun.status)
            }
          >
            发布成绩
          </Button>
        </Card>
      ) : null}

      <Card className="p-0">
        <div className="p-4 text-sm font-semibold">复核队列</div>
        <div className="border-t">
          {reviewQueue.isLoading ? (
            <div className="p-6 text-sm text-muted-foreground">加载中...</div>
          ) : reviewQueue.isError ? (
            <div className="p-6 text-sm text-danger">加载失败，请稍后重试。</div>
          ) : !reviewQueue.data || reviewQueue.data.length === 0 ? (
            <EmptyState title="队列为空" description="没有失败或待复核的结果，可以直接发布成绩。" />
          ) : (
            <table className="w-full text-sm">
              <thead className="border-b text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-4 py-2">学生</th>
                  <th className="px-4 py-2">题号</th>
                  <th className="px-4 py-2">状态</th>
                  <th className="px-4 py-2">原因</th>
                  <th className="px-4 py-2">覆盖分数</th>
                  <th className="px-4 py-2 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {reviewQueue.data.map((r) => (
                  <tr key={r.id} className="border-b last:border-0">
                    <td className="px-4 py-2 font-mono text-xs">{r.student_id.slice(0, 12)}</td>
                    <td className="px-4 py-2">{r.q_id}</td>
                    <td className="px-4 py-2">{r.result_status}</td>
                    <td className="px-4 py-2 text-muted-foreground">{r.review_reason ?? "—"}</td>
                    <td className="px-4 py-2">
                      <div className="flex gap-2">
                        <Input
                          className="h-8 w-20"
                          type="number"
                          min={0}
                          max={r.ai_max_score}
                          value={edits[r.id]?.score ?? ""}
                          onChange={(e) =>
                            setEdits((s) => ({ ...s, [r.id]: { ...s[r.id] ?? { comment: "" }, score: e.target.value } }))
                          }
                        />
                        <Input
                          className="h-8 min-w-40"
                          value={edits[r.id]?.comment ?? ""}
                          placeholder="复核评语"
                          onChange={(e) =>
                            setEdits((s) => ({ ...s, [r.id]: { ...s[r.id] ?? { score: "" }, comment: e.target.value } }))
                          }
                        />
                      </div>
                    </td>
                    <td className="px-4 py-2 text-right">
                      <Button
                        variant="ghost"
                        className="h-7"
                        onClick={() => submitReview(r.id)}
                        disabled={review.isPending || !edits[r.id]?.score}
                      >
                        提交复核
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Card>

      <Card>
        <p className="text-xs text-muted-foreground">
          AI 原始分数不可覆盖；复核会新增一条教师调整记录，展示值取最新复核。
        </p>
      </Card>
    </div>
  );
}
