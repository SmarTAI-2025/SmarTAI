import { Link } from "react-router-dom";
import { useCourses } from "@/api/hooks/education";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Teacher landing page: the courses they own with student counts, and a quick
 * link to create a new course. Reuses the existing Card/SectionHeader styling;
 * no marketing copy.
 */
export function TeacherDashboardPage() {
  const courses = useCourses();

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader
        title="教师工作台"
        description="你负责的课程与作业。点击课程进入详情。"
        action={<Link to="/teacher/courses" className="text-sm font-medium text-primary">管理课程</Link>}
      />
      {courses.isLoading ? (
        <Card>加载中...</Card>
      ) : courses.isError ? (
        <Card className="text-danger">加载失败，请稍后重试。</Card>
      ) : !courses.data || courses.data.length === 0 ? (
        <EmptyState title="还没有课程" description="去「课程」页创建第一门课。" />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {courses.data.map((c) => (
            <Link key={c.id} to={`/teacher/courses/${c.id}`}>
              <Card className="transition hover:border-primary">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="font-semibold">{c.name}</p>
                    <p className="text-xs text-muted-foreground">{c.code || "—"}</p>
                  </div>
                  <span className="text-sm text-muted-foreground">{c.student_count} 名学生</span>
                </div>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
