import { BookOpenCheck, BrainCircuit, FileUp, ShieldCheck, SlidersHorizontal } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { TaskStepper } from "@/components/tasks/TaskStepper";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Textarea } from "@/components/ui/Input";

export function TaskSetupPage() {
  const { taskId = "draft" } = useParams();

  return (
    <div className="grid gap-5">
      <TaskStepper current="setup" />
      <SectionHeader
        title="批改配置"
        description="先确认专家、规则与本任务参考资料；本页不展示语言控制项。"
      />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.8fr)]">
        <Card className="grid gap-4">
          <div className="flex items-start gap-3">
            <span className="rounded-md bg-muted p-2 text-accent">
              <BookOpenCheck className="h-5 w-5" />
            </span>
            <div>
              <h2 className="text-base font-semibold">本任务参考资料</h2>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                这里仅放本次批改使用的资料，例如评分细则、课堂讲义或补充答案。资料只随当前任务配置流转。
              </p>
            </div>
          </div>
          <div className="rounded-lg border border-dashed bg-muted/40 p-5">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <p className="text-sm font-medium">暂无本任务资料</p>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  API 接入后可上传、查看和删除任务内 KB 文档，并选择是否在本次批改中启用。
                </p>
              </div>
              <Button type="button" variant="secondary" className="w-fit">
                <FileUp className="h-4 w-4" />
                添加资料
              </Button>
            </div>
          </div>
          <div className="grid gap-2 rounded-lg bg-muted/40 p-3 text-sm text-muted-foreground">
            <div className="flex items-center gap-2 text-foreground">
              <ShieldCheck className="h-4 w-4 text-accent" />
              <span className="font-medium">边界说明</span>
            </div>
            <p className="leading-6">
              当前只表达 task-scoped KB：资料用于当前任务的检索增强，不提供跨任务复用入口。
            </p>
          </div>
        </Card>

        <div className="grid gap-4">
          <Card className="grid gap-4">
            <div className="flex items-start gap-3">
              <span className="rounded-md bg-muted p-2 text-primary">
                <BrainCircuit className="h-5 w-5" />
              </span>
              <div>
                <h2 className="text-base font-semibold">专家与质量门槛</h2>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  后续接入 BYOK Experts、单专家/多专家、抽样复核与低置信标记。
                </p>
              </div>
            </div>
            <div className="grid gap-2 text-sm">
              <div className="flex items-center justify-between rounded-md border px-3 py-2">
                <span>默认专家组</span>
                <span className="text-muted-foreground">待接入</span>
              </div>
              <div className="flex items-center justify-between rounded-md border px-3 py-2">
                <span>低置信提醒</span>
                <span className="text-muted-foreground">开启占位</span>
              </div>
            </div>
          </Card>

          <Card className="grid gap-4">
            <div className="flex items-start gap-3">
              <span className="rounded-md bg-muted p-2 text-warning">
                <SlidersHorizontal className="h-5 w-5" />
              </span>
              <div>
                <h2 className="text-base font-semibold">批改注意事项</h2>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  仅收集教师补充规则；后续提交时由 API client 统一封装兼容字段。
                </p>
              </div>
            </div>
            <Field label="教师补充规则">
              <Textarea placeholder="例：请重点检查推导步骤；忽略书写规范；某概念允许同义表达。" />
            </Field>
          </Card>
        </div>
      </div>
      <div className="flex flex-wrap justify-end gap-2">
        <Link to={`/tasks/${taskId}/upload/problems`}>
          <Button type="button">继续上传题目</Button>
        </Link>
      </div>
    </div>
  );
}
