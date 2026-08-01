import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  ExternalLink,
  Loader2,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import {
  useAddExpertKey,
  useExperts,
  useProviderCatalog,
  useRemoveExpert,
  useSelectExpert,
  useUpdateExpert,
  useVerifyExpert,
} from "@/api/hooks";
import { LibraryDialog } from "@/components/knowledge-base/LibraryDialog";
import { ProviderIcon } from "@/components/models/ProviderIcon";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import {
  hasFriendlyModelName,
  modelDisplayName,
  modelSecondaryLabel,
} from "@/lib/modelPresentation";
import type {
  AddExpertKeyRequest,
  ExpertConfig,
  ProviderCatalogItem,
  ProviderType,
  UpdateExpertRequest,
} from "@/types";

const providerOptions: Array<{ value: ProviderType; label: string; defaultModel: string }> = [
  { value: "gemini", label: "Google Gemini", defaultModel: "gemini-3-flash-preview" },
  { value: "openai", label: "OpenAI", defaultModel: "gpt-4o" },
  { value: "zhipu", label: "Zhipu AI", defaultModel: "glm-4.5-air" },
  { value: "anthropic", label: "Anthropic", defaultModel: "claude-sonnet-4-20250514" },
];

type EditorTarget =
  | { mode: "add" }
  | { mode: "edit"; expert: ExpertConfig };

type Confirmation =
  | { kind: "verify"; expert: ExpertConfig }
  | { kind: "delete"; expert: ExpertConfig };

interface ExpertFormValue {
  provider: ProviderType;
  apiKey: string;
  model: string;
  baseUrl: string;
  displayName: string;
  maxConcurrent: number;
  rpm: number;
}

