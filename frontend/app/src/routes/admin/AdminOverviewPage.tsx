import { useAdminOverview } from "@/api/hooks/admin";
import type { AdminOverview } from "@/api/admin";
import { Card, SectionHeader } from "@/components/ui/Card";

export function AdminOverviewPage() {
  const overview = useAdminOverview();
  return (
    <div className="space-y-6">
      <SectionHeader title="运营概览" description="只展示数据库中已经存在的事实；尚未采集的使用与费用指标保持未知。" />
      {overview.isLoading ? <Card>加载中...</Card> : overview.isError || !overview.data ? <Card className="text-danger">概览加载失败，请稍后重试。</Card> : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Metric label="账号总数" value={overview.data.users.total} />
            <Metric label="当前启用" value={overview.data.users.active} />
            <Metric label="课程" value={overview.data.education.courses} />
            <Metric label="批改运行" value={overview.data.education.grading_runs} />
          </div>
          <Card>
            <h2 className="font-semibold">平台共用额度</h2>
            <p className="mt-2 text-sm text-muted-foreground">{overview.data.shared_pool.enabled ? "已由运行配置开启" : "默认关闭"}。页面只读。现有模型额度按进程计数，尚非跨进程的全局预算；部署侧维持原有规则。存储额度可在“业务配置”调整。</p>
            <div className="mt-4 grid gap-2 text-sm sm:grid-cols-2"><span>每日请求上限：{overview.data.shared_pool.daily_request_limit}</span><span>估算输入 token 上限：{overview.data.shared_pool.daily_estimated_token_limit.toLocaleString()}</span></div>
          </Card>
          <UsageSummary usage={overview.data.usage} />
        </>
      )}
    </div>
  );
}

const usageLabels: Record<string, string> = {
  active_users: "活跃用户",
  activated_users: "已激活用户",
  new_users: "新增账号",
  login_successes: "登录次数",
  tasks_created: "任务创建",
  courses_created: "课程创建",
  assignments_created: "作业创建",
  grading_runs_started: "批改启动",
  submissions_created: "提交次数",
};

function UsageSummary({ usage }: { usage: AdminOverview["usage"] }) {
  const available = usage.metrics.filter((metric) => usage.metric_status[metric] === "available");
  return (
    <Card>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="font-semibold">近 30 天使用统计</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {usage.status === "available" ? `已采集 ${usage.event_count.toLocaleString()} 条事件` : "尚未采集到使用事件，后续事件会自动进入统计。"}
          </p>
        </div>
        <span className="rounded-full border px-2 py-1 text-xs text-muted-foreground">
          {usage.status === "available" ? "数据可用" : "等待数据"}
        </span>
      </div>
      {available.length ? (
        <>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {available.slice(0, 8).map((metric) => (
              <div key={metric} className="rounded-md bg-muted/40 p-3">
                <p className="text-xs text-muted-foreground">{usageLabels[metric] ?? metric}</p>
                <p className="mt-1 text-xl font-semibold">{(usage.totals[metric] ?? 0).toLocaleString()}</p>
              </div>
            ))}
          </div>
          {usage.series.length ? (
            <div className="mt-5 overflow-x-auto">
              <table className="w-full min-w-[620px] text-left text-sm">
                <thead className="border-b text-muted-foreground"><tr><th className="px-2 py-2">日期</th>{available.slice(0, 5).map((metric) => <th key={metric} className="px-2 py-2">{usageLabels[metric] ?? metric}</th>)}</tr></thead>
                <tbody>{usage.series.slice(-14).map((row) => <tr key={String(row.bucket)} className="border-b last:border-0"><td className="px-2 py-2">{String(row.bucket)}</td>{available.slice(0, 5).map((metric) => <td key={metric} className="px-2 py-2">{Number(row[metric] ?? 0).toLocaleString()}</td>)}</tr>)}</tbody>
              </table>
            </div>
          ) : null}
        </>
      ) : (
        <p className="mt-4 text-sm text-muted-foreground">当前没有可用的使用事件。统计不会把未知数据显示成 0。</p>
      )}
    </Card>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return <Card><p className="text-sm text-muted-foreground">{label}</p><p className="mt-2 text-3xl font-semibold">{value.toLocaleString()}</p></Card>;
}
