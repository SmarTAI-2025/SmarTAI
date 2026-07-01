import { useRef, useState, type ChangeEvent } from "react";
import {
  AlertTriangle,
  ArrowRight,
  BookOpenCheck,
  BrainCircuit,
  FileText,
  FileUp,
  Loader2,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  Trash2,
} from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { toast } from "sonner";
import { useDeleteKBDoc, useExperts, useKBDocs, useTask, useUploadKBDoc } from "@/api/hooks";
import { TaskStepper } from "@/components/tasks/TaskStepper";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Textarea } from "@/components/ui/Input";
import type { ExpertConfig, KBDoc, Task, TaskStatus } from "@/types";

const MAX_KB_FILE_BYTES = 5 * 1024 * 1024;
const MAX_KB_DOCS = 3;
const KB_ACCEPT = ".pdf,.txt,.md,.markdown,.rst,text/plain,text/markdown,application/pdf";

const statusMeta: Record<TaskStatus, { label: string; className: string }> = {
  draft: { label: "草稿", className: "border-muted bg-muted text-muted-foreground" },
  extracting_problems: { label: "题目识别中", className: "border-warning/40 bg-warning/10 text-warning" },
  problems_ready: { label: "题目已就绪", className: "border-accent/40 bg-accent/10 text-accent" },
  parsing_submissions: { label: "作答解析中", className: "border-warning/40 bg-warning/10 text-warning" },
  submissions_ready: { label: "作答已就绪", className: "border-accent/40 bg-accent/10 text-accent" },
  grading: { label: "批改中", className: "border-primary/40 bg-primary/10 text-primary" },
  graded: { label: "已完成", className: "border-accent/40 bg-accent/10 text-accent" },
  error: { label: "需要处理", className: "border-danger/40 bg-danger/10 text-danger" },
};

