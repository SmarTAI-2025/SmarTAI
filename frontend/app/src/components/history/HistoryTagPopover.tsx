import { Check, Pencil, Plus, Tag as TagIcon, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { normalizeAPIError } from "@/api/client";
import { useCreateTag, useDeleteTag, useUpdateTag, useUpdateTask } from "@/api/hooks";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import type { TagColor, TaskLite, TaskTag } from "@/types";
import { TAG_COLORS, TAG_COLOR_LABEL_KEYS, TAG_TONE_CLASSES } from "./historyPresentation";

export function HistoryTagPopover({ task, tags }: { task: TaskLite; tags: TaskTag[] }) {
  const { locale, t } = useI18n();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [newColor, setNewColor] = useState<TagColor>("slate");
  const [editingTag, setEditingTag] = useState<TaskTag | null>(null);
  const [editingName, setEditingName] = useState("");
  const [editingColor, setEditingColor] = useState<TagColor>("slate");
  const rootRef = useRef<HTMLDivElement>(null);
  const updateTask = useUpdateTask();
  const createTag = useCreateTag();
  const updateTag = useUpdateTag();
  const deleteTag = useDeleteTag();
  const taskTagIds = task.tag_ids ?? [];
  const pending = updateTask.isPending || createTag.isPending || updateTag.isPending || deleteTag.isPending;
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const filteredTags = useMemo(
    () => tags.filter((tag) => !normalizedSearch || tag.name.toLocaleLowerCase().includes(normalizedSearch)),
    [normalizedSearch, tags],
  );

  useEffect(() => {
    if (!open) return;
    function close(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  async function toggleTag(tagId: string) {
    const next = taskTagIds.includes(tagId)
      ? taskTagIds.filter((id) => id !== tagId)
      : [...taskTagIds, tagId];
    try {
      await updateTask.mutateAsync({ taskId: task.task_id, patch: { tag_ids: next } });
    } catch (error) {
      toast.error(normalizeAPIError(error).message);
    }
  }

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    const name = search.trim();
    if (!name) return;
    try {
      const tag = await createTag.mutateAsync({ name, color: newColor });
      if (!taskTagIds.includes(tag.id)) {
        await updateTask.mutateAsync({ taskId: task.task_id, patch: { tag_ids: [...taskTagIds, tag.id] } });
      }
      setSearch("");
      setNewColor("slate");
    } catch (error) {
      toast.error(normalizeAPIError(error).message);
    }
  }

  function beginEdit(tag: TaskTag) {
    setEditingTag(tag);
    setEditingName(tag.name);
    setEditingColor(tag.color);
  }

  async function saveEdit() {
    const name = editingName.trim();
    if (!editingTag || !name) return;
    try {
      await updateTag.mutateAsync({ tagId: editingTag.id, patch: { name, color: editingColor } });
      setEditingTag(null);
    } catch (error) {
      toast.error(normalizeAPIError(error).message);
    }
  }

  async function handleDeleteTag(tag: TaskTag) {
    const usageCount = tag.usage_count ?? 0;
    const confirmed = window.confirm(
      `${t("historyTagDeleteConfirmPrefix")}${tag.name}${t("historyTagDeleteConfirmSuffix")}`
      + (locale === "zh-CN"
        ? `${t("historyTagDeleteUsagePrefix")}${usageCount}${t("historyTagDeleteUsageSuffix")}`
        : `It will be removed from ${usageCount} related ${usageCount === 1 ? "task" : "tasks"} without deleting ${usageCount === 1 ? "the task" : "them"}.`),
    );
    if (!confirmed) return;
    try {
      await deleteTag.mutateAsync(tag.id);
      setEditingTag(null);
      toast.success(t("historyTagDeleteSuccess"));
    } catch (error) {
      toast.error(normalizeAPIError(error).message);
    }
  }

  return (
    <div ref={rootRef} className="relative inline-flex">
      <button
        type="button"
        aria-label={t("historyManageTags")}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="inline-flex h-6 w-6 items-center justify-center rounded-full border bg-background text-muted-foreground outline-none transition-colors hover:border-primary/40 hover:text-primary focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Plus aria-hidden="true" className="h-3.5 w-3.5" />
      </button>

      {open ? (
        <div role="dialog" aria-label={t("historyTagMenuLabel")} className="absolute left-0 top-8 z-40 w-[320px] max-w-[calc(100vw-40px)] rounded-[10px] border bg-card p-3 text-left shadow-xl">
          <div className="flex items-center justify-between gap-3">
            <span className="inline-flex items-center gap-2 text-sm font-semibold"><TagIcon aria-hidden="true" className="h-4 w-4 text-muted-foreground" />{t("historyManageTags")}</span>
            <button type="button" aria-label={t("historyTagCancel")} className="rounded p-1 text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring" onClick={() => setOpen(false)}>
              <X aria-hidden="true" className="h-4 w-4" />
            </button>
          </div>

          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t("historyTagSearchPlaceholder")}
            className="mt-3 h-8 w-full rounded-md border bg-background px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
          />

          <div className="mt-2 max-h-48 overflow-y-auto">
            {filteredTags.length ? filteredTags.map((tag) => {
              const selected = taskTagIds.includes(tag.id);
              return (
                <div key={tag.id} className="flex items-center gap-1 rounded-md px-1 py-1 hover:bg-muted/50">
                  <button
                    type="button"
                    disabled={pending}
                    onClick={() => void toggleTag(tag.id)}
                    className="flex min-w-0 flex-1 items-center gap-2 rounded px-1 py-1 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                  >
                    <span className={cn("inline-flex h-4 w-4 shrink-0 items-center justify-center rounded border", selected ? "border-primary bg-primary text-primary-foreground" : "bg-background")}>
                      {selected ? <Check aria-hidden="true" className="h-3 w-3" /> : null}
                    </span>
                    <span className={cn("truncate rounded-full border px-2 py-0.5 text-xs", TAG_TONE_CLASSES[tag.color])}>{tag.name}</span>
                    <span className="shrink-0 text-[11px] text-muted-foreground">
                      {locale === "zh-CN"
                        ? `${tag.usage_count ?? 0}${t("historyTagUsageSuffix")}`
                        : `${tag.usage_count ?? 0} ${(tag.usage_count ?? 0) === 1 ? "task" : "tasks"}`}
                    </span>
                  </button>
                  <button type="button" aria-label={`${t("historyTagRename")} ${tag.name}`} className="rounded p-1.5 text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring" onClick={() => beginEdit(tag)}>
                    <Pencil aria-hidden="true" className="h-3.5 w-3.5" />
                  </button>
                </div>
              );
            }) : <p className="px-2 py-3 text-xs text-muted-foreground">{t("historyTagEmpty")}</p>}
          </div>

          {editingTag ? (
            <div className="mt-2 rounded-lg border bg-background p-2">
              <input value={editingName} onChange={(event) => setEditingName(event.target.value)} className="h-8 w-full rounded-md border bg-card px-2 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/15" />
              <ColorPalette selected={editingColor} onChange={setEditingColor} />
              <div className="mt-2 flex items-center justify-between gap-2">
                <button type="button" disabled={pending} onClick={() => void handleDeleteTag(editingTag)} className="inline-flex items-center gap-1 text-xs font-medium text-danger outline-none focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50">
                  <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />{t("historyTagDelete")}
                </button>
                <div className="flex gap-2">
                  <button type="button" onClick={() => setEditingTag(null)} className="text-xs text-muted-foreground">{t("historyTagCancel")}</button>
                  <button type="button" disabled={pending || !editingName.trim()} onClick={() => void saveEdit()} className="rounded-md bg-primary px-2.5 py-1.5 text-xs font-semibold text-primary-foreground disabled:opacity-50">{t("historyTagSave")}</button>
                </div>
              </div>
            </div>
          ) : (
            <form className="mt-2 border-t pt-2" onSubmit={handleCreate}>
              <ColorPalette selected={newColor} onChange={setNewColor} />
              <button type="submit" disabled={pending || !search.trim()} className="mt-2 inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-md border bg-background text-xs font-semibold text-primary outline-none hover:border-primary/40 focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50">
                <Plus aria-hidden="true" className="h-3.5 w-3.5" />
                {createTag.isPending ? t("historyTagCreating") : t("historyTagCreate")}
              </button>
              <p className="mt-2 text-[11px] leading-4 text-muted-foreground">{t("historyTagLimitHint")}</p>
            </form>
          )}
        </div>
      ) : null}
    </div>
  );
}

function ColorPalette({ selected, onChange }: { selected: TagColor; onChange: (color: TagColor) => void }) {
  const { t } = useI18n();
  return (
    <div className="mt-2 flex items-center gap-1" role="radiogroup" aria-label={t("historyTagColor")}>
      {TAG_COLORS.map((color) => (
        <button
          key={color}
          type="button"
          role="radio"
          aria-checked={selected === color}
          aria-label={t(TAG_COLOR_LABEL_KEYS[color])}
          onClick={() => onChange(color)}
          className={cn("h-5 w-5 rounded-full border outline-none focus-visible:ring-2 focus-visible:ring-ring", TAG_TONE_CLASSES[color], selected && "ring-2 ring-primary ring-offset-1")}
        />
      ))}
    </div>
  );
}
