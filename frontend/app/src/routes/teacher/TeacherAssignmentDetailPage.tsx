import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  useAddQuestion,
  useAssignment,
  useGradingRuns,
  usePublishAssignment,
  useQuestions,
  useStartGradingRun,
} from "@/api/hooks/education";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Teacher assignment detail: add questions to a draft, publish (optimistic-lock
 * version carried in the request body; a stale client gets 409 version_conflict),
 * start a grading run, and list prior runs. Questions are frozen once published.
 */
export function TeacherAssignmentDetailPage() {
  const { assignmentId } = useParams<{ assignmentId: string }>();
  const assignment = useAssignment(assignmentId);
  const questions = useQuestions(assignmentId);
  const runs = useGradingRuns(assignmentId);
  const addQuestion = useAddQuestion();
  const publish = usePublishAssignment();
  const startRun = useStartGradingRun();

  const editable = assignment.data?.status === "draft" || assignment.data?.status === "ready";

  const [qId, setQId] = useState("");
  const [stem, setStem] = useState("");
  const [type, setType] = useState("short");
  const [maxScore, setMaxScore] = useState(10);

  function addQ(e: React.FormEvent) {
    e.preventDefault();
    // Use the loaded assignment's id (authoritative) rather than the route
    // param, so the mutate fires once data is available even if the route
    // param is absent in tests.
    const id = assignment.data?.id ?? assignmentId;
    if (!id || !qId.trim()) return;
    addQuestion.mutate(
      {
        assignmentId: id,
        input: {
          q_id: qId.trim(),
          order_index: questions.data?.length ?? 0,
          type,
          stem: stem.trim(),
          max_score: maxScore,
        },
      },
      {
        onSuccess: () => {
          setQId("");
          setStem("");
        },
      },
    );
  }

  function doPublish() {
    if (!assignment.data) return;
    publish.mutate(
      { assignmentId: assignment.data.id, expectedVersion: assignment.data.version },
      {},
    );
  }

  function doStartRun() {
    const id = assignment.data?.id ?? assignmentId;
    if (!id) return;
    startRun.mutate(id);
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader
        title={assignment.data?.name ?? "作业"}
        description={`状态：${assignment.data?.status ?? "—"} · 版本 ${assignment.data?.version ?? 0}`}
      />

      <Card className="flex flex-wrap items-center gap-3">
        <Button onClick={doPublish} disabled={publish.isPending || !editable || (questions.data?.length ?? 0) === 0}>
          发布作业
        </Button>
        <Button variant="ghost" onClick={doStartRun} disabled={startRun.isPending || !assignment.data || assignment.data.status === "draft"}>
          启动批改
        </Button>
        <Link to={`/teacher/assignments/${assignmentId}/grading`} className="text-sm text-primary hover:underline">
          查看批改与复核 →
        </Link>
        <Link to={`/teacher/assignments/${assignmentId}/submissions`} className="text-sm text-primary hover:underline">
          查看提交 →
        </Link>
        <Link to={`/teacher/assignments/${assignmentId}/results`} className="text-sm text-primary hover:underline">
          查看结果 →
        </Link>
      </Card>

      <Card className="p-0">
        <div className="p-4 text-sm font-semibold">题目</div>
        <div className="border-t">
          {questions.isLoading ? (
            <div className="p-6 text-sm text-muted-foreground">加载中...</div>
          ) : !questions.data || questions.data.length === 0 ? (
            <EmptyState title="暂无题目" description={editable ? "添加题目后再发布。" : "作业尚无题目。"} />
          ) : (
            <ol className="divide-y">
              {questions.data.map((q, i) => (
                <li key={q.id} className="px-4 py-3 text-sm">
                  <div className="font-medium">
                    {i + 1}. {q.q_id} <span className="text-muted-foreground">({q.type}, {q.max_score} 分)</span>
                  </div>
                  <div className="mt-1 text-muted-foreground">{q.stem || "（无题干）"}</div>
                </li>
              ))}
            </ol>
          )}
        </div>
      </Card>

      {editable ? (
        <Card>
          <form className="grid gap-4" onSubmit={addQ}>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="题目 ID">
                <Input value={qId} onChange={(e) => setQId(e.target.value)} placeholder="q1" />
              </Field>
              <Field label="题型">
                <select className="h-9 rounded-md border bg-background px-2 text-sm" value={type} onChange={(e) => setType(e.target.value)}>
                  <option value="short">short</option>
                  <option value="concept">concept</option>
                  <option value="calculation">calculation</option>
                  <option value="programming">programming</option>
                  <option value="proof">proof</option>
                </select>
              </Field>
            </div>
            <Field label="题干">
              <textarea
                className="min-h-24 w-full rounded-md border bg-background p-2 text-sm"
                value={stem}
                onChange={(e) => setStem(e.target.value)}
                placeholder="题目内容"
              />
            </Field>
            <div className="grid gap-4 sm:grid-cols-[auto_auto]">
              <Field label="满分">
                <Input type="number" value={maxScore} onChange={(e) => setMaxScore(Number(e.target.value))} />
              </Field>
              <div className="flex items-end">
                <Button type="submit" disabled={addQuestion.isPending || !qId.trim()}>
                  添加题目
                </Button>
              </div>
            </div>
          </form>
        </Card>
      ) : null}

      <Card className="p-0">
        <div className="p-4 text-sm font-semibold">批改运行</div>
        <div className="border-t">
          {runs.isLoading ? (
            <div className="p-6 text-sm text-muted-foreground">加载中...</div>
          ) : !runs.data || runs.data.length === 0 ? (
            <EmptyState title="暂无运行" description="发布作业并启动批改后，运行记录会出现在这里。" />
          ) : (
            <table className="w-full text-sm">
              <thead className="border-b text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-4 py-2">运行</th>
                  <th className="px-4 py-2">状态</th>
                  <th className="px-4 py-2">完成/失败</th>
                  <th className="px-4 py-2 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.map((r) => (
                  <tr key={r.id} className="border-b last:border-0">
                    <td className="px-4 py-2 font-mono text-xs">{r.id.slice(0, 10)}</td>
                    <td className="px-4 py-2">{r.status}</td>
                    <td className="px-4 py-2">{r.completed_submissions}/{r.failed_submissions}</td>
                    <td className="px-4 py-2 text-right">
                      <Link to={`/teacher/assignments/${assignmentId}/grading?run=${r.id}`} className="text-primary hover:underline">
                        复核 / 发布
                      </Link>
                    </td>
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
