import { BarChart3, FileText, Search, UserRound } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ResultsLayout } from "@/components/tasks/ResultsLayout";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

export function TaskResultsPage() {
  const { taskId } = useParams();
  const [searchParams] = useSearchParams();
  const view = searchParams.get("view");
  const context = view === "questions" ? "by-question" : view === "charts" ? "visualization" : "overview";
  const title =
    context === "by-question" ? "按题分析" : context === "visualization" ? "结果可视化" : "批改结果总览";
  const description =
    context === "by-question"
      ? "按题查看得分分布、易错点与进入单题全班详情的路径。"
      : context === "visualization"
        ? "预留 Plotly 与自然语言图表生成区，只展示安全白名单图表类型。"
        : "总览班级表现、低置信结果、筛选入口与详情跳转。";

  return (
    <ResultsLayout context={context} title={title} description={description}>
      {context === "overview" ? <OverviewPlaceholder taskId={taskId ?? "draft"} /> : null}
      {context === "by-question" ? <ByQuestionPlaceholder taskId={taskId ?? "draft"} /> : null}
      {context === "visualization" ? <VisualizationPlaceholder /> : null}
    </ResultsLayout>
  );
}

function OverviewPlaceholder({ taskId }: { taskId: string }) {
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(280px,0.9fr)]">
      <Card className="grid gap-4">
        <h2 className="text-base font-semibold">班级结果占位</h2>
        <div className="grid gap-3 sm:grid-cols-3">
          {[
            ["已批改", "0"],
            ["低置信", "待接入"],
            ["平均分", "--"],
          ].map(([label, value]) => (
            <div key={label} className="rounded-lg border p-3">
              <p className="text-xs text-muted-foreground">{label}</p>
              <p className="mt-2 text-2xl font-semibold">{value}</p>
            </div>
          ))}
        </div>
        <div className="rounded-lg border border-dashed p-5 text-sm leading-6 text-muted-foreground">
          接入结果后，这里展示学生筛选、低置信列表、批量导出与自然语言结果摘要。
        </div>
      </Card>
      <Card className="grid content-start gap-4">
        <h2 className="text-base font-semibold">详情跳转</h2>
        <p className="text-sm leading-6 text-muted-foreground">
          从总览点击学生进入学生详情；从题目或错因摘要进入单题全班详情。结果导航会持续保留。
        </p>
        <div className="flex flex-wrap gap-2">
          <Link to={`/tasks/${taskId}/results/student-a`}>
            <Button type="button" variant="secondary">
              <UserRound className="h-4 w-4" />
              学生详情示例
            </Button>
          </Link>
          <Link to={`/tasks/${taskId}/questions/q1`}>
            <Button type="button" variant="secondary">
              <FileText className="h-4 w-4" />
              题目详情示例
            </Button>
          </Link>
        </div>
      </Card>
    </div>
  );
}

function ByQuestionPlaceholder({ taskId }: { taskId: string }) {
  return (
    <Card className="grid gap-4">
      <div className="flex items-start gap-3">
        <span className="rounded-md bg-muted p-2 text-accent">
          <Search className="h-5 w-5" />
        </span>
        <div>
          <h2 className="text-base font-semibold">按题分析占位</h2>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            后续接入 /analytics/:id/per_question/:q_id，展示分布、典型错误和需要复核的学生。
          </p>
        </div>
      </div>
      <div className="grid gap-2">
        {["Q1", "Q2", "Q3"].map((question) => (
          <Link
            key={question}
            to={`/tasks/${taskId}/questions/${question.toLowerCase()}`}
            className="flex items-center justify-between rounded-md border px-3 py-2 text-sm transition hover:bg-muted"
          >
            <span>{question} 全班作答详情</span>
            <span className="text-muted-foreground">查看占位</span>
          </Link>
        ))}
      </div>
    </Card>
  );
}

function VisualizationPlaceholder() {
  return (
    <Card className="grid gap-4">
      <div className="flex items-start gap-3">
        <span className="rounded-md bg-muted p-2 text-primary">
          <BarChart3 className="h-5 w-5" />
        </span>
        <div>
          <h2 className="text-base font-semibold">图表占位</h2>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            后续只渲染 schema 允许的 bar、scatter、pie、histogram、box 图表，并在这里保留自然语言图表请求入口。
          </p>
        </div>
      </div>
      <EmptyState title="暂无图表数据" description="等待 analytics 与 Plotly 渲染接入。" />
    </Card>
  );
}
