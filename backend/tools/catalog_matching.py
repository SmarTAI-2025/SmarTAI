"""Deterministic lexical matching for owner-scoped catalog entries.

This module deliberately does not call an LLM or an embedding service.  It is
used by the New Task course/tag pickers to distinguish a normalized exact
match from a conservative lexical recommendation.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Generic, Iterable, Mapping, Optional, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class CatalogMatch(Generic[T]):
    item: T
    match_kind: str
    score: float
    reason: str


def normalize_catalog_text(value: str) -> tuple[str, str]:
    """Return a display value and its NFKC/casefold/whitespace key."""

    display = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
    return display, display.casefold()


def classify_catalog_match(
    query: str,
    fields: Mapping[str, str],
) -> Optional[tuple[str, float, str]]:
    """Classify one item as ``exact``/``related`` or return no match.

    Related matches are intentionally lexical: substring, token overlap, or a
    conservative edit-similarity threshold.  The field name is included in the
    stable reason code so clients can explain why a candidate was suggested.
    """

    _, query_key = normalize_catalog_text(query)
    if not query_key:
        return None

    best: Optional[tuple[str, float, str]] = None
    query_tokens = set(query_key.split())
    for field_name, raw_value in fields.items():
        _, value_key = normalize_catalog_text(raw_value)
        if not value_key:
            continue
        if query_key == value_key:
            return "exact", 1.0, f"{field_name}_exact"

        candidate: Optional[tuple[str, float, str]] = None
        if query_key in value_key or value_key in query_key:
            coverage = min(len(query_key), len(value_key)) / max(
                len(query_key), len(value_key), 1,
            )
            candidate = (
                "related",
                0.82 + 0.16 * coverage,
                f"{field_name}_substring",
            )
        else:
            value_tokens = set(value_key.split())
            overlap = query_tokens & value_tokens
            if overlap:
                union = query_tokens | value_tokens
                token_score = len(overlap) / max(len(union), 1)
                candidate = (
                    "related",
                    0.66 + 0.18 * token_score,
                    f"{field_name}_token_overlap",
                )
            else:
                ratio = SequenceMatcher(None, query_key, value_key).ratio()
                # Short CJK abbreviations such as "高数" -> "高等数学" are
                # useful recommendations, while one-character coincidences are
                # too noisy to block creation.
                threshold = 0.60 if min(len(query_key), len(value_key)) >= 2 else 0.78
                if ratio >= threshold:
                    candidate = (
                        "related",
                        0.55 + 0.25 * ratio,
                        f"{field_name}_lexical_similarity",
                    )

        if candidate is not None and (best is None or candidate[1] > best[1]):
            best = candidate
    return best


def match_catalog_items(
    query: str,
    items: Iterable[T],
    *,
    fields_for_item,
) -> list[CatalogMatch[T]]:
    """Return deterministically ranked matches for ``query``."""

    matches: list[CatalogMatch[T]] = []
    for item in items:
        classification = classify_catalog_match(query, fields_for_item(item))
        if classification is None:
            continue
        kind, score, reason = classification
        matches.append(CatalogMatch(
            item=item,
            match_kind=kind,
            score=round(score, 4),
            reason=reason,
        ))
    matches.sort(key=lambda match: (
        0 if match.match_kind == "exact" else 1,
        -match.score,
        str(fields_for_item(match.item)),
    ))
    return matches