export function TaskSetupPage() {
  const { taskId = "" } = useParams();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [deletingDocId, setDeletingDocId] = useState<string | null>(null);

  const taskQuery = useTask(taskId || undefined);
  const kbQuery = useKBDocs(taskId || undefined);
  const expertsQuery = useExperts();
  const uploadKBDoc = useUploadKBDoc();
  const deleteKBDoc = useDeleteKBDoc();

  const task = taskQuery.data;
  const docs = kbQuery.data?.docs ?? [];
  const experts = expertsQuery.data ?? [];
  const enabledExperts = experts.filter((expert) => expert.enabled);
  const nextStep = getNextStep(task, taskId);
  const uploadDisabledReason = getUploadDisabledReason(taskId, docs, experts, expertsQuery.isSuccess);

  async function handleKBFileChange(event: ChangeEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    const file = input.files?.[0];
    if (!file) {
      return;
    }

    if (!taskId) {
      toast.error("缺少任务 ID，无法上传本任务资料。");
      input.value = "";
      return;
    }

    if (file.size > MAX_KB_FILE_BYTES) {
      toast.error("本任务资料文件不能超过 5 MB。");
      input.value = "";
      return;
    }

    try {
      setUploadProgress(0);
      const result = await uploadKBDoc.mutateAsync({
        taskId,
        file,
        onProgress: (percent) => setUploadProgress(percent),
      });

      if (result.status === "already_done") {
        toast.info(`本任务资料已存在：${result.filename}`);
      } else {
        const chunkText = `${result.chunk_count} 个片段`;
        const embedderText = result.embedder ? `，索引方式：${result.embedder}` : "";
        toast.success(`已添加本任务资料：${result.filename}（${chunkText}${embedderText}）`);
      }
    } catch (error) {
      toast.error(`本任务资料上传失败：${formatError(error)}`);
    } finally {
      setUploadProgress(null);
      input.value = "";
    }
  }

  async function handleDeleteDoc(doc: KBDoc) {
    if (!taskId) {
      toast.error("缺少任务 ID，无法删除本任务资料。");
      return;
    }

    const confirmed = window.confirm(`确定删除本任务资料“${doc.filename}”？`);
    if (!confirmed) {
      return;
    }

    try {
      setDeletingDocId(doc.doc_id);
      await deleteKBDoc.mutateAsync({ taskId, docId: doc.doc_id });
      toast.success(`已删除本任务资料：${doc.filename}`);
    } catch (error) {
      toast.error(`删除本任务资料失败：${formatError(error)}`);
    } finally {
      setDeletingDocId(null);
    }
  }

  return (
    <div className="grid gap-5">
      <TaskStepper current="setup" />
      <SectionHeader
        title="批改配置"
        description="先确认任务状态、BYOK 专家、本任务资料与补充规则，再进入上传题目。"
      />
      <TaskSummaryCard
        task={task}
        taskId={taskId}
        isLoading={taskQuery.isLoading}
        isError={taskQuery.isError}
        error={taskQuery.error}
        nextStep={nextStep}
        onRetry={() => void taskQuery.refetch()}
      />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.8fr)]">
        <Card className="grid gap-4">
          <div className="flex items-start gap-3">
            <span className="rounded-md bg-muted p-2 text-accent">
              <BookOpenCheck className="h-5 w-5" />
            </span>
            <div>
              <h2 className="text-base font-semibold">本任务资料</h2>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                这里仅放当前任务会用到的资料，例如评分细则、讲义片段或补充答案。资料只随当前任务流转。
              </p>
            </div>
          </div>
          <input
            ref={fileInputRef}
            className="sr-only"
            type="file"
            accept={KB_ACCEPT}
            onChange={handleKBFileChange}
          />
          <div className="rounded-lg border border-dashed bg-muted/40 p-5">
            <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <p className="text-sm font-medium">添加本任务资料</p>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  支持 PDF、MD、TXT、RST；每份不超过 5 MB，本任务最多 3 份。
                </p>
                {uploadDisabledReason ? (
                  <p className="mt-2 text-xs leading-5 text-warning">{uploadDisabledReason}</p>
                ) : null}
              </div>
              <Button
                type="button"
                variant="secondary"
                className="w-fit"
                disabled={uploadKBDoc.isPending || Boolean(uploadDisabledReason)}
                onClick={() => fileInputRef.current?.click()}
              >
                {uploadKBDoc.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <FileUp className="h-4 w-4" />
                )}
                {uploadKBDoc.isPending ? "上传中" : "上传资料"}
              </Button>
            </div>
            {uploadProgress !== null ? (
              <div className="mt-4">
                <div className="h-2 overflow-hidden rounded-full bg-muted">
                  <div className="h-full bg-accent transition-all" style={{ width: `${uploadProgress}%` }} />
                </div>
                <p className="mt-1 text-xs text-muted-foreground">上传进度 {uploadProgress}%</p>
              </div>
            ) : null}
          </div>
          <KBDocList
            docs={docs}
            isLoading={kbQuery.isLoading}
            isError={kbQuery.isError}
            error={kbQuery.error}
            deletingDocId={deletingDocId}
            isDeleting={deleteKBDoc.isPending}
            onRetry={() => void kbQuery.refetch()}
            onDelete={(doc) => void handleDeleteDoc(doc)}
          />
          <div className="grid gap-2 rounded-lg bg-muted/40 p-3 text-sm text-muted-foreground">
            <div className="flex items-center gap-2 text-foreground">
              <ShieldCheck className="h-4 w-4 text-accent" />
              <span className="font-medium">边界说明</span>
            </div>
            <p className="leading-6">
              本任务资料只用于当前任务的检索增强；任务删除或后端重启后，内存中的资料索引可能失效。
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
                <h2 className="text-base font-semibold">BYOK 专家概览</h2>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  显示当前已注册专家的 provider、模型与启用状态，用于判断本次批改是否已经具备模型来源。
                </p>
              </div>
            </div>
            <ExpertsOverview
              experts={experts}
              enabledCount={enabledExperts.length}
              isLoading={expertsQuery.isLoading}
              isError={expertsQuery.isError}
              error={expertsQuery.error}
              onRetry={() => void expertsQuery.refetch()}
            />
          </Card>

          <Card className="grid gap-4">
            <div className="flex items-start gap-3">
              <span className="rounded-md bg-muted p-2 text-warning">
                <SlidersHorizontal className="h-5 w-5" />
              </span>
              <div>
                <h2 className="text-base font-semibold">批改注意事项</h2>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  可记录本次批改希望特别关注的规则；题干与评分标准仍在题目校对页逐题维护。
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
        <Link to={nextStep.to}>
          <Button type="button">
            {nextStep.buttonLabel}
            <ArrowRight className="h-4 w-4" />
          </Button>
        </Link>
      </div>
    </div>
  );
}

