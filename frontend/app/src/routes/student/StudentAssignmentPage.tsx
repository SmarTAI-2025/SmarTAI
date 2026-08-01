import { useState } from "react";
import { useParams } from "react-router-dom";
import {
  useAssignment,
  useMyStudentResult,
  useQuestions,
  useSubmitOnline,
  useUploadSubmission,
} from "@/api/hooks/education";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Student assignment page: list the questions, accept online answers, submit a
 * new immutable revision, and show released per-question results. Before a run
 * is released the results section shows a "not yet released" notice — students
 * never see draft grades or provider traces.
 */
export function StudentAssignmentPage() {
  const { assignmentId } = useParams<{ assignmentId: string }>();
  const assignment = useAssignment(assignmentId);
  const questions = useQuestions(assignmentId);
  const myResult = useMyStudentResult(assignmentId);
  const submit = useSubmitOnline();
  const upload = useUploadSubmission();

  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submissionFile, setSubmissionFile] = useState<File | null>(null);

  function submitAnswers(e: React.FormEvent) {
    e.preventDefault();
    // Use the loaded assignment's id (authoritative) so the submit fires even
    // when the route param is absent, e.g. in tests with a bare MemoryRouter.
    const id = assignment.data?.id ?? assignmentId;
    if (!id) return;
    const payload = (questions.data ?? []).map((q) => ({
      q_id: q.q_id,
      content: answers[q.q_id] ?? "",
    }));
    submit.mutate({ assignmentId: id, answers: payload }, {});
  }

  function uploadAnswers() {
    const id = assignment.data?.id ?? assignmentId;
    if (!id || !submissionFile) return;
    upload.mutate(
      { assignmentId: id, file: submissionFile },
      { onSuccess: () => setSubmissionFile(null) },
    );
  }

  const open = assignment.data?.status === "published";

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <SectionHeader
        title={assignment.data?.name ?? "作业"}
        description={open ? "作答后提交；每次提交都会生成新的不可改版本。" : `状态：${assignment.data?.status ?? "—"}`}
      />

      {!open ? null : (
        <>
          <form className="space-y-4" onSubmit={submitAnswers}>
            {questions.isLoading ? (
              <Card>加载中...</Card>
            ) : !questions.data || questions.data.length === 0 ? (
              <EmptyState title="暂无题目" description="作业尚无题目。" />
            ) : (
              questions.data.map((q, i) => (
                <Card key={q.id}>
                  <div className="text-sm font-medium">
                    {i + 1}. {q.q_id} <span className="text-muted-foreground">({q.max_score} 分)</span>
                  </div>
                  <p className="mt-1 text-sm text-muted-foreground">{q.stem || "（无题干）"}</p>
                  <textarea
                    className="mt-3 min-h-24 w-full rounded-md border bg-background p-2 text-sm"
                    value={answers[q.q_id] ?? ""}
                    onChange={(e) => setAnswers((s) => ({ ...s, [q.q_id]: e.target.value }))}
                    placeholder="你的作答"
                  />
                </Card>
              ))
            )}
            <Button type="submit" disabled={submit.isPending}>
              提交作答
            </Button>
          </form>

          <Card>
            <label className="grid gap-2 text-sm font-medium">
              上传手写作业
              <input
                type="file"
                accept=".txt,.md,.csv,.pdf,.jpg,.jpeg,.png,.webp,.zip"
                onChange={(event) => setSubmissionFile(event.target.files?.[0] ?? null)}
                className="block w-full text-sm"
              />
            </label>
            <p className="mt-2 text-xs text-muted-foreground">
              图片或扫描 PDF 会先进行 OCR，再按当前作业题目拆分答案。
            </p>
            <Button
              className="mt-3"
              onClick={uploadAnswers}
              disabled={upload.isPending || !submissionFile}
            >
              {upload.isPending ? "识别并提交中…" : "识别并提交"}
            </Button>
            {upload.data ? (
              <p className="mt-2 text-sm text-muted-foreground">
                已创建第 {upload.data.revision_number} 个提交版本。
              </p>
            ) : null}
          </Card>
        </>
      )}

      <Card className="p-0">
        <div className="p-4 text-sm font-semibold">我的成绩</div>
        <div className="border-t">
          {myResult.isLoading ? (
            <div className="p-6 text-sm text-muted-foreground">加载中...</div>
          ) : myResult.isError ? (
            <div className="p-6 text-sm text-danger">加载失败，请稍后重试。</div>
          ) : !myResult.data || myResult.data.length === 0 ? (
            <EmptyState title="暂无成绩" description="教师发布成绩后这里会显示每题得分与评语。" />
          ) : (
            <table className="w-full text-sm">
              <thead className="border-b text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-4 py-2">题号</th>
                  <th className="px-4 py-2">得分</th>
                  <th className="px-4 py-2">评语</th>
                </tr>
              </thead>
              <tbody>
                {myResult.data.map((r) => (
                  <tr key={r.q_id} className="border-b last:border-0">
                    <td className="px-4 py-2 font-medium">{r.q_id}</td>
                    <td className="px-4 py-2">
                      {r.score ?? "—"} / {r.ai_max_score}
                    </td>
                    <td className="px-4 py-2 text-muted-foreground">{r.teacher_comment || r.ai_comment || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Card>
    </div>
  );
}
