import { AlertTriangle, KeyRound, Loader2, Power, RefreshCw, Trash2 } from "lucide-react";
import type { FormEvent } from "react";
import { useState } from "react";
import { toast } from "sonner";
import { normalizeAPIError } from "@/api/client";
import { useAddExpertKey, useExperts, useRemoveExpert, useSelectExpert } from "@/api/hooks";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import type { ExpertConfig, ProviderType } from "@/types";

const providerOptions: Array<{ value: ProviderType; label: string; defaultModel: string }> = [
  { value: "gemini", label: "Gemini", defaultModel: "gemini-3-flash-preview" },
  { value: "openai", label: "OpenAI", defaultModel: "gpt-4o" },
  { value: "zhipu", label: "Zhipu", defaultModel: "glm-4.5-air" },
  { value: "anthropic", label: "Anthropic", defaultModel: "claude-sonnet-4-20250514" },
];

const providerDefaults = providerOptions.reduce<Record<ProviderType, string>>(
  (acc, provider) => {
    acc[provider.value] = provider.defaultModel;
    return acc;
  },
  {
    gemini: "gemini-3-flash-preview",
    openai: "gpt-4o",
    zhipu: "glm-4.5-air",
    anthropic: "claude-sonnet-4-20250514",
  },
);

export function ExpertsPage() {
  const expertsQuery = useExperts();
  const addExpertKey = useAddExpertKey();
  const selectExpert = useSelectExpert();
  const removeExpert = useRemoveExpert();
  const [provider, setProvider] = useState<ProviderType>("gemini");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [rpm, setRpm] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const experts = expertsQuery.data ?? [];
  const enabledCount = experts.filter((expert) => expert.enabled).length;
  const defaultModel = providerDefaults[provider];
  const isAdding = addExpertKey.isPending;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);

    const trimmedKey = apiKey.trim();
    const trimmedModel = model.trim();
    const trimmedBaseUrl = baseUrl.trim();
    const trimmedRpm = rpm.trim();

    if (!trimmedKey) {
      setFormError("请输入你自己的 API key。");
      return;
    }

    const rpmValue = trimmedRpm ? Number(trimmedRpm) : undefined;
    if (rpmValue !== undefined && (!Number.isInteger(rpmValue) || rpmValue < 0)) {
      setFormError("RPM 需要是大于或等于 0 的整数。");
      return;
    }

    try {
      const response = await addExpertKey.mutateAsync({
        provider_type: provider,
        api_key: trimmedKey,
        model: trimmedModel || defaultModel,
        base_url: trimmedBaseUrl || null,
        rpm: rpmValue,
      });

      setApiKey("");

      if (response.status === "success") {
        toast.success("BYOK expert 已添加", {
          description: response.provider_id ?? `${provider}:${trimmedModel || defaultModel}`,
        });
        return;
      }

      toast.warning(response.message ?? "后端返回了未预期的添加结果。");
    } catch (error) {
      const message = normalizeAPIError(error).message;
      setApiKey("");
      setFormError(message);
      toast.error("添加 BYOK expert 失败", { description: message });
    }
  }

  async function handleToggle(expert: ExpertConfig) {
    try {
      const nextEnabled = !expert.enabled;
      const response = await selectExpert.mutateAsync({
        providerId: expert.provider_id,
        enabled: nextEnabled,
      });

      if (response.status === "success") {
        toast.success(nextEnabled ? "Expert 已启用" : "Expert 已停用", {
          description: expert.provider_id,
        });
        return;
      }

      toast.warning(response.message ?? "后端返回了未预期的启停结果。");
    } catch (error) {
      toast.error("更新 expert 状态失败", { description: normalizeAPIError(error).message });
    }
  }

  async function handleRemove(expert: ExpertConfig) {
    const confirmed = window.confirm(`删除 ${expert.provider_id}？此操作会移除这把 BYOK key。`);
    if (!confirmed) {
      return;
    }

    try {
      const response = await removeExpert.mutateAsync(expert.provider_id);

      if (response.status === "success") {
        toast.success("Expert 已删除", { description: expert.provider_id });
        return;
      }

      toast.warning(response.message ?? "后端返回了未预期的删除结果。");
    } catch (error) {
      toast.error("删除 expert 失败", { description: normalizeAPIError(error).message });
    }
  }

  return (
    <div className="grid gap-5">
      <SectionHeader
        title="BYOK 专家"
        description="添加教师自己持有的模型 API key，用于当前教师端批改流程；本页不配置平台共享池，也不会回显 key。"
        action={
          <Button
            type="button"
            variant="secondary"
            onClick={() => void expertsQuery.refetch()}
            disabled={expertsQuery.isFetching}
          >
            <RefreshCw
              aria-hidden="true"
              className={expertsQuery.isFetching ? "animate-spin" : undefined}
              size={16}
            />
            刷新
          </Button>
        }
      />

      <Card className="grid gap-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 className="text-base font-semibold">已配置 experts</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              当前可用 {enabledCount} / {experts.length} 个。列表只显示 provider、model、RPM 与启用状态。
            </p>
          </div>
        </div>

        {expertsQuery.isLoading ? (
          <div className="flex items-center gap-2 rounded-md border bg-muted/40 p-4 text-sm text-muted-foreground">
            <Loader2 aria-hidden="true" className="animate-spin" size={16} />
            正在读取 experts...
          </div>
        ) : null}

        {expertsQuery.isError ? (
          <InlineError message={normalizeAPIError(expertsQuery.error).message} />
        ) : null}

        {!expertsQuery.isLoading && !expertsQuery.isError && experts.length === 0 ? (
          <EmptyState title="还没有 BYOK expert" description="添加你自己的模型 API key 后即可用于批改。" />
        ) : null}

        {experts.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] border-separate border-spacing-0 text-left text-sm">
              <thead className="text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="border-b px-3 py-2 font-medium">Provider</th>
                  <th className="border-b px-3 py-2 font-medium">Model</th>
                  <th className="border-b px-3 py-2 font-medium">RPM</th>
                  <th className="border-b px-3 py-2 font-medium">状态</th>
                  <th className="border-b px-3 py-2 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {experts.map((expert) => (
                  <ExpertRow
                    key={expert.provider_id}
                    expert={expert}
                    controlsDisabled={selectExpert.isPending || removeExpert.isPending}
                    onRemove={() => void handleRemove(expert)}
                    onToggle={() => void handleToggle(expert)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </Card>

      <Card className="max-w-3xl">
        <form className="grid gap-4" onSubmit={handleSubmit}>
          <div>
            <h2 className="text-base font-semibold">添加 API key</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              仅提交你自带的 key。提交成功后会立即清空输入框，页面不会保存或展示 key 明文。
            </p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Provider">
              <select
                className="h-9 rounded-md border bg-background px-3 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20"
                disabled={isAdding}
                onChange={(event) => setProvider(event.target.value as ProviderType)}
                value={provider}
              >
                {providerOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="API key">
              <Input
                autoComplete="off"
                disabled={isAdding}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder="提交后清空，不会回显"
                type="password"
                value={apiKey}
              />
            </Field>
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Model（可选）" hint={`留空使用 ${defaultModel}`}>
              <Input
                disabled={isAdding}
                onChange={(event) => setModel(event.target.value)}
                placeholder={defaultModel}
                value={model}
              />
            </Field>
            <Field label="Base URL（可选）">
              <Input
                disabled={isAdding}
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder="OpenAI compatible endpoint"
                value={baseUrl}
              />
            </Field>
            <Field label="RPM（可选）" hint="0 或留空表示不额外限速">
              <Input
                disabled={isAdding}
                inputMode="numeric"
                min={0}
                onChange={(event) => setRpm(event.target.value)}
                placeholder="例如 15"
                type="number"
                value={rpm}
              />
            </Field>
          </div>

          {formError ? <InlineError message={formError} /> : null}

          <Button type="submit" className="w-fit" disabled={isAdding}>
            {isAdding ? (
              <Loader2 aria-hidden="true" className="animate-spin" size={16} />
            ) : (
              <KeyRound aria-hidden="true" size={16} />
            )}
            {isAdding ? "添加中..." : "添加 BYOK expert"}
          </Button>
        </form>
      </Card>
    </div>
  );
}

function ExpertRow({
  expert,
  controlsDisabled,
  onRemove,
  onToggle,
}: {
  expert: ExpertConfig;
  controlsDisabled: boolean;
  onRemove: () => void;
  onToggle: () => void;
}) {
  return (
    <tr className="align-top">
      <td className="border-b px-3 py-3">
        <div className="font-medium">{providerLabel(expert.provider_type)}</div>
        <div className="mt-1 break-all text-xs text-muted-foreground">{expert.provider_id}</div>
      </td>
      <td className="border-b px-3 py-3">
        <div className="break-all font-medium">{expert.model}</div>
      </td>
      <td className="border-b px-3 py-3">{formatRpm(expert.rpm)}</td>
      <td className="border-b px-3 py-3">
        <StatusBadge enabled={expert.enabled} />
      </td>
      <td className="border-b px-3 py-3">
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onToggle} disabled={controlsDisabled}>
            <Power aria-hidden="true" size={16} />
            {expert.enabled ? "停用" : "启用"}
          </Button>
          <Button type="button" variant="danger" onClick={onRemove} disabled={controlsDisabled}>
            <Trash2 aria-hidden="true" size={16} />
            删除
          </Button>
        </div>
      </td>
    </tr>
  );
}

function StatusBadge({ enabled }: { enabled: boolean }) {
  return (
    <span
      className={
        enabled
          ? "inline-flex rounded-md bg-accent/10 px-2 py-1 text-xs font-medium text-accent"
          : "inline-flex rounded-md bg-muted px-2 py-1 text-xs font-medium text-muted-foreground"
      }
    >
      {enabled ? "已启用" : "已停用"}
    </span>
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

function providerLabel(providerType: string) {
  return providerOptions.find((option) => option.value === providerType)?.label ?? providerType;
}

function formatRpm(rpm: number) {
  return rpm > 0 ? `${rpm} / min` : "0（不限）";
}