function TaskSummaryCard({
  task,
  taskId,
  isLoading,
  isError,
  error,
  nextStep,
  onRetry,
}: {
  task?: Task;
  taskId: string;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  nextStep: NextStep;
  onRetry: () => void;
}) {
  if (isLoading) {
    return (
      <Card className="flex items-center gap-3">
        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        <span className="text-sm text-muted-foreground">正在读取任务信息...</span>
      </Card>
    );
  }

  if (isError) {
    return (
      <Card className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-2 text-sm text-danger">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>任务信息读取失败：{formatError(error)}</span>
        </div>
        <Button type="button" variant="secondary" className="w-fit" onClick={onRetry}>
          <RefreshCw className="h-4 w-4" />
          重试
        </Button>
      </Card>
    );
  }

  return (
    <Card className="grid gap-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="break-words text-lg font-semibold">{task?.name ?? "未命名任务"}</h2>
            {task ? <StatusBadge status={task.status} /> : null}
          </div>
          <p className="mt-1 break-all text-xs text-muted-foreground">任务 ID：{taskId || "-"}</p>
        </div>
        <Link to={nextStep.to} className="w-fit">
          <Button type="button" variant="secondary">
            {nextStep.buttonLabel}
            <ArrowRight className="h-4 w-4" />
          </Button>
        </Link>
      </div>
      <div className="grid gap-2 md:grid-cols-4">
        <TaskMetric label="题目" value={String(task?.problem_count ?? 0)} />
        <TaskMetric label="学生作答" value={String(task?.student_count ?? 0)} />
        <TaskMetric label="本任务资料" value={String(task?.kb_doc_count ?? 0)} />
        <TaskMetric label="更新时间" value={formatTimestamp(task?.updated_at)} />
      </div>
      <div className="rounded-md border bg-background p-3">
        <div className="text-xs font-medium text-muted-foreground">下一步</div>
        <div className="mt-1 text-sm font-semibold">{nextStep.title}</div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{nextStep.description}</p>
      </div>
      {task?.error ? (
        <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">{task.error}</span>
        </div>
      ) : null}
    </Card>
  );
}

