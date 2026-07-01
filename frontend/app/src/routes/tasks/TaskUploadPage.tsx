import {
  AlertCircle,
  CheckCircle2,
  ChevronRight,
  FileArchive,
  FileText,
  Images,
  ListChecks,
  Loader2,
  PlayCircle,
  RefreshCw,
  Save,
  UploadCloud,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ChangeEvent, type DragEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import {
  useExtractProblems,
  useParseSubmissions,
  useStartGrading,
  useTask,
  useUpdateProblem,
  useUpdateStudentAnswer,
} from "@/api/hooks/tasks";
import { TaskStepper } from "@/components/tasks/TaskStepper";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Textarea } from "@/components/ui/Input";
import { useTaskProgress } from "@/hooks/useTaskProgress";
import { cn } from "@/lib/cn";
import type { ProblemInfo, StudentAnswerInfo, StudentSubmission, TaskStatus } from "@/types";

const STATUS_LABELS: Partial<Record<TaskStatus, string>> = {
  draft: "草稿",
  extracting_problems: "正在识别题目",
  problems_ready: "题目已就绪",
  parsing_submissions: "正在解析作答",
  submissions_ready: "作答已就绪",
  grading: "批改中",
  graded: "已完成",
  error: "出错",
};

const ACTIVE_STATUS = new Set<TaskStatus>(["extracting_problems", "parsing_submissions", "grading"]);
const PROBLEMS_ACCEPT = ".pdf,.doc,.docx,.txt,.md";
const SUBMISSIONS_ACCEPT = ".zip,.pdf,.doc,.docx,.txt,.csv,.xlsx";

