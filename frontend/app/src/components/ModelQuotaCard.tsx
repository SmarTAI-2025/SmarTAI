import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { useI18n } from "@/i18n/I18nProvider";
import { Button } from "@/components/ui/Button";

type Usage = { resets_at: string; shared_pool_enabled: boolean; shared: { requests: number; request_limit: number; estimated_input_tokens: number; estimated_token_limit: number }; history: { requests: number; request_limit: number } };
export function ModelQuotaCard() {
  const { locale } = useI18n(); const zh = locale === "zh-CN";
  const quota = useQuery({ queryKey: ["model-quota"], queryFn: async () => (await apiClient.get<Usage>("/experts/quota")).data, retry: false });
  const q = quota.data;
  const limit = (v: number) => v < 0 ? (zh ? "无限制" : "Unlimited") : v.toLocaleString();
  const reached = q && ((q.shared.request_limit >= 0 && q.shared.requests >= q.shared.request_limit) || (q.shared.estimated_token_limit >= 0 && q.shared.estimated_input_tokens >= q.shared.estimated_token_limit));
  return <section className="rounded-[10px] border bg-card p-5 space-y-3" aria-label={zh ? "模型日额度" : "Daily model allowance"}>
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">{zh ? "模型日额度 · UTC" : "Daily model allowance · UTC"}</h2><Button variant="secondary" disabled={quota.isFetching} onClick={() => void quota.refetch()}>{zh ? "刷新用量" : "Refresh usage"}</Button></div>
    {quota.isPending ? <p role="status">{zh ? "读取用量中…" : "Loading usage…"}</p> : quota.isError ? <p role="alert">{zh ? "暂时无法读取模型额度，请重试。" : "Allowance unavailable; please retry."}</p> : q && <>
      <p className="text-sm">{zh ? "共享模型请求" : "Shared model requests"}：{q.shared.requests} / {limit(q.shared.request_limit)}；{zh ? "估算输入 token" : "Estimated input tokens"}：{q.shared.estimated_input_tokens} / {limit(q.shared.estimated_token_limit)}。</p>
      <p className="text-sm">{zh ? "任务 Ask 调用" : "Task Ask calls"}：{q.history.requests} / {limit(q.history.request_limit)}。</p>
      {reached && <p role="status" className="text-sm text-amber-700">{zh ? "共享模型日额度已用完。请等待 UTC 00:00 重置、联系管理员调整，或配置自己的 BYOK 模型继续批改。" : "Shared allowance exhausted. Wait for UTC midnight, contact an administrator, or use your own BYOK model."}</p>}
      {!q.shared_pool_enabled && <p className="text-sm text-muted-foreground">{zh ? "平台共享模型当前关闭；额度设置不会打开共享池。" : "The shared pool is disabled; an allowance does not enable it."}</p>}
      <p className="text-xs text-muted-foreground">{zh ? "失败和中断的已准入调用仍计数；估算值不代表实际 token 或费用。普通 BYOK 批改不受共享额度限制。下次重置：" : "Admitted failed or interrupted calls count. Estimates are not actual tokens or cost. Ordinary BYOK grading is outside the shared allowance. Next reset: "}{q.resets_at}</p>
    </>}
  </section>;
}
