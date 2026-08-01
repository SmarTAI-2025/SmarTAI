import { useHealthStatus } from "@/api/hooks/health";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";

/**
 * Admin system overview: backend health + engine + resource usage. Read-only;
 * the reset/backup boundary lives in the operations docs, not here.
 */
export function AdminSystemPage() {
  const health = useHealthStatus();

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <SectionHeader
        title="系统状态"
        description="后端健康检查、引擎与资源占用。重置与备份属运维边界，见部署文档。"
        action={
          <Button variant="secondary" onClick={() => health.refetch()} disabled={health.isFetching}>
            刷新
          </Button>
        }
      />
      <Card className="grid gap-3 text-sm">
        {health.isLoading ? (
          <p className="text-muted-foreground">检查中...</p>
        ) : health.isError ? (
          <p className="text-danger">无法连接后端。</p>
        ) : health.data ? (
          <>
            <Row label="状态" value={health.data.status} />
            <Row label="引擎" value={health.data.engine} />
            <Row label="内存 (MB)" value={String(health.data.memory_usage_mb)} />
            <Row label="CPU (%)" value={String(health.data.cpu_percent)} />
          </>
        ) : null}
      </Card>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between border-b py-1 last:border-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}
