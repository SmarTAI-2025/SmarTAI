import { Link, useParams } from "react-router-dom";
import { useAssignments, useCourse } from "@/api/hooks/education";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Student course detail: lists published assignments for an enrolled course.
 * The backend returns only published assignments a student can access; a
 * closed/published assignment is still visible so the student can submit (until
 * closed) or view results.
 */
export function StudentCourseDetailPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const course = useCourse(courseId);
  const assignments = useAssignments(courseId);

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader title={course.data?.name ?? "课程"} description="已发布的作业。点击进入作答或查看成绩。" />
      <Card className="p-0">
        {assignments.isLoading ? (
          <div className="p-6 text-sm text-muted-foreground">加载中...</div>
        ) : assignments.isError ? (
          <div className="p-6 text-sm text-danger">加载失败，请稍后重试。</div>
        ) : !assignments.data || assignments.data.length === 0 ? (
          <EmptyState title="暂无作业" description="课程还没有已发布的作业。" />
        ) : (
          <table className="w-full text-sm">
            <thead className="border-b text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-4 py-2">作业</th>
                <th className="px-4 py-2">状态</th>
                <th className="px-4 py-2">题目数</th>
                <th className="px-4 py-2 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {assignments.data.map((a) => (
                <tr key={a.id} className="border-b last:border-0">
                  <td className="px-4 py-2 font-medium">{a.name}</td>
                  <td className="px-4 py-2 text-muted-foreground">{a.status}</td>
                  <td className="px-4 py-2">{a.question_count}</td>
                  <td className="px-4 py-2 text-right">
                    <Link to={`/student/assignments/${a.id}`} className="text-primary hover:underline">
                      作答 / 成绩
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