function KBDocList({
  docs,
  isLoading,
  isError,
  error,
  deletingDocId,
  isDeleting,
  onRetry,
  onDelete,
}: {
  docs: KBDoc[];
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  deletingDocId: string | null;
  isDeleting: boolean;
  onRetry: () => void;
  onDelete: (doc: KBDoc) => void;
}) {
  if (isLoading) {
    return (
      <div className="flex items-center gap-2 rounded-md border bg-background p-3 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        正在读取本任务资料...
      </div>
    );
  }

  if (isError) {
    return (
      <div className="grid gap-3 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">本任务资料读取失败：{formatError(error)}</span>
        </div>
        <Button type="button" variant="secondary" className="w-fit" onClick={onRetry}>
          <RefreshCw className="h-4 w-4" />
          重新读取
        </Button>
      </div>
    );
  }

  if (docs.length === 0) {
    return (
      <div className="rounded-lg border border-dashed bg-background p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm font-medium">暂无本任务资料</p>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              上传后会在这里列出文件名、片段数量与索引方式，删除只影响当前任务。
            </p>
          </div>
          <FileText className="h-8 w-8 text-muted-foreground" />
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-2">
      <div className="text-xs font-medium text-muted-foreground">已添加的本任务资料</div>
      {docs.map((doc) => {
        const isCurrentDelete = isDeleting && deletingDocId === doc.doc_id;
        return (
          <div
            key={doc.doc_id}
            className="flex flex-col gap-3 rounded-md border bg-background p-3 sm:flex-row sm:items-center sm:justify-between"
          >
            <div className="flex min-w-0 items-start gap-3">
              <span className="rounded-md bg-muted p-2 text-accent">
                <FileText className="h-4 w-4" />
              </span>
              <div className="min-w-0">
                <p className="break-words text-sm font-medium">{doc.filename}</p>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  {doc.chunk_count} 个片段
                  {doc.embedder ? ` · ${doc.embedder}` : ""}
                  {" · "}
                  {formatTimestamp(doc.created_at)}
                </p>
              </div>
            </div>
            <Button
              type="button"
              variant="danger"
              className="h-8 w-fit"
              disabled={isDeleting}
              onClick={() => onDelete(doc)}
            >
              {isCurrentDelete ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
              删除
            </Button>
          </div>
        );
      })}
    </div>
  );
}

function ExpertsOverview({
  experts,
  enabledCount,
  isLoading,
  isError,
  error,
  onRetry,
}: {
  experts: ExpertConfig[];
  enabledCount: number;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  if (isLoading) {
    return (
      <div className="flex items-center gap-2 rounded-md border bg-background p-3 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        正在读取 BYOK 专家...
      </div>
    );
  }

  if (isError) {
    return (
      <div className="grid gap-3 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="min-w-0 break-words">BYOK 专家读取失败：{formatError(error)}</span>
        </div>
        <Button type="button" variant="secondary" className="w-fit" onClick={onRetry}>
          <RefreshCw className="h-4 w-4" />
          重新读取
        </Button>
      </div>
    );
  }

  if (experts.length === 0) {
    return (
      <div className="rounded-md border border-dashed bg-background p-4 text-sm">
        <p className="font-medium">暂无 BYOK 专家</p>
        <p className="mt-1 leading-6 text-muted-foreground">
          添加 BYOK 专家后，本页会显示 provider、模型和启用状态，方便确认本次批改是否可运行。
        </p>
      </div>
    );
  }

  return (
    <div className="grid gap-3">
      <div className="grid gap-2 sm:grid-cols-2">
        <TaskMetric label="已启用专家" value={`${enabledCount}`} />
        <TaskMetric label="已注册专家" value={`${experts.length}`} />
      </div>
      <div className="grid gap-2">
        {experts.map((expert) => (
          <div key={expert.provider_id} className="rounded-md border bg-background p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="break-words text-sm font-medium">
                  {expert.display_name || providerLabel(expert.provider_type)}
                </p>
                <p className="mt-1 break-words text-xs text-muted-foreground">
                  {providerLabel(expert.provider_type)} · {expert.model}
                </p>
              </div>
              <span
                className={
                  expert.enabled
                    ? "rounded-full border border-accent/40 bg-accent/10 px-2 py-1 text-xs font-medium text-accent"
                    : "rounded-full border bg-muted px-2 py-1 text-xs font-medium text-muted-foreground"
                }
              >
                {expert.enabled ? "已启用" : "未启用"}
              </span>
            </div>
            <div className="mt-3 grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
              <span>并发上限：{expert.max_concurrent}</span>
              <span>RPM：{expert.rpm > 0 ? expert.rpm : "未限制"}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: TaskStatus }) {
  const meta = statusMeta[status];
  return (
    <span className={`rounded-full border px-2 py-1 text-xs font-medium ${meta.className}`}>
      {meta.label}
    </span>
  );
}

function TaskMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-background p-3">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className="mt-1 break-words text-sm font-semibold">{value}</div>
    </div>
  );
}

interface NextStep {
  title: string;
  description: string;
  buttonLabel: string;
  to: string;
}

function getNextStep(task: Task | undefined, taskId: string): NextStep {
  if (!task || !taskId) {
    return {
      title: "读取任务后继续",
      description: "任务信息读取完成后，会根据当前状态跳到对应环节。",
      buttonLabel: "前往任务列表",
      to: "/history",
    };
  }

  const problemUpload = `/tasks/${taskId}/upload/problems`;
  const submissionUpload = `/tasks/${taskId}/upload/submissions`;
  const results = `/tasks/${taskId}/results`;

  switch (task.status) {
    case "draft":
      return {
        title: "上传题目",
        description: "当前任务仍是草稿，下一步上传题目文件并进入题目校对。",
        buttonLabel: "继续上传题目",
        to: problemUpload,
      };
    case "extracting_problems":
      return {
        title: "等待题目识别",
        description: "题目正在处理，完成后可以继续上传学生作答。",
        buttonLabel: "查看题目上传",
        to: problemUpload,
      };
    case "problems_ready":
      return {
        title: "上传学生作答",
        description: "题目已就绪，下一步上传学生作答并校对识别结果。",
        buttonLabel: "继续上传作答",
        to: submissionUpload,
      };
    case "parsing_submissions":
      return {
        title: "等待作答解析",
        description: "学生作答正在解析，完成后可以进入批改进度页。",
        buttonLabel: "查看作答上传",
        to: submissionUpload,
      };
    case "submissions_ready":
      return {
        title: "进入批改",
        description: "题目与学生作答都已就绪，可以进入批改页启动或查看任务。",
        buttonLabel: "进入批改",
        to: results,
      };
    case "grading":
      return {
        title: "查看批改进度",
        description: "任务正在批改中，可以在结果页查看进度与后续结果。",
        buttonLabel: "查看进度",
        to: results,
      };
    case "graded":
      return {
        title: "查看批改结果",
        description: "任务已完成批改，可以查看总览、按题分析和学生详情。",
        buttonLabel: "查看结果",
        to: results,
      };
    case "error":
      return {
        title: "处理任务错误",
        description: "任务当前需要处理。可回到最近的上传环节检查文件或重新上传。",
        buttonLabel: "返回上传环节",
        to: task.problem_count > 0 ? submissionUpload : problemUpload,
      };
  }
}

function getUploadDisabledReason(
  taskId: string,
  docs: KBDoc[],
  experts: ExpertConfig[],
  expertsLoaded: boolean,
) {
  if (!taskId) {
    return "缺少任务 ID。";
  }
  if (docs.length >= MAX_KB_DOCS) {
    return "本任务资料已达到 3 份上限。";
  }
  if (expertsLoaded && experts.length === 0) {
    return "需要先配置 BYOK 专家，才能为本任务资料建立索引。";
  }
  return null;
}

function providerLabel(providerType: string) {
  const normalized = providerType.toLowerCase();
  if (normalized === "openai") return "OpenAI";
  if (normalized === "gemini") return "Gemini";
  if (normalized === "zhipu") return "Zhipu";
  if (normalized === "anthropic") return "Anthropic";
  return providerType;
}

function formatTimestamp(value?: number) {
  if (!value) {
    return "-";
  }
  return new Date(value * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatError(error: unknown) {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "未知错误";
}