export function ExpertsPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const expertsQuery = useExperts();
  const catalogQuery = useProviderCatalog();
  const addExpert = useAddExpertKey();
  const updateExpert = useUpdateExpert();
  const selectExpert = useSelectExpert();
  const verifyExpert = useVerifyExpert();
  const removeExpert = useRemoveExpert();
  const [editor, setEditor] = useState<EditorTarget | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);

  const experts = expertsQuery.data ?? [];
  const enabledCount = experts.filter((expert) => expert.enabled).length;
  const verifiedCount = experts.filter(
    (expert) => expert.verification_status === "verified",
  ).length;
  const attentionCount = experts.filter(
    (expert) =>
      expert.editable !== false &&
      !["verified", "platform_managed"].includes(expert.verification_status ?? "unverified"),
  ).length;
  const returnTo = safeExpertsReturnTo(new URLSearchParams(location.search).get("returnTo"));
  const controlsPending =
    addExpert.isPending ||
    updateExpert.isPending ||
    selectExpert.isPending ||
    verifyExpert.isPending ||
    removeExpert.isPending;

  async function handleSave(value: ExpertFormValue) {
    if (!editor) return;
    try {
      if (editor.mode === "add") {
        const request: AddExpertKeyRequest = {
          provider_type: value.provider,
          api_key: value.apiKey,
          model: value.model,
          base_url: value.baseUrl || null,
          display_name: value.displayName || null,
          max_concurrent: value.maxConcurrent,
          rpm: value.rpm,
        };
        await addExpert.mutateAsync(request);
        toast.success(zh ? "模型配置已添加" : "Model configuration added", {
          description: value.displayName || value.model,
        });
      } else {
        const request: UpdateExpertRequest = {
          api_key: value.apiKey || null,
          model: value.model,
          base_url: value.baseUrl || null,
          display_name: value.displayName || null,
          max_concurrent: value.maxConcurrent,
          rpm: value.rpm,
        };
        await updateExpert.mutateAsync({
          providerId: editor.expert.provider_id,
          request,
        });
        toast.success(zh ? "模型配置已更新" : "Model configuration updated", {
          description: value.displayName || value.model,
        });
      }
      setEditor(null);
    } catch (error) {
      const message = safeExpertError(error, locale);
      toast.error(
        editor.mode === "add"
          ? zh
            ? "添加失败"
            : "Unable to add configuration"
          : zh
            ? "保存失败"
            : "Unable to save configuration",
        { description: message },
      );
      throw new Error(message);
    }
  }

  async function handleToggle(expert: ExpertConfig) {
    if (expert.editable === false) {
      toast.info(zh ? "平台托管模型为只读配置。" : "Platform-managed models are read-only.");
      return;
    }
    try {
      const enabled = !expert.enabled;
      await selectExpert.mutateAsync({ providerId: expert.provider_id, enabled });
      toast.success(
        enabled
          ? zh
            ? "配置已启用"
            : "Configuration enabled"
          : zh
            ? "配置已停用"
            : "Configuration disabled",
        { description: modelDisplayName(expert) },
      );
    } catch (error) {
      toast.error(zh ? "无法更新启用状态" : "Unable to update status", {
        description: safeExpertError(error, locale),
      });
    }
  }

  async function handleConfirm() {
    if (!confirmation) return;
    const target = confirmation;
    setConfirmation(null);
    if (target.kind === "delete") {
      try {
        await removeExpert.mutateAsync(target.expert.provider_id);
        toast.success(zh ? "模型配置已删除" : "Model configuration deleted", {
          description: modelDisplayName(target.expert),
        });
      } catch (error) {
        toast.error(zh ? "删除失败" : "Unable to delete configuration", {
          description: safeExpertError(error, locale),
        });
      }
      return;
    }

    try {
      await verifyExpert.mutateAsync(target.expert.provider_id);
      toast.success(zh ? "验证通过" : "Verification passed", {
        description: modelDisplayName(target.expert),
      });
    } catch (error) {
      toast.error(zh ? "验证未通过" : "Verification did not pass", {
        description: safeExpertError(error, locale),
      });
    }
  }

  return (
    <div className="grid gap-5">
      <header className="flex flex-col gap-4 border-b pb-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-[26px] font-semibold tracking-tight">
            {zh ? "模型与 BYOK" : "Models & BYOK"}
          </h1>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">
            {zh
              ? "管理你自己的模型密钥、启用状态与连通性。密钥只用于模型请求，保存后不会在页面回显。"
              : "Manage your model keys, enabled state, and connectivity. Keys are used only for model requests and are never displayed again."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {returnTo ? (
            <Button
              type="button"
              variant="secondary"
              onClick={() => navigate(returnTo, { state: location.state })}
            >
              <ArrowLeft aria-hidden="true" size={16} />
              {zh ? "返回任务" : "Back to task"}
            </Button>
          ) : null}
          <Button type="button" onClick={() => setEditor({ mode: "add" })}>
            <Plus aria-hidden="true" size={16} />
            {zh ? "添加模型配置" : "Add configuration"}
          </Button>
        </div>
      </header>

      <section
        aria-label={zh ? "模型配置概览" : "Model configuration overview"}
        className="grid grid-cols-2 gap-3 lg:grid-cols-4"
      >
        <SummaryMetric
          label={zh ? "全部配置" : "Configurations"}
          value={expertsQuery.isLoading ? "—" : String(experts.length)}
          tone="primary"
        />
        <SummaryMetric
          label={zh ? "已启用" : "Enabled"}
          value={expertsQuery.isLoading ? "—" : String(enabledCount)}
          tone="accent"
        />
        <SummaryMetric
          label={zh ? "验证通过" : "Verified"}
          value={expertsQuery.isLoading ? "—" : String(verifiedCount)}
          tone="success"
        />
        <SummaryMetric
          label={zh ? "需要检查" : "Needs review"}
          value={expertsQuery.isLoading ? "—" : String(attentionCount)}
          tone={attentionCount > 0 ? "warning" : "neutral"}
        />
      </section>

      <section className="overflow-hidden rounded-[10px] border bg-card">
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3.5 sm:px-5">
          <div>
            <h2 className="text-[15px] font-semibold">
              {zh ? "模型配置" : "Model configurations"}
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {zh
                ? "“已启用”只表示参与任务候选；“验证通过”才表示最近一次最小请求成功。"
                : "Enabled means selectable for tasks; verified means the latest minimal request succeeded."}
            </p>
          </div>
          <button
            type="button"
            className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md px-2 text-xs font-semibold text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            onClick={() => void expertsQuery.refetch()}
            disabled={expertsQuery.isFetching}
          >
            <RefreshCw
              aria-hidden="true"
              size={14}
              className={expertsQuery.isFetching ? "animate-spin" : undefined}
            />
            {zh ? "刷新" : "Refresh"}
          </button>
        </div>

        {expertsQuery.isLoading ? (
          <div className="flex min-h-48 items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 aria-hidden="true" className="animate-spin" size={17} />
            {zh ? "正在读取模型配置…" : "Loading model configurations…"}
          </div>
        ) : null}
        {expertsQuery.isError ? (
          <div className="m-4">
            <InlineError message={safeExpertError(expertsQuery.error, locale)} />
          </div>
        ) : null}
        {!expertsQuery.isLoading && !expertsQuery.isError && experts.length === 0 ? (
          <div className="p-4">
            <EmptyState
              title={zh ? "还没有模型配置" : "No model configurations yet"}
              description={
                zh
                  ? "添加自己的 API key 后，模型才会出现在任务配置中。"
                  : "Add your own API key to make a model available in task setup."
              }
            />
          </div>
        ) : null}
        {experts.length > 0 ? (
          <>
            <div className="hidden overflow-x-auto sm:block">
              <table className="w-full min-w-[980px] table-fixed border-collapse text-left text-sm">
                <colgroup>
                  <col className="w-[33%]" />
                  <col className="w-[12%]" />
                  <col className="w-[14%]" />
                  <col className="w-[13%]" />
                  <col className="w-[28%]" />
                </colgroup>
                <thead className="h-10 bg-slate-50 text-xs text-slate-500 dark:bg-slate-900/45 dark:text-slate-400">
                  <tr>
                    <th className="px-5 align-middle font-medium">
                      {zh ? "服务商与模型" : "Provider & model"}
                    </th>
                    <th className="px-3 text-center align-middle font-medium">
                      {zh ? "配置状态" : "Configuration"}
                    </th>
                    <th className="px-3 text-center align-middle font-medium">
                      {zh ? "连通性" : "Connectivity"}
                    </th>
                    <th className="px-3 text-center align-middle font-medium">
                      {zh ? "调用限制" : "Limits"}
                    </th>
                    <th className="px-5 text-right align-middle font-medium">
                      {zh ? "操作" : "Actions"}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {experts.map((expert) => (
                    <ExpertTableRow
                      key={expert.provider_id}
                      expert={expert}
                      locale={locale}
                      disabled={controlsPending}
                      onEdit={() => setEditor({ mode: "edit", expert })}
                      onToggle={() => void handleToggle(expert)}
                      onVerify={() => setConfirmation({ kind: "verify", expert })}
                      onDelete={() => setConfirmation({ kind: "delete", expert })}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="divide-y sm:hidden">
              {experts.map((expert) => (
                <ExpertMobileRow
                  key={expert.provider_id}
                  expert={expert}
                  locale={locale}
                  disabled={controlsPending}
                  onEdit={() => setEditor({ mode: "edit", expert })}
                  onToggle={() => void handleToggle(expert)}
                  onVerify={() => setConfirmation({ kind: "verify", expert })}
                  onDelete={() => setConfirmation({ kind: "delete", expert })}
                />
              ))}
            </div>
          </>
        ) : null}
      </section>

      <OfficialProviderLinks
        catalog={catalogQuery.data ?? []}
        loading={catalogQuery.isLoading}
        error={catalogQuery.isError}
        locale={locale}
        onRetry={() => void catalogQuery.refetch()}
      />

      {editor ? (
        <ExpertEditorDialog
          key={editor.mode === "add" ? "add" : editor.expert.provider_id}
          target={editor}
          locale={locale}
          pending={addExpert.isPending || updateExpert.isPending}
          onClose={() => setEditor(null)}
          onSave={handleSave}
        />
      ) : null}

      {confirmation ? (
        <ConfirmationDialog
          confirmation={confirmation}
          locale={locale}
          pending={verifyExpert.isPending || removeExpert.isPending}
          onClose={() => setConfirmation(null)}
          onConfirm={() => void handleConfirm()}
        />
      ) : null}
    </div>
  );
}

function SummaryMetric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "primary" | "accent" | "success" | "warning" | "neutral";
}) {
  const toneClass = {
    primary: "text-blue-600",
    accent: "text-teal-600",
    success: "text-emerald-600",
    warning: "text-amber-600",
    neutral: "text-slate-600 dark:text-slate-300",
  }[tone];
  return (
    <div className="min-h-[92px] rounded-[10px] border bg-card px-4 py-3.5 sm:px-5">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className={cn("mt-1 text-[25px] font-semibold leading-none", toneClass)}>{value}</p>
    </div>
  );
}

function ExpertTableRow({
  expert,
  locale,
  disabled,
  onEdit,
  onToggle,
  onVerify,
  onDelete,
}: ExpertRowProps) {
  const zh = locale === "zh-CN";
  return (
    <tr className="h-[76px] border-t first:border-t-0">
      <td className="max-w-[330px] px-5 align-middle">
        <div className="flex min-w-0 items-center gap-3">
          <ProviderIcon providerType={expert.provider_type} />
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="truncate font-semibold">{modelDisplayName(expert)}</span>
              {expert.is_shared ? (
                <span className="shrink-0 rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-semibold text-blue-700 dark:bg-blue-950/50 dark:text-blue-200">
                  {zh ? "平台" : "Platform"}
                </span>
              ) : null}
            </div>
            <p className="mt-1 truncate text-xs text-muted-foreground" title={modelSecondaryLabel(expert)}>
              {modelSecondaryLabel(expert)}
            </p>
          </div>
        </div>
      </td>
      <td className="px-3 text-center align-middle">
        <EnabledBadge enabled={expert.enabled} locale={locale} />
      </td>
      <td className="px-3 text-center align-middle">
        <VerificationBadge expert={expert} locale={locale} />
      </td>
      <td className="px-3 text-center align-middle text-xs text-muted-foreground">
        <span className="block">RPM {expert.rpm > 0 ? expert.rpm : "—"}</span>
        <span className="mt-1 block">
          {zh ? "并发" : "Concurrency"} {expert.max_concurrent}
        </span>
      </td>
      <td className="px-5 align-middle">
        <ExpertActions
          expert={expert}
          locale={locale}
          disabled={disabled}
          onEdit={onEdit}
          onToggle={onToggle}
          onVerify={onVerify}
          onDelete={onDelete}
        />
      </td>
    </tr>
  );
}

interface ExpertRowProps {
  expert: ExpertConfig;
  locale: "zh-CN" | "en-US";
  disabled: boolean;
  onEdit: () => void;
  onToggle: () => void;
  onVerify: () => void;
  onDelete: () => void;
}

function ExpertMobileRow(props: ExpertRowProps) {
  const { expert, locale } = props;
  const zh = locale === "zh-CN";
  return (
    <article className="grid gap-3 px-4 py-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <ProviderIcon providerType={expert.provider_type} />
          <div className="min-w-0">
            <p className="truncate font-semibold">{modelDisplayName(expert)}</p>
            <p className="mt-1 truncate text-xs text-muted-foreground">
              {modelSecondaryLabel(expert)}
            </p>
          </div>
        </div>
        <EnabledBadge enabled={expert.enabled} locale={locale} />
      </div>
      <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
        <VerificationBadge expert={expert} locale={locale} />
        <span>
          RPM {expert.rpm > 0 ? expert.rpm : "—"} · {zh ? "并发" : "Concurrency"}{" "}
          {expert.max_concurrent}
        </span>
      </div>
      <ExpertActions {...props} />
    </article>
  );
}

function ExpertActions({
  expert,
  locale,
  disabled,
  onEdit,
  onToggle,
  onVerify,
  onDelete,
}: ExpertRowProps) {
  const zh = locale === "zh-CN";
  if (expert.editable === false) {
    return (
      <div className="flex justify-end text-xs text-muted-foreground">
        {zh ? "平台托管，只读" : "Platform managed, read-only"}
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center justify-end gap-1">
      <RowAction label={zh ? "编辑" : "Edit"} onClick={onEdit} disabled={disabled}>
        <Pencil aria-hidden="true" size={14} />
      </RowAction>
      <RowAction label={zh ? "验证" : "Verify"} onClick={onVerify} disabled={disabled}>
        <ShieldCheck aria-hidden="true" size={14} />
      </RowAction>
      <RowAction
        label={expert.enabled ? (zh ? "停用" : "Disable") : zh ? "启用" : "Enable"}
        onClick={onToggle}
        disabled={disabled}
      >
        <Power aria-hidden="true" size={14} />
      </RowAction>
      <RowAction
        label={zh ? "删除" : "Delete"}
        onClick={onDelete}
        disabled={disabled}
        danger
      >
        <Trash2 aria-hidden="true" size={14} />
      </RowAction>
    </div>
  );
}

function RowAction({
  label,
  children,
  danger = false,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-xs font-semibold outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40",
        danger ? "text-danger hover:bg-danger/10" : "text-muted-foreground hover:text-foreground",
      )}
      {...props}
    >
      {children}
      {label}
    </button>
  );
}

