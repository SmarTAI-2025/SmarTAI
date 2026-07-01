import type { FormEvent } from "react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { normalizeAPIError } from "@/api/client";
import { useCreateTask } from "@/api/hooks";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";

export function NewTaskPage() {
  const navigate = useNavigate();
  const createTask = useCreateTask();
  const [name, setName] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);

    const trimmedName = name.trim();
    if (!trimmedName) {
      setFormError("请输入任务名称。");
      return;
    }

    try {
      const task = await createTask.mutateAsync(trimmedName);
      toast.success("任务已创建，进入配置页。");
      navigate(`/tasks/${task.task_id}/setup`);
    } catch (error) {
      setFormError(normalizeAPIError(error).message);
    }
  }

  return (
    <div className="grid gap-5">
      <SectionHeader title="新建任务" description="创建一个面向本次批改的任务流程。" />
      <Card className="max-w-2xl">
        <form className="grid gap-4" onSubmit={handleSubmit}>
          <Field label="任务名称" hint="例如：大学物理第一次习题">
            <Input
              autoFocus
              disabled={createTask.isPending}
              onChange={(event) => setName(event.target.value)}
              placeholder="输入任务名称"
              required
              value={name}
            />
          </Field>
          {formError ? (
            <div className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
              {formError}
            </div>
          ) : null}
          <Button type="submit" className="w-fit" disabled={createTask.isPending}>
            {createTask.isPending ? "创建中..." : "创建并进入配置"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
