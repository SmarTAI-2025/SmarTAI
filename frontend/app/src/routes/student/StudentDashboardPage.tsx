import { Link } from "react-router-dom";
import { useCourses } from "@/api/hooks/education";
import { useCurrentUser } from "@/api/hooks";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Student landing page: the courses the student is enrolled in (the backend
 * list-courses endpoint returns only enrolled courses for a student role).
 * Each course links to its published-assignment list.
 */
export function StudentDashboardPage() {
  const currentUser = useCurrentUser();
  const courses = useCourses();

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader
        title="学生工作台"
        description={`欢迎回来，${currentUser.data?.username ?? "同学"}。进入课程查看作业。`}
      />
      {courses.isLoading ? (
        <Card>加载中...</Card>
      ) : courses.isError ? (
        <Card className="text-danger">加载失败，请稍后重试。</Card>
      ) : !courses.data || courses.data.length === 0 ? (
        <EmptyState title="还没有课程" description="等待教师把你加入课程后即可看到作业。" />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {courses.data.map((c) => (
            <Link key={c.id} to={`/student/courses/${c.id}`}>
              <Card className="transition hover:border-primary">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="font-semibold">{c.name}</p>
                    <p className="text-xs text-muted-foreground">{c.code || "—"}</p>
                  </div>
                  <span className="text-sm text-muted-foreground">{c.student_count} 名同学</span>
                </div>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
