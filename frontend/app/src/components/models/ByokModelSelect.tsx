import { useState } from "react";
import { getAPIErrorCode } from "@/api/client";
import { useBindByokModel } from "@/api/hooks/byok";
import { modelDisplayName } from "@/lib/modelPresentation";
import type { ByokConnection, ExpertConfig, ReasoningEffort } from "@/types";

type Locale = "zh-CN" | "en-US";

const selectStyle = "h-10 w-full min-w-0 rounded-md border bg-background px-2 text-sm focus-visible:ring-2 focus-visible:ring-primary/30 disabled:opacity-60";

export function byokConnectionLabel(connection: ByokConnection) {
  return connection.display_name || connection.endpoint_identity;
}

export function byokError(error: unknown, locale: Locale) {
  const zh = locale === "zh-CN";
  switch (getAPIErrorCode(error)) {
    case "byok_model_not_listed":
      return zh ? "该模型已不在目录中，请刷新模型后重选。" : "This model is no longer in the catalog. Refresh and choose again.";
    case "byok_effort_unsupported":
      return zh ? "该模型不支持所选档位，请重新选择。" : "This model does not support that effort. Choose again.";
    case "byok_model_disabled":
      return zh ? "该模型配置已停用，请选择其他模型。" : "This model configuration is disabled. Choose another model.";
    case "expert_default_replacement_required":
      return zh ? "请先选用另一个默认模型，再移除此连接。" : "Choose another default model before removing this connection.";
    case "custom_provider_endpoints_disabled":
      return zh ? "当前环境未启用自定义服务。" : "Custom endpoints are disabled in this environment.";
    default:
      return zh ? "操作失败，请检查连接后重试。" : "Could not complete this action. Check the connection and retry.";
  }
}

export function byokCatalogLabel(status: ByokConnection["catalog_status"], locale: Locale) {
  const zh = locale === "zh-CN";
  switch (status) {
    case "ready":
      return zh ? "目录已获取" : "Catalog loaded";
    case "partial":
      return zh ? "目录不完整，可手填模型 ID" : "Partial catalog. You can enter a model ID.";
    case "denied":
      return zh ? "目录访问被拒绝，请检查 Key 或手填模型 ID" : "Catalog access denied. Check the key or enter a model ID.";
    case "timeout":
      return zh ? "目录查询超时，可手填模型 ID" : "Catalog request timed out. You can enter a model ID.";
    case "rate_limited":
      return zh ? "目录查询暂受限，可手填模型 ID" : "Catalog request limited. You can enter a model ID.";
    default:
      return zh ? "目录不可用，可手填模型 ID" : "Catalog unavailable. You can enter a model ID.";
  }
}

const effortLabels: Record<ReasoningEffort, [string, string]> = {
  none: ["关闭", "Off"],
  minimal: ["极低", "Minimal"],
  low: ["低", "Low"],
  medium: ["中", "Medium"],
  high: ["高", "High"],
  xhigh: ["很高", "Extra high"],
  max: ["最高", "Maximum"],
};

interface Props {
  id: string;
  locale: Locale;
  connections: ByokConnection[];
  experts?: ExpertConfig[];
  value?: string;
  disabled?: boolean;
  allowEmpty?: boolean;
  placeholder?: string;
  actionLabel?: string;
  initialConnection?: string;
  onChange: (id: string) => void | Promise<void>;
}

/** Selection is committed only after the concrete route is available to task requests. */
export function ByokModelSelect(props: Props) {
  const selected = props.experts?.find((expert) => expert.provider_id === props.value);
  const appliedKey = selected?.provider_id ?? props.value ?? "";
  return <ByokModelSelectForm key={`${appliedKey}:${Boolean(selected)}`} {...props} selected={selected} />;
}

