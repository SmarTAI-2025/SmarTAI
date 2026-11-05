"""Bounded native Office/text extraction in a killable child process."""
from __future__ import annotations

from io import BytesIO
import json
import re
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

MAX_BYTES = 64 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024


def _word_attribute(node, name):
    return next((value for key, value in node.attrib.items() if key.rsplit("}", 1)[-1] == name), None)


def _docx_numbering_unread(root, archive):
    """Inspect used paragraph styles only, including inherited/default ones.

    Native extraction does not implement Word's list-counter rendering. An
    unused ListNumber definition in a normal Word template is not a blocker.
    """
    styles, default_style = {}, None
    if "word/styles.xml" in archive.namelist():
        definition = ET.fromstring(archive.read("word/styles.xml"))
        for node in definition:
            local = node.tag.rsplit("}", 1)[-1]
            if local == "docDefaults" and any(item.tag.rsplit("}", 1)[-1] == "numPr" for item in node.iter()):
                return True
            if local == "style" and _word_attribute(node, "type") == "paragraph":
                style_id = _word_attribute(node, "styleId")
                styles[style_id] = node
                if _word_attribute(node, "default") in {"1", "true"}:
                    default_style = style_id
    references = set()
    for paragraph in root.iter():
        if paragraph.tag.rsplit("}", 1)[-1] == "p":
            style = next((node for node in paragraph.iter() if node.tag.rsplit("}", 1)[-1] == "pStyle"), None)
            references.add(_word_attribute(style, "val") if style is not None else default_style)
    for style_id in references:
        seen = set()
        while style_id:
            if style_id in seen or style_id not in styles:
                return True  # Broken/cyclic style chains cannot prove completeness.
            seen.add(style_id)
            style = styles[style_id]
            if any(node.tag.rsplit("}", 1)[-1] == "numPr" for node in style.iter()):
                return True
            base = next((node for node in style if node.tag.rsplit("}", 1)[-1] == "basedOn"), None)
            style_id = _word_attribute(base, "val") if base is not None else None
    return False


def _docx_blocks(node):
    """Keep native paragraphs, explicit code whitespace and table cell order."""
    local = node.tag.rsplit("}", 1)[-1]
    if local == "p":
        characters = {"tab": "\t", "br": "\n", "cr": "\n", "noBreakHyphen": "\u2011", "softHyphen": "\u00ad"}
        yield "".join(child.text or "" if child.tag.rsplit("}", 1)[-1] == "t"
                      else characters.get(child.tag.rsplit("}", 1)[-1], "")
                      for child in node.iter())
    elif local == "tbl":
        for row in node:
            if row.tag.rsplit("}", 1)[-1] == "tr":
                yield "\t".join("\n".join(_docx_blocks(cell)) for cell in row
                                if cell.tag.rsplit("}", 1)[-1] == "tc")
    else:
        for child in node:
            yield from _docx_blocks(child)


def extract(kind, body, *, preserve_docx_layout=False):
    warnings = []
    if kind not in {".docx", ".pptx"}:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = body.decode("gb18030")
        if len(text) > MAX_TEXT:
            raise ValueError("native text budget")
        return dict(units=[text[i:i + 80000] for i in range(0, len(text), 80000)] or [""],
                    unit="section", warning_codes=[])
    with ZipFile(BytesIO(body)) as archive:
        files = archive.infolist()
        if len(files) > 10000 or sum(f.file_size for f in files) > 128 * 1024 * 1024:
            raise ValueError("expanded document budget")
        if any(f.file_size > 32 * 1024 * 1024 for f in files):
            raise ValueError("individual XML/media budget")
        if any("/media/" in f.filename for f in files):
            warnings.append("office_embedded_visuals_unprocessed")
        if kind == ".docx":
            names = ["word/document.xml"]
            unit = "section"
            if any(re.match(r"word/(header|footer|footnotes|endnotes)", f.filename) for f in files):
                warnings.append("office_supplementary_text_unprocessed")
        else:
            names = sorted((f.filename for f in files if re.fullmatch(r"ppt/slides/slide\d+\.xml", f.filename)),
                           key=lambda name: int(re.search(r"slide(\d+)\.xml", name).group(1)))
            unit = "slide"
        units, chars = [], 0
        for name in names:
            root = ET.fromstring(archive.read(name))
            if kind == ".docx" and preserve_docx_layout:
                # This is a native reader, not a claim to have OCRed pictures,
                # evaluated fields, or resolved tracked changes/embedded parts.
                unsupported = {"drawing", "pict", "object", "altChunk", "oMath", "oMathPara", "del", "ins",
                               "gridSpan", "vMerge", "sdt", "AlternateContent", "numPr", "sym",
                               "moveFrom", "moveTo", "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
                               "fldSimple", "fldChar", "instrText", "commentReference", "commentRangeStart", "commentRangeEnd"}
                if (any(node.tag.rsplit("}", 1)[-1] in unsupported for node in root.iter())
                        or _docx_numbering_unread(root, archive)):
                    warnings.append("office_structure_native_unverified")
                if any(info.filename.startswith("word/comments") for info in files):
                    warnings.append("office_supplementary_text_unprocessed")
                text = "\n".join(_docx_blocks(root))
                if len(text) > MAX_TEXT:
                    raise ValueError("native text budget")
                units.extend([text[i:i + 80000] for i in range(0, len(text), 80000)] or [""])
                continue
            paragraphs = []
            for node in root.iter():
                local = node.tag.rsplit("}", 1)[-1]
                if local in {"oMath", "tbl"}:
                    warnings.append("office_structure_native_unverified")
                if local == "p":
                    paragraphs.append("".join(child.text or "" for child in node.iter()
                                              if child.tag.rsplit("}", 1)[-1] == "t"))
            text = "\n".join(paragraphs)
            chars += len(text)
            if chars > MAX_TEXT:
                raise ValueError("native text budget")
            units.extend([text[i:i + 80000] for i in range(0, len(text), 80000)] or [""])
            if len(text) > 80000 and unit == "slide":
                unit = "section"
                warnings.append("oversized_slide_split")
    return dict(units=units or [""], unit=unit, warning_codes=sorted(set(warnings)))


def main():
    if sys.platform.startswith("linux"):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (50, 50))
    body = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("input budget")
    sys.stdout.write(json.dumps(extract(sys.argv[1], body,
        preserve_docx_layout="--preserve-docx-layout" in sys.argv[2:]), ensure_ascii=True))


if __name__ == "__main__":
    main()
