import type { ReactNode } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";

export const integer = (value: number | null) => value === null ? "暂无数据" : value.toLocaleString("zh-CN", { maximumFractionDigits: 1 });
export function bytes(value: number | null): string {
  if (value === null) return "不可用";
  const sign = value < 0 ? "−" : "";
  const absolute = Math.abs(value);
  const exponent = Math.min(4, Math.max(0, Math.floor(Math.log2(absolute || 1) / 10)));
  return `${sign}${(absolute / 1024 ** exponent).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ${["B", "KiB", "MiB", "GiB", "TiB"][exponent]}`;
}
export const timestamp = (value: number | null, zone = "Asia/Singapore") => value === null ? "尚无记录" : new Date(value * 1000).toLocaleString("zh-CN", { timeZone: zone, hour12: false });

export function ObservationMetric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <Card><p className="text-sm text-muted-foreground">{label}</p><p className="mt-2 text-2xl font-semibold tabular-nums">{value}</p><p className="mt-2 text-xs leading-5 text-muted-foreground">{detail}</p></Card>;
}
export function ObservationError({ retry }: { retry: () => void }) {
  return <Card role="alert"><p className="font-medium text-danger">数据加载失败</p><p className="mt-1 text-sm text-muted-foreground">请检查连接和管理员权限后重试。若区间数据过多，请缩短时间区间。</p><Button variant="secondary" className="mt-3" onClick={retry}>重试</Button></Card>;
}
export function ObservationNotes({ children }: { children: ReactNode }) {
  return <details className="rounded-lg border bg-card p-4 text-sm"><summary className="cursor-pointer font-medium">指标口径与覆盖范围</summary><div className="mt-3 space-y-2 leading-6 text-muted-foreground">{children}</div></details>;
}

export const chartColors = { blue: "rgb(var(--primary))", green: "rgb(var(--accent))", muted: "rgb(var(--muted-foreground))" };
export const chartTooltip = { backgroundColor: "rgb(var(--card))", border: "1px solid rgb(var(--border))", borderRadius: 8, color: "rgb(var(--foreground))" };
