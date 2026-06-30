import { CheckCircle2, CircleDashed, ListChecks, Plus } from "lucide-react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { StatTile } from "@/components/ui/StatTile";

export function DashboardPage() {
  return (
    <div className="grid gap-5">
      <SectionHeader
        title="教师工作台"
        description="创建批改任务，上传题目与学生作答，查看 AI 批改和学情分析。"
        action={
          <Link to="/tasks/new">
            <Button>
              <Plus className="h-4 w-4" />
              新建任务
            </Button>
          </Link>
        }
      />
      <div className="grid gap-3 md:grid-cols-3">
        <StatTile icon={CircleDashed} label="进行中/草稿" value="—" tone="warning" />
        <StatTile icon={CheckCircle2} label="已完成" value="—" tone="accent" />
        <StatTile icon={ListChecks} label="全部任务" value="—" />
      </div>
      <Card>
        <h2 className="text-base font-semibold">近期任务</h2>
        <div className="mt-3">
          <EmptyState
            title="等待连接任务数据"
            description="API hooks 接入后，这里会展示当前教师的任务列表。"
            action={
              <Link to="/tasks/new">
                <Button variant="secondary">创建第一个任务</Button>
              </Link>
            }
          />
        </div>
      </Card>
    </div>
  );
}