export function TaskUploadPage() {
  const { taskId, kind } = useParams();
  const navigate = useNavigate();
  const isSubmissions = kind === "submissions";
  const isProblems = !isSubmissions;
  const safeTaskId = taskId ?? "";

  const taskQuery = useTask(taskId);
  const progressQuery = useTaskProgress(taskId);
  const extractProblems = useExtractProblems();
  const parseSubmissions = useParseSubmissions();
  const updateProblem = useUpdateProblem();
  const updateStudentAnswer = useUpdateStudentAnswer();
  const startGrading = useStartGrading();

  const [uploadPercent, setUploadPercent] = useState(0);
  const [uploadFileName, setUploadFileName] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [editingProblemId, setEditingProblemId] = useState<string | null>(null);
  const [problemDraft, setProblemDraft] = useState({ stem: "", criterion: "" });
  const [selectedStudentId, setSelectedStudentId] = useState<string | null>(null);
  const [editingAnswerKey, setEditingAnswerKey] = useState<string | null>(null);
  const [answerDraft, setAnswerDraft] = useState("");
  const lastDetailRefetchKeyRef = useRef<string | null>(null);

  const task = taskQuery.data;
  const currentStatus = (progressQuery.data?.status ?? task?.status ?? "draft") as TaskStatus;
  const isProcessing = progressQuery.isActive || ACTIVE_STATUS.has(currentStatus);
  const isUploading = extractProblems.isPending || parseSubmissions.isPending;

  const problems = useMemo(
    () => Object.values(task?.problem_data ?? {}).sort(compareProblems),
    [task?.problem_data],
  );
  const students = useMemo(
    () => Object.values(task?.student_data ?? {}).sort(compareStudents),
    [task?.student_data],
  );
  const stateProblemCount = progressQuery.data?.problem_count ?? 0;
  const stateStudentCount = progressQuery.data?.student_count ?? 0;
  const expectedProblemCount = Math.max(problems.length, task?.problem_count ?? 0, stateProblemCount);
  const expectedStudentCount = Math.max(students.length, task?.student_count ?? 0, stateStudentCount);

  const selectedStudent = useMemo(
    () => students.find((student) => student.stu_id === selectedStudentId) ?? students[0] ?? null,
    [selectedStudentId, students],
  );

  useEffect(() => {
    const firstStudentId = students[0]?.stu_id ?? null;
    if (!firstStudentId) {
      setSelectedStudentId(null);
      return;
    }
    if (!selectedStudentId || !students.some((student) => student.stu_id === selectedStudentId)) {
      setSelectedStudentId(firstStudentId);
    }
  }, [selectedStudentId, students]);

  useEffect(() => {
    if (ACTIVE_STATUS.has(currentStatus)) {
      lastDetailRefetchKeyRef.current = null;
      return;
    }

    const completedStatus =
      currentStatus === "problems_ready" ||
      currentStatus === "submissions_ready" ||
      currentStatus === "graded" ||
      currentStatus === "error";
    const detailCountMismatch = stateProblemCount > problems.length || stateStudentCount > students.length;

    if (!completedStatus && !detailCountMismatch) {
      return;
    }

    const refetchKey = [
      currentStatus,
      stateProblemCount,
      stateStudentCount,
      problems.length,
      students.length,
    ].join(":");

    if (lastDetailRefetchKeyRef.current === refetchKey) {
      return;
    }

    lastDetailRefetchKeyRef.current = refetchKey;
    void taskQuery.refetch();
  }, [currentStatus, problems.length, stateProblemCount, stateStudentCount, students.length, taskQuery.refetch]);

  const handleUpload = async (file: File) => {
    if (!taskId) {
      toast.error("缺少任务 ID，无法上传。");
      return;
    }

    const label = isProblems ? "题目文件" : "学生作答";
    setUploadPercent(0);
    setUploadFileName(file.name);

    try {
      const response = await (isProblems ? extractProblems : parseSubmissions).mutateAsync({
        taskId,
        file,
        onProgress: (percent) => setUploadPercent(percent),
      });

      setUploadPercent(100);
      if (response.status === "already_running") {
        toast.info(`${label}正在处理中`, { description: "页面会继续轮询当前任务状态。" });
      } else if (response.status === "already_done") {
        toast.success(`${label}已解析过`, { description: "已恢复现有预览数据。" });
      } else {
        toast.success(`${label}已上传`, { description: "正在刷新识别状态与预览数据。" });
      }
      void progressQuery.refetch();
      void taskQuery.refetch();
    } catch (error) {
      toast.error(`${label}上传失败`, { description: getErrorMessage(error) });
    }
  };

  const handleFileInput = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) {
      void handleUpload(file);
    }
    event.target.value = "";
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) {
      void handleUpload(file);
    }
  };

  const startProblemEdit = (problem: ProblemInfo) => {
    setEditingProblemId(problem.q_id);
    setProblemDraft({ stem: problem.stem ?? "", criterion: problem.criterion ?? "" });
  };

  const saveProblem = async (problem: ProblemInfo) => {
    if (!taskId) {
      toast.error("缺少任务 ID，无法保存题目。");
      return;
    }

    try {
      await updateProblem.mutateAsync({
        taskId,
        qId: problem.q_id,
        stem: problemDraft.stem,
        criterion: problemDraft.criterion,
      });
      toast.success(`已保存 ${problemLabel(problem)} 的题干与评分标准。`);
      setEditingProblemId(null);
      void taskQuery.refetch();
    } catch (error) {
      toast.error("保存题目失败", { description: getErrorMessage(error) });
    }
  };

  const startAnswerEdit = (student: StudentSubmission, answer: StudentAnswerInfo) => {
    setEditingAnswerKey(answerKey(student.stu_id, answer.q_id));
    setAnswerDraft(answer.content ?? "");
  };

  const saveAnswer = async (student: StudentSubmission, answer: StudentAnswerInfo) => {
    if (!taskId) {
      toast.error("缺少任务 ID，无法保存作答。");
      return;
    }

    try {
      await updateStudentAnswer.mutateAsync({
        taskId,
        studentId: student.stu_id,
        qId: answer.q_id,
        content: answerDraft,
      });
      toast.success(`已保存 ${studentName(student)} 的 ${answerLabel(answer)} 作答。`);
      setEditingAnswerKey(null);
      void taskQuery.refetch();
    } catch (error) {
      toast.error("保存作答失败", { description: getErrorMessage(error) });
    }
  };

  const handleStartGrading = async () => {
    if (!taskId) {
      toast.error("缺少任务 ID，无法启动批改。");
      return;
    }

    if (!students.length) {
      toast.error("请先上传并确认学生作答。");
      return;
    }

    try {
      const response = await startGrading.mutateAsync({ taskId });
      if (response.status === "already_done") {
        toast.success("该任务已完成批改。");
        navigate(`/tasks/${taskId}/results`);
      } else if (response.status === "already_running") {
        toast.info("批改已在进行中", { description: "页面会继续轮询进度。" });
      } else {
        toast.success("已启动批改", { description: "可以留在本页观察进度，也可以进入结果页查看。" });
      }
      void progressQuery.refetch();
      void taskQuery.refetch();
    } catch (error) {
      toast.error("启动批改失败", { description: getErrorMessage(error) });
    }
  };

  if (!taskId) {
    return (
      <EmptyState
        title="缺少任务 ID"
        description="请从教师工作台或任务列表进入上传流程。"
        action={
          <Link to="/">
            <Button variant="secondary">返回工作台</Button>
          </Link>
        }
      />
    );
  }

  const canContinueToSubmissions =
    problems.length > 0 ||
    currentStatus === "problems_ready" ||
    currentStatus === "parsing_submissions" ||
    currentStatus === "submissions_ready" ||
    currentStatus === "grading" ||
    currentStatus === "graded";
  const canStartGrading =
    students.length > 0 && currentStatus !== "grading" && currentStatus !== "graded" && !startGrading.isPending;

  return (
    <div className="grid gap-5">
      <TaskStepper current={isProblems ? "problems" : "submissions"} />
      <SectionHeader
        title={isProblems ? "上传题目" : "上传学生作答"}
        description={
          isProblems
            ? "上传题目文件，识别完成后校对题干与评分标准，再进入学生作答上传。"
            : "上传学生作答文件，按学生检查识别结果；确认后即可启动批改。"
        }
        action={
          <Button
            type="button"
            variant="secondary"
            onClick={() => {
              lastDetailRefetchKeyRef.current = null;
              void taskQuery.refetch();
              void progressQuery.refetch();
            }}
          >
            <RefreshCw className="h-4 w-4" />
            刷新
          </Button>
        }
      />

      {taskQuery.isError ? <InlineAlert message={getErrorMessage(taskQuery.error)} /> : null}
      {progressQuery.isError ? <InlineAlert message={getErrorMessage(progressQuery.error)} /> : null}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(280px,0.9fr)]">
        <UploadCard
          accept={isProblems ? PROBLEMS_ACCEPT : SUBMISSIONS_ACCEPT}
          currentFileName={isProblems ? task?.problem_file_name : task?.submission_file_name}
          isDragging={isDragging}
          isProblems={isProblems}
          isUploading={isUploading}
          uploadFileName={uploadFileName}
          uploadPercent={uploadPercent}
          onDragChange={setIsDragging}
          onDrop={handleDrop}
          onFileInput={handleFileInput}
        />
        <StatusCard
          currentStatus={currentStatus}
          isLoading={taskQuery.isLoading}
          isProcessing={isProcessing}
          latestMessage={progressQuery.latestMessage?.message ?? null}
          percent={progressQuery.percent}
          problemCount={expectedProblemCount}
          progressError={progressQuery.progress?.error_detail ?? task?.error ?? null}
          studentCount={expectedStudentCount}
        />
      </div>

      {isProblems ? (
        <ProblemsReview
          editingProblemId={editingProblemId}
          isSaving={updateProblem.isPending}
          problemDraft={problemDraft}
          problems={problems}
          expectedCount={expectedProblemCount}
          onCancel={() => setEditingProblemId(null)}
          onDraftChange={setProblemDraft}
          onEdit={startProblemEdit}
          onSave={(problem) => void saveProblem(problem)}
        />
      ) : (
        <SubmissionsReview
          answerDraft={answerDraft}
          editingAnswerKey={editingAnswerKey}
          isSaving={updateStudentAnswer.isPending}
          selectedStudent={selectedStudent}
          selectedStudentId={selectedStudent?.stu_id ?? null}
          students={students}
          expectedCount={expectedStudentCount}
          onAnswerDraftChange={setAnswerDraft}
          onCancel={() => setEditingAnswerKey(null)}
          onEdit={startAnswerEdit}
          onSave={(student, answer) => void saveAnswer(student, answer)}
          onSelectStudent={setSelectedStudentId}
        />
      )}

      <div className="flex flex-wrap justify-end gap-2">
        <Link to={isProblems ? `/tasks/${safeTaskId}/setup` : `/tasks/${safeTaskId}/upload/problems`}>
          <Button type="button" variant="secondary">
            {isProblems ? "返回配置" : "返回题目"}
          </Button>
        </Link>
        {isProblems ? (
          <Button
            type="button"
            disabled={!canContinueToSubmissions}
            onClick={() => navigate(`/tasks/${safeTaskId}/upload/submissions`)}
          >
            继续上传作答
            <ChevronRight className="h-4 w-4" />
          </Button>
        ) : currentStatus === "grading" || currentStatus === "graded" ? (
          <Button type="button" onClick={() => navigate(`/tasks/${safeTaskId}/results`)}>
            {currentStatus === "graded" ? "查看结果" : "查看批改进度"}
            <ChevronRight className="h-4 w-4" />
          </Button>
        ) : (
          <Button type="button" disabled={!canStartGrading} onClick={() => void handleStartGrading()}>
            {startGrading.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <PlayCircle className="h-4 w-4" />}
            开始批改
          </Button>
        )}
      </div>
    </div>
  );
}

