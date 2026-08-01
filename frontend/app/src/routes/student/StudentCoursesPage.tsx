import { Link } from "react-router-dom";
import { useCourses } from "@/api/hooks/education";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Student course list. The backend /courses endpoint is role-scoped: a student
 * sees only courses they are enrolled in (resolved via course_enrollments, not
 * the legacy course_ids mirror). Each course links to its published assignments.
 */
export function StudentCoursesPage() {
  const courses = useCourses();

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader title="我的课程" description="已加入的课程及其已发布作业。" />
      <Card className="p-0">
        {courses.isLoading ? (
          <div className="p-6 text-sm text-muted-foreground">加载中...</div>
        ) : courses.isError ? (
          <div className="p-6 text-sm text-danger">加载失败，请稍后重试。</div>
        ) : !courses.data || courses.data.length === 0 ? (
          <EmptyState title="暂无课程" description="老师把你加入课程后，这里会显示。" />
        ) : (
          <table className="w-full text-sm">
            <thead className="border-b text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-4 py-2">课程</th>
                <th className="px-4 py-2">代码</th>
                <th className="px-4 py-2 text-right">作业</th>
              </tr>
            </thead>
            <tbody>
              {courses.data.map((c) => (
                <tr key={c.id} className="border-b last:border-0">
                  <td className="px-4 py-2 font-medium">
                    <Link to={`/student/courses/${c.id}`} className="text-primary hover:underline">
                      {c.name}
                    </Link>
                  </td>
                  <td className="px-4 py-2 text-muted-foreground">{c.code || "—"}</td>
                  <td className="px-4 py-2 text-right">
                    <Link to={`/student/courses/${c.id}`} className="text-primary hover:underline">
                      查看
                    </Link>
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
