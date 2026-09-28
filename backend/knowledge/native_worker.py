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


def extract(kind, body):
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
    sys.stdout.write(json.dumps(extract(sys.argv[1], body), ensure_ascii=True))


if __name__ == "__main__":
    main()
