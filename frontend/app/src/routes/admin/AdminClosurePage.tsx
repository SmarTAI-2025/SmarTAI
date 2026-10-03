import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { apiClient } from "@/api/client";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";

type Closure = { id: string; status: string; mode: string; error_code: string | null };
export function AdminClosurePage() {
  const { closureId } = useParams();
  const result = useQuery({ queryKey: ["admin", "closure", closureId], queryFn: async () => (await apiClient.get<Closure>(`/admin/closures/${closureId}`)).data, refetchInterval: q => q.state.data?.status === "completed" ? false : 5000 });
  const done = result.data?.status === "completed";
  return <div className="max-w-2xl space-y-5"><SectionHeader title={done ? "销户完成" : "销户进度"} description="关闭或刷新本页面不影响后台清理，可通过此页面地址再次查看。" /><Card>
    {result.isError ? <p role="alert">暂时无法读取进度；这不表示清理失败或已完成。<Button variant="secondary" onClick={() => void result.refetch()}>重新查询</Button></p> : !result.data ? <p role="status">正在读取进度…</p> : <><p role="status" className="font-semibold">{done ? "账号与业务数据已清理完成。" : "账号已禁止登录，正在清理业务数据与文件。"}</p><p className="mt-3 text-sm text-muted-foreground">{done ? result.data.mode === "blacklist" ? "用户名已释放；原邮箱保留注册限制。" : "用户名与邮箱已释放，可以重新注册。" : "清理完成前保留身份占用，防止新账号与旧数据混淆。"}</p>{result.data.error_code && <p className="mt-3 text-sm text-muted-foreground">{result.data.error_code === "cleanup_retry_required" ? "清理遇到暂时故障，后台会重试。请在运行监控中检查存储与数据库。" : "正在等待任务或文件清理。后台任务服务需保持运行。"}</p>}</>}
    <Link className="mt-6 inline-block text-primary" to="/admin/users">返回用户管理</Link></Card></div>;
}
