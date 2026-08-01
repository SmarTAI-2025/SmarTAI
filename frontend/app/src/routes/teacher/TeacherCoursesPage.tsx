import { useState } from "react";
import { Link } from "react-router-dom";
import { useCourses, useCreateCourse, useDeleteCourse } from "@/api/hooks/education";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Teacher course management: create a course, list owned courses with student
 * counts, and delete one. Deletion is owner-scoped on the backend (a non-owner
 * gets 404, never a payload leak). Assignment management lives one level down
 * at /teacher/courses/:courseId.
 */
export function TeacherCoursesPage() {
  const courses = useCourses();
  const createCourse = useCreateCourse();
  const deleteCourse = useDeleteCourse();
  const [name, setName] = useState("");
  const [code, setCode] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    createCourse.mutate(
      { name: name.trim(), code: code.trim() || undefined },
      {
        onSuccess: () => {
          setName("");
          setCode("");
        },
      },
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader title="课程" description="新建课程、查看已加入学生数并管理课程。" />
      <Card>
        <form className="grid gap-4 sm:grid-cols-[1fr_auto_auto]" onSubmit={submit}>
          <Field label="课程名称">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="高等数学" />
          </Field>
          <Field label="课程代码（可选）">
            <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="MATH101" />
          </Field>
          <div className="flex items-end">
            <Button type="submit" disabled={createCourse.isPending || !name.trim()}>
              新建课程
            </Button>
          </div>
        </form>
      </Card>

      <Card className="p-0">
        {courses.isLoading ? (
          <div className="p-6 text-sm text-muted-foreground">加载中...</div>
        ) : courses.isError ? (
          <div className="p-6 text-sm text-danger">加载失败，请稍后重试。</div>
        ) : !courses.data || courses.data.length === 0 ? (
          <EmptyState title="暂无课程" description="新建第一门课程后即可添加作业。" />
        ) : (
          <table className="w-full text-sm">
            <thead className="border-b text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-4 py-2">课程</th>
                <th className="px-4 py-2">代码</th>
                <th className="px-4 py-2">学生数</th>
                <th className="px-4 py-2 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {courses.data.map((c) => (
                <tr key={c.id} className="border-b last:border-0">
                  <td className="px-4 py-2 font-medium">
                    <Link to={`/teacher/courses/${c.id}`} className="text-primary hover:underline">
                      {c.name}
                    </Link>
                  </td>
                  <td className="px-4 py-2 text-muted-foreground">{c.code || "—"}</td>
                  <td className="px-4 py-2">{c.student_count}</td>
                  <td className="px-4 py-2 text-right">
                    <Button
                      variant="ghost"
                      className="h-7"
                      onClick={() => deleteCourse.mutate(c.id)}
                      disabled={deleteCourse.isPending}
                    >
                      删除
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
