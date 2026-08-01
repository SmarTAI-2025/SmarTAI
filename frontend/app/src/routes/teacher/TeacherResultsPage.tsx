import { Link, useParams } from "react-router-dom";
import { useAssignment, usePerQuestionAggregates, useTeacherSummary } from "@/api/hooks/education";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

type Summary = { students?: { student_id: string; total: number }[]; graded_count?: number; needs_review_count?: number };
type QuestionAggregate = { q_id: string; count: number; max_score: number; mean: number; min: number; max: number };

export function TeacherResultsPage() {
  const { assignmentId } = useParams<{ assignmentId: string }>();
  const assignment = useAssignment(assignmentId);
  const summary = useTeacherSummary(assignmentId);
  const byQuestion = usePerQuestionAggregates(assignmentId);
  const summaryData = (summary.data ?? {}) as Summary;
  const questionData = (byQuestion.data ?? []) as QuestionAggregate[];

  if (assignment.isLoading) return <Card>加载中...</Card>;
  if (!assignment.data) return <Card className="text-danger">作业不存在或无权访问。</Card>;

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader
        title={`结果：${assignment.data.name}`}
        description="查看已发布运行的学生总分和按题统计。"
        action={<Link className="text-sm text-primary hover:underline" to={`/teacher/assignments/${assignmentId}/grading`}>批改与复核</Link>}
      />
      <div className="grid gap-4 sm:grid-cols-3">
        <Card><div className="text-xs text-muted-foreground">已评分</div><div className="mt-1 text-2xl font-semibold">{summaryData.graded_count ?? 0}</div></Card>
        <Card><div className="text-xs text-muted-foreground">待复核</div><div className="mt-1 text-2xl font-semibold">{summaryData.needs_review_count ?? 0}</div></Card>
        <Card><div className="text-xs text-muted-foreground">学生数</div><div className="mt-1 text-2xl font-semibold">{summaryData.students?.length ?? 0}</div></Card>
      </div>
      <Card className="p-0">
        <div className="p-4 text-sm font-semibold">学生总分</div>
        {summaryData.students?.length ? (
          <table className="w-full text-sm"><thead className="border-y text-left text-xs text-muted-foreground"><tr><th className="px-4 py-2">学生</th><th className="px-4 py-2">总分</th></tr></thead><tbody>
            {summaryData.students.map((student) => <tr key={student.student_id} className="border-b last:border-0"><td className="px-4 py-2 font-mono text-xs">{student.student_id}</td><td className="px-4 py-2">{student.total}</td></tr>)}
          </tbody></table>
        ) : <EmptyState title="暂无已发布结果" description="发布成绩后，学生结果会出现在这里。" />}
      </Card>
      <Card className="p-0">
        <div className="p-4 text-sm font-semibold">按题统计</div>
        {questionData.length ? (
          <table className="w-full text-sm"><thead className="border-y text-left text-xs text-muted-foreground"><tr><th className="px-4 py-2">题号</th><th className="px-4 py-2">均分</th><th className="px-4 py-2">最低 / 最高</th><th className="px-4 py-2">样本</th></tr></thead><tbody>
            {questionData.map((item) => <tr key={item.q_id} className="border-b last:border-0"><td className="px-4 py-2 font-mono text-xs">{item.q_id}</td><td className="px-4 py-2">{item.mean} / {item.max_score}</td><td className="px-4 py-2">{item.min} / {item.max}</td><td className="px-4 py-2">{item.count}</td></tr>)}
          </tbody></table>
        ) : <EmptyState title="暂无题目统计" description="发布一轮成绩后才能计算统计。" />}
      </Card>
    </div>
  );
}
