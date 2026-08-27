"""
Post-extraction problem deduplication.

The LLM extraction pass can emit one real question as several rows:

- a sub-question split out of a full essay question (the "第一问" becomes its
  own row next to the complete question),
- a near-duplicate row for the same numbered question,
- a question cut across two extraction chunks: the earlier chunk holds the
  (possibly truncated) head, the later chunk holds a leading fragment with an
  empty ``number`` and/or the complete question again.

``dedupe_extracted_problems`` collapses those rows deterministically before
``resolve_question_score_policy`` freezes max scores, so every real question
owns exactly one row and therefore exactly one max_score.

Merge rules (checked in priority order for each pair; the first-seen position
is always kept):

0. suffix-prefix splice — the earlier stem's tail is verbatim the later
   stem's head (>= ``MIN_CONTAINMENT_CHARS`` normalized chars): the stems are
   spliced back into the full question;
1. same normalized number and (similarity >= ``SIMILARITY_THRESHOLD`` or one
   stem contains the other): keep the longer stem;
2. different numbers but one stem contains the other (shorter stem >=
   ``MIN_CONTAINMENT_CHARS`` and at least ``CONTAINMENT_RATIO`` of the longer):
   keep the longer stem — this is the sub-question-fragment case.

Survivors are renumbered ``q1``..``qN`` in original order and every
``preparation_issues[*].q_id`` is rewritten (issues of dropped duplicates are
carried onto their survivor). The input dict is not mutated.
"""
from __future__ import annotations

import logging
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A stem must be at least this long (normalized chars) before any containment
# or splice match counts — shorter stems are too generic to compare safely.
MIN_CONTAINMENT_CHARS = 40
# Cross-number containment only merges when the shorter stem is at least this
# fraction of the longer one (guards against matching a generic shared phrase).
CONTAINMENT_RATIO = 0.3
SIMILARITY_THRESHOLD = 0.9
# Upper bound of normalized chars scanned for a suffix/prefix splice.
MAX_OVERLAY_SCAN = 300

# Text fields backfilled from a dropped duplicate into its survivor.
# max_score is intentionally excluded: it is frozen later by the score policy.
_BACKFILL_FIELDS = ("number", "type", "criterion", "reference_answer", "solution_code")


def normalize_stem(text: str) -> str:
    """NFKC-normalize, drop all whitespace, and casefold a stem for comparison."""
    if not text:
        return ""
    return "".join(unicodedata.normalize("NFKC", text).split()).casefold()


def _normalize_number(number: Any) -> str:
    """Canonical form of a display number: '1.' / '（1）' / '(1)' all -> '1'."""
    if number is None:
        return ""
    s = unicodedata.normalize("NFKC", str(number)).strip().casefold()
    while s and s[0] in "（(【[ ":
        s = s[1:]
    while s and s[-1] in "）)】] .、,，：:;；":
        s = s[:-1]
    return s.strip()


def _suffix_prefix_overlap(a_norm: str, b_norm: str) -> int:
    """Length of the longest overlap where the tail of ``a`` equals the head of ``b``.

    Returns 0 when no overlap of at least ``MIN_CONTAINMENT_CHARS`` exists.
    Scans candidate lengths from large to small, bounded by ``MAX_OVERLAY_SCAN``.
    """
    if not a_norm or not b_norm:
        return 0
    max_len = min(len(a_norm), len(b_norm), MAX_OVERLAY_SCAN)
    if max_len < MIN_CONTAINMENT_CHARS:
        return 0
    for length in range(max_len, MIN_CONTAINMENT_CHARS - 1, -1):
        if a_norm[-length:] == b_norm[:length]:
            return length
    return 0


def _is_contained(short_norm: str, long_norm: str) -> bool:
    return len(short_norm) >= MIN_CONTAINMENT_CHARS and short_norm in long_norm


def _raw_prefix_len(raw: str, target_norm: str) -> int:
    """Shortest raw prefix of ``raw`` whose normalized form covers ``target_norm``.

    Normalization is per-character (NFKC + whitespace drop + casefold), so the
    normalized length of a raw prefix is monotone in the raw prefix length and
    a binary search is valid.
    """
    if not target_norm:
        return 0
    norm = normalize_stem(raw)
    if len(norm) < len(target_norm) or not norm.startswith(target_norm):
        return len(raw)
    target_len = len(target_norm)
    lo, hi = 0, len(raw)
    while lo < hi:
        mid = (lo + hi) // 2
        if len(normalize_stem(raw[:mid])) >= target_len:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _merge_decision(
    keeper: Dict[str, Any],
    dropped: Dict[str, Any],
    keeper_norm: str,
    dropped_norm: str,
) -> Optional[Tuple[str, int]]:
    """Return (kind, overlap) if the later row must merge into the earlier one."""
    overlap = _suffix_prefix_overlap(keeper_norm, dropped_norm)
    if overlap:
        return ("splice", overlap)

    if keeper_norm and dropped_norm:
        if len(keeper_norm) >= len(dropped_norm):
            short_norm, long_norm = dropped_norm, keeper_norm
        else:
            short_norm, long_norm = keeper_norm, dropped_norm
        contained = _is_contained(short_norm, long_norm)

    keeper_num = _normalize_number(keeper.get("number"))
    dropped_num = _normalize_number(dropped.get("number"))
    if keeper_num and keeper_num == dropped_num:
        # Same display number: containment alone merges (no ratio floor — a
        # split-out sub-question can be a small slice of the full question);
        # otherwise require a high similarity score.
        if contained:
            return ("keep_longer", 0)
        if SequenceMatcher(None, keeper_norm, dropped_norm).ratio() >= SIMILARITY_THRESHOLD:
            return ("keep_longer", 0)
        return None

    # Different (or empty) numbers: only a strong containment match counts,
    # e.g. a chunk-boundary fragment numbered "" against the full question.
    if contained and len(short_norm) / len(long_norm) >= CONTAINMENT_RATIO:
        return ("keep_longer", 0)
    return None


