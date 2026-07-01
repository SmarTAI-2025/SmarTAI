import { BookOpenCheck, Database, FileText, Info, Loader2, Trash2, UploadCloud } from "lucide-react";
import { useRef, useState, type ChangeEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import {
  addPersonalKBDoc,
  deletePersonalKBDoc,
  listPersonalKBDocs,
  type PersonalKBDoc,
} from "@/lib/personalKnowledgeBase";

const PERSONAL_KB_ACCEPT = ".pdf,.txt,.md,.markdown,.rst,text/plain,text/markdown,application/pdf";

export function KnowledgeBasePage() {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [docs, setDocs] = useState<PersonalKBDoc[]>(() => listPersonalKBDocs());
  const [isAdding, setIsAdding] = useState(false);

  function refreshDocs() {
    setDocs(listPersonalKBDocs());
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    const files = Array.from(input.files ?? []);
    if (files.length === 0) {
      return;
    }

    setIsAdding(true);
    for (const file of files) {
      addPersonalKBDoc({
        filename: file.name,
        size: file.size,
        source: "manual",
      });
    }
    refreshDocs();
    toast.success(`已加入个人知识库前端清单：${files.length} 份资料`);
    input.value = "";
    setIsAdding(false);
  }

  function handleDelete(doc: PersonalKBDoc) {
    const confirmed = window.confirm(`从个人知识库前端清单中移除“${doc.filename}”？`);
    if (!confirmed) {
      return;
    }
    deletePersonalKBDoc(doc.id);
    refreshDocs();
    toast.success(`已移除：${doc.filename}`);
  }

  return (
    <div className="grid gap-5">
      <SectionHeader
        title="个人知识库"
        description="提前整理可跨任务复用的教材、讲义和评分参考；当前是前端先行版本。"
      />

      <Card className="grid gap-4 border-warning/30 bg-warning/5">
        <div className="flex items-start gap-3">
          <span className="rounded-md bg-background p-2 text-warning">
            <Info className="h-5 w-5" />
          </span>
          <div>
            <h2 className="text-base font-semibold">后端能力待接入</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              这里先保存浏览器本地的资料清单和任务选择关系，不上传文件内容到 user-scoped KB，也不会让这些资料直接参与当前批改。
              现阶段真正参与 RAG 的仍是任务 Setup 里上传的本任务资料。
            </p>
          </div>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <Card className="grid gap-4">
          <div className="flex items-start gap-3">
            <span className="rounded-md bg-muted p-2 text-accent">
              <BookOpenCheck className="h-5 w-5" />
            </span>
            <div>
              <h2 className="text-base font-semibold">资料清单</h2>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                这些条目可在任务 Setup 里被选择。后端接入后，选择结果会对应到真正的用户级知识库文档。
              </p>
            </div>
          </div>

          {docs.length === 0 ? (
            <div className="rounded-lg border border-dashed bg-background p-6">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-sm font-medium">暂无个人知识库资料</p>
                  <p className="mt-1 text-sm leading-6 text-muted-foreground">
                    可以先添加文件名和来源，后续在批改配置中选择；真正索引和复用需要后端 API。
                  </p>
                </div>
                <Database className="h-9 w-9 text-muted-foreground" />
              </div>
            </div>
          ) : (
            <div className="grid gap-2">
              {docs.map((doc) => (
                <div
                  key={doc.id}
                  className="flex flex-col gap-3 rounded-md border bg-background p-3 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="flex min-w-0 items-start gap-3">
                    <span className="rounded-md bg-muted p-2 text-accent">
                      <FileText className="h-4 w-4" />
                    </span>
                    <div className="min-w-0">
                      <p className="break-words text-sm font-medium">{doc.filename}</p>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">
                        {formatBytes(doc.size)}
                        {" · "}
                        {doc.source === "task-upload" ? "来自任务上传" : "手动加入"}
                        {doc.chunkCount ? ` · ${doc.chunkCount} 个片段` : ""}
                        {doc.embedder ? ` · ${doc.embedder}` : ""}
                        {" · "}
                        {formatDate(doc.createdAt)}
                      </p>
                    </div>
                  </div>
                  <Button type="button" variant="danger" className="h-8 w-fit" onClick={() => handleDelete(doc)}>
                    <Trash2 className="h-4 w-4" />
                    删除
                  </Button>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="grid content-start gap-4">
          <div className="flex items-start gap-3">
            <span className="rounded-md bg-muted p-2 text-primary">
              <UploadCloud className="h-5 w-5" />
            </span>
            <div>
              <h2 className="text-base font-semibold">加入资料</h2>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                当前只记录资料元数据；请勿把这里当作已完成的云端知识库。
              </p>
            </div>
          </div>
          <input
            ref={fileInputRef}
            className="sr-only"
            type="file"
            accept={PERSONAL_KB_ACCEPT}
            multiple
            onChange={handleFileChange}
          />
          <Button type="button" className="w-fit" onClick={() => fileInputRef.current?.click()} disabled={isAdding}>
            {isAdding ? <Loader2 className="h-4 w-4 animate-spin" /> : <UploadCloud className="h-4 w-4" />}
            选择文件加入清单
          </Button>
          <div className="rounded-md border bg-muted/30 p-3 text-xs leading-5 text-muted-foreground">
            后端完成后，这里应改为真实上传、列表、删除和跨任务引用 API；目前不会保存文件内容。
          </div>
        </Card>
      </div>
    </div>
  );
}

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) {
    return "未知大小";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(value: number) {
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