function EnabledBadge({
  enabled,
  locale,
}: {
  enabled: boolean;
  locale: "zh-CN" | "en-US";
}) {
  return (
    <span
      className={cn(
        "inline-flex min-w-[72px] justify-center rounded-full px-3 py-1 text-xs font-semibold",
        enabled
          ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-200"
          : "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-300",
      )}
    >
      {enabled ? (locale === "zh-CN" ? "已启用" : "Enabled") : locale === "zh-CN" ? "已停用" : "Disabled"}
    </span>
  );
}

function VerificationBadge({
  expert,
  locale,
}: {
  expert: ExpertConfig;
  locale: "zh-CN" | "en-US";
}) {
  const zh = locale === "zh-CN";
  const status = expert.verification_status ?? (expert.is_shared ? "platform_managed" : "unverified");
  const statuses = {
    verified: {
      label: zh ? "验证通过" : "Verified",
      className: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-200",
    },
    failed: {
      label: zh ? "验证失败" : "Failed",
      className: "bg-rose-50 text-rose-700 dark:bg-rose-950/50 dark:text-rose-200",
    },
    platform_managed: {
      label: zh ? "平台托管" : "Platform managed",
      className: "bg-blue-50 text-blue-700 dark:bg-blue-950/50 dark:text-blue-200",
    },
    unverified: {
      label: zh ? "尚未验证" : "Not verified",
      className: "bg-amber-50 text-amber-700 dark:bg-amber-950/50 dark:text-amber-200",
    },
  };
  const meta = statuses[status] ?? statuses.unverified;
  return (
    <div className="flex min-h-[45px] flex-col items-center justify-center">
      <span className={cn("inline-flex rounded-full px-3 py-1 text-xs font-semibold", meta.className)}>
        {meta.label}
      </span>
      {expert.last_checked_at ? (
        <p className="mt-1 text-[10px] text-muted-foreground">
          {formatCheckedAt(expert.last_checked_at, locale)}
        </p>
      ) : null}
    </div>
  );
}

