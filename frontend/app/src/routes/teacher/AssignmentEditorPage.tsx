import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  useAddQuestion,
  useAssignment,
  useImportQuestionsFile,
  usePublishAssignment,
  useQuestions,
} from "@/api/hooks/education";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input, Textarea } from "@/components/ui/Input";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Assignment editor: list questions (ordered), add a question to a draft/ready
 * assignment, and publish once at least one question exists. Publishing sends
 * the current version; a stale version surfaces as a 409 the toast layer shows.
 * The question set is frozen after publish; further edits require a new
 * assignment version (out of scope here).
 */
export function AssignmentEditorPage() {
  const { assignmentId } = useParams<{ assignmentId: string }>();
  const assignment = useAssignment(assignmentId);
  const questions = useQuestions(assignmentId);
  const addQuestion = useAddQuestion();
  const importQuestionsFile = useImportQuestionsFile();
  const publish = usePublishAssignment();

  const [qId, setQId] = useState("");
  const [stem, setStem] = useState("");
  const [type, setType] = useState("short");
  const [maxScore, setMaxScore] = useState(10);
  const [questionFile, setQuestionFile] = useState<File | null>(null);
  const [bulkPayload, setBulkPayload] = useState("");
  const [bulkError, setBulkError] = useState<string | null>(null);

  const editable = assignment.data?.status === "draft" || assignment.data?.status === "ready";

  function add(e: React.FormEvent) {
    e.preventDefault();
    if (!qId.trim() || !assignmentId) return;
    addQuestion.mutate(
      {
        assignmentId,
        input: { q_id: qId.trim(), order_index: (questions.data?.length ?? 0), type, stem, max_score: maxScore },
      },
      { onSuccess: () => { setQId(""); setStem(""); } },
    );
  }

  function publishAssignment() {
    if (!assignmentId || !assignment.data) return;
    publish.mutate({ assignmentId, expectedVersion: assignment.data.version });
  }

  function importQuestionUpload() {
    if (!assignmentId || !questionFile) return;
    importQuestionsFile.mutate(
      { assignmentId, file: questionFile },
      { onSuccess: () => setQuestionFile(null) },
    );
  }

  async function importQuestions() {
    if (!assignmentId) return;
    setBulkError(null);
    try {
      const items = JSON.parse(bulkPayload) as Array<Record<string, unknown>>;
      if (!Array.isArray(items)) throw new Error("JSON 必须是数组");
      for (const [index, item] of items.entries()) {
        const qId = typeof item.q_id === "string" ? item.q_id.trim() : "";
        const itemType = typeof item.type === "string" ? item.type : "short";
        const itemStem = typeof item.stem === "string" ? item.stem : "";
        const itemMaxScore = typeof item.max_score === "number" ? item.max_score : 10;
        if (!qId) throw new Error(`第 ${index + 1} 项缺少 q_id`);
        await addQuestion.mutateAsync({
          assignmentId,
          input: { q_id: qId, order_index: (questions.data?.length ?? 0) + index, type: itemType, stem: itemStem, max_score: itemMaxScore },
        });
      }
      setBulkPayload("");
    } catch (cause) {
      setBulkError(cause instanceof Error ? cause.message : "导入失败");
    }
  }

  if (assignment.isLoading) return <Card>加载中...</Card>;
  if (assignment.isError || !assignment.data) return <Card className="text-danger">作业不存在或无权访问。</Card>;
  const a = assignment.data;

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <SectionHeader
        title={a.name}
        description={`状态：${a.status} · 版本 ${a.version} · 题目 ${a.question_count}`}
        action={
          <div className="flex gap-2">
            <Link to={`/teacher/assignments/${a.id}/grading`} className="text-sm font-medium text-primary">
              批改与发布
            </Link>
            <Button
              onClick={publishAssignment}
              disabled={publish.isPending || a.question_count === 0 || !editable}
            >
              发布
            </Button>
          </div>
        }
      />

      {editable ? (
        <Card>
          <form className="grid gap-3" onSubmit={add}>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="题目 ID">
                <Input value={qId} onChange={(e) => setQId(e.target.value)} placeholder="q1" />
              </Field>
              <Field label="题型">
                <select className="h-9 rounded-md border bg-background px-2 text-sm" value={type} onChange={(e) => setType(e.target.value)}>
                  <option value="short">简答</option>
                  <option value="calculation">计算</option>
                  <option value="programming">编程</option>
                  <option value="proof">证明</option>
                </select>
              </Field>
            </div>
            <Field label="题干">
              <Textarea value={stem} onChange={(e) => setStem(e.target.value)} placeholder="题干内容…" />
            </Field>
            <Field label="满分">
              <Input type="number" value={maxScore} onChange={(e) => setMaxScore(Number(e.target.value))} />
            </Field>
            <div>
              <Button type="submit" disabled={addQuestion.isPending || !qId.trim()}>添加题目</Button>
            </div>
          </form>
        </Card>
      ) : (
        <Card className="text-sm text-muted-foreground">
          作业已发布，题目集已冻结。如需修改请新建作业版本。
        </Card>
      )}

      {editable ? (
        <Card>
          <div className="grid gap-3">
            <label className="grid gap-2 text-sm font-medium">
              OCR 导入题目文件
              <input
                type="file"
                accept=".txt,.md,.csv,.pdf,.jpg,.jpeg,.png,.webp"
                onChange={(event) => setQuestionFile(event.target.files?.[0] ?? null)}
                className="block w-full text-sm"
              />
            </label>
            <p className="text-xs text-muted-foreground">
              图片和扫描 PDF 会先进行 OCR，再自动拆分题目并保存原文件。
            </p>
            <div>
              <Button
                onClick={importQuestionUpload}
                disabled={importQuestionsFile.isPending || !questionFile}
              >
                {importQuestionsFile.isPending ? "识别中…" : "识别并导入"}
              </Button>
            </div>
            {importQuestionsFile.data ? (
              <p className="text-sm text-muted-foreground">
                已导入 {importQuestionsFile.data.length} 道题。
              </p>
            ) : null}
          </div>
        </Card>
      ) : null}

      {editable ? (
        <Card>
          <label className="grid gap-2 text-sm font-medium">
            批量题目 JSON
            <Textarea
              value={bulkPayload}
              onChange={(event) => setBulkPayload(event.target.value)}
              placeholder='[{"q_id":"q1","type":"short","stem":"题干","max_score":10}]'
              className="min-h-28 font-mono text-xs"
            />
          </label>
          {bulkError ? <p className="mt-2 text-sm text-danger">{bulkError}</p> : null}
          <Button className="mt-3" onClick={importQuestions} disabled={addQuestion.isPending || !bulkPayload.trim()}>
            导入题目
          </Button>
        </Card>
      ) : null}

      {questions.isLoading ? (
        <Card>加载中...</Card>
      ) : !questions.data || questions.data.length === 0 ? (
        <EmptyState title="还没有题目" description={editable ? "在上方添加第一题。" : "该作业暂无题目。"} />
      ) : (
        <Card className="p-0">
          <ol className="divide-y">
            {questions.data.map((q, i) => (
              <li key={q.id} className="px-4 py-3 text-sm">
                <div className="flex items-center justify-between">
                  <span className="font-medium">{i + 1}. {q.q_id} · {q.type} · {q.max_score} 分</span>
                </div>
                <p className="mt-1 text-muted-foreground">{q.stem || "（无题干）"}</p>
              </li>
            ))}
          </ol>
        </Card>
      )}
    </div>
  );
}
