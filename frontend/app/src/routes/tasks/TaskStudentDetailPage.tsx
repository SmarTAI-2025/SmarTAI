import { ArrowLeftRight, FileText } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { ResultsLayout } from "@/components/tasks/ResultsLayout";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

export function TaskStudentDetailPage() {
  const { taskId = "draft", studentId } = useParams();
  return (
    <ResultsLayout
      context="student-detail"
      title={`学生详情 ${studentId ?? ""}`}
      description="查看单个学生的逐题表现，并从每一题横跳到单题全班详情。"
    >
      <Card className="grid gap-4">
        <div className="flex items-start gap-3">
          <span className="rounded-md bg-muted p-2 text-accent">
            <ArrowLeftRight className="h-5 w-5" />
          </span>
          <div>
            <h2 className="text-base font-semibold">跨导航占位</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              后续从这里查看该学生每题得分、AI 评语、专家差异与教师复核；点击题号可切到同一题的全班详情。
            </p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link to={`/tasks/${taskId}/questions/q1`}>
            <Button type="button" variant="secondary">
              <FileText className="h-4 w-4" />
              查看 Q1 全班详情
            </Button>
          </Link>
          <Link to={`/tasks/${taskId}/results`}>
            <Button type="button" variant="secondary">
              返回总览
            </Button>
          </Link>
        </div>
      </Card>
      <EmptyState title="学生批改详情待接入" description="后续展示每题得分、AI 评语、专家详情与教师评语。" />
    </ResultsLayout>
  );
}
