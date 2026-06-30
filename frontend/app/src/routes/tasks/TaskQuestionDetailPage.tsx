import { ArrowLeftRight, UserRound } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { ResultsLayout } from "@/components/tasks/ResultsLayout";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

export function TaskQuestionDetailPage() {
  const { taskId = "draft", questionId } = useParams();
  return (
    <ResultsLayout
      context="question-detail"
      title={`题目详情 ${questionId ?? ""}`}
      description="查看单题的全班表现，并从任一作答跳回对应学生详情。"
    >
      <Card className="grid gap-4">
        <div className="flex items-start gap-3">
          <span className="rounded-md bg-muted p-2 text-accent">
            <ArrowLeftRight className="h-5 w-5" />
          </span>
          <div>
            <h2 className="text-base font-semibold">跨导航占位</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              后续从这里查看题干、评分标准、得分分布、典型错误和全班作答；点击学生可切回该学生逐题详情。
            </p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link to={`/tasks/${taskId}/results/student-a`}>
            <Button type="button" variant="secondary">
              <UserRound className="h-4 w-4" />
              查看学生详情
            </Button>
          </Link>
          <Link to={`/tasks/${taskId}/results?view=questions`}>
            <Button type="button" variant="secondary">
              返回按题分析
            </Button>
          </Link>
        </div>
      </Card>
      <EmptyState title="单题全班详情待接入" description="后续展示题干、评分标准、统计、AI 易错点和全班作答。" />
    </ResultsLayout>
  );
}
