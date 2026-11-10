"""Ensure freshly generated reference answers select one primary strategy per target."""
import re

from backend.services.question_structure import _allocation_marker_pattern

_METHOD_HEADING = re.compile(
    r"(?:^|[。；;\n])\s*(?:#{1,6}\s*)?(?:\*\*)?[（(【]?\s*"
    r"(?:(?:解法|方法|证法|证明方法|思路|法)\s*([一二三四五六七八九十\dA-C]+)|"
    r"(?:method|proof|approach|solution)\s+([1-9A-C])(?=\b)|"
    r"(?P<other>另一(?:种)?(?:解法|方法|证法)|另解|alternative (?:method|proof|solution)))"
    r"\s*(?:\*\*)?(?=[：:、.．)）】\s]|$)", re.I,
)
_MULTIPLE_REQUIRED = re.compile(
    r"(?:[两二三多几2-9]|至少\s*[两二三2-9])\s*种(?:不同)?(?:方法|思路|解法|证法)|"
    r"(?:different|two|both|multiple|three)\s+(?:different\s+)?(?:methods|approaches|proofs|solutions)|"
    r"分别(?:用|使用|采用)[^。；;\n]{1,60}(?:法|定理|公式)", re.I,
)


# A second solution can be introduced inside prose. Require a new strategy
# and a fresh solution, not merely a theorem or a check of the preceding work.
_INLINE_PARALLEL = re.compile(
    r"(?:[。；;，,\n]\s*)(?:(?:也可以|还可以|也可|还可|或者可以|另一种解法是)\s*"
    r"(?:改用|用|采用|利用)[^。；;\n]{1,32}?(?:求解|解此方程|求根|解得|得根)|"
    r"(?:用|采用|利用)[^。；;\n]{1,24}?(?:法|公式)(?:同样|也)(?:可)?(?:解得|得到|得出|求得))|"
    r"[.;]\s*alternatively,?\s+(?:solve|we can solve)\b", re.I,
)


def validate_generated_reference(stem, structure, answer, teacher=""):
    if hasattr(structure, "model_dump"):
        structure = structure.model_dump()
    structure = structure or {}
    parts = structure.get("subparts") or []
    shared = structure.get("shared_stem", "") if parts else stem
    if _MULTIPLE_REQUIRED.search(shared) or (not parts and _MULTIPLE_REQUIRED.search(teacher)):
        return
    # Only explicit part headings in the answer move the current target.
    # A parenthesized quantity in the middle of an equation does not.
    target = ""
    scopes = {p["label"]: p["stem"] for p in parts}
    boundaries = []
    for label in scopes:
        pattern = r"(?:^|(?<=[\n。；;]))\s*(?:#{1,6}\s*)?(?:\*\*)?" + _allocation_marker_pattern(label)
        boundaries.extend((m.start(), m.end(), label) for m in re.finditer(pattern, answer, re.I))
    sections = {}
    start = 0
    for left, right, label in sorted(boundaries):
        sections.setdefault(target, []).append(answer[start:left])
        target, start = label, right
    sections.setdefault(target, []).append(answer[start:])
    for label, chunks in sections.items():
        scoped_teacher = [line for line in re.split(r"[。；;\n]", teacher)
                          if label and re.match(_allocation_marker_pattern(label), line.strip(), re.I)]
        if any(_MULTIPLE_REQUIRED.search(text) for text in (scopes.get(label, ""), *scoped_teacher)):
            continue
        methods = set()
        for chunk in chunks:
            parallel = _INLINE_PARALLEL.search(chunk)
            if parallel and chunk[:parallel.start()].strip():
                methods.update(("first", "another"))
            for heading in _METHOD_HEADING.finditer(chunk):
                if heading["other"] and (methods or chunk[:heading.start()].strip()):
                    methods.add("another")
                    methods.add("preceding")
                else:
                    methods.add((heading[1] or heading[2] or "another").casefold())
        if len(methods) > 1:
            raise ValueError(f"{label or 'question'}: choose one worked reference strategy for this target; "
                             "remove the parallel solution and keep every required subpart and proof direction.")
