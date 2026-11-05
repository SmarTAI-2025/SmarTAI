"""Decode Python uploads according to their source encoding before mapping/grading.

The submission store owns the untouched upload and its byte hash. The decoded
student answer keeps its original comments and line endings. The runner writes
UTF-8 files, so it receives a separate copy with an equivalent UTF-8 cookie.
"""
from __future__ import annotations

import codecs
import io
import re
import tokenize


class PythonSourceEncodingError(ValueError):
    def __init__(self):
        super().__init__("programming_source_encoding_unavailable")


# ASCII-compatible source encodings with data-only text decoders. Transforming
# codecs such as unicode_escape, UTF-7, base64 or compression are not input
# formats for this grading workflow.
_TEXT_CODECS = {
    "ascii", "utf-8", "utf-8-sig", "gb2312", "gbk", "gb18030", "big5", "big5hkscs",
    "shift_jis", "cp932", "cp950", "cp949", "euc_kr", "euc_jp", "koi8-r", "koi8-u",
    "mac-roman", "cp874", *(f"cp{i}" for i in range(1250, 1259)),
    *(f"iso8859-{i}" for i in (*range(1, 12), *range(13, 17))),
}
# CPython recognizes a cookie on line 2 only when line 1 is a comment/blank.
_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)", re.ASCII)
_COMMENT_OR_BLANK = re.compile(r"^[ \t\f]*(?:#|$)")
_PHYSICAL_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n|$)")
_RAW_LINE = re.compile(rb"[^\r\n]*(?:\r\n|\r|\n|$)")


def _source_codec(name: str | None, bom=False) -> str:
    # Ask the Python tokenizer to normalize its cookie aliases and check BOM
    # compatibility. The *original comment* may itself contain non-UTF8 bytes,
    # so supply only the ASCII directive, not a prematurely decoded comment.
    header = b"# coding: " + name.encode("ascii") + b"\n" if name else b"\n"
    if bom:
        header = codecs.BOM_UTF8 + header
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(header).readline)
        codec = codecs.lookup(encoding).name
    except (SyntaxError, UnicodeError, LookupError, ValueError):
        raise PythonSourceEncodingError() from None
    if codec not in _TEXT_CODECS:
        raise PythonSourceEncodingError()
    return codec


def _cookie_span(source: str):
    for index, part in enumerate(_PHYSICAL_LINE.finditer(source)):
        if index == 2:
            break
        line = part.group().rstrip("\r\n")
        cookie = _COOKIE.match(line)
        if cookie:
            a, b = cookie.span(1)
            return part.start() + a, part.start() + b
        if not _COMMENT_OR_BLANK.match(line):
            break
    return None


def decode_python_submission(data: bytes) -> str:
    """Read a declared source encoding strictly, with the Python 3 UTF-8 default."""
    bom = data.startswith(codecs.BOM_UTF8)
    start = len(codecs.BOM_UTF8) if bom else 0
    # Latin-1 is a 1:1 view of header bytes. Every allowed encoding represents
    # the cookie in ASCII. Decode the actual submission only after finding it.
    lines = _RAW_LINE.finditer(data, start)
    first = next(lines)
    second = next(lines, None)
    end = (second or first).end()
    header = data[start:end].decode("latin-1")
    span = _cookie_span(header)
    name = header[span[0]:span[1]] if span else None
    codec = _source_codec(name, bom)
    try:
        text = data.decode(codec, errors="strict")
    except UnicodeError:
        raise PythonSourceEncodingError() from None
    if text.startswith("\ufeff") or any(
        ch not in "\t\n\r\f" and (ord(ch) < 32 or 127 <= ord(ch) <= 159) for ch in text
    ):
        raise PythonSourceEncodingError()
    return text


def python_execution_source(source: str) -> str:
    """Build a verified UTF-8 execution copy without changing program tokens."""
    if source.startswith("\ufeff"):
        # A decoded source reaching this helper can come from a prior UTF-8
        # reader. A BOM conflicting with its cookie is never silently removed.
        source = decode_python_submission(source.encode("utf-8"))
    span = _cookie_span(source)
    if span is None:
        return source
    start, end = span
    declared = _source_codec(source[start:end])
    if declared in {"ascii", "utf-8", "utf-8-sig"}:
        if declared == "ascii" and not source.isascii():
            raise PythonSourceEncodingError()
        return source
    try:
        if decode_python_submission(source.encode(declared)) != source:
            raise PythonSourceEncodingError()
        copied = source[:start] + "utf-8" + source[end:]
        if decode_python_submission(copied.encode("utf-8")) != copied:
            raise PythonSourceEncodingError()
    except UnicodeError:
        raise PythonSourceEncodingError() from None
    return copied
