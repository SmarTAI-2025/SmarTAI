"""Markdown helpers — LaTeX-aware rendering.

Reflex's `rx.markdown` already enables `remark-math` + `rehype-katex` by default
(see reflex_components_markdown/markdown.py:476-481), so `$inline$` and `$$display$$`
math renders correctly out of the box.

The problem is that LLM output frequently uses LaTeX-flavored delimiters
`\\(...\\)` and `\\[...\\]` instead of dollar signs. We normalize those to
the markdown-math syntax so KaTeX picks them up.

Use `format_math` at the data layer (in State computed vars or in the API
response handler), since rx.markdown takes its source as a Var/string and
runs the regex in Python at state-update time, not at render time.
"""
from __future__ import annotations

import re

import reflex as rx


_INLINE_RE = re.compile(r"\\\((.*?)\\\)", re.DOTALL)
_DISPLAY_RE = re.compile(r"\\\[(.*?)\\\]", re.DOTALL)


def format_math(text) -> str:
    """Convert LaTeX delimiters to markdown-math delimiters.

    `\\(...\\)`  →  `$...$`     (inline)
    `\\[...\\]`  →  `$$...$$`   (display)

    Pass-through if `text` is not a string (e.g. None / other types).
    Safe to call on already-converted strings (no double-escaping).
    """
    if not isinstance(text, str):
        return text
    text = _DISPLAY_RE.sub(r"$$\1$$", text)
    text = _INLINE_RE.sub(r"$\1$", text)
    return text


def smart_markdown(text, **kwargs) -> rx.Component:
    """Render markdown with LaTeX support.

    `text` should already have its math delimiters normalized (call
    `format_math` upstream — typically in a State `@rx.var`). This wrapper
    just delegates to `rx.markdown`, which has KaTeX wired in by default.

    Kept as a function (rather than calling rx.markdown directly) so we have
    a single place to add per-page math configuration if we ever need it.
    """
    return rx.markdown(text, **kwargs)
