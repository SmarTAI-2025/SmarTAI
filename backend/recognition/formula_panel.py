"""Locate an independent crop in a printed question and compare its formula list."""

import re
import unicodedata

_HEADING = re.compile(
    r"(?m)^[ \t#*$]*(\d+(?:[ \t]*[.．][ \t]*\d+)+)(?=[①-⑳\s.:：、$（(])"
)
_FORMULA = re.compile(r"\$+([^$]+?)\$+|\\\((.*?)\\\)", re.S)


def _sections(text):
    matches = list(_HEADING.finditer(text))
    for index, item in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        yield re.sub(r"\s+", "", item[1]).replace("．", "."), text[item.end() : end]


def _items(text):
    fragments = [match[1] or match[2] for match in _FORMULA.finditer(text)]
    if not fragments:
        fragments = (
            [text.split("：")[-1].split(":")[-1]]
            if ":" in text or "：" in text
            else [text]
        )
    result = []
    for segment in fragments:
        for item in re.split(r"[,，、;；\n]", segment):
            value = re.sub(
                r"\s+|\\(?:left|right|quad|qquad|[,;!])",
                "",
                unicodedata.normalize("NFKC", item),
            )
            value = re.sub(r"\\(?:times|cdot)|\*", "", value)
            value = re.sub(r"\{([a-zA-Z0-9])\}", r"\1", value)
            value = value.strip("。.$")
            if value and re.search(r"[a-zA-Z0-9]", value):
                result.append(value)
    return result


def _scripts(item):
    atom = r"(\\?log|[a-z0-9)])([_^])(?:\{([^{}]+)\}|\(([^()]+)\)|(\\[A-Za-z]+|[a-zA-Z0-9]))"
    return [
        (op.lstrip("\\"), sign, braced or parens or bare)
        for op, sign, braced, parens, bare in re.findall(atom, item)
    ]


def _difference(whole, panel):
    left, right = _items(whole), _items(panel)
    if min(len(left), len(right)) < 3:
        return False
    if len(left) != len(right):
        # Only assert a dropped/inserted expression when the complete shorter
        # list is embedded between shared first and last mathematical anchors.
        short, long = sorted([left, right], key=len)
        remaining = iter(long)
        return (
            short[0] == long[0]
            and short[-1] == long[-1]
            and all(any(term == candidate for candidate in remaining) for term in short)
        )
    anchors = sum(a == b for a, b in zip(left, right))
    if anchors < 2 or anchors * 3 < len(right):
        return False
    for a, b in zip(left, right):
        if a == b:
            continue
        if any(
            op == other and sign == sign2 and value != value2
            for (op, sign, value), (other, sign2, value2) in zip(
                _scripts(a), _scripts(b)
            )
        ):
            return True
        if a.count("log") != b.count("log") and "log" in a and "log" in b:
            return True
    return False


def conflicting_heading(whole, panel):
    cropped = list(_sections(panel))
    if len(cropped) > 1:
        return None
    label, excerpt = cropped[0] if cropped else (None, panel)
    sources = [
        (heading, text)
        for heading, text in _sections(whole)
        if not label or heading == label
    ]
    conflicts = [heading for heading, text in sources if _difference(text, excerpt)]
    return (
        conflicts[0]
        if len(conflicts) == 1
        and len([h for h, _ in sources if h == conflicts[0]]) == 1
        else None
    )