function UploadCard({
  accept,
  currentFileName,
  isDragging,
  isProblems,
  isUploading,
  uploadFileName,
  uploadPercent,
  onDragChange,
  onDrop,
  onFileInput,
}: {
  accept: string;
  currentFileName?: string | null;
  isDragging: boolean;
  isProblems: boolean;
  isUploading: boolean;
  uploadFileName: string | null;
  uploadPercent: number;
  onDragChange: (isDragging: boolean) => void;
  onDrop: (event: DragEvent<HTMLDivElement>) => void;
  onFileInput: (event: ChangeEvent<HTMLInputElement>) => void;
}) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  return (
    <Card className="grid gap-4">
      <div
        className={cn(
          "rounded-lg border border-dashed bg-muted/40 p-8 text-center transition",
          isDragging ? "border-primary bg-primary/10" : "border-border",
        )}
        onDragEnter={(event) => {
          event.preventDefault();
          onDragChange(true);
        }}
        onDragLeave={(event) => {
          event.preventDefault();
          onDragChange(false);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDrop={onDrop}
      >
        <UploadCloud className="mx-auto h-10 w-10 text-muted-foreground" />
        <h2 className="mt-3 text-base font-semibold">{isProblems ? "拖入题目文件" : "拖入作答文件或压缩包"}</h2>
        <p className="mx-auto mt-2 max-w-xl text-sm leading-6 text-muted-foreground">
          {isProblems
            ? "支持上传 PDF、文档或文本格式的题目材料；图片题面与 OCR 识别仍是后续接入项。"
            : "支持上传按学生整理的文件、压缩包或表格索引；图片与手写 OCR 仍是后续接入项。"}
        </p>
        <input ref={fileInputRef} type="file" accept={accept} className="hidden" onChange={onFileInput} />
        <Button type="button" className="mt-5" disabled={isUploading} onClick={() => fileInputRef.current?.click()}>
          {isUploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <UploadCloud className="h-4 w-4" />}
          选择文件
        </Button>
        {uploadFileName ? (
          <div className="mx-auto mt-4 max-w-md text-left">
            <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
              <span className="truncate">{uploadFileName}</span>
              <span>{uploadPercent}%</span>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${uploadPercent}%` }} />
            </div>
          </div>
        ) : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        {(isProblems
          ? [
              { icon: FileText, title: "题干识别", text: "拆分题目与小问" },
              { icon: ListChecks, title: "评分标准", text: "校对分值与要点" },
              { icon: Images, title: "图片题面", text: "OCR 后续接入" },
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

      {currentFileName ? (
        <div className="flex items-start gap-2 rounded-md bg-muted/50 p-3 text-sm">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
          <div>
            <p className="font-medium">当前文件</p>
            <p className="mt-1 break-all text-muted-foreground">{currentFileName}</p>
          </div>
        </div>
      ) : null}
    </Card>
  );
}

function StatusCard({
  currentStatus,
  isLoading,
  isProcessing,
  latestMessage,
  percent,
  problemCount,
  progressError,
  studentCount,
}: {
  currentStatus: TaskStatus;
  isLoading: boolean;
  isProcessing: boolean;
  latestMessage: string | null;
  percent: number;
  problemCount: number;
  progressError: string | null;
  studentCount: number;
}) {
  return (
    <Card className="grid content-start gap-4">
      <div>
        <h2 className="text-base font-semibold">任务状态</h2>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">页面会按任务状态恢复预览与进度，离开后再回来也能继续。</p>
      </div>
      <div className="grid gap-2 text-sm">
        <StatusRow label="当前状态" value={isLoading ? "加载中" : formatStatus(currentStatus)} />
        <StatusRow label="题目数量" value={String(problemCount)} />
        <StatusRow label="学生数量" value={String(studentCount)} />
      </div>
      {isProcessing ? (
        <div className="rounded-lg border p-3">
          <div className="flex items-center justify-between gap-3 text-sm">
            <span className="font-medium">后台处理中</span>
            <span className="text-muted-foreground">{percent}%</span>
          </div>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${percent}%` }} />
          </div>
          {latestMessage ? <p className="mt-3 text-xs leading-5 text-muted-foreground">{latestMessage}</p> : null}
        </div>
      ) : null}
      {progressError ? <InlineAlert message={progressError} /> : null}
    </Card>
  );
}

