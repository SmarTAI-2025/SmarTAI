import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { queryAdminAdoption, type AdoptionResult, type RetentionCell } from "@/api/adminMonitoring";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { chartColors, chartTooltip, integer, ObservationError, ObservationMetric, ObservationNotes, timestamp } from "./observationUi";

export function adoptionWindowStart(days: number, zone: string, now = Date.now() / 1000) {
  const offset = zone === "Asia/Singapore" ? 8 * 3600 : 0;
  return Math.floor((now + offset) / 86400) * 86400 - offset - (days - 1) * 86400;
}

export function AdminAnalyticsPage() {
  const [days, setDays] = useState(30);
  const [zone, setZone] = useState("Asia/Singapore");
  const query = useQuery({ queryKey: ["admin", "adoption", days, zone], queryFn: ({ signal }) => queryAdminAdoption({ start: adoptionWindowStart(days, zone), timezone: zone }, signal), retry: false, staleTime: 60_000 });
  const data = query.data;
  return <div className="space-y-6">
    <SectionHeader title="运营统计" description="了解教师账号增长、使用频率和回访；所有数值来自已记录的业务事件。" action={<Button variant="secondary" onClick={() => void query.refetch()} disabled={query.isFetching}><RefreshCw size={16} />{query.isFetching ? "加载中" : "刷新统计"}</Button>} />
    <div className="flex flex-wrap items-end gap-4">
      <label className="space-y-1 text-sm"><span className="block text-muted-foreground">时间区间</span><select className="h-9 rounded-md border bg-card px-3" value={days} onChange={event => setDays(Number(event.target.value))}>{[7, 30, 90].map(value => <option key={value} value={value}>近 {value} 天</option>)}</select></label>
      <label className="space-y-1 text-sm"><span className="block text-muted-foreground">日历时区</span><select className="h-9 rounded-md border bg-card px-3" value={zone} onChange={event => setZone(event.target.value)}><option value="Asia/Singapore">新加坡（UTC+8）</option><option value="UTC">UTC</option></select></label>
      <span className="pb-2 text-xs text-muted-foreground">教师角色 · 所有未分类账号（含内部测试） · 当前数据库</span>
    </div>
    {query.isLoading ? <Card role="status">正在汇总使用事件…</Card> : query.isError || !data ? <ObservationError retry={() => void query.refetch()} /> : <>
      <Card className="border-primary/20 bg-primary/[0.03]"><h2 className="text-sm font-semibold">{data.coverage.status === "unavailable" ? "尚无可用事件" : "部分覆盖 · 已记录行为口径"}</h2><p className="mt-2 text-sm leading-6 text-muted-foreground">最早保留事件：{timestamp(data.coverage.first_retained_event_at, zone)}。此前历史未知，内部测试账号尚未区分；业务动作不等于教师已确认的批改完成。</p><p className="mt-1 text-xs leading-5 text-muted-foreground">查询 {timestamp(data.start, zone)} 至 {timestamp(data.end, zone)}（结束时刻不包含）· 当前日未结束 · 最后事件 {timestamp(data.coverage.last_retained_event_at, zone)}</p></Card>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <ObservationMetric label="新增教师账号" value={integer(data.summary.new_users)} detail={`已记录账号创建；当前存量 ${integer(data.summary.current_teacher_accounts)}`} />
        <ObservationMetric label="业务活跃教师" value={integer(data.summary.business_users)} detail="至少一次建任务、课程、作业或启动批改" />
        <ObservationMetric label="登录活跃教师" value={integer(data.summary.login_users)} detail="成功登录去重，独立于业务活跃" />
        <ObservationMetric label="每位活跃教师动作数" value={integer(data.summary.actions_per_business_user)} detail={`${integer(data.summary.business_actions)} 条业务动作 / ${integer(data.summary.business_users)} 位业务活跃教师`} />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Trend data={data} title="教师增长" description="每天新增的已记录教师账号；不重建历史累计账号。" lines={[{ key: "new_users", name: "新增教师", color: chartColors.blue }]} />
        <Trend data={data} title="活跃与回访" description="每日去重人数，不能将各日相加作为区间活跃人数。" lines={[{ key: "business_users", name: "业务活跃", color: chartColors.blue }, { key: "login_users", name: "登录活跃", color: chartColors.green }]} />
      </div>
      <Card><h2 className="font-semibold">使用频率</h2><p className="mt-1 text-sm text-muted-foreground">区间内有业务动作的天数，仅含业务活跃教师。</p>{data.frequency.length ? <div className="mt-4 grid gap-3 sm:grid-cols-4">{data.frequency.map(bucket => <div key={bucket.label} className="rounded-md bg-muted/50 p-3"><p className="text-sm text-muted-foreground">{bucket.label}</p><p className="mt-2 text-xl font-semibold">{integer(bucket.users)} <span className="text-sm font-normal text-muted-foreground">人</span></p></div>)}</div> : <p className="py-6 text-sm text-muted-foreground">尚无频率数据。</p>}</Card>
      <Card><h2 className="font-semibold">首次业务动作后的留存</h2><p className="mt-1 text-sm leading-6 text-muted-foreground">采用首次保留业务动作作为起点。W1 为第 7–14 天，W4 为第 28–35 天；只将完整经历窗口的教师计入分母，区间截止为观察截止。</p>
        {data.retention.length ? <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[560px] text-left text-sm"><thead className="border-b text-muted-foreground"><tr><th className="px-2 py-3">首次动作日期</th><th className="px-2 py-3">群组人数</th><th className="px-2 py-3">W1 留存</th><th className="px-2 py-3">W4 留存</th></tr></thead><tbody>{data.retention.map(cohort => <tr key={cohort.date} className="border-b last:border-0"><td className="px-2 py-3">{cohort.date}</td><td className="px-2 py-3">{cohort.users}</td><td className="px-2 py-3"><Retention value={cohort.w1} /></td><td className="px-2 py-3"><Retention value={cohort.w4} /></td></tr>)}</tbody></table></div> : <p className="py-8 text-sm text-muted-foreground">区间内尚无首次业务动作群组。新采集的数据需要等待窗口成熟。</p>}
      </Card>
      <ObservationNotes>{Object.entries(data.definitions).filter(([key]) => key !== "business_events").map(([key, value]) => <p key={key}>{value}</p>)}{data.coverage.notes.map(note => <p key={note}>{note}</p>)}<p>口径版本：{data.metric_version} · 数据截至 {timestamp(data.as_of, zone)} · {integer(data.coverage.event_count)} 条已记录事件。</p></ObservationNotes>
    </>}
  </div>;
}

function Retention({ value }: { value: RetentionCell }) {
  return <div><span className="font-medium tabular-nums">{value.rate === null ? "尚未成熟" : `${(value.rate * 100).toFixed(1)}%`}</span><p className="mt-1 text-xs text-muted-foreground">{value.numerator} / {value.denominator} 人{value.pending ? ` · ${value.pending} 人待观察` : ""}</p></div>;
}

function Trend({ data, title, description, lines }: { data: AdoptionResult; title: string; description: string; lines: Array<{ key: string; name: string; color: string }> }) {
  return <Card className="min-w-0"><h2 className="font-semibold">{title}</h2><p className="mt-1 text-sm leading-6 text-muted-foreground">{description}</p>{data.coverage.status === "unavailable" ? <p className="py-20 text-center text-sm text-muted-foreground">尚无事件，不生成历史曲线。</p> : <div className="mt-4 h-64 w-full min-w-0" role="img" aria-label={`${title}每日时间曲线`}><ResponsiveContainer width="100%" height="100%"><LineChart data={data.series} margin={{ top: 10, right: 10, left: -20, bottom: 5 }}><CartesianGrid stroke="rgb(var(--border))" vertical={false} strokeDasharray="3 3" /><XAxis dataKey="date" tickFormatter={(date: string) => date.slice(5)} minTickGap={30} tick={{ fontSize: 12 }} /><YAxis allowDecimals={false} tick={{ fontSize: 12 }} /><Tooltip contentStyle={chartTooltip} /><Legend />{lines.map(line => <Line key={line.key} type="linear" dataKey={line.key} name={line.name} stroke={line.color} strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />)}</LineChart></ResponsiveContainer></div>}</Card>;
}
