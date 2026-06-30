import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";

export function NewTaskPage() {
  return (
    <div className="grid gap-5">
      <SectionHeader title="新建任务" description="创建一个 task-centric 批改流程。" />
      <Card className="max-w-2xl">
        <form className="grid gap-4">
          <Field label="任务名称" hint="例如：大学物理第一次作业">
            <Input placeholder="输入任务名称" />
          </Field>
          <Button type="button" className="w-fit">
            创建并进入配置
          </Button>
        </form>
      </Card>
    </div>
  );
}

