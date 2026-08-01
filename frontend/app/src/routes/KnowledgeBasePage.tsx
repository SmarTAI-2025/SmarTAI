import { FolderPlus, Search, Upload, X } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  useCourseMaterialGroups,
  useCourseMaterials,
  useCourses,
} from "@/api/hooks";
import {
  GroupDialog,
  MaterialDialog,
  UploadDialog,
} from "@/components/knowledge-base/CourseLibraryDialogs";
import { CourseLibraryTable } from "@/components/knowledge-base/CourseLibraryTable";
import { Button } from "@/components/ui/Button";
import { useI18n } from "@/i18n/I18nProvider";
import type {
  CourseMaterial,
  CourseMaterialCategory,
  CourseMaterialGroup,
} from "@/types";

type CategoryFilter = "all" | CourseMaterialCategory;
type DialogState =
  | { kind: "upload" }
  | { kind: "create-group" }
  | { kind: "edit-group"; group: CourseMaterialGroup }
  | { kind: "edit-material"; material: CourseMaterial }
  | { kind: "delete-material"; material: CourseMaterial }
  | null;

const categoryFilters: CategoryFilter[] = ["all", "textbook", "answer", "lecture", "rubric"];

export function KnowledgeBasePage() {
  const { locale } = useI18n();
  const [searchValue, setSearchValue] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<CategoryFilter>("all");
  const [groupId, setGroupId] = useState("");
  const [dialog, setDialog] = useState<DialogState>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(searchValue.trim()), 220);
    return () => window.clearTimeout(timer);
  }, [searchValue]);

  const materialsQuery = useCourseMaterials({
    q: query || undefined,
    group_id: groupId || undefined,
    category: category === "all" ? undefined : category,
    page: 1,
    page_size: 100,
  });
  const visibleGroupsQuery = useCourseMaterialGroups(query);
  const allGroupsQuery = useCourseMaterialGroups();
  const coursesQuery = useCourses();
  const groups = allGroupsQuery.data?.items ?? [];
  const activeGroup = groups.find((group) => group.group_id === groupId) ?? null;
  const showGroups = category === "all" && !groupId;

  const isLoading = materialsQuery.isLoading || (showGroups && visibleGroupsQuery.isLoading);
  const hasError = materialsQuery.isError || (showGroups && visibleGroupsQuery.isError);
  const summary = materialsQuery.data?.summary;
  const visibleGroups = showGroups ? (visibleGroupsQuery.data?.items ?? []) : [];
  const materials = materialsQuery.data?.items ?? [];

  const retry = () => {
    void materialsQuery.refetch();
    if (showGroups) void visibleGroupsQuery.refetch();
  };

  const resultLabel = useMemo(() => {
    if (!query && !groupId && category === "all") return "";
    const count = materials.length + visibleGroups.length;
    return tx(locale, `${count} 项结果`, `${count} ${count === 1 ? "result" : "results"}`);
  }, [category, groupId, locale, materials.length, query, visibleGroups.length]);

  function closeDialog() {
    setDialog(null);
  }

  return (
    <div className="w-full min-w-0 max-w-full overflow-x-clip">
      <div className="flex min-h-10 flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <h1 className="text-[26px] font-semibold leading-10 tracking-[-0.02em]">
          {tx(locale, "课程资料库", "Course Library")}
        </h1>
        <div className="flex flex-wrap gap-3">
          <Button type="button" variant="secondary" className="h-10 min-w-[120px]" onClick={() => setDialog({ kind: "create-group" })}>
            <FolderPlus aria-hidden="true" className="h-4 w-4" />
            {tx(locale, "新建分组", "New group")}
          </Button>
          <Button type="button" className="h-10 min-w-[120px]" onClick={() => setDialog({ kind: "upload" })}>
            <Upload aria-hidden="true" className="h-4 w-4" />
            {tx(locale, "上传资料", "Upload")}
          </Button>
        </div>
      </div>

      <div className="relative mt-8">
        <Search aria-hidden="true" className="pointer-events-none absolute left-5 top-1/2 h-5 w-5 -translate-y-1/2 text-muted-foreground" />
        <input
          type="search"
          value={searchValue}
          onChange={(event) => setSearchValue(event.target.value)}
          placeholder={tx(locale, "搜索课程文件、标签、分组…", "Search course files, labels, groups…")}
          className="h-[54px] w-full rounded-[10px] border bg-card pl-14 pr-12 text-[15px] outline-none transition placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
          aria-label={tx(locale, "搜索课程资料库", "Search course library")}
        />
        {searchValue ? (
          <button type="button" className="absolute right-4 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-md text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring" onClick={() => { setSearchValue(""); setQuery(""); }} aria-label={tx(locale, "清除搜索", "Clear search")}>
            <X aria-hidden="true" className="h-4 w-4" />
          </button>
        ) : null}
      </div>

      <div className="mt-6 flex min-h-9 flex-wrap items-center gap-2 sm:gap-4">
        <div className="flex max-w-full gap-1 overflow-x-auto py-1" role="tablist" aria-label={tx(locale, "资料类型", "Material type")}>
          {categoryFilters.map((filter) => (
            <button
              key={filter}
              type="button"
              role="tab"
              aria-selected={category === filter}
              className={`h-8 shrink-0 rounded-full px-4 text-sm font-medium outline-none transition focus-visible:ring-2 focus-visible:ring-ring ${category === filter ? "bg-blue-100 text-primary dark:bg-blue-950/60" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}
              onClick={() => setCategory(filter)}
            >
              {categoryLabel(filter, locale)}
            </button>
          ))}
        </div>

        {activeGroup ? (
          <button type="button" className="inline-flex h-8 max-w-full items-center gap-2 rounded-full bg-teal-50 px-4 text-sm font-medium text-teal-700 outline-none hover:bg-teal-100 focus-visible:ring-2 focus-visible:ring-ring dark:bg-teal-950/50 dark:text-teal-200" onClick={() => setGroupId("")}>
            <span className="truncate">{activeGroup.name}</span>
            <X aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
          </button>
        ) : null}

        {resultLabel ? <span className="ml-auto text-xs text-muted-foreground">{resultLabel}</span> : null}
      </div>

      <div className="mt-[42px]">
        <CourseLibraryTable
          materials={materials}
          groups={visibleGroups}
          locale={locale}
          isLoading={isLoading}
          hasError={hasError}
          query={query}
          showGroups={showGroups}
          onOpenGroup={(group) => { setGroupId(group.group_id); setCategory("all"); }}
          onEditGroup={(group) => setDialog({ kind: "edit-group", group })}
          onEditMaterial={(material) => setDialog({ kind: "edit-material", material })}
          onDeleteMaterial={(material) => setDialog({ kind: "delete-material", material })}
          onRetry={retry}
        />
      </div>

      <section className="mt-10 flex min-h-[88px] flex-col gap-3 rounded-[10px] border bg-card px-5 py-4 sm:flex-row sm:items-center">
        <h2 className="shrink-0 text-sm font-semibold">{tx(locale, "资料库概况", "Library overview")}</h2>
        <div className="flex flex-wrap gap-2 sm:ml-8">
          <SummaryPill tone="blue">{summary?.materials ?? 0} {tx(locale, "份资料", "materials")}</SummaryPill>
          <SummaryPill tone="teal">{summary?.groups ?? 0} {tx(locale, "个分组", "groups")}</SummaryPill>
          <SummaryPill tone="slate">{summary?.referenced ?? 0} {tx(locale, "份被任务引用", "used by tasks")}</SummaryPill>
        </div>
        <p className="text-xs leading-5 text-muted-foreground sm:ml-auto sm:max-w-[300px] sm:text-right">
          {tx(locale, "试用存储可能在服务重启后清空，请保留本地原文件。", "Trial storage may reset when the service restarts. Keep local originals.")}
        </p>
      </section>

      {dialog?.kind === "create-group" ? (
        <GroupDialog
          courses={coursesQuery.data ?? []}
          onClose={closeDialog}
          onSaved={closeDialog}
          onUseExisting={(selectedGroupId) => { setGroupId(selectedGroupId); setCategory("all"); closeDialog(); }}
        />
      ) : null}
      {dialog?.kind === "edit-group" ? (
        <GroupDialog
          group={dialog.group}
          courses={coursesQuery.data ?? []}
          onClose={closeDialog}
          onSaved={closeDialog}
          onUseExisting={(selectedGroupId) => { setGroupId(selectedGroupId); setCategory("all"); closeDialog(); }}
          onDeleted={() => { if (groupId === dialog.group.group_id) setGroupId(""); closeDialog(); }}
        />
      ) : null}
      {dialog?.kind === "upload" ? (
        <UploadDialog courses={coursesQuery.data ?? []} groups={groups} onClose={closeDialog} onUploaded={closeDialog} />
      ) : null}
      {dialog?.kind === "edit-material" ? (
        <MaterialDialog material={dialog.material} courses={coursesQuery.data ?? []} groups={groups} onClose={closeDialog} onSaved={closeDialog} onDeleted={closeDialog} />
      ) : null}
      {dialog?.kind === "delete-material" ? (
        <MaterialDialog material={dialog.material} courses={coursesQuery.data ?? []} groups={groups} onClose={closeDialog} onSaved={closeDialog} onDeleted={closeDialog} initialConfirmDelete />
      ) : null}
    </div>
  );
}

function tx(locale: "zh-CN" | "en-US", zh: string, en: string) {
  return locale === "zh-CN" ? zh : en;
}

function categoryLabel(category: CategoryFilter, locale: "zh-CN" | "en-US") {
  const labels = {
    all: ["全部", "All"],
    textbook: ["教材", "Textbooks"],
    answer: ["课后答案", "Answer keys"],
    lecture: ["讲义", "Lectures"],
    rubric: ["评分标准", "Rubrics"],
    other: ["其他", "Other"],
  } as const;
  return labels[category][locale === "zh-CN" ? 0 : 1];
}

function SummaryPill({ children, tone }: { children: ReactNode; tone: "blue" | "teal" | "slate" }) {
  const classes = {
    blue: "bg-blue-100 text-blue-700 dark:bg-blue-950/60 dark:text-blue-200",
    teal: "bg-teal-50 text-teal-700 dark:bg-teal-950/50 dark:text-teal-200",
    slate: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  };
  return <span className={`rounded-full px-3 py-1 text-xs font-medium ${classes[tone]}`}>{children}</span>;
}
