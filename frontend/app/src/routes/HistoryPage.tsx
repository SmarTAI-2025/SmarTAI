import { SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

export function HistoryPage() {
  return (
    <div className="grid gap-5">
      <SectionHeader title="历史任务" description="查看已完成和进行中的批改任务。" />
      <EmptyState title="历史任务待接入" description="下一阶段接入 /tasks 列表与筛选。" />
    </div>
  );
}

