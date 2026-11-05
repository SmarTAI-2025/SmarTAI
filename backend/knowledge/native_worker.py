"""Bounded native Office/text extraction in a killable child process."""
from __future__ import annotations

from io import BytesIO
import json
import re
import struct
import sys
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
import zlib

MAX_BYTES = 64 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024


def _word_attribute(node, name):
    return next((value for key, value in node.attrib.items() if key.rsplit("}", 1)[-1] == name), None)


def _verify_word_members(archive, body):
    # ZipExtFile stops at the *declared* uncompressed length. Verify the actual
    # deflate end as well, so forged size/CRC fields cannot hide a suffix.
    raw = memoryview(body)
    for part in archive.infolist():
        with archive.open(part):  # checks name, local header, and overlapping members
            pass
        flags, method = struct.unpack_from("<HH", raw, part.header_offset + 6)
        if flags != part.flag_bits or method != part.compress_type:
            raise ValueError("inconsistent local Word header")
        if not flags & 8:  # Otherwise CRC/sizes follow the part in a data descriptor.
            crc, compressed_size, file_size = struct.unpack_from("<III", raw, part.header_offset + 14)
            if (crc != part.CRC or compressed_size not in {part.compress_size, 0xffffffff}
                    or file_size not in {part.file_size, 0xffffffff}):
                raise ValueError("inconsistent local Word sizes")
        name_size, extra_size = struct.unpack_from("<HH", raw, part.header_offset + 26)
        start = part.header_offset + 30 + name_size + extra_size
        compressed = raw[start:start + part.compress_size]
        if part.compress_type == ZIP_STORED:
            content = compressed
        elif part.compress_type == ZIP_DEFLATED:
            decoder = zlib.decompressobj(-15)
            content = decoder.decompress(compressed, part.file_size + 1)
            if not decoder.eof or decoder.unused_data:
                raise ValueError("incomplete or trailing deflate stream")
        else:
            raise ValueError("unsupported Word compression")
        if len(content) != part.file_size or zlib.crc32(content) != part.CRC:
            raise ValueError("Word part size or CRC")


def _word_xml(data):
    # lxml is also used by python-docx. Disable external resolution and reject
    # DTDs, including UTF-16 input. Normal Word parts do not require either.
    from lxml import etree
    root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, load_dtd=False,
                            no_network=True, remove_comments=True, remove_pis=True))
    if root.getroottree().docinfo.doctype:
        raise ValueError("DTD in Word part")
    return root


_SEMANTIC_FORMATTING = {"numPr", "vertAlign", "vanish", "webHidden", "strike", "dstrike",
                        "position", "ind", "caps", "smallCaps", "bidi", "rtl"}


def _unread_word_styles(root, archive):
    """Inspect used paragraph styles only, including inherited/default ones.

    Native extraction does not implement Word's list-counter rendering. An
    unused ListNumber definition in a normal Word template is not a blocker.
    """
    styles, default_style = {}, None
    if "word/styles.xml" in archive.namelist():
        definition = _word_xml(archive.read("word/styles.xml"))
        for node in definition:
            local = node.tag.rsplit("}", 1)[-1]
            if local == "docDefaults" and any(item.tag.rsplit("}", 1)[-1] in _SEMANTIC_FORMATTING for item in node.iter()):
                return True
            if local == "style" and _word_attribute(node, "type") in {"paragraph", "character", "table"}:
                style_id = _word_attribute(node, "styleId")
                styles[style_id] = node
                if _word_attribute(node, "type") == "paragraph" and _word_attribute(node, "default") in {"1", "true"}:
                    default_style = style_id
    references = set()
    for paragraph in root.iter():
        if paragraph.tag.rsplit("}", 1)[-1] in {"rStyle", "tblStyle"}:
            references.add(_word_attribute(paragraph, "val"))
        elif paragraph.tag.rsplit("}", 1)[-1] == "p":
            style = next((node for node in paragraph.iter() if node.tag.rsplit("}", 1)[-1] == "pStyle"), None)
            references.add(_word_attribute(style, "val") if style is not None else default_style)
    for style_id in references:
        seen = set()
        while style_id:
            if style_id in seen or style_id not in styles:
                return True  # Broken/cyclic style chains cannot prove completeness.
            seen.add(style_id)
            style = styles[style_id]
            if any(node.tag.rsplit("}", 1)[-1] in _SEMANTIC_FORMATTING for node in style.iter()):
                return True
            base = next((node for node in style if node.tag.rsplit("}", 1)[-1] == "basedOn"), None)
            style_id = _word_attribute(base, "val") if base is not None else None
    return False


