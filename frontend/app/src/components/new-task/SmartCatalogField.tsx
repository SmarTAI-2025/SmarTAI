import { Check, ChevronDown, LoaderCircle, Plus, Search, X } from "lucide-react";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type CompositionEvent,
  type FocusEvent,
  type KeyboardEvent,
} from "react";
import { getAPIErrorDetail, normalizeAPIError } from "@/api/client";
import { useI18n } from "@/i18n/I18nProvider";
import type { CatalogCandidate } from "@/types";

type OptionRow<T> =
  | { key: string; kind: "item"; candidate: CatalogCandidate<T> }
  | { key: string; kind: "create" };

interface SmartCatalogFieldProps<T> {
  label: string;
  hint: string;
  placeholder: string;
  resource: "course" | "tag";
  query: string;
  onQueryChange: (value: string) => void;
  selected: T[];
  initialItems: T[];
  searchCandidates: CatalogCandidate<T>[];
  isSearching: boolean;
  isCreating: boolean;
  multiple?: boolean;
  getId: (item: T) => string;
  getLabel: (item: T) => string;
  getMeta?: (item: T) => string | undefined;
  onSelect: (item: T) => void;
  onRemove: (item: T) => void;
  onCreate: (name: string, force: boolean) => Promise<T>;
}

export function SmartCatalogField<T>({
  label,
  hint,
  placeholder,
  resource,
  query,
  onQueryChange,
  selected,
  initialItems,
  searchCandidates,
  isSearching,
  isCreating,
  multiple = false,
  getId,
  getLabel,
  getMeta,
  onSelect,
  onRemove,
  onCreate,
}: SmartCatalogFieldProps<T>) {
  const { t } = useI18n();
  const fieldId = useId();
  const listId = `${fieldId}-listbox`;
  const rootRef = useRef<HTMLDivElement>(null);
  const isComposingRef = useRef(false);
  const pendingCompositionCommitRef = useRef<number | null>(null);
  const lastCommittedQueryRef = useRef(query);
  const [inputValue, setInputValue] = useState(query);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [createError, setCreateError] = useState<string | null>(null);
  const [conflictCandidates, setConflictCandidates] = useState<CatalogCandidate<T>[]>([]);
  const trimmedQuery = query.trim();

  const candidates = useMemo<CatalogCandidate<T>[]>(() => {
    if (trimmedQuery) return searchCandidates;
    return initialItems.map((item) => ({ item, match_kind: "exact", score: 1, reason: "available" }));
  }, [initialItems, searchCandidates, trimmedQuery]);

  const visibleCandidates = conflictCandidates.length ? conflictCandidates : candidates;
  const exactCandidates = visibleCandidates.filter((candidate) => candidate.match_kind === "exact");
  const relatedCandidates = visibleCandidates.filter((candidate) => candidate.match_kind === "related");
  const canCreate = Boolean(trimmedQuery)
    && !isSearching
    && !exactCandidates.length
    && !conflictCandidates.length;
  const rows = useMemo<OptionRow<T>[]>(() => [
    ...visibleCandidates.map((candidate) => ({
      key: `item-${getId(candidate.item)}`,
      kind: "item" as const,
      candidate,
    })),
    ...(canCreate ? [{ key: "create", kind: "create" as const }] : []),
  ], [canCreate, getId, visibleCandidates]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query, conflictCandidates.length]);

  useEffect(() => {
    lastCommittedQueryRef.current = query;
    if (!isComposingRef.current && pendingCompositionCommitRef.current === null) {
      setInputValue((current) => current === query ? current : query);
    }
  }, [query]);

  useEffect(() => () => {
    if (pendingCompositionCommitRef.current !== null) {
      window.clearTimeout(pendingCompositionCommitRef.current);
    }
  }, []);

  useEffect(() => {
    function closeOutside(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", closeOutside);
    return () => document.removeEventListener("mousedown", closeOutside);
  }, []);

  function selectItem(item: T, closeAfterSelection = false) {
    onSelect(item);
    if (pendingCompositionCommitRef.current !== null) {
      window.clearTimeout(pendingCompositionCommitRef.current);
      pendingCompositionCommitRef.current = null;
    }
    isComposingRef.current = false;
    lastCommittedQueryRef.current = "";
    setInputValue("");
    onQueryChange("");
    setCreateError(null);
    setConflictCandidates([]);
    setOpen(closeAfterSelection ? false : multiple);
  }

  async function createValue(force: boolean) {
    if (!trimmedQuery || isCreating) return;
    setCreateError(null);
    try {
      const item = await onCreate(trimmedQuery, force);
      // A newly created catalog value is a completed action. Close the menu
      // even for multi-select tags so it does not cover the rest of the form.
      selectItem(item, true);
    } catch (error) {
      const normalized = normalizeAPIError(error);
      const detail = getAPIErrorDetail(normalized);
      if (
        normalized.status === 409
        && detail
        && "code" in detail
        && detail.code === "similar_items"
        && "candidates" in detail
        && Array.isArray(detail.candidates)
      ) {
        setConflictCandidates(detail.candidates as CatalogCandidate<T>[]);
        setOpen(true);
        return;
      }
      setCreateError(normalized.message);
    }
  }

  function activate(row: OptionRow<T>) {
    if (isCreating) return;
    if (row.kind === "create") void createValue(false);
    else selectItem(row.candidate.item);
  }

  function commitQuery(value: string) {
    if (lastCommittedQueryRef.current === value) return;
    lastCommittedQueryRef.current = value;
    onQueryChange(value);
  }

  function flushComposition(input: HTMLInputElement) {
    if (pendingCompositionCommitRef.current !== null) {
      window.clearTimeout(pendingCompositionCommitRef.current);
      pendingCompositionCommitRef.current = null;
    }
    isComposingRef.current = false;
    const finalValue = input.value;
    setInputValue(finalValue);
    commitQuery(finalValue);
  }

  function handleChange(event: ChangeEvent<HTMLInputElement>) {
    const value = event.currentTarget.value;
    setInputValue(value);
    if (
      !isComposingRef.current
      && pendingCompositionCommitRef.current === null
      && !(event.nativeEvent as InputEvent).isComposing
    ) {
      commitQuery(value);
    }
    setCreateError(null);
    setConflictCandidates([]);
    setOpen(true);
  }

  function handleCompositionStart() {
    if (pendingCompositionCommitRef.current !== null) {
      window.clearTimeout(pendingCompositionCommitRef.current);
      pendingCompositionCommitRef.current = null;
    }
    isComposingRef.current = true;
  }

  function handleCompositionEnd(event: CompositionEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    isComposingRef.current = false;
    setInputValue(input.value);
    pendingCompositionCommitRef.current = window.setTimeout(() => {
      flushComposition(input);
    }, 0);
  }

  function handleBlur(event: FocusEvent<HTMLInputElement>) {
    if (isComposingRef.current || pendingCompositionCommitRef.current !== null) {
      flushComposition(event.currentTarget);
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (
      isComposingRef.current
      || pendingCompositionCommitRef.current !== null
      || event.nativeEvent.isComposing
      || event.key === "Process"
      || event.keyCode === 229
    ) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((current) => {
        if (!rows.length) return 0;
        const delta = event.key === "ArrowDown" ? 1 : -1;
        return (current + delta + rows.length) % rows.length;
      });
    } else if (event.key === "Enter" && open && rows[activeIndex]) {
      event.preventDefault();
      activate(rows[activeIndex]);
    } else if (event.key === "Escape") {
      setOpen(false);
      setConflictCandidates([]);
    } else if (event.key === "Backspace" && !query && multiple && selected.length) {
      onRemove(selected[selected.length - 1]);
    }
  }

  function renderCandidate(candidate: CatalogCandidate<T>) {
    const item = candidate.item;
    const selectedNow = selected.some((selectedItem) => getId(selectedItem) === getId(item));
    const index = rows.findIndex((row) => row.key === `item-${getId(item)}`);
    return (
      <button
        id={`${listId}-${index}`}
        key={getId(item)}
        type="button"
        tabIndex={-1}
        role="option"
        aria-selected={selectedNow}
        disabled={isCreating}
        className={`flex w-full items-center gap-3 px-3 py-2 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-wait disabled:opacity-50 ${index === activeIndex ? "bg-blue-50 text-foreground dark:bg-slate-700" : "hover:bg-muted"}`}
        onMouseEnter={() => setActiveIndex(index)}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => { if (!isCreating) selectItem(item); }}
      >
        <span className="min-w-0 flex-1">
          <span className="block truncate font-medium">{getLabel(item)}</span>
          {getMeta?.(item) ? <span className="mt-0.5 block truncate text-xs text-muted-foreground">{getMeta(item)}</span> : null}
        </span>
        {selectedNow ? <Check aria-hidden="true" className="h-4 w-4 text-primary" /> : null}
      </button>
    );
  }

  const showMenu = open && (rows.length > 0 || isSearching || conflictCandidates.length > 0);

  return (
    <div ref={rootRef} className="relative min-w-0">
      <label htmlFor={fieldId} className="block text-[14px] font-semibold leading-5 text-foreground">
        {label}
      </label>
      <p className="sr-only">{hint}</p>
      <div
        className="mt-1 flex min-h-11 w-full flex-wrap items-center gap-1.5 rounded-[8px] border bg-card px-3 py-1.5 transition focus-within:border-primary focus-within:ring-2 focus-within:ring-primary/15"
        onClick={() => { if (!isCreating) setOpen(true); }}
      >
        {selected.map((item) => (
          <span key={getId(item)} className="inline-flex max-w-full items-center gap-1 rounded-full bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700 dark:bg-blue-950/60 dark:text-blue-200">
            <span className="truncate">{getLabel(item)}</span>
            <button type="button" disabled={isCreating} className="rounded-full outline-none hover:text-danger focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-50" onClick={(event) => { event.stopPropagation(); if (!isCreating) onRemove(item); }} aria-label={`${t("newTaskRemoveSelection")} ${getLabel(item)}`}>
              <X aria-hidden="true" className="h-3.5 w-3.5" />
            </button>
          </span>
        ))}
        <div className="relative min-w-[140px] flex-1">
          <Search aria-hidden="true" className="pointer-events-none absolute left-0 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            id={fieldId}
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={showMenu}
            aria-controls={listId}
            aria-activedescendant={showMenu && rows[activeIndex] ? `${listId}-${activeIndex}` : undefined}
            autoComplete="off"
            disabled={isCreating}
            value={inputValue}
            placeholder={selected.length ? t("newTaskSearchAnother") : placeholder}
            className="h-7 w-full bg-transparent pl-6 pr-7 text-[14px] text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-60"
            onFocus={() => setOpen(true)}
            onChange={handleChange}
            onBlur={handleBlur}
            onCompositionStart={handleCompositionStart}
            onCompositionEnd={handleCompositionEnd}
            onKeyDown={handleKeyDown}
          />
          {isSearching ? <LoaderCircle aria-hidden="true" className="absolute right-0 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-muted-foreground" /> : <ChevronDown aria-hidden="true" className="absolute right-0 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />}
        </div>
      </div>

      {showMenu ? (
        <div id={listId} role="listbox" aria-multiselectable={multiple || undefined} className="absolute z-30 mt-1 max-h-64 w-full overflow-y-auto rounded-[6px] border bg-card py-1 shadow-lg">
          {conflictCandidates.length ? (
            <div className="border-b px-3 py-2 text-xs leading-4 text-amber-700 dark:text-amber-300">
              {t(resource === "course" ? "newTaskCourseConflict" : "newTaskTagConflict")}
            </div>
          ) : null}
          {exactCandidates.length ? <div className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{trimmedQuery ? t("newTaskExactMatches") : t("newTaskAvailableOptions")}</div> : null}
          {exactCandidates.map(renderCandidate)}
          {relatedCandidates.length ? <div className="border-t px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{t("newTaskRelatedMatches")}</div> : null}
          {relatedCandidates.map(renderCandidate)}
          {canCreate ? (
            <button id={`${listId}-${rows.length - 1}`} type="button" tabIndex={-1} role="option" aria-selected="false" disabled={isCreating} className={`flex w-full items-center gap-2 border-t px-3 py-2 text-left text-sm font-medium text-primary outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-wait disabled:opacity-50 ${activeIndex === rows.length - 1 ? "bg-blue-50 dark:bg-slate-700" : "hover:bg-muted"}`} onMouseEnter={() => setActiveIndex(rows.length - 1)} onMouseDown={(event) => event.preventDefault()} onClick={() => void createValue(false)}>
              <Plus aria-hidden="true" className="h-4 w-4" />
              {isCreating ? t("newTaskCreating") : <>{t(resource === "course" ? "newTaskCreateCourse" : "newTaskCreateTag")} “{trimmedQuery}”</>}
            </button>
          ) : null}
          {conflictCandidates.length ? (
            <div className="border-t p-2">
              <button type="button" disabled={isCreating} className="w-full rounded-md border px-3 py-2 text-sm font-medium text-foreground hover:bg-muted disabled:opacity-50" onClick={() => void createValue(true)}>
                {isCreating ? t("newTaskCreating") : t("newTaskCreateAnyway")}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
      {createError ? <p className="mt-1 text-xs text-danger" role="alert">{createError}</p> : null}
    </div>
  );
}
