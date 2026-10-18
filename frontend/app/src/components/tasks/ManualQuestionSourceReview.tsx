import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { getBlob } from "@/api/client";
import { useManuallyCompleteQuestionPreparation } from "@/api/hooks/tasks";
import { useWorkflowInput } from "@/api/workflowInputs";
import { usePageDraft } from "@/hooks/usePageDraft";
import { objectDraftCodec } from "@/lib/pageDraftStore";
import { useI18n } from "@/i18n/I18nProvider";
import { Button } from "@/components/ui/Button";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";

const blank = () => ({ q_id: "", type: "short", stem: "", reference_answer: "", criterion: "", max_score: 10 });
const initial = () => ({ questions: [blank()], completed: [] as string[] });
const codec = objectDraftCodec(initial());

export function ManualQuestionSourceReview({ taskId, jobId, revision }: { taskId: string; jobId: string; revision: number }) {
  const { locale } = useI18n();
  const text = (zh: string, en: string) => locale === "zh-CN" ? zh : en;
  const navigate = useNavigate();
  const input = useWorkflowInput(taskId, "problems");
  const mutation = useManuallyCompleteQuestionPreparation();
  const draft = usePageDraft(`manual-questions:${taskId}:${jobId}`, initial, codec, String(revision), undefined, mutation.isPending);
  const [questions, setQuestions] = draft.field("questions");
  const [completed, setCompleted] = draft.field("completed");
  const [error, setError] = useState("");
  const sources = input.data?.input?.job_id === jobId ? input.data.input.sources : [];
  const ready = sources.length > 0 && sources.every(source => completed.includes(source.source_token))
    && questions.length > 0 && questions.every(question => question.q_id.trim() && question.stem.trim() && question.reference_answer.trim() && question.criterion.trim() && question.max_score > 0);
  async function save() {
    if (!ready || mutation.isPending || draft.protection.conflict) return;
    setError("");
    try {
      await mutation.mutateAsync({ taskId, jobId, expectedWorkflowRevision: revision,
        manualQuestions: questions, completedSourceTokens: completed });
      await draft.clear();
      navigate(`/tasks/${taskId}/questions`, { replace: true });
    } catch { setError(text("保存失败，请检查题号是否重复、评分分值、来源确认，或刷新任务状态。", "Could not save. Check duplicate question IDs, rubric points and source confirmations, or refresh the task.")); }
  }
  async function original(token: string, filename: string) {
    try {
      const blob = await getBlob(`/tasks/${encodeURIComponent(taskId)}/question-preparation/${encodeURIComponent(jobId)}/manual-review/${encodeURIComponent(token)}/content`);
      const url = URL.createObjectURL(blob); const link = document.createElement("a");
      link.href = url; link.download = filename; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { setError(text("原资料暂时无法读取，请保留队列项并稍后重试。", "The original is unavailable. Keep this queue item and retry later.")); }
  }
  return <section className="grid max-w-[1100px] gap-5">
    <h1 className="text-2xl font-bold">{text("人工复核题目来源", "Review question sources manually")}</h1>
    <NewTaskStepper currentStep={2} blockedFromStep={3} lockedStepReason={text("请先完成全部来源并保存题目", "Complete all sources and save the questions first")} />
    <p>{text("已暂停，尚未形成完整题目清单。原资料和已完成识别已保留。请按原资料自行录入题目，并逐项确认来源；完成前不能进入下一阶段。", "Paused before a complete question list was available. Original sources and completed recognition are preserved. Enter the questions from the originals and confirm each source before continuing.")}</p>
    <ul aria-label={text("未完成来源复核队列", "Unfinished source review queue")} className="grid gap-2">
      {sources.map(source => <li key={source.source_token} className="flex flex-wrap items-center gap-3 rounded-lg border p-3">
        <span>{source.filename} · {completed.includes(source.source_token) ? text("已人工核对来源", "Source checked manually") : text("已暂停，等待人工处理", "Paused; awaiting manual review")}</span>
        <Button variant="secondary" onClick={() => void original(source.source_token, source.filename)}>{text("下载原资料", "Download original")}</Button>
        <label><input type="checkbox" checked={completed.includes(source.source_token)} disabled={mutation.isPending} onChange={event => setCompleted(current => event.target.checked ? [...current, source.source_token] : current.filter(token => token !== source.source_token))} /> {text("已按此来源补齐题目和资料", "Questions and materials from this source are complete")}</label>
      </li>)}
    </ul>
    {input.isError ? <p role="alert">{text("来源队列读取失败，请刷新。", "Could not load the source queue. Refresh to retry.")}</p> : null}
    {questions.map((question, index) => <fieldset key={index} disabled={mutation.isPending} className="grid gap-3 rounded-lg border p-4">
      <legend>{text(`人工录入 ${index + 1}`, `Manual entry ${index + 1}`)}</legend>
      {([ ["q_id", "题号", "Question ID"], ["stem", "题干", "Question"], ["reference_answer", "标准答案", "Reference answer"], ["criterion", "评分标准（写明分值）", "Rubric (include points)"] ] as const).map(([field, zh, en]) => <label key={field} className="grid gap-1 text-sm">{text(zh, en)}<textarea value={question[field]} rows={field === "q_id" ? 1 : 3} className="rounded border p-2" onChange={event => setQuestions(current => current.map((row, at) => at === index ? { ...row, [field]: event.target.value } : row))} /></label>)}
      <label>{text("题型", "Question type")} <select value={question.type} onChange={event => setQuestions(current => current.map((row, at) => at === index ? { ...row, type: event.target.value } : row))}><option value="short">{text("简答/计算", "Written / calculation")}</option><option value="programming">{text("编程", "Programming")}</option></select></label>
      <label>{text("满分", "Maximum score")} <input type="number" min="0.1" max="10000" value={question.max_score} onChange={event => setQuestions(current => current.map((row, at) => at === index ? { ...row, max_score: Number(event.target.value) } : row))} /></label>
      <Button variant="secondary" onClick={() => setQuestions(current => current.filter((_, at) => at !== index))}>{text("移除此录入项", "Remove this entry")}</Button>
    </fieldset>)}
    <Button variant="secondary" disabled={mutation.isPending} onClick={() => setQuestions(current => [...current, blank()])}>{text("新增题目", "Add question")}</Button>
    {error ? <p role="alert">{error}</p> : null}
    <Button disabled={!ready || mutation.isPending || !draft.protection.loaded || Boolean(draft.protection.conflict)} onClick={() => void save()}>{text("保存题目并继续复核", "Save questions and continue review")}</Button>
  </section>;
}
