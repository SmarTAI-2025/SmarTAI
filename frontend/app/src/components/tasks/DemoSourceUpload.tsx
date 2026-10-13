import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { FileCheck2, LoaderCircle } from "lucide-react";
import { useTask } from "@/api/hooks";
import { useParseSubmissions } from "@/api/hooks/tasks";
import { useStartQuestionPreparation } from "@/api/hooks/problemSources";
import { getGradingSetup } from "@/api/gradingSetup";
import { preflightProblemSource } from "@/api/problemSources";
import { getTask } from "@/api/tasks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import { useI18n } from "@/i18n/I18nProvider";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { QUESTION_FIXTURE, SUBMISSION_FIXTURE, DEMO_SCORE_POLICY, demoLockedText, fixtureFile } from "@/lib/frontierFixtures";
import { getFrontierGeneration, isFrontierExitPending } from "@/lib/frontierSession";

/** Demo uses the same task APIs with fixed source files, never fixture results. */
export function DemoSourceUpload({ stage }: { stage: "questions" | "submissions" }) {
  const { taskId } = useParams();
  const { locale } = useI18n();
  const navigate = useNavigate();
  const query = useTask(taskId, { refetchOnMount: "always" });
  const prepareQuestions = useStartQuestionPreparation();
  const recognizeSubmissions = useParseSubmissions();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const alive = useRef(true);
  const inFlight = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const questions = stage === "questions";
  const tx = (zh: string, en: string) => locale === "zh-CN" ? zh : en;
  const filename = questions ? "question_source.pdf" : "submissions_raw.zip";
  const destination = `/tasks/${taskId}/${questions ? "questions" : "submissions"}`;
  const progress = `/tasks/${taskId}/${questions ? "problems" : "submissions"}/progress`;
  const task = query.data;
  const running = task?.status === (questions ? "extracting_problems" : "parsing_submissions");
  const ready = Boolean(questions ? task?.problem_count : task?.student_count);
  const sourceFailed = task?.status === "error" && Boolean(task.last_failed_job_id)
    && task.last_failed_job_id === (questions ? task.extract_job_id : task.parse_job_id);
  const label = running || sourceFailed ? tx("查看识别进度与恢复", "View recognition and recovery") : ready
    ? tx(questions ? "进入审核题目" : "进入校对作答", questions ? "Review Questions" : "Review Submissions")
    : tx(questions ? "开始识别题目" : "开始识别作答", questions ? "Recognize Questions" : "Recognize Submissions");

  async function start() {
    if (!taskId || !task || inFlight.current || isFrontierExitPending()) return;
    if (running || sourceFailed) { navigate(progress); return; }
    if (ready) { navigate(destination); return; }
    const generation = getFrontierGeneration();
    const current = () => alive.current && generation === getFrontierGeneration() && !isFrontierExitPending();
    async function step<T>(operation: () => Promise<T>) {
      if (!current()) throw new Error("demo_session_changed");
      const result = await operation();
      if (!current()) throw new Error("demo_session_changed");
      return result;
    }
    inFlight.current = true; setBusy(true); setError(null);
    try {
      const latest = await step(() => getTask(taskId));
      const setup = await step(() => getGradingSetup(taskId));
      const enabled = setup.available_experts.filter(item => item.enabled);
      const preferred = setup.grading_setup?.primary_provider_id ?? setup.suggested_setup?.primary_provider_id;
      const provider = enabled.find(item => item.provider_id === preferred) ?? enabled[0];
      if (!provider) throw new Error("no_provider_configured");
      const file = await step(() => fixtureFile(questions ? QUESTION_FIXTURE : SUBMISSION_FIXTURE, filename, questions ? "application/pdf" : "application/zip"));
      if (questions) {
        const preflight = await step(() => preflightProblemSource({ taskId, mode: "upload", role: "problem", file,
          structureMode: "organized", extractionHint: "Preserve the Q1–Q4 numbering and mathematical notation.",
          saveToLibrary: false, recognitionProviderId: provider.provider_id }));
        const revision = preflight.workflow_revision ?? preflight.base_workflow_revision;
        if (revision === undefined) throw new Error("task_workflow_changed");
        await step(() => prepareQuestions.mutateAsync({ taskId, sourceTokens: [preflight.source_token], expectedWorkflowRevision: revision,
          scorePolicy: { mode: "per_question", perQuestionText: DEMO_SCORE_POLICY }, recognitionProviderId: provider.provider_id }));
      } else {
        await step(() => recognizeSubmissions.mutateAsync({ taskId, file, identityMode: "filename", recognitionProviderId: provider.provider_id, expectedWorkflowRevision: latest.workflow_revision }));
      }
      navigate(progress);
    } catch (caught) { if (current()) setError(caught); }
    finally { inFlight.current = false; if (alive.current) setBusy(false); }
  }
  return <div className="w-full max-w-[1300px]">
    <h1 className="text-[30px] font-bold">{tx(questions ? "上传题目" : "上传学生作答", questions ? "Upload Questions" : "Upload Student Submissions")}</h1>
    <NewTaskStepper currentStep={questions ? 1 : 3} />
    <section title={demoLockedText(locale)} className="mx-auto mt-8 max-w-[900px] rounded-[10px] border bg-card p-6 sm:p-10">
      <FileCheck2 aria-hidden="true" className="mb-4 h-9 w-9 text-primary" />
      <h2 className="text-lg font-bold">{tx("演示文件已预置", "Demo files are preloaded")}</h2>
      <p className="mt-3 break-all font-medium">{filename}</p>
      <p className="mt-2 text-sm text-muted-foreground">{tx(questions ? "4 道合成题目，满分依次为 5、8、7、10 分。" : "4 份合成学生作答，包含排版、混排和手写内容。", questions ? "Four synthetic questions worth 5, 8, 7, and 10 points." : "Four synthetic submissions with typeset, mixed, and handwritten content.")}</p>
      <p className="mt-4 text-sm text-muted-foreground">{demoLockedText(locale)}</p>
      <button type="button" disabled className="mt-4 rounded-lg border px-4 py-2 text-sm text-muted-foreground">{tx("演示文件不可替换", "Demo files cannot be replaced")}</button>
      {ready ? <p className="mt-4 text-sm text-teal-700">{tx("已有识别结果，可直接进入校对。", "Recognized content is available. Continue to review it.")}</p> : null}
    </section>
    {error || query.error ? <RecoverableActionState locale={locale} className="mx-auto mt-4 max-w-[900px]" info={classifyRecoverableError(error ?? query.error, { locale, taskId })} primaryAction={{ label: tx("重试", "Retry"), onClick: () => error ? void start() : void query.refetch(), busy }} /> : null}
    <div className="mx-auto mt-6 flex max-w-[900px] flex-wrap justify-end gap-3">
      <Link className="rounded-lg border px-4 py-2" to={`/tasks/${taskId}/${questions ? "edit" : "questions"}`}>{tx(questions ? "查看任务信息" : "查看题目资料", questions ? "View Task Details" : "View Questions")}</Link>
      <button type="button" onClick={() => void start()} disabled={busy || !task || (!questions && !task.problem_count)} className="flex items-center gap-2 rounded-lg bg-primary px-5 py-2 text-primary-foreground disabled:opacity-50">{busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : null}{label}</button>
    </div>
  </div>;
}
