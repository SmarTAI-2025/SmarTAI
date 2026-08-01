import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  useAssignment,
  useSubmissions,
  useTeacherImport,
  useTeacherUploadSubmission,
} from "@/api/hooks/education";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Input, Textarea } from "@/components/ui/Input";

type ImportItem = {
  student_id: string;
  file_name?: string;
  answers: { q_id: string; content: string }[];
};

export function TeacherSubmissionsPage() {
  const { assignmentId } = useParams<{ assignmentId: string }>();
  const assignment = useAssignment(assignmentId);
  const submissions = useSubmissions(assignmentId);
  const importMutation = useTeacherImport();
  const uploadMutation = useTeacherUploadSubmission();
  const [payload, setPayload] = useState("");
  const [studentId, setStudentId] = useState("");
  const [submissionFile, setSubmissionFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);

  function importSubmissions() {
    if (!assignmentId) return;
    setError(null);
    try {
      const items = JSON.parse(payload) as ImportItem[];
      if (!Array.isArray(items)) throw new Error("JSON 必须是数组");
      importMutation.mutate({ assignmentId, items });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "JSON 无法解析");
    }
  }

  function uploadSubmission() {
    if (!assignmentId || !studentId.trim() || !submissionFile) return;
    uploadMutation.mutate(
      {
        assignmentId,
        studentId: studentId.trim(),
        file: submissionFile,
      },
      {
        onSuccess: () => {
          setStudentId("");
          setSubmissionFile(null);
        },
      },
    );
  }

  if (assignment.isLoading) return <Card>加载中...</Card>;
  if (!assignment.data) return <Card className="text-danger">作业不存在或无权访问。</Card>;

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader
        title={`提交：${assignment.data.name}`}
        description="查看当前修订，上传手写作业或导入教师代提交。"
        action={<Link className="text-sm text-primary hover:underline" to={`/teacher/assignments/${assignmentId}/grading`}>批改与复核</Link>}
      />
      <Card>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="grid gap-2 text-sm font-medium">
            学生 ID
            <Input
              value={studentId}
              onChange={(event) => setStudentId(event.target.value)}
              placeholder="student-1"
            />
          </label>
          <label className="grid gap-2 text-sm font-medium">
            手写作业文件
            <input
              type="file"
              accept=".txt,.md,.csv,.pdf,.jpg,.jpeg,.png,.webp,.zip"
              onChange={(event) => setSubmissionFile(event.target.files?.[0] ?? null)}
              className="block w-full text-sm"
            />
          </label>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          图片和扫描 PDF 会先 OCR；识别出的姓名或学号不会覆盖这里指定的学生 ID。
        </p>
        <Button
          className="mt-3"
          onClick={uploadSubmission}
          disabled={uploadMutation.isPending || !studentId.trim() || !submissionFile}
        >
          {uploadMutation.isPending ? "识别并导入中…" : "识别并导入"}
        </Button>
        {uploadMutation.data ? (
          <p className="mt-2 text-sm text-muted-foreground">
            已创建第 {uploadMutation.data.revision_number} 个提交版本。
          </p>
        ) : null}
      </Card>
      <Card>
        <label className="grid gap-2 text-sm font-medium">
          批量导入 JSON
          <Textarea
            value={payload}
            onChange={(event) => setPayload(event.target.value)}
            placeholder='[{"student_id":"student-1","answers":[{"q_id":"q1","content":"..."}]}]'
            className="min-h-32 font-mono text-xs"
          />
        </label>
        {error ? <p className="mt-2 text-sm text-danger">{error}</p> : null}
        <Button className="mt-3" onClick={importSubmissions} disabled={importMutation.isPending || !payload.trim()}>
          导入提交
        </Button>
        {importMutation.data ? (
          <p className="mt-2 text-sm text-muted-foreground">
            成功 {importMutation.data.succeeded.length} 项，失败 {importMutation.data.failed.length} 项
          </p>
        ) : null}
      </Card>
      <Card className="p-0">
        <div className="p-4 text-sm font-semibold">当前提交</div>
        <div className="border-t">
          {submissions.isLoading ? <div className="p-6 text-sm text-muted-foreground">加载中...</div> : null}
          {!submissions.isLoading && !submissions.data?.length ? <EmptyState title="暂无提交" description="学生提交或导入后会出现在这里。" /> : null}
          {submissions.data?.length ? (
            <table className="w-full text-sm">
              <thead className="border-b text-left text-xs text-muted-foreground">
                <tr><th className="px-4 py-2">学生</th><th className="px-4 py-2">修订</th><th className="px-4 py-2">更新时间</th></tr>
              </thead>
              <tbody>
                {submissions.data.map((item) => (
                  <tr key={item.id} className="border-b last:border-0">
                    <td className="px-4 py-2 font-mono text-xs">{item.student_id}</td>
                    <td className="px-4 py-2">{item.current_revision_number ?? "—"}</td>
                    <td className="px-4 py-2 text-muted-foreground">{new Date(item.updated_at * 1000).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </div>
      </Card>
    </div>
  );
}
