import { ChevronLeft, ChevronRight } from "lucide-react";
import { useI18n } from "@/i18n/I18nProvider";
import type { TaskHistoryQuery } from "@/types";

export function HistoryPagination({
  query,
  total,
  onChange,
}: {
  query: TaskHistoryQuery;
  total: number;
  onChange: (patch: Partial<TaskHistoryQuery>, keepPage?: boolean) => void;
}) {
  const { t } = useI18n();
  const totalPages = Math.max(1, Math.ceil(total / query.page_size));
  const page = Math.min(query.page, totalPages);

  return (
    <nav className="mt-3 flex w-full flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground" aria-label={t("historyPagePrefix")}>
      <label className="inline-flex items-center gap-2">
        <span>{t("historyPageSize")}</span>
        <select
          value={query.page_size}
          onChange={(event) => onChange({ page: 1, page_size: Number(event.target.value) as 25 | 50 | 100 }, true)}
          className="h-8 rounded-md border bg-card px-2 text-xs text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
        >
          {[25, 50, 100].map((size) => <option key={size} value={size}>{size}{t("historyPageSizeSuffix")}</option>)}
        </select>
      </label>

      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={page <= 1}
          onClick={() => onChange({ page: page - 1 }, true)}
          className="inline-flex h-8 items-center gap-1 rounded-md border bg-card px-2.5 text-foreground outline-none hover:border-primary/40 hover:text-primary focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40"
        >
          <ChevronLeft aria-hidden="true" className="h-3.5 w-3.5" />{t("historyPagePrevious")}
        </button>
        <span className="min-w-[90px] text-center tabular-nums">
          {t("historyPagePrefix")}{page}{t("historyPageSeparator")}{totalPages}{t("historyPageSuffix")}
        </span>
        <button
          type="button"
          disabled={page >= totalPages}
          onClick={() => onChange({ page: page + 1 }, true)}
          className="inline-flex h-8 items-center gap-1 rounded-md border bg-card px-2.5 text-foreground outline-none hover:border-primary/40 hover:text-primary focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40"
        >
          {t("historyPageNext")}<ChevronRight aria-hidden="true" className="h-3.5 w-3.5" />
        </button>
      </div>
    </nav>
  );
}
