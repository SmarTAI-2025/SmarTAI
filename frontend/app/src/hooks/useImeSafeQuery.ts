import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type CompositionEvent,
  type FocusEvent,
} from "react";

interface ImeSafeQueryOptions {
  value: string;
  onCommit: (value: string) => void;
  onDraftChange?: (value: string) => void;
}

/** Keeps IME composition text local until the browser exposes its final value. */
export function useImeSafeQuery({ value, onCommit, onDraftChange }: ImeSafeQueryOptions) {
  const [draftValue, setDraftValue] = useState(value);
  const draftValueRef = useRef(value);
  const composingRef = useRef(false);
  const pendingCommitRef = useRef<number | null>(null);
  const lastCommittedRef = useRef(value);
  const onCommitRef = useRef(onCommit);
  const onDraftChangeRef = useRef(onDraftChange);

  onCommitRef.current = onCommit;
  onDraftChangeRef.current = onDraftChange;

  const cancelPendingCommit = useCallback(() => {
    if (pendingCommitRef.current === null) return;
    window.clearTimeout(pendingCommitRef.current);
    pendingCommitRef.current = null;
  }, []);

  const updateDraft = useCallback((nextValue: string) => {
    draftValueRef.current = nextValue;
    setDraftValue(nextValue);
    onDraftChangeRef.current?.(nextValue);
  }, []);

  const commit = useCallback((nextValue: string) => {
    if (lastCommittedRef.current === nextValue) return;
    lastCommittedRef.current = nextValue;
    onCommitRef.current(nextValue);
  }, []);

  const flushComposition = useCallback((input: HTMLInputElement) => {
    cancelPendingCommit();
    composingRef.current = false;
    const finalValue = input.value;
    updateDraft(finalValue);
    commit(finalValue);
  }, [cancelPendingCommit, commit, updateDraft]);

  useEffect(() => {
    lastCommittedRef.current = value;
    if (!composingRef.current && pendingCommitRef.current === null) {
      draftValueRef.current = value;
      setDraftValue((current) => current === value ? current : value);
    }
  }, [value]);

  useEffect(() => cancelPendingCommit, [cancelPendingCommit]);

  const handleChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    const nextValue = event.currentTarget.value;
    updateDraft(nextValue);
    if (
      !composingRef.current
      && pendingCommitRef.current === null
      && !(event.nativeEvent as InputEvent).isComposing
    ) {
      commit(nextValue);
    }
  }, [commit, updateDraft]);

  const handleCompositionStart = useCallback(() => {
    cancelPendingCommit();
    composingRef.current = true;
  }, [cancelPendingCommit]);

  const handleCompositionEnd = useCallback((event: CompositionEvent<HTMLInputElement>) => {
    const input = event.currentTarget;
    composingRef.current = false;
    updateDraft(input.value);
    pendingCommitRef.current = window.setTimeout(() => flushComposition(input), 0);
  }, [flushComposition, updateDraft]);

  const handleBlur = useCallback((event: FocusEvent<HTMLInputElement>) => {
    if (composingRef.current || pendingCommitRef.current !== null) {
      flushComposition(event.currentTarget);
    }
  }, [flushComposition]);

  const commitValue = useCallback((nextValue: string) => {
    cancelPendingCommit();
    composingRef.current = false;
    updateDraft(nextValue);
    commit(nextValue);
  }, [cancelPendingCommit, commit, updateDraft]);

  const commitDraft = useCallback(() => {
    const nextValue = draftValueRef.current;
    commitValue(nextValue);
    return nextValue;
  }, [commitValue]);

  return {
    draftValue,
    handleBlur,
    handleChange,
    handleCompositionEnd,
    handleCompositionStart,
    commitDraft,
    commitValue,
  };
}