def _unread_word_content(root):
    children = {
        "document": {"body"}, "body": {"p", "tbl", "sectPr"},
        "tbl": {"tblPr", "tblGrid", "tr"}, "tr": {"trPr", "tc"}, "tc": {"tcPr", "p", "tbl"},
        "p": {"pPr", "r", "proofErr", "bookmarkStart", "bookmarkEnd"},
        "r": {"rPr", "t", "tab", "br", "cr", "noBreakHyphen", "softHyphen", "lastRenderedPageBreak"},
    }
    namespace = root.tag.rsplit("}", 1)[0] + "}"
    for node in root.iter():
        local = node.tag.removeprefix(namespace)
        if (local != "t" and node.text and node.text.strip()) or (node.tail and node.tail.strip()):
            return True
        if local in _SEMANTIC_FORMATTING or local in {
            "hMerge", "vMerge", "gridSpan", "gridBefore", "gridAfter", "headerReference",
            "footerReference", "pPrChange", "rPrChange", "tblPrChange", "tcPrChange", "sectPrChange",
        }:
            return True
        if local in children and any(child.tag.removeprefix(namespace) not in children[local] for child in node):
            return True
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
            _verify_word_members(archive, body)
            # Relationship targets need not use conventional filenames.
            if "word/_rels/document.xml.rels" in archive.namelist():
                relationships = _word_xml(archive.read("word/_rels/document.xml.rels"))
                for rel in relationships:
                    kind_name = rel.get("Type", "").rsplit("/", 1)[-1]
                    if kind_name in {"header", "footer", "footnotes", "endnotes", "comments", "aFChunk",
                                     "oleObject", "package", "image", "chart", "diagramData"}:
                        warnings.append("office_supplementary_text_unprocessed")
                    if kind_name == "styles" and (rel.get("Target") not in {"styles.xml", "/word/styles.xml"}
                            or "word/styles.xml" not in archive.namelist()):
                        warnings.append("office_structure_native_unverified")
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
            root = _word_xml(archive.read(name)) if kind == ".docx" else ET.fromstring(archive.read(name))
            if kind == ".docx" and preserve_docx_layout:
                # This is a native reader, not a claim to have OCRed pictures,
                # evaluated fields, or resolved tracked changes/embedded parts.
                if _unread_word_content(root) or _unread_word_styles(root, archive):
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
    max_input = 100 * 1024 * 1024 if sys.argv[1] == ".docx" else MAX_BYTES
    body = sys.stdin.buffer.read(max_input + 1)
    if len(body) > max_input:
        raise ValueError("input budget")
    if sys.argv[1] == ".docx":
        from lxml.etree import XMLSyntaxError
        try:
            result = extract(".docx", body, preserve_docx_layout=True)
            if len(sys.argv) > 2 and sys.argv[2].isdigit() and sum(map(len, result["units"])) > int(sys.argv[2]):
                result = {"error": "source_text_too_large"}
        except (BadZipFile, KeyError, struct.error, XMLSyntaxError, ValueError, zlib.error, EOFError, NotImplementedError):
            result = {"error": "source_docx_invalid"}
    else:
        result = extract(sys.argv[1], body)
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