function ByokModelSelectForm({
  id, locale, connections, experts = [], selected, disabled, allowEmpty,
  placeholder, actionLabel, initialConnection, onChange,
}: Props & { selected?: ExpertConfig }) {
  const zh = locale === "zh-CN";
  const bind = useBindByokModel();
  const [selection, setSelection] = useState(selected?.byok_connection_id
    ? `connection:${selected.byok_connection_id}`
    : selected ? `model:${selected.provider_id}` : initialConnection ? `connection:${initialConnection}` : "");
  const [model, setModel] = useState(selected?.byok_connection_id ? selected.model : "");
  const [effort, setEffort] = useState<ReasoningEffort | "">(selected?.reasoning_effort ?? "");
  const [manual, setManual] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fixedModels = experts.filter((expert) => !expert.byok_connection_id);
  const connection = connections.find((row) => `connection:${row.id}` === selection);
  const safeId = model.trim();
  const options = connection?.models.find((entry) => entry.id === safeId)?.reasoning_efforts ?? [];
  const useManualId = Boolean(connection && connection.catalog_status !== "ready" && (manual || !connection.models.length));
  const canChoose = Boolean(connection && connection.enabled !== false && safeId && (useManualId || connection.models.some((entry) => entry.id === safeId)));
  const chosenEffort = effort && options.includes(effort) ? effort : null;
  const alreadyChosen = selected?.byok_connection_id === connection?.id
    && selected?.model === safeId && (selected?.reasoning_effort ?? null) === chosenEffort;

  async function commit(providerId?: string) {
    setSaving(true);
    setError(null);
    try {
      if (connection && providerId === undefined) {
        const result = await bind.mutateAsync({ connectionId: connection.id, model: safeId, effort: chosenEffort });
        await onChange(result.provider_id);
      } else {
        await onChange(providerId ?? "");
      }
    } catch (cause) {
      setError(byokError(cause, locale));
    } finally {
      setSaving(false);
    }
  }

  return <div className="w-full min-w-0">
    <fieldset disabled={disabled || saving} className="grid min-w-0 grid-cols-2 gap-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)_minmax(0,1fr)]">
      <label className={`grid min-w-0 gap-1 text-xs ${connection ? "col-span-2 lg:col-span-1" : "col-span-full"}`}>
        {fixedModels.length ? (zh ? "连接或模型" : "Connection or model") : (zh ? "BYOK 连接" : "BYOK connection")}
        <select id={id} value={selection} className={selectStyle} onChange={(event) => {
          const next = event.target.value;
          setSelection(next);
          setModel("");
          setEffort("");
          setManual(false);
          setError(null);
          if (!next || next.startsWith("model:")) void commit(next.replace(/^model:/, ""));
        }}>
          <option value="" disabled={!allowEmpty}>{placeholder ?? (zh ? "选择连接或模型" : "Choose a connection or model")}</option>
          {connections.map((row) => <option key={row.id} value={`connection:${row.id}`} disabled={row.enabled === false}>
            {byokConnectionLabel(row)}{connections.some(other => other.id !== row.id && byokConnectionLabel(other) === byokConnectionLabel(row)) ? ` · ${row.id.slice(-6)}` : ""}
          </option>)}
          {fixedModels.map((expert) =>
            <option key={expert.provider_id} value={`model:${expert.provider_id}`}>{modelDisplayName(expert)}</option>)}
        </select>
      </label>
      {connection ? <>
        <div className="min-w-0">
          <label className="grid min-w-0 gap-1 text-xs">
            {zh ? "模型 ID" : "Model ID"}
            {useManualId ? (
              <input className={selectStyle} value={model} maxLength={200} onChange={(event) => {
                setModel(event.target.value);
                setEffort("");
              }} />
            ) : (
              <select value={model} className={selectStyle} onChange={(event) => {
                setModel(event.target.value);
                setEffort("");
              }}>
                <option value="">{zh ? "选择模型" : "Choose a model"}</option>
                {safeId && !connection.models.some((entry) => entry.id === safeId) ? <option value={safeId} disabled>{safeId}</option> : null}
                {connection.models.map((entry) => <option key={entry.id} value={entry.id}>{entry.id}</option>)}
              </select>
            )}
          </label>
          {connection.catalog_status !== "ready" && connection.models.length > 0 ? (
            <button type="button" className="mt-1 text-xs text-primary underline" onClick={() => {
              setManual(!manual);
              setModel("");
              setEffort("");
            }}>{useManualId ? (zh ? "从目录选择" : "Choose from catalog") : (zh ? "手填 ID" : "Enter ID")}</button>
          ) : null}
        </div>
        <label className="grid min-w-0 content-start gap-1 text-xs">
          {zh ? "推理强度" : "Reasoning effort"}
          <select className={selectStyle} disabled={!canChoose || options.length === 0} value={chosenEffort ?? ""} onChange={(event) => setEffort(event.target.value as ReasoningEffort | "")}>
            <option value="">{zh ? "服务商默认" : "Provider default"}</option>
            {options.map((value) => <option key={value} value={value}>{effortLabels[value][zh ? 0 : 1]}</option>)}
          </select>
        </label>
        <div className="col-span-full flex flex-wrap items-center gap-2">
          {!alreadyChosen ? <button type="button" disabled={!canChoose} className="rounded-md border px-3 py-1.5 text-xs font-semibold text-primary disabled:opacity-50" onClick={() => void commit()}>
            {saving ? (zh ? "应用中…" : "Applying…") : actionLabel ?? (zh ? "应用选择" : "Apply selection")}
          </button> : null}
          {selected && !alreadyChosen ? <span className="min-w-0 break-all text-xs text-muted-foreground">
            {zh ? "当前使用" : "Currently using"} {modelDisplayName(selected)}
          </span> : null}
        </div>
      </> : null}
    </fieldset>
    {error ? <p role="alert" className="mt-2 text-xs text-danger">{error}</p> : null}
  </div>;
}