function ProblemsReview({
  editingProblemId,
  expectedCount,
  isSaving,
  problemDraft,
  problems,
  onCancel,
  onDraftChange,
  onEdit,
  onSave,
}: {
  editingProblemId: string | null;
  expectedCount: number;
  isSaving: boolean;
  problemDraft: { stem: string; criterion: string };
  problems: ProblemInfo[];
  onCancel: () => void;
  onDraftChange: (draft: { stem: string; criterion: string }) => void;
  onEdit: (problem: ProblemInfo) => void;
  onSave: (problem: ProblemInfo) => void;
}) {
  return (
    <Card className="grid gap-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-base font-semibold">题目预览与校对</h2>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            检查识别出的题干和评分标准；保存后后续批改会使用更新后的内容。
          </p>
        </div>
        <span className="text-sm text-muted-foreground">{expectedCount} 道题</span>
      </div>

      {problems.length === 0 ? (
        expectedCount > 0 ? (
          <DetailSyncState
            title="正在同步题目详情"
            description={`已识别 ${expectedCount} 道题，题干与评分标准正在载入。`}
          />
        ) : (
          <EmptyState title="暂无题目预览" description="上传题目文件后，识别结果会显示在这里。" />
        )
      ) : (
        <div className="grid gap-3">
          {problems.map((problem) => {
            const isEditing = editingProblemId === problem.q_id;
            return (
              <article key={problem.q_id} className="rounded-lg border p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <p className="text-sm font-semibold">{problemLabel(problem)}</p>
                    <p className="mt-1 text-xs text-muted-foreground">{problem.type || "未识别题型"}</p>
                  </div>
                  {isEditing ? null : (
                    <Button type="button" variant="secondary" onClick={() => onEdit(problem)}>
                      编辑
                    </Button>
                  )}
                </div>

                {isEditing ? (
                  <div className="mt-4 grid gap-3">
                    <Field label="题干">
                      <Textarea
                        value={problemDraft.stem}
                        onChange={(event) => onDraftChange({ ...problemDraft, stem: event.target.value })}
                      />
                    </Field>
                    <Field label="评分标准">
                      <Textarea
                        value={problemDraft.criterion}
                        onChange={(event) => onDraftChange({ ...problemDraft, criterion: event.target.value })}
                      />
                    </Field>
                    <div className="flex flex-wrap justify-end gap-2">
                      <Button type="button" variant="ghost" disabled={isSaving} onClick={onCancel}>
                        <X className="h-4 w-4" />
                        取消
                      </Button>
                      <Button type="button" disabled={isSaving} onClick={() => onSave(problem)}>
                        {isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                        保存
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="mt-4 grid gap-3 text-sm">
                    <PreviewBlock label="题干" value={problem.stem} emptyText="题干为空，请编辑补充。" />
                    <PreviewBlock label="评分标准" value={problem.criterion} emptyText="评分标准为空，请编辑补充。" />
                    {problem.reference_answer ? (
                      <PreviewBlock label="参考答案" value={problem.reference_answer} emptyText="暂无参考答案。" />
                    ) : null}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function SubmissionsReview({
  answerDraft,
  editingAnswerKey,
  expectedCount,
  isSaving,
  selectedStudent,
  selectedStudentId,
  students,
  onAnswerDraftChange,
  onCancel,
  onEdit,
  onSave,
  onSelectStudent,
}: {
  answerDraft: string;
  editingAnswerKey: string | null;
  expectedCount: number;
  isSaving: boolean;
  selectedStudent: StudentSubmission | null;
  selectedStudentId: string | null;
  students: StudentSubmission[];
  onAnswerDraftChange: (value: string) => void;
  onCancel: () => void;
  onEdit: (student: StudentSubmission, answer: StudentAnswerInfo) => void;
  onSave: (student: StudentSubmission, answer: StudentAnswerInfo) => void;
  onSelectStudent: (studentId: string) => void;
}) {
  const answers = [...(selectedStudent?.stu_ans ?? [])].sort(compareAnswers);

  return (
    <Card className="grid gap-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-base font-semibold">学生作答预览与校对</h2>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            按学生检查识别结果；可以逐题修正作答内容后再启动批改。
          </p>
        </div>
        <span className="text-sm text-muted-foreground">{expectedCount} 名学生</span>
      </div>

      {students.length === 0 ? (
        expectedCount > 0 ? (
          <DetailSyncState
            title="正在同步作答详情"
            description={`已解析 ${expectedCount} 名学生，逐题作答正在载入。`}
          />
        ) : (
          <EmptyState title="暂无学生作答" description="上传学生作答文件后，学生列表与逐题答案会显示在这里。" />
        )
      ) : (
        <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
          <div className="grid max-h-[520px] content-start gap-2 overflow-y-auto pr-1">
            {students.map((student) => {
              const active = student.stu_id === selectedStudentId;
              return (
                <button
                  key={student.stu_id}
                  type="button"
                  className={cn(
                    "rounded-lg border p-3 text-left text-sm transition hover:bg-muted",
                    active ? "border-primary bg-primary/10" : "bg-card",
                  )}
                  onClick={() => onSelectStudent(student.stu_id)}
                >
                  <span className="font-medium">{studentName(student)}</span>
                  <span className="mt-1 block text-xs text-muted-foreground">
                    {student.stu_id} · {student.stu_ans?.length ?? 0} 题
                  </span>
                </button>
              );
            })}
          </div>

          <div className="grid content-start gap-3">
            <div className="rounded-lg border bg-muted/30 p-3">
              <p className="text-sm font-semibold">{selectedStudent ? studentName(selectedStudent) : "未选择学生"}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {selectedStudent?.stu_id ?? "--"} · {answers.length} 份识别答案
              </p>
            </div>

            {answers.length === 0 ? (
              <EmptyState title="该学生暂无答案" description="可以检查上传包命名或重新上传作答文件。" />
            ) : (
              answers.map((answer) => {
                if (!selectedStudent) {
                  return null;
                }
                const editKey = answerKey(selectedStudent.stu_id, answer.q_id);
                const isEditing = editingAnswerKey === editKey;
                return (
                  <article key={editKey} className="rounded-lg border p-4">
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                      <div>
                        <p className="text-sm font-semibold">{answerLabel(answer)}</p>
                        <p className="mt-1 text-xs text-muted-foreground">{answer.type || "未识别题型"}</p>
                      </div>
                      {isEditing ? null : (
                        <Button type="button" variant="secondary" onClick={() => onEdit(selectedStudent, answer)}>
                          编辑答案
                        </Button>
                      )}
                    </div>

                    {isEditing ? (
                      <div className="mt-4 grid gap-3">
                        <Field label="作答内容">
                          <Textarea value={answerDraft} onChange={(event) => onAnswerDraftChange(event.target.value)} />
                        </Field>
                        <div className="flex flex-wrap justify-end gap-2">
                          <Button type="button" variant="ghost" disabled={isSaving} onClick={onCancel}>
                            <X className="h-4 w-4" />
                            取消
                          </Button>
                          <Button type="button" disabled={isSaving} onClick={() => onSave(selectedStudent, answer)}>
                            {isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                            保存
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <div className="mt-4 grid gap-3">
                        <PreviewBlock label="识别答案" value={answer.content} emptyText="答案为空，请编辑补充。" />
                        {answer.flag?.length ? (
                          <div className="flex flex-wrap gap-2">
                            {answer.flag.map((flag) => (
                              <span key={flag} className="rounded-md border px-2 py-1 text-xs text-muted-foreground">
                                {flag}
                              </span>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    )}
                  </article>
                );
              })
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

function DetailSyncState({ title, description }: { title: string; description: string }) {
  return (
    <div className="flex items-start gap-3 rounded-lg border bg-muted/30 p-4">
      <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-primary" />
      <div>
        <p className="text-sm font-medium">{title}</p>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">{description}</p>
      </div>
    </div>
  );
}

function PreviewBlock({ label, value, emptyText }: { label: string; value?: string | null; emptyText: string }) {
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className="mt-1 whitespace-pre-wrap break-words rounded-md bg-muted/50 p-3 text-sm leading-6">
        {value?.trim() ? value : emptyText}
      </p>
    </div>
  );
}

function StatusRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-md border px-3 py-2">
      <span>{label}</span>
      <span className="text-right text-muted-foreground">{value}</span>
    </div>
  );
}

function InlineAlert({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
      <span className="leading-5">{message}</span>
    </div>
  );
}

function formatStatus(status: string) {
  return STATUS_LABELS[status as TaskStatus] ?? status;
}

function problemLabel(problem: ProblemInfo) {
  return problem.number || problem.q_id;
}

function answerLabel(answer: StudentAnswerInfo) {
  return answer.number || answer.q_id;
}

function studentName(student: StudentSubmission) {
  return student.stu_name || student.stu_id;
}

function answerKey(studentId: string, qId: string) {
  return `${studentId}:${qId}`;
}

function compareProblems(a: ProblemInfo, b: ProblemInfo) {
  return naturalCompare(problemLabel(a), problemLabel(b));
}

function compareAnswers(a: StudentAnswerInfo, b: StudentAnswerInfo) {
  return naturalCompare(answerLabel(a), answerLabel(b));
}

function compareStudents(a: StudentSubmission, b: StudentSubmission) {
  return naturalCompare(studentName(a), studentName(b));
}

function naturalCompare(a: string, b: string) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

function getErrorMessage(error: unknown) {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "请求失败，请稍后重试。";
}
