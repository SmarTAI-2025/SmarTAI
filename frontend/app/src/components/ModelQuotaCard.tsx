import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { useI18n } from "@/i18n/I18nProvider";
import { Button } from "@/components/ui/Button";

type Allowance = { requests: number; request_limit: number; estimated_input_tokens: number; estimated_token_limit: number };
type Weekly = { limit_usd: number; spent_usd: number; reserved_usd: number; remaining_usd: number; resets_at: string; unknown_requests: number };
type Usage = { resets_at: string; shared_pool_enabled: boolean; shared_quota_mode?: "weekly_usd" | "daily_estimated"; shared_weekly?: Weekly; shared: Allowance; shared_sol?: Allowance; history: { requests: number; request_limit: number } };
export function ModelQuotaCard() {
  const { locale } = useI18n(); const zh = locale === "zh-CN";
  const quota = useQuery({ queryKey: ["model-quota"], queryFn: async () => (await apiClient.get<Usage>("/experts/quota")).data, retry: false });
  const q = quota.data;
  const limit = (v: number) => v < 0 ? (zh ? "无限制" : "Unlimited") : v.toLocaleString();
  const reached = q && ((q.shared.request_limit >= 0 && q.shared.requests >= q.shared.request_limit) || (q.shared.estimated_token_limit >= 0 && q.shared.estimated_input_tokens >= q.shared.estimated_token_limit));
  if (q?.shared_quota_mode === "weekly_usd") return <WeeklyQuotaCard usage={q} zh={zh} busy={quota.isFetching} failed={quota.isError} refresh={() => void quota.refetch()} />;
  return <section className="rounded-[10px] border bg-card p-5 space-y-3" aria-label={zh ? "模型日额度" : "Daily model allowance"}>
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">{zh ? "模型日额度 · UTC" : "Daily model allowance · UTC"}</h2><Button variant="secondary" disabled={quota.isFetching} onClick={() => void quota.refetch()}>{zh ? "刷新用量" : "Refresh usage"}</Button></div>
    {quota.isPending ? <p role="status">{zh ? "读取用量中…" : "Loading usage…"}</p> : quota.isError ? <p role="alert">{zh ? "暂时无法读取模型额度，请重试。" : "Allowance unavailable; please retry."}</p> : q && <>
      <p className="text-sm">{zh ? "共享模型请求" : "Shared model requests"}：{q.shared.requests} / {limit(q.shared.request_limit)}；{zh ? "估算输入 token" : "Estimated input tokens"}：{q.shared.estimated_input_tokens} / {limit(q.shared.estimated_token_limit)}。</p>
      {q.shared_sol && <p className="text-sm">{zh ? "其中 Sol（共用上述额度，最多一半）" : "Sol within the shared allowance (up to half)"}：{q.shared_sol.requests} / {limit(q.shared_sol.request_limit)} {zh ? "次" : "requests"}；{zh ? "估算输入 token" : "Estimated input tokens"}：{q.shared_sol.estimated_input_tokens} / {limit(q.shared_sol.estimated_token_limit)}。</p>}
      <p className="text-sm">{zh ? "任务 Ask 调用" : "Task Ask calls"}：{q.history.requests} / {limit(q.history.request_limit)}。</p>
      {reached && <p role="status" className="text-sm text-amber-700">{zh ? "共享模型日额度已用完。请等待 UTC 00:00 重置、联系管理员调整，或配置自己的 BYOK 模型继续批改。" : "Shared allowance exhausted. Wait for UTC midnight, contact an administrator, or use your own BYOK model."}</p>}
      {!q.shared_pool_enabled && <p className="text-sm text-muted-foreground">{zh ? "平台共享模型暂未开放。" : "Shared models are currently unavailable."}</p>}
      <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">{zh ? "额度说明" : "Allowance details"}</summary><p className="mt-2 leading-5">{zh ? "失败和中断的已准入调用仍计数；估算值不代表实际 token 或费用。普通 BYOK 批改不受共享额度限制。下次重置：" : "Admitted failed or interrupted calls count. Estimates are not actual tokens or cost. Ordinary BYOK grading is outside the shared allowance. Next reset: "}{q.resets_at}</p></details>
    </>}
  </section>;
}

function WeeklyQuotaCard({ usage: q, zh, busy, failed, refresh }: { usage: Usage; zh: boolean; busy: boolean; failed: boolean; refresh: () => void }) {
  const w = q.shared_weekly;
  const dollars = (n: number) => `$${n.toFixed(2)}`;
  return <section className="rounded-[10px] border bg-card p-5 space-y-3" aria-label={zh ? "共享模型周额度" : "Weekly shared allowance"}>
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">{zh ? "共享模型周额度" : "Weekly shared allowance"}</h2><Button variant="secondary" disabled={busy} onClick={refresh}>{zh ? "刷新用量" : "Refresh usage"}</Button></div>
    {failed && w && <p role="alert" className="text-sm">{zh ? "刷新失败，显示的是上次读取的用量。请重试。" : "Refresh failed. Showing the last known usage; please retry."}</p>}
    {!w ? <p role="alert">{zh ? "暂时无法读取周额度，请重试。" : "Weekly allowance unavailable; please retry."}</p> : <>
      <p className="text-sm">{zh ? "本周已用" : "Spent this week"}：{dollars(w.spent_usd)} / {dollars(w.limit_usd)}</p>
      <p className="text-sm text-muted-foreground">{zh ? "三个共享模型共用，每周一新加坡时间 00:00 重置。" : "Shared by all three models. Resets Monday at 00:00 Singapore time."}</p>
      {w.reserved_usd > 0 && <p className="text-sm">{zh ? "待结算预留" : "Reserved pending settlement"}：{dollars(w.reserved_usd)}</p>}
      {w.unknown_requests > 0 && <p role="status" className="text-sm text-amber-700">{zh ? "部分调用用量尚未确认，预留金额暂未释放。" : "Some calls have unconfirmed usage; their reservations remain held."}</p>}
      {w.spent_usd >= w.limit_usd && <p role="status" className="text-sm text-amber-700">{zh ? "本周共享额度已用完，请等待重置或使用自己的 BYOK 模型。" : "Weekly allowance exhausted. Wait for reset or use your own BYOK model."}</p>}
      <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">{zh ? "额度说明" : "Allowance details"}</summary><p className="mt-2 leading-5">{zh ? "费用按 OpenAI 返回的真实用量及官方价格计算。预留金额不是已消费金额，结算后释放未使用部分。个人 BYOK 独立。下次重置：" : "Cost uses OpenAI-reported usage and official prices. Reservations are not spending; unused funds are released after settlement. Personal BYOK is separate. Next reset: "}{w.resets_at}</p></details>
    </>}
    <p className="text-sm">{zh ? "任务 Ask 每日调用" : "Daily Task Ask calls"}：{q.history.requests} / {q.history.request_limit < 0 ? (zh ? "无限制" : "Unlimited") : q.history.request_limit}</p>
    {!q.shared_pool_enabled && <p className="text-sm text-muted-foreground">{zh ? "平台共享模型暂未开放。" : "Shared models are currently unavailable."}</p>}
  </section>;
}