function OfficialProviderLinks({
  catalog,
  loading,
  error,
  locale,
  onRetry,
}: {
  catalog: ProviderCatalogItem[];
  loading: boolean;
  error: boolean;
  locale: "zh-CN" | "en-US";
  onRetry: () => void;
}) {
  const zh = locale === "zh-CN";
  return (
    <section className="rounded-[10px] border bg-card px-4 py-4 sm:px-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold">{zh ? "服务商官方入口" : "Official provider links"}</h2>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            {zh
              ? "用量与余额以服务商官网为准；SmarTAI 不读取或估算账户余额。"
              : "Usage and balance come from each provider. SmarTAI does not read or estimate account balances."}
          </p>
        </div>
        {loading ? (
          <Loader2 aria-label={zh ? "正在读取官方入口" : "Loading official links"} className="animate-spin text-muted-foreground" size={16} />
        ) : null}
        {error ? (
          <button type="button" className="text-xs font-semibold text-primary" onClick={onRetry}>
            {zh ? "重新加载" : "Retry"}
          </button>
        ) : null}
      </div>
      {catalog.length > 0 ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          {catalog.map((provider) => (
            <div
              key={provider.provider_type}
              className="flex min-w-0 items-center justify-between gap-3 rounded-lg bg-slate-50 px-3 py-2.5 dark:bg-slate-900/45"
            >
              <span className="truncate text-xs font-semibold">{provider.display_name}</span>
              <div className="flex shrink-0 items-center gap-2 text-[11px] font-semibold text-primary">
                <OfficialLink href={provider.console_url} label={zh ? "密钥" : "Keys"} />
                <OfficialLink href={provider.usage_url} label={zh ? "用量" : "Usage"} />
                <OfficialLink href={provider.docs_url} label={zh ? "文档" : "Docs"} />
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function OfficialLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-0.5 rounded outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
    >
      {label}
      <ExternalLink aria-hidden="true" size={10} />
    </a>
  );
}

function ExpertEditorDialog({
  target,
  locale,
  pending,
  onClose,
  onSave,
}: {
  target: EditorTarget;
  locale: "zh-CN" | "en-US";
  pending: boolean;
  onClose: () => void;
  onSave: (value: ExpertFormValue) => Promise<void>;
}) {
  const zh = locale === "zh-CN";
  const initialProvider =
    target.mode === "edit" && isProviderType(target.expert.provider_type)
      ? target.expert.provider_type
      : "gemini";
  const [provider, setProvider] = useState<ProviderType>(initialProvider);
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState(
    target.mode === "edit" ? target.expert.model : providerDefault(initialProvider),
  );
  const [baseUrl, setBaseUrl] = useState(target.mode === "edit" ? target.expert.base_url ?? "" : "");
  const [displayName, setDisplayName] = useState(
    target.mode === "edit" && hasFriendlyModelName(target.expert)
      ? target.expert.display_name!.trim()
      : "",
  );
  const [maxConcurrent, setMaxConcurrent] = useState(
    String(target.mode === "edit" ? target.expert.max_concurrent : 5),
  );
  const [rpm, setRpm] = useState(String(target.mode === "edit" ? target.expert.rpm : 0));
  const [formError, setFormError] = useState<string | null>(null);
  const allowsBaseUrl = provider === "openai" || provider === "zhipu";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const nextModel = model.trim();
    const nextKey = apiKey.trim();
    const nextConcurrency = Number(maxConcurrent);
    const nextRpm = Number(rpm);
    if (!nextModel || (target.mode === "add" && !nextKey)) {
      setFormError(
        zh ? "请填写模型名称和 API key。" : "Enter a model name and API key.",
      );
      return;
    }
    if (!Number.isInteger(nextConcurrency) || nextConcurrency < 1 || nextConcurrency > 10) {
      setFormError(zh ? "并发上限需要是 1–10 的整数。" : "Concurrency must be an integer from 1 to 10.");
      return;
    }
    if (!Number.isInteger(nextRpm) || nextRpm < 0 || nextRpm > 10_000) {
      setFormError(zh ? "RPM 需要是 0–10000 的整数。" : "RPM must be an integer from 0 to 10000.");
      return;
    }
    try {
      await onSave({
        provider,
        apiKey: nextKey,
        model: nextModel,
        baseUrl: allowsBaseUrl ? baseUrl.trim() : "",
        displayName: displayName.trim(),
        maxConcurrent: nextConcurrency,
        rpm: nextRpm,
      });
    } catch (error) {
      setApiKey("");
      setFormError(error instanceof Error ? error.message : zh ? "保存失败。" : "Unable to save.");
    }
  }

  return (
    <LibraryDialog
      title={
        target.mode === "add"
          ? zh
            ? "添加模型配置"
            : "Add model configuration"
          : zh
            ? "编辑模型配置"
            : "Edit model configuration"
      }
      description={
        target.mode === "add"
          ? zh
            ? "保存配置不会自动发起模型调用。"
            : "Saving does not automatically call the model."
          : zh
            ? "API key 留空将保留现有密钥；保存后请重新验证。"
            : "Leave the API key blank to keep it. Verify again after saving."
      }
      closeLabel={zh ? "关闭" : "Close"}
      onClose={onClose}
      footer={
        <>
          <Button type="button" variant="secondary" disabled={pending} onClick={onClose}>
            {zh ? "取消" : "Cancel"}
          </Button>
          <Button type="submit" form="expert-editor-form" disabled={pending}>
            {pending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : <CheckCircle2 aria-hidden="true" size={16} />}
            {pending ? (zh ? "保存中…" : "Saving…") : zh ? "保存配置" : "Save configuration"}
          </Button>
        </>
      }
    >
      <form id="expert-editor-form" className="grid gap-4" onSubmit={handleSubmit}>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={zh ? "服务商" : "Provider"}>
            <select
              value={provider}
              disabled={pending || target.mode === "edit"}
              className="h-9 rounded-md border bg-background px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/15 disabled:opacity-60"
              onChange={(event) => {
                const next = event.target.value as ProviderType;
                setProvider(next);
                setModel(providerDefault(next));
                setBaseUrl("");
              }}
            >
              {providerOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label={zh ? "配置名称（可选）" : "Configuration name (optional)"}
            hint={zh ? "留空时显示模型名称。" : "The model name is shown when this is left blank."}
          >
            <Input
              value={displayName}
              disabled={pending}
              maxLength={120}
              placeholder={zh ? "例如：高数批改" : "Example: Calculus grading"}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </Field>
        </div>
        <Field label={zh ? "模型名称" : "Model name"}>
          <Input
            value={model}
            disabled={pending}
            maxLength={200}
            onChange={(event) => setModel(event.target.value)}
          />
        </Field>
        <Field
          label={target.mode === "add" ? "API key" : zh ? "替换 API key（可选）" : "Replace API key (optional)"}
          hint={
            target.mode === "add"
              ? zh
                ? "提交后立即清空，页面和接口都不会回显。"
                : "Cleared after submission and never returned by the UI or API."
              : zh
                ? "留空保留现有密钥。"
                : "Leave blank to keep the current key."
          }
        >
          <Input
            value={apiKey}
            disabled={pending}
            type="password"
            autoComplete="new-password"
            maxLength={512}
            placeholder={target.mode === "add" ? "••••••••" : zh ? "留空不修改" : "Leave blank to keep"}
            onChange={(event) => setApiKey(event.target.value)}
          />
        </Field>
        {allowsBaseUrl ? (
          <Field
            label={zh ? "官方 API Base URL（可选）" : "Official API base URL (optional)"}
            hint={
              zh
                ? "仅接受该服务商的官方 HTTPS 地址；通常留空即可。"
                : "Only the provider's official HTTPS URL is accepted; usually leave this blank."
            }
          >
            <Input
              value={baseUrl}
              disabled={pending}
              type="url"
              placeholder={provider === "openai" ? "https://api.openai.com/v1" : "https://open.bigmodel.cn/api/paas/v4"}
              onChange={(event) => setBaseUrl(event.target.value)}
            />
          </Field>
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={zh ? "并发上限" : "Max concurrency"} hint="1–10">
            <Input
              value={maxConcurrent}
              disabled={pending}
              type="number"
              inputMode="numeric"
              min={1}
              max={10}
              onChange={(event) => setMaxConcurrent(event.target.value)}
            />
          </Field>
          <Field
            label="RPM"
            hint={zh ? "0 表示不额外限速" : "0 means no additional limit"}
          >
            <Input
              value={rpm}
              disabled={pending}
              type="number"
              inputMode="numeric"
              min={0}
              max={10_000}
              onChange={(event) => setRpm(event.target.value)}
            />
          </Field>
        </div>
        {formError ? <InlineError message={formError} /> : null}
      </form>
    </LibraryDialog>
  );
}

function ConfirmationDialog({
  confirmation,
  locale,
  pending,
  onClose,
  onConfirm,
}: {
  confirmation: Confirmation;
  locale: "zh-CN" | "en-US";
  pending: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const zh = locale === "zh-CN";
  const verify = confirmation.kind === "verify";
  return (
    <LibraryDialog
      title={verify ? (zh ? "验证模型连通性" : "Verify model connectivity") : zh ? "删除模型配置" : "Delete model configuration"}
      description={modelDisplayName(confirmation.expert)}
      closeLabel={zh ? "关闭" : "Close"}
      onClose={onClose}
      footer={
        <>
          <Button type="button" variant="secondary" disabled={pending} onClick={onClose}>
            {zh ? "取消" : "Cancel"}
          </Button>
          <Button type="button" variant={verify ? "primary" : "danger"} disabled={pending} onClick={onConfirm}>
            {pending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : verify ? <ShieldCheck aria-hidden="true" size={16} /> : <Trash2 aria-hidden="true" size={16} />}
            {pending
              ? zh
                ? "处理中…"
                : "Working…"
              : verify
                ? zh
                  ? "发起一次验证"
                  : "Run one verification"
                : zh
                  ? "确认删除"
                  : "Delete"}
          </Button>
        </>
      }
    >
      {verify ? (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900 dark:border-amber-900 dark:bg-amber-950/35 dark:text-amber-100">
          {zh
            ? "系统将向该服务商发送一次最小文本请求。这可能消耗极少量额度；不会自动重试，也不会读取账户余额或用量。"
            : "SmarTAI will send one minimal text request. It may consume a tiny amount of quota, will not retry automatically, and will not read account balance or usage."}
        </div>
      ) : (
        <div className="flex items-start gap-3 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm leading-6 text-rose-900 dark:border-rose-900 dark:bg-rose-950/35 dark:text-rose-100">
          <AlertTriangle aria-hidden="true" className="mt-1 shrink-0" size={17} />
          <p>
            {zh
              ? "删除后这把 BYOK key 会从当前内存注册表移除，使用该配置的后续任务将无法继续调用。"
              : "The BYOK key will be removed from the current in-memory registry, and future task calls using it will no longer work."}
          </p>
        </div>
      )}
    </LibraryDialog>
  );
}

function InlineError({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
      <AlertTriangle aria-hidden="true" className="mt-0.5 shrink-0" size={16} />
      <p className="min-w-0 break-words">{message}</p>
    </div>
  );
}

function providerDefault(providerType: ProviderType) {
  return providerOptions.find((option) => option.value === providerType)?.defaultModel ?? "";
}

function isProviderType(value: string): value is ProviderType {
  return providerOptions.some((option) => option.value === value);
}

function formatCheckedAt(value: string, locale: "zh-CN" | "en-US") {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return locale === "zh-CN" ? "最近已检查" : "Checked recently";
  return new Intl.DateTimeFormat(locale, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
}

function safeExpertsReturnTo(value: string | null): string | null {
  if (!value || value.includes("\\") || value.includes("#")) return null;
  try {
    const parsed = new URL(value, window.location.origin);
    if (parsed.origin !== window.location.origin || !parsed.pathname.startsWith("/tasks/")) {
      return null;
    }
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return null;
  }
}

function safeExpertError(error: unknown, locale: "zh-CN" | "en-US") {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(normalized);
  const zh = locale === "zh-CN";
  const messages: Record<string, [string, string]> = {
    expert_verification_auth_failed: ["密钥无效或没有调用权限。", "The key is invalid or lacks permission."],
    expert_verification_model_not_found: ["模型名称不存在或当前账户无权使用。", "The model was not found or is unavailable to this account."],
    expert_verification_rate_limited: ["服务商限制了本次请求，请稍后再试。", "The provider rate-limited this request. Try again later."],
    expert_verification_timeout: ["服务商响应超时，配置仍然保留。", "The provider timed out. Your configuration is unchanged."],
    expert_verification_connection_failed: ["无法连接服务商，请检查网络后重试。", "Unable to reach the provider. Check the network and retry."],
    expert_verification_provider_error: ["服务商拒绝了验证请求。", "The provider rejected the verification request."],
    expert_verification_stale: ["配置已在验证期间改变，请重新验证。", "The configuration changed during verification. Verify again."],
    provider_base_url_not_allowed: ["仅允许该服务商的官方 HTTPS API 地址。", "Only the provider's official HTTPS API URL is allowed."],
    expert_provider_conflict: ["相同服务商与模型的配置已经存在。", "A configuration for this provider and model already exists."],
  };
  if (code && messages[code]) return zh ? messages[code][0] : messages[code][1];
  return normalized.message || (zh ? "请求失败，请稍后重试。" : "Request failed. Try again later.");
}
