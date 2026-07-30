import { FileText, Folder, MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import type { Locale } from "@/i18n/messages";
import type { CourseMaterial, CourseMaterialGroup } from "@/types";

interface CourseLibraryTableProps {
  materials: CourseMaterial[];
  groups: CourseMaterialGroup[];
  locale: Locale;
  isLoading: boolean;
  hasError: boolean;
  query: string;
  showGroups: boolean;
  onOpenGroup: (group: CourseMaterialGroup) => void;
  onEditGroup: (group: CourseMaterialGroup) => void;
  onEditMaterial: (material: CourseMaterial) => void;
  onDeleteMaterial: (material: CourseMaterial) => void;
  onRetry: () => void;
}

const zh = (locale: Locale) => locale === "zh-CN";

export function CourseLibraryTable({
  materials,
  groups,
  locale,
  isLoading,
  hasError,
  query,
  showGroups,
  onOpenGroup,
  onEditGroup,
  onEditMaterial,
  onDeleteMaterial,
  onRetry,
}: CourseLibraryTableProps) {
  const rows = materials.length + (showGroups ? groups.length : 0);

  return (
    <div className="w-full min-w-0 max-w-full overflow-hidden rounded-[10px] border bg-card">
      <div className="w-full min-w-0 max-w-full overflow-x-auto overscroll-x-contain">
        <table className="w-full min-w-[900px] border-collapse text-left text-sm">
          <thead className="bg-slate-50/80 text-[13px] font-medium text-muted-foreground dark:bg-slate-900/50">
            <tr className="h-[48px] border-b">
              <th className="w-[31%] px-4 font-medium">{zh(locale) ? "文件 / 分组" : "File / group"}</th>
              <th className="w-[10%] px-3 font-medium">{zh(locale) ? "类型" : "Type"}</th>
              <th className="w-[18%] px-3 font-medium">{zh(locale) ? "标签" : "Labels"}</th>
              <th className="w-[14%] px-3 font-medium">{zh(locale) ? "状态" : "Status"}</th>
              <th className="w-[12%] px-3 font-medium">{zh(locale) ? "最近使用" : "Last used"}</th>
              <th className="w-[12%] px-3 font-medium">{zh(locale) ? "任务引用" : "Task use"}</th>
              <th className="w-20 px-2" aria-label={zh(locale) ? "操作" : "Actions"} />
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <MessageRow message={zh(locale) ? "正在读取课程资料…" : "Loading course materials…"} />
            ) : hasError ? (
              <tr>
                <td colSpan={7} className="h-48 px-5 text-center">
                  <p className="text-sm text-muted-foreground">{zh(locale) ? "课程资料暂时无法读取。" : "Course materials could not be loaded."}</p>
                  <button type="button" className="mt-3 rounded-md border px-3 py-2 text-sm font-medium hover:bg-muted" onClick={onRetry}>
                    {zh(locale) ? "重新加载" : "Retry"}
                  </button>
                </td>
              </tr>
            ) : rows === 0 ? (
              <MessageRow message={query ? (zh(locale) ? "没有匹配的文件或分组" : "No files or groups match") : (zh(locale) ? "还没有课程资料，先上传一份文件" : "No course materials yet. Upload a file to begin.")} />
            ) : (
              <>
                {showGroups ? groups.map((group) => (
                  <tr key={group.group_id} className="h-[54px] border-b last:border-b-0 hover:bg-slate-50/60 dark:hover:bg-slate-900/30">
                    <td className="px-4 py-2">
                      <button type="button" className="flex max-w-full items-center gap-2 text-left font-semibold outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring" onClick={() => onOpenGroup(group)}>
                        <Folder aria-hidden="true" className="h-4 w-4 shrink-0 text-primary" />
                        <span className="truncate">{group.name}</span>
                      </button>
                      {group.course_name ? <p className="ml-6 mt-0.5 truncate text-xs text-muted-foreground">{group.course_name}</p> : null}
                    </td>
                    <td className="px-3 text-muted-foreground">{zh(locale) ? "分组" : "Group"}</td>
                    <td className="px-3"><MutedPill>{zh(locale) ? "资料分组" : "Collection"}</MutedPill></td>
                    <td className="px-3"><MutedPill>{group.material_count} {zh(locale) ? "份资料" : group.material_count === 1 ? "file" : "files"}</MutedPill></td>
                    <td className="px-3 text-muted-foreground">—</td>
                    <td className="px-3 text-muted-foreground">—</td>
                    <td className="px-3">
                      <ActionButton label={zh(locale) ? `管理分组 ${group.name}` : `Manage group ${group.name}`} onClick={() => onEditGroup(group)} />
                    </td>
                  </tr>
                )) : null}
                {materials.map((material) => (
                  <tr key={material.material_id} className="h-[54px] border-b last:border-b-0 hover:bg-slate-50/60 dark:hover:bg-slate-900/30">
                    <td className="px-4 py-2">
                      <div className="flex min-w-0 items-center gap-2 font-semibold">
                        <FileText aria-hidden="true" className="h-4 w-4 shrink-0 text-slate-500" />
                        <span className="truncate">{material.filename}</span>
                        {query && material.match_kind ? (
                          <span className={`shrink-0 text-[11px] font-medium ${material.match_kind === "exact" ? "text-primary" : "text-muted-foreground"}`}>
                            {material.match_kind === "exact" ? (zh(locale) ? "完全匹配" : "Exact") : (zh(locale) ? "相近结果" : "Related")}
                          </span>
                        ) : null}
                      </div>
                      <p className="ml-6 mt-0.5 truncate text-xs text-muted-foreground">
                        {[material.group_name, material.course_name, formatBytes(material.size_bytes)].filter(Boolean).join(" · ")}
                      </p>
                    </td>
                    <td className="px-3 text-muted-foreground">{fileType(material.filename)}</td>
                    <td className="px-3">
                      <div className="flex max-w-[220px] gap-1 overflow-hidden">
                        {(material.labels.length ? material.labels : [categoryLabel(material.category, locale)]).slice(0, 2).map((label, index) => (
                          <span key={`${label}-${index}`} className={`truncate rounded-full px-2.5 py-1 text-xs font-medium ${index === 0 ? "bg-blue-50 text-blue-700 dark:bg-blue-950/50 dark:text-blue-200" : "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300"}`}>{label}</span>
                        ))}
                      </div>
                    </td>
                    <td className="px-3"><span className="inline-flex min-w-[84px] justify-center rounded-full bg-emerald-50 px-3 py-1 text-xs font-semibold text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-200">{zh(locale) ? "已解析" : "Parsed"}</span></td>
                    <td className="px-3 text-muted-foreground">{relativeTime(material.last_used_at ?? material.updated_at, locale)}</td>
                    <td className="px-3 text-muted-foreground">{material.task_reference_count ? `${material.task_reference_count} ${zh(locale) ? "个任务" : material.task_reference_count === 1 ? "task" : "tasks"}` : (zh(locale) ? "尚未使用" : "Not used")}</td>
                    <td className="px-2">
                      <div className="flex items-center justify-end gap-1">
                        <IconActionButton
                          label={zh(locale) ? `编辑资料 ${material.filename}` : `Edit material ${material.filename}`}
                          onClick={() => onEditMaterial(material)}
                          icon={<Pencil aria-hidden="true" className="h-4 w-4" />}
                        />
                        <IconActionButton
                          label={zh(locale) ? `删除资料 ${material.filename}` : `Delete material ${material.filename}`}
                          onClick={() => onDeleteMaterial(material)}
                          tone="danger"
                          icon={<Trash2 aria-hidden="true" className="h-4 w-4" />}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function MessageRow({ message }: { message: string }) {
  return <tr><td colSpan={7} className="h-48 px-5 text-center text-sm text-muted-foreground">{message}</td></tr>;
}

function MutedPill({ children }: { children: ReactNode }) {
  return <span className="inline-flex rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300">{children}</span>;
}

function ActionButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button type="button" className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring" aria-label={label} title={label} onClick={onClick}>
      <MoreHorizontal aria-hidden="true" className="h-4 w-4" />
    </button>
  );
}

function IconActionButton({
  label,
  onClick,
  icon,
  tone = "default",
}: {
  label: string;
  onClick: () => void;
  icon: ReactNode;
  tone?: "default" | "danger";
}) {
  return (
    <button
      type="button"
      className={`inline-flex h-8 w-8 items-center justify-center rounded-md outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring ${
        tone === "danger"
          ? "text-muted-foreground hover:bg-red-50 hover:text-danger dark:hover:bg-red-950/40"
          : "text-muted-foreground hover:bg-muted hover:text-foreground"
      }`}
      aria-label={label}
      title={label}
      onClick={onClick}
    >
      {icon}
    </button>
  );
}

function fileType(filename: string) {
  const extension = filename.split(".").pop()?.toUpperCase();
  return extension && extension !== filename.toUpperCase() ? extension : "FILE";
}

function categoryLabel(category: CourseMaterial["category"], locale: Locale) {
  const labels = {
    textbook: ["教材", "Textbook"],
    answer: ["课后答案", "Answer"],
    lecture: ["讲义", "Lecture"],
    rubric: ["评分标准", "Rubric"],
    other: ["其他", "Other"],
  } as const;
  return labels[category][zh(locale) ? 0 : 1];
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function relativeTime(timestamp: number, locale: Locale) {
  const seconds = Math.max(0, Date.now() / 1000 - timestamp);
  if (seconds < 60 * 60) return zh(locale) ? "刚刚" : "Just now";
  if (seconds < 60 * 60 * 24) return zh(locale) ? "今天" : "Today";
  const days = Math.floor(seconds / (60 * 60 * 24));
  if (days === 1) return zh(locale) ? "昨天" : "Yesterday";
  if (days < 7) return zh(locale) ? `${days} 天前` : `${days}d ago`;
  return new Date(timestamp * 1000).toLocaleDateString(locale, { month: "short", day: "numeric" });
}
