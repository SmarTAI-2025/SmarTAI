import { FileArchive, FileText, Images, ListChecks, UploadCloud } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { TaskStepper } from "@/components/tasks/TaskStepper";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";

export function TaskUploadPage() {
  const { taskId = "draft", kind } = useParams();
  const isProblems = kind === "problems";

  return (
    <div className="grid gap-5">
      <TaskStepper current={isProblems ? "problems" : "submissions"} />
      <SectionHeader
        title={isProblems ? "上传题目" : "上传学生作答"}
        description={
          isProblems
            ? "上传题目文件，后续识别题干、题型与评分标准，并进入人工校对。"
            : "上传学生作答文件，后续按学生和题目解析作答，并进入识别结果校对。"
        }
      />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(280px,0.9fr)]">
        <Card className="grid gap-4">
          <div className="rounded-lg border border-dashed bg-muted/40 p-8 text-center">
            <UploadCloud className="mx-auto h-10 w-10 text-muted-foreground" />
            <h2 className="mt-3 text-base font-semibold">
              {isProblems ? "拖入题目文件" : "拖入作答文件或压缩包"}
            </h2>
            <p className="mx-auto mt-2 max-w-xl text-sm leading-6 text-muted-foreground">
              {isProblems
                ? "占位支持 PDF、图片或文档。接入后会展示上传进度、解析状态、题目切分与评分标准校对入口。"
                : "占位支持按学生命名的文件、文件夹压缩包或表格索引。接入后会展示上传进度、学生匹配和逐题识别校对入口。"}
            </p>
            <Button type="button" className="mt-5">
              选择文件
            </Button>
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            {(isProblems
              ? [
                  { icon: FileText, title: "题干识别", text: "拆分题目与小问" },
                  { icon: ListChecks, title: "评分标准", text: "校对分值与要点" },
                  { icon: Images, title: "图片题面", text: "OCR 待后续接入" },
                ]
              : [
                  { icon: FileArchive, title: "批量上传", text: "解析学生文件包" },
                  { icon: ListChecks, title: "识别校对", text: "逐题确认作答" },
                  { icon: FileText, title: "缺失提示", text: "标记未提交与异常" },
                ]
            ).map((item) => {
              const Icon = item.icon;
              return (
                <div key={item.title} className="rounded-lg border p-3">
                  <Icon className="h-4 w-4 text-accent" />
                  <p className="mt-2 text-sm font-medium">{item.title}</p>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">{item.text}</p>
                </div>
              );
            })}
          </div>
        </Card>

        <Card className="grid content-start gap-4">
          <h2 className="text-base font-semibold">{isProblems ? "题目预览占位" : "作答预览占位"}</h2>
          <p className="text-sm leading-6 text-muted-foreground">
            {isProblems
              ? "解析完成后在这里列出题号、题型、分值、评分标准状态，并提供进入题目编辑的入口。"
              : "解析完成后在这里列出学生、文件状态、题目覆盖情况，并提供进入学生作答编辑的入口。"}
          </p>
          <div className="grid gap-2 text-sm">
            <div className="flex items-center justify-between rounded-md border px-3 py-2">
              <span>{isProblems ? "待识别题目" : "待识别学生"}</span>
              <span className="text-muted-foreground">0</span>
            </div>
            <div className="flex items-center justify-between rounded-md border px-3 py-2">
              <span>需要人工确认</span>
              <span className="text-muted-foreground">待接入</span>
            </div>
            <div className="flex items-center justify-between rounded-md border px-3 py-2">
              <span>下一步</span>
              <span className="text-muted-foreground">
                {isProblems ? "上传作答" : "启动批改"}
              </span>
            </div>
          </div>
        </Card>
      </div>
      <div className="flex flex-wrap justify-end gap-2">
        <Link to={`/tasks/${taskId}/setup`}>
          <Button type="button" variant="secondary">
            返回配置
          </Button>
        </Link>
        <Link to={isProblems ? `/tasks/${taskId}/upload/submissions` : `/tasks/${taskId}/results`}>
          <Button type="button">{isProblems ? "继续上传作答" : "查看批改进度"}</Button>
        </Link>
      </div>
    </div>
  );
}
