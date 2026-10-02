import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";

type Preview = { available: boolean; environment: string; reason?: string; fingerprint?: string; confirmation?: string; tables?: Record<string, number>; storage?: { files: number; versions_and_markers: number; multipart_uploads: number; bytes: number } };
export function AdminMaintenancePage() {
  const preview = useQuery({ queryKey: ["admin", "reset-preview"], queryFn: async () => (await apiClient.get<Preview>("/admin/maintenance/preview")).data, retry: false, staleTime: 0, refetchOnWindowFocus: false });
  const p = preview.data;
  return <div className="space-y-5">
    <SectionHeader title="系统维护" description="先核对清理范围；全清空需要停止服务后，在维护终端明确确认。" />
    <Card className="border-red-200"><h2 className="text-lg font-semibold text-danger">全清空业务数据</h2><p className="mt-2 text-sm leading-6">恢复到无人注册使用的状态。包括所有管理员和教师、课程与批改任务、作答与结果、教材题库、上传文件、登录与验证记录、后台任务、邮箱黑名单、业务配置和单用户配额覆盖、运营事件及应用内审计。完成后用户名和邮箱均可重新注册。</p><p className="mt-3 text-sm leading-6">保留数据库结构、运行基础设施、部署配置和必要密钥。清空后由运维在私有终端重新初始化首位管理员；公开注册不能创建管理员。业务规则恢复为部署环境变量与代码默认值，不保留后台的旧覆盖值；重新开放注册前需核对初始邮箱域名规则。</p></Card>
    <Card><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold">清理范围预览</h2><Button variant="secondary" disabled={preview.isFetching} onClick={() => void preview.refetch()}>{preview.isFetching ? "正在核对…" : "刷新清理预览"}</Button></div>
      {preview.isError ? <p role="alert" className="mt-4 text-danger">预览读取失败，请重试。未执行任何清理。</p> : preview.isLoading ? <p role="status" className="mt-4">正在读取隔离环境范围…</p> : !p?.available ? <p className="mt-4 text-sm leading-6">{p?.reason === "production_execution_not_enabled" ? "当前版本不开放生产环境全清空。" : "尚未验证专用存储与维护目录，无法提供完整清理预览。请先按维护文档配置隔离环境，再刷新。"} 页面没有执行删除的接口。</p> : <>
        <dl className="mt-5 grid grid-cols-2 gap-4 sm:grid-cols-4">{[["环境", p.environment], ["全部账号", p.tables?.users ?? 0], ["数据库记录", Object.values(p.tables ?? {}).reduce((a, b) => a + b, 0)], ["本地文件", p.storage?.files ?? 0], ["对象版本及标记", p.storage?.versions_and_markers ?? 0], ["未完成分段上传", p.storage?.multipart_uploads ?? 0], ["已盘点存储", `${((p.storage?.bytes ?? 0) / 1024 / 1024).toFixed(2)} MiB`]].map(([name, value]) => <div key={name}><dt className="text-sm text-muted-foreground">{name}</dt><dd className="mt-1 text-lg font-semibold">{value}</dd></div>)}</dl>
        <p className="mt-5 break-all text-xs text-muted-foreground">本次预览编号：{p.fingerprint}</p><p className="mt-2 text-sm">服务停止后，终端还会重新盘点范围；数据变化则本次预览作废，必须重新预览。</p>
      </>}
    </Card>
    <Card><h2 className="font-semibold">维护执行步骤</h2><ol className="mt-3 list-decimal space-y-3 pl-5 text-sm leading-6"><li>配置专用数据库、上传与临时目录，确认外部备份和第三方副本的处理范围。</li><li>停止公共服务、管理服务、后台任务和调度器；维护锁阻止正在运行时执行清空。</li><li>在维护终端运行清理工具的预览，核对数量，输入该次预览的确认短语后执行。</li><li>失败后保持服务停止，使用同一维护记录恢复；部分文件删除不可回滚，不能提前宣称成功。</li><li>完成后保留业务库外的不含用户名和邮箱的维护回执，重新初始化首位管理员并重启全部进程。</li></ol><p className="mt-4 text-sm text-muted-foreground">本次只在可丢弃的隔离数据上验证。共享临时目录、外部备份和第三方服务中的副本不能自动算作已清理。</p></Card>
  </div>;
}
