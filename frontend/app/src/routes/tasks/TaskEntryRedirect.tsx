import { AlertCircle, LoaderCircle } from "lucide-react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useTask } from "@/api/hooks";
import { useI18n } from "@/i18n/I18nProvider";
import { getTaskDestination } from "@/lib/taskFlow";

/** Canonical, status-aware entry for task links and legacy task bookmarks. */
export function TaskEntryRedirect() {
  const { taskId } = useParams();
  const { t } = useI18n();
  const taskQuery = useTask(taskId);

  if (taskQuery.isSuccess) {
    return <Navigate replace to={getTaskDestination(taskQuery.data)} />;
  }

  if (!taskId) {
    return (
      <TaskEntryState
        title={t("taskEntryMissingTitle")}
        description={t("taskEntryMissingDescription")}
        backLabel={t("taskEntryBackToHistory")}
      />
    );
  }

  if (taskQuery.isError) {
    return (
      <TaskEntryState
        title={t("taskEntryLoadErrorTitle")}
        description={t("taskEntryLoadErrorDescription")}
        backLabel={t("taskEntryBackToHistory")}
        retryLabel={t("retry")}
        retrying={taskQuery.isFetching}
        onRetry={() => void taskQuery.refetch()}
      />
    );
  }

  return (
    <div className="flex min-h-[420px] w-full items-center justify-center" role="status" aria-live="polite">
      <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
        <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin text-primary" />
        {t("taskEntryLoading")}
      </div>
    </div>
  );
}

interface TaskEntryStateProps {
  title: string;
  description: string;
  backLabel: string;
  retryLabel?: string;
  retrying?: boolean;
  onRetry?: () => void;
}

function TaskEntryState({
  title,
  description,
  backLabel,
  retryLabel,
  retrying = false,
  onRetry,
}: TaskEntryStateProps) {
  return (
    <div className="flex min-h-[420px] w-full items-center justify-center px-5">
      <section className="w-full max-w-[560px] rounded-[10px] border bg-card px-6 py-8 text-center sm:px-10">
        <AlertCircle aria-hidden="true" className="mx-auto h-7 w-7 text-amber-500" />
        <h1 className="mt-4 text-xl font-bold text-foreground">{title}</h1>
        <p className="mx-auto mt-2 max-w-[440px] text-sm leading-6 text-muted-foreground">{description}</p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          {onRetry && retryLabel ? (
            <button
              type="button"
              disabled={retrying}
              onClick={onRetry}
              className="inline-flex h-9 items-center justify-center rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground outline-none hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
            >
              {retrying ? <LoaderCircle aria-hidden="true" className="mr-2 h-4 w-4 animate-spin" /> : null}
              {retryLabel}
            </button>
          ) : null}
          <Link
            to="/history"
            className="inline-flex h-9 items-center justify-center rounded-[7px] border bg-card px-4 text-sm font-semibold text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
          >
            {backLabel}
          </Link>
        </div>
      </section>
    </div>
  );
}
