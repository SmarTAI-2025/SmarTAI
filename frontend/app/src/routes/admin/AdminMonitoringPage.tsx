import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { getAdminMonitoring, type CapacitySample, type HealthCheck } from "@/api/adminMonitoring";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { bytes, chartColors, chartTooltip, integer, ObservationError, ObservationMetric, ObservationNotes, timestamp } from "./observationUi";

export function capacityChartData(samples: CapacitySample[]) {
  const data: Array<{ at: number; free_bytes: number | null; application_bytes: number | null }> = [];
  for (const [index, sample] of samples.entries()) {
    if (index > 0 && sample.at - samples[index - 1].at > 900) data.push({ at: sample.at - 1, free_bytes: null, application_bytes: null });
    data.push(sample);
  }
  return data;
}

export function AdminMonitoringPage() {
  const query = useQuery({ queryKey: ["admin", "monitoring"], queryFn: ({ signal }) => getAdminMonitoring(signal), retry: false, staleTime: 30_000 });
  const data = query.data;
  return <div className="space-y-6">
    <SectionHeader title="运行监控" description="查看服务检查、应用占用与分区容量，及时安排维护和扩容。" action={<Button variant="secondary" onClick={() => void query.refetch()} disabled={query.isFetching}><RefreshCw size={16} />{query.isFetching ? "检查中" : "刷新检查"}</Button>} />
    {query.isLoading ? <Card role="status">正在检查服务与容量…</Card> : query.isError || !data ? <ObservationError retry={() => void query.refetch()} /> : <>
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>检查时间：{timestamp(data.as_of)} · Asia/Singapore</span><span>仅当前管理员服务实例</span></div>
      <div className="grid gap-4 sm:grid-cols-3">
        <Health label="管理员服务" value={data.health.application} detail="当前私有请求已响应" />
        <Health label="数据库" value={data.health.database} detail="仅验证连接与简单查询" />
        <Health label="文件存储" value={data.health.storage} detail={data.health.storage.backend === "object" ? "仅验证对象桶可访问" : "仅验证本地目录访问权限"} />
      </div>
      <p className="text-xs text-muted-foreground">公开应用：未检查 · 模型服务：未检查。当前检查不执行文件读写或模型调用。</p>
      <Card className={data.recommendation.level === "critical" ? "border-danger/40" : data.recommendation.level === "warning" ? "border-warning/40" : ""}>
        <h2 className="font-semibold">容量建议</h2><p className={`mt-2 text-sm leading-6 ${data.recommendation.level === "critical" ? "text-danger" : "text-muted-foreground"}`}>{data.recommendation.message}</p>
        <p className="mt-2 text-xs text-muted-foreground">{data.disk.scope === "configured_host_mount" ? "运维已配置宿主挂载路径；请确认它确实对应目标宿主分区。" : "目前观测应用所在分区。宿主磁盘：不可用，尚未配置并核验宿主挂载。"}</p>
      </Card>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <ObservationMetric label="分区总容量" value={bytes(data.disk.total_bytes)} detail="当前进程可见分区的实测值" />
        <ObservationMetric label="分区可用空间" value={bytes(data.disk.free_bytes)} detail={data.disk.used_ratio === null ? "无法读取分区使用率" : `已用 ${(data.disk.used_ratio * 100).toFixed(1)}% · ${bytes(data.disk.used_bytes)}`} />
        <ObservationMetric label="应用文件账面占用" value={bytes(data.application_storage.bytes)} detail={`${integer(data.application_storage.objects)} 个去重对象，含待清理文件`} />
        <ObservationMetric label="预计剩余空间时间" value={data.history.projection.days_remaining === null ? "暂无估计" : `${integer(data.history.projection.days_remaining)} 天`} detail={data.history.projection.status === "available" ? `净变化 ${bytes(data.history.projection.bytes_per_day)}/天；非容量保证` : "需要至少 24 小时连续性足够的采样"} />
      </div>
      <Card>
        <h2 className="font-semibold">近 7 天空间趋势</h2><p className="mt-1 text-sm leading-6 text-muted-foreground">分区空间与应用账面占用口径不同。刷新页面时采样，断档不连线；尚未安装后台采样。</p>
        {data.history.samples.length ? <>
          <div className="mt-5 h-72 w-full min-w-0" role="img" aria-label="磁盘可用空间与应用账面占用时间曲线">
            <ResponsiveContainer width="100%" height="100%"><LineChart data={capacityChartData(data.history.samples)} margin={{ top: 8, right: 12, bottom: 8, left: 8 }}>
              <CartesianGrid stroke="rgb(var(--border))" strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="at" type="number" domain={["dataMin", "dataMax"]} tickFormatter={(at: number) => new Date(at * 1000).toLocaleDateString("zh-CN", { timeZone: "Asia/Singapore", month: "numeric", day: "numeric" })} minTickGap={30} tick={{ fontSize: 12 }} />
              <YAxis tickFormatter={bytes} width={80} tick={{ fontSize: 12 }} />
              <Tooltip labelFormatter={(at) => timestamp(Number(at))} formatter={(value) => bytes(Number(value))} contentStyle={chartTooltip} />
              <Legend /><Line type="linear" dataKey="free_bytes" name="分区可用空间" stroke={chartColors.blue} dot={data.history.samples.length < 10} isAnimationActive={false} connectNulls={false} />
              <Line type="linear" dataKey="application_bytes" name="应用账面占用" stroke={chartColors.green} dot={data.history.samples.length < 10} isAnimationActive={false} connectNulls={false} />
            </LineChart></ResponsiveContainer>
          </div>
          <p className="mt-3 text-xs text-muted-foreground">本图起点 {timestamp(data.history.coverage_start)} · {data.history.samples.length} 条实测样本 · 最多每 {data.history.sample_interval_seconds / 60} 分钟一条</p>
          <details className="mt-4 text-sm"><summary className="cursor-pointer">查看最近采样明细</summary><div className="mt-3 overflow-x-auto"><table className="w-full min-w-[540px] text-left text-sm"><thead><tr className="border-b text-muted-foreground"><th className="py-2">时间（新加坡）</th><th>分区可用</th><th>应用账面</th></tr></thead><tbody>{data.history.samples.slice(-12).map(row => <tr key={row.at} className="border-b last:border-0"><td className="py-2">{timestamp(row.at)}</td><td>{bytes(row.free_bytes)}</td><td>{bytes(row.application_bytes)}</td></tr>)}</tbody></table></div></details>
        </> : <p className="py-10 text-center text-sm text-muted-foreground">尚无容量历史。磁盘与数据库均可用时，刷新会保存第一条样本。</p>}
      </Card>
      <ObservationNotes><p>知识文件预留：{bytes(data.application_storage.knowledge_reserved_bytes)}，预留不代表已占用。</p>{data.notes.map(note => <p key={note}>{note}</p>)}<p>口径版本：{data.metric_version}</p></ObservationNotes>
    </>}
  </div>;
}

function Health({ label, value, detail }: { label: string; value: HealthCheck; detail: string }) {
  return <Card><div className="flex justify-between gap-2"><h2 className="font-medium">{label}</h2><span className={`text-sm ${value.status === "ok" ? "text-accent" : value.status === "error" ? "text-danger" : "text-muted-foreground"}`}>{value.status === "ok" ? "检查通过" : value.status === "error" ? "检查失败" : "不可用"}</span></div><p className="mt-2 text-xs leading-5 text-muted-foreground">{detail}{value.latency_ms != null ? ` · ${value.latency_ms} ms` : ""}</p></Card>;
}