def _apply_merge(
    items: List[Dict[str, Any]],
    norms: List[str],
    keeper_idx: int,
    dropped_idx: int,
    kind: str,
    overlap: int,
) -> None:
    keeper = items[keeper_idx]
    dropped = items[dropped_idx]
    if kind == "splice":
        raw_split = _raw_prefix_len(
            str(dropped.get("stem") or ""),
            norms[dropped_idx][:overlap],
        )
        new_stem = (
            str(keeper.get("stem") or "")
            + str(dropped.get("stem") or "")[raw_split:]
        )
        keeper["stem"] = new_stem
        norms[keeper_idx] = normalize_stem(new_stem)
    elif len(norms[dropped_idx]) > len(norms[keeper_idx]):
        keeper["stem"] = dropped.get("stem")
        norms[keeper_idx] = norms[dropped_idx]
    for field in _BACKFILL_FIELDS:
        if not keeper.get(field) and dropped.get(field):
            keeper[field] = dropped[field]
    keeper["preparation_issues"] = list(
        list(keeper.get("preparation_issues") or [])
        + list(dropped.get("preparation_issues") or [])
    )


def dedupe_extracted_problems(
    problems: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Collapse duplicate extraction rows into one row per real question.

    Returns a new dict keyed ``q1``..``qN`` in original (first-seen) order with
    ``preparation_issues[*].q_id`` rewritten to the new ids.
    """
    items = [dict(item) for item in problems.values() if isinstance(item, dict)]
    if not items:
        return {}
    norms: List[str] = [normalize_stem(str(item.get("stem") or "")) for item in items]
    live: List[int] = list(range(len(items)))
    absorbed_into: Dict[int, int] = {}

    # Repeated pair scans to a fixpoint: a fragment may only become matchable
    # once a near-duplicate head has first merged into the full question.
    while True:
        action = None
        for i_pos in range(len(live)):
            i = live[i_pos]
            for j_pos in range(i_pos + 1, len(live)):
                j = live[j_pos]
                decision = _merge_decision(items[i], items[j], norms[i], norms[j])
                if decision is not None:
                    action = (i, j, decision)
                    break
            if action is not None:
                break
        if action is None:
            break
        i, j, (kind, overlap) = action
        _apply_merge(items, norms, i, j, kind, overlap)
        absorbed_into[j] = i
        live.remove(j)
        logger.info(
            "problem_dedup: merged %s (number=%r) into %s (number=%r) via %s%s",
            items[j].get("q_id"),
            items[j].get("number"),
            items[i].get("q_id"),
            items[i].get("number"),
            kind,
            f" (overlap={overlap})" if overlap else "",
        )

    def resolve(idx: int) -> int:
        # Chains are strictly decreasing in original index, so this terminates.
        seen = set()
        while idx in absorbed_into and idx not in seen:
            seen.add(idx)
            idx = absorbed_into[idx]
        return idx

    survivor_order = sorted(live)
    final_qid = {idx: f"q{pos}" for pos, idx in enumerate(survivor_order, start=1)}
    remap: Dict[str, str] = {}
    for idx, item in enumerate(items):
        old_qid = str(item.get("q_id") or "")
        if old_qid:
            remap[old_qid] = final_qid[resolve(idx)]

    result: Dict[str, Dict[str, Any]] = {}
    for idx, new_qid in final_qid.items():
        item = dict(items[idx])
        item["q_id"] = new_qid
        issues = []
        for issue in item.get("preparation_issues") or []:
            if not isinstance(issue, dict):
                issues.append(issue)
                continue
            issue = dict(issue)
            old = str(issue.get("q_id") or "")
            if old in remap:
                issue["q_id"] = remap[old]
            issues.append(issue)
        item["preparation_issues"] = issues
        result[new_qid] = item
    if len(result) != len(items):
        logger.info(
            "problem_dedup: %d rows -> %d questions", len(items), len(result)
        )
    return result
