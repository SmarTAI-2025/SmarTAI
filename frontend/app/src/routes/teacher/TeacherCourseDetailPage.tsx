import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAssignments, useCourse, useCreateAssignment, useEnrollStudents } from "@/api/hooks/education";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Teacher course detail: enroll students by id and list the course's
 * assignments with a create form. Enrolling an unknown id is a no-op on the
 * backend; assignment creation requires the course to be owned (404 otherwise).
 */
export function TeacherCourseDetailPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const course = useCourse(courseId);
  const assignments = useAssignments(courseId);
  const createAssignment = useCreateAssignment();
  const enrollStudents = useEnrollStudents();
  const [studentIds, setStudentIds] = useState("");
  const [name, setName] = useState("");

  function enroll(e: React.FormEvent) {
    e.preventDefault();
    const ids = studentIds.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    if (!courseId || ids.length === 0) return;
    enrollStudents.mutate(
      { courseId, studentIds: ids },
      { onSuccess: () => setStudentIds("") },
    );
  }

  function createAsg(e: React.FormEvent) {
    e.preventDefault();
    if (!courseId || !name.trim()) return;
    createAssignment.mutate(
      { course_id: courseId, name: name.trim() },
      { onSuccess: () => setName("") },
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader
        title={course.data?.name ?? "课程"}
        description="在此报名学生、查看与新建作业。"
      />
      <Card>
        <form className="grid gap-4 sm:grid-cols-[1fr_auto]" onSubmit={enroll}>
          <Field label="报名学生 ID（逗号或换行分隔）">
            <Input value={studentIds} onChange={(e) => setStudentIds(e.target.value)} placeholder="u_stu1, u_stu2" />
          </Field>
          <div className="flex items-end">
            <Button type="submit" disabled={enrollStudents.isPending || !studentIds.trim()}>
              报名
            </Button>
          </div>
        </form>
        <p className="mt-2 text-xs text-muted-foreground">
          已报名 {course.data?.student_count ?? 0} 名学生。
        </p>
      </Card>

      <Card className="p-0">
        <div className="flex items-center justify-between p-4">
          <h2 className="text-sm font-semibold">作业</h2>
        </div>
        <div className="border-t">
          {assignments.isLoading ? (
            <div className="p-6 text-sm text-muted-foreground">加载中...</div>
          ) : assignments.isError ? (
            <div className="p-6 text-sm text-danger">加载失败，请稍后重试。</div>
          ) : !assignments.data || assignments.data.length === 0 ? (
            <EmptyState title="暂无作业" description="新建第一份作业后即可添加题目并发布。" />
          ) : (
            <table className="w-full text-sm">
              <thead className="border-b text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-4 py-2">作业</th>
                  <th className="px-4 py-2">状态</th>
                  <th className="px-4 py-2">题目数</th>
                  <th className="px-4 py-2">版本</th>
                </tr>
              </thead>
              <tbody>
                {assignments.data.map((a) => (
                  <tr key={a.id} className="border-b last:border-0">
                    <td className="px-4 py-2 font-medium">
                      <Link to={`/teacher/assignments/${a.id}`} className="text-primary hover:underline">
                        {a.name}
                      </Link>
                    </td>
                    <td className="px-4 py-2 text-muted-foreground">{a.status}</td>
                    <td className="px-4 py-2">{a.question_count}</td>
                    <td className="px-4 py-2 text-muted-foreground">{a.version}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Card>

      <Card>
        <form className="grid gap-4 sm:grid-cols-[1fr_auto]" onSubmit={createAsg}>
          <Field label="新建作业">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="第一次作业" />
          </Field>
          <div className="flex items-end">
            <Button type="submit" disabled={createAssignment.isPending || !name.trim()}>
              新建作业
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}
