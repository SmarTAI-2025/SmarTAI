"""Canonical upload metadata and role policy (no application startup)."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import math
import stat
import struct
from pathlib import PurePosixPath
from typing import BinaryIO
from types import MappingProxyType
import zipfile
import zlib
from xml.parsers import expat


MIB = 1024 * 1024
KNOWLEDGE_MAX_BYTES = 64 * MIB
QUESTION_SOURCE_MAX_BYTES = 5 * MIB
INDIVIDUAL_SUBMISSION_MAX_BYTES = 64 * MIB
COURSE_MATERIAL_FILENAME_MAX_CHARACTERS = 240

DOCUMENT_EXTENSIONS = frozenset({"pdf", "txt", "md", "markdown", "docx",
                                 "jpg", "jpeg", "png", "bmp", "tif", "tiff", "webp"})
ROLE_EXTENSIONS = MappingProxyType({
    "course_knowledge": DOCUMENT_EXTENSIONS,
    "problem_reference": DOCUMENT_EXTENSIONS,
    "rubric": DOCUMENT_EXTENSIONS,
    "programming_tests": DOCUMENT_EXTENSIONS | {"json", "csv"},
    "student_submission": DOCUMENT_EXTENSIONS,
})
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_BINARY_MEDIA = {
    "pdf": "application/pdf", "png": "image/png",
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "bmp": "image/bmp", "tif": "image/tiff", "tiff": "image/tiff", "webp": "image/webp",
    "docx": DOCX_MIME,
}
_OFFICE_MEMBERS = frozenset({"[Content_Types].xml", "_rels/.rels", "word/document.xml"})
_PURPOSE_ROLES = {
    "problems": "problem_reference", "reference": "problem_reference",
    "rubric": "rubric", "test_cases": "programming_tests",
    "submissions": "student_submission", "knowledge": "course_knowledge",
}


@dataclass(frozen=True)
class ValidatedUpload:
    file_name: str
    media_type: str
    extension: str
    sha256: str


class UploadContractError(ValueError):
    """Stable, content-free failure projected by the caller's API boundary."""

    def __init__(self, code: str, *, message: str | None = None, **details):
        self.code = code
        self.details = details
        super().__init__(message or code)


def upload_role_for_purpose(purpose: str) -> str:
    try:
        return _PURPOSE_ROLES[purpose]
    except KeyError:
        raise UploadContractError("invalid_source_role") from None


def _limits() -> dict[str, int]:
    # Lazy imports reuse existing byte/filename ceilings without starting the
    # application or making file_processing depend on this module at import time.
    from backend.rag import chunker
    from backend.tools import file_processing as files
    from backend.config import settings

    return {
        "knowledge_max_bytes": min(KNOWLEDGE_MAX_BYTES, chunker.MAX_FILE_BYTES),
        "question_source_max_bytes": QUESTION_SOURCE_MAX_BYTES,
        "individual_submission_max_bytes": INDIVIDUAL_SUBMISSION_MAX_BYTES,
        "image_max_bytes": min(10 * MIB, settings.ocr_max_image_bytes),
        "submission_upload_max_bytes": min(100 * MIB, files.SUBMISSION_UPLOAD_MAX_BYTES),
        "archive_max_files": min(500, files.SUBMISSION_ARCHIVE_MAX_FILES),
        "archive_max_expanded_bytes": min(100 * MIB, files.SUBMISSION_ARCHIVE_MAX_EXPANDED_BYTES),
        "archive_max_member_bytes": min(5 * MIB, files.SUBMISSION_ARCHIVE_MAX_MEMBER_BYTES),
        "source_filename_max_characters": min(512, files.MAX_SOURCE_FILENAME_CHARACTERS),
        "course_material_filename_max_characters": min(COURSE_MATERIAL_FILENAME_MAX_CHARACTERS, files.MAX_SOURCE_FILENAME_CHARACTERS),
    }


def _role_max_bytes(role: str, limits: dict[str, int]) -> int:
    if role == "course_knowledge":
        return limits["knowledge_max_bytes"]
    if role == "student_submission":
        return limits["submission_upload_max_bytes"]
    return limits["question_source_max_bytes"]


def upload_limit(name: str, maximum: int | None = None) -> int:
    """Effective canonical ceiling, preserving a caller's stricter legacy cap."""
    value = _limits()[name]
    return min(value, maximum) if maximum is not None else value


def get_upload_contract() -> dict:
    """Return fresh policy projections; individual/ZIP limits remain distinct."""
    limits = _limits()
    return {
        "contract_version": 1,
        "roles": {
            role: {
                "extensions": sorted(extensions),
                "max_file_bytes": _role_max_bytes(role, limits),
                "allow_vision": role not in {"rubric", "programming_tests"},
                "allow_vision_opt_in": role in {"rubric", "programming_tests"},
                "allow_native_text": True,
            }
            for role, extensions in ROLE_EXTENSIONS.items()
        },
        "limits": limits,
    }


def _check_word_part(name: str, stream: BinaryIO) -> None:
    """Validate the OPC declarations and main story without building an XML tree."""
    types = "http://schemas.openxmlformats.org/package/2006/content-types"
    rels = "http://schemas.openxmlformats.org/package/2006/relationships"
    words = {"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
             "http://purl.oclc.org/ooxml/wordprocessingml/main"}
    office_rels = {"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
                   "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"}
    expected_roots = {"[Content_Types].xml": {types + "}Types"}, "_rels/.rels": {rels + "}Relationships"},
                      "word/document.xml": {ns + "}document" for ns in words}}
    main_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    depth = matches = 0
    root = ""
    def start(tag, attrs):
        nonlocal depth, root, matches
        depth += 1
        if depth > 128:
            raise ValueError("XML depth")
        if depth == 1:
            root = tag
            if tag not in expected_roots[name]:
                raise ValueError("office XML root")
        if depth != 2:
            return
        if name == "word/document.xml" and tag == root.removesuffix("document") + "body":
            matches += 1
        elif name == "[Content_Types].xml" and tag == types + "}Override" and attrs.get("PartName") == "/word/document.xml":
            if attrs.get("ContentType") != main_type:
                raise ValueError("wrong main part type")
            matches += 1
        elif name == "_rels/.rels" and tag == rels + "}Relationship" and attrs.get("Type") in office_rels:
            if (attrs.get("Target") not in {"word/document.xml", "/word/document.xml"}
                    or attrs.get("TargetMode", "Internal") != "Internal"):
                raise ValueError("wrong main part target")
            matches += 1
    def end(_tag):
        nonlocal depth
        depth -= 1
    def reject_doctype(*_args):
        raise ValueError("DTD in Office XML")
    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.StartDoctypeDeclHandler = reject_doctype
    for block in iter(lambda: stream.read(65536), b""):
        parser.Parse(block, False)
    parser.Parse(b"", True)
    if matches != 1:
        raise ValueError("missing or ambiguous main part")


def validate_docx_package(data: bytes) -> None:
    limits = _limits()
    try:
        # Bound central-directory allocations *before* ZipFile builds ZipInfo objects.
        # Classic single-disk ZIP is sufficient at these document size limits.
        end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
        if end < 0 or not data.startswith(b"PK\x03\x04"):
            raise ValueError("not a Word ZIP")
        disk, directory_disk, local_count, count, size, offset, comment = struct.unpack_from("<4H2IH", data, end + 4)
        if (disk or directory_disk or local_count != count or offset + size != end or end + 22 + comment != len(data)):
            raise ValueError("unexpected ZIP directory")
        if count > limits["archive_max_files"] or size > 512 * 1024:
            raise UploadContractError("source_docx_limit_exceeded")
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            infos = package.infolist()
            names = set()
            if len(infos) != count:
                raise ValueError("wrong ZIP entry count")
            if sum(info.file_size for info in infos) > limits["archive_max_expanded_bytes"]:
                raise UploadContractError("source_docx_limit_exceeded")
            for info in infos:
                if info.file_size > limits["archive_max_member_bytes"]:
                    raise UploadContractError("source_docx_limit_exceeded")
                path = info.filename.rstrip("/")
                if (not path or path.casefold() in names or info.filename != info.orig_filename
                        or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/"))
                        or path.casefold().endswith("/vbaproject.bin")
                        or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1
                        or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    raise ValueError("unsafe or oversized office member")
                names.add(path.casefold())
            for name in _OFFICE_MEMBERS:
                info = package.getinfo(name)
                if info.is_dir() or not info.file_size:
                    raise ValueError("missing Word part")
                with package.open(info) as stream:
                    _check_word_part(name, stream)
    except UploadContractError:
        raise
    except (KeyError, OSError, ValueError, RuntimeError, EOFError, struct.error,
            expat.ExpatError, zlib.error, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise UploadContractError("source_docx_invalid") from None


def _validate_json(data: bytes) -> None:
    def reject_constant(value):
        raise ValueError("non-finite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    try:
        json.loads(data.decode("utf-8-sig"), parse_constant=reject_constant, parse_float=finite_float)
    except (UnicodeError, ValueError, RecursionError):
        raise UploadContractError("programming_tests_json_invalid") from None


def validate_upload(role: str, file_name: str, data: bytes, *, from_library: bool = False) -> ValidatedUpload:
    """Validate one ordinary file. ZIP container handling is a separate boundary."""
    extensions = ROLE_EXTENSIONS.get(role)
    if role == "legacy_problem":
        extensions = ROLE_EXTENSIONS["problem_reference"] | {"csv", "rst"}
    if extensions is None:
        raise UploadContractError("invalid_source_role")
    extension = PurePosixPath(file_name).suffix.lower().lstrip(".")
    if extension not in extensions:
        raise UploadContractError("submission_source_unsupported" if role == "student_submission" else "source_type_not_allowed", role=role)
    limits = _limits()
    if len(file_name) > limits["source_filename_max_characters"]:
        raise UploadContractError("source_filename_too_long")
    if not data:
        raise UploadContractError("submission_source_empty" if role == "student_submission" else "source_empty")
    max_bytes = limits["knowledge_max_bytes"] if from_library else _role_max_bytes(role, limits)
    if extension in {"png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"}:
        max_bytes = min(max_bytes, limits["image_max_bytes"])
    if len(data) > max_bytes:
        raise UploadContractError("submission_source_too_large" if role == "student_submission" else "source_too_large", max_bytes=max_bytes)

    if extension == "docx":
        validate_docx_package(data)
    from backend.tools.file_processing import inspect_upload_content
    inspection = inspect_upload_content(data, file_name)
    media_type = inspection.content_type
    if media_type == "application/pdf" and extension in {"txt", "md", "markdown", "csv", "rst", "json"}:
        from backend.tools.file_processing import _looks_like_text
        if _looks_like_text(data):
            media_type = "text/plain"  # Final acceptance uses the bounded PDF probe.
    expected_binary = _BINARY_MEDIA.get(extension)
    if (expected_binary and media_type != expected_binary) or (not expected_binary and not media_type.startswith("text/")):
        raise UploadContractError("submission_source_content_type_mismatch")
    if extension == "json":
        _validate_json(data)
        media_type = "application/json"
    return ValidatedUpload(file_name, media_type, extension, hashlib.sha256(data).hexdigest())


def validate_submission_upload(file_name: str, data: bytes) -> None:
    """Validate supported legacy archives separately from ordinary sources."""
    from backend.tools.file_processing import ARCHIVE_EXTENSIONS, inspect_upload_content
    if not file_name.lower().endswith(ARCHIVE_EXTENSIONS):
        validate_upload("student_submission", file_name, data)
        return
    limits = _limits()
    if len(file_name) > limits["source_filename_max_characters"]:
        raise UploadContractError("source_filename_too_long")
    if not data:
        raise UploadContractError("submission_source_empty")
    if len(data) > limits["submission_upload_max_bytes"]:
        raise UploadContractError("submission_source_too_large", max_bytes=limits["submission_upload_max_bytes"])
    inspection = inspect_upload_content(data, file_name)
    if inspection.mismatch or inspection.content_type not in {
        "application/zip", "application/vnd.rar", "application/x-7z-compressed",
        "application/x-tar", "application/gzip", "application/x-bzip2",
    } or (file_name.lower().endswith(".zip") and not zipfile.is_zipfile(io.BytesIO(data))):
        raise UploadContractError("submission_archive_invalid")
    # Only fixed framing/CRC here. Opening 7z or compressed TAR containers can
    # decompress their metadata before member limits are applied. Full parsing
    # remains in the existing bounded member pipeline, never this preflight.
    try:
        if inspection.content_type == "application/x-7z-compressed":
            crc, offset, size, next_crc = struct.unpack("<IQQI", data[8:32])
            start = 32 + offset
            if (zlib.crc32(data[12:32]) != crc or start > len(data)
                    or size > len(data) - start or zlib.crc32(memoryview(data)[start:start + size]) != next_crc):
                raise ValueError("invalid fixed header")
        elif inspection.content_type == "application/vnd.rar":
            if data.startswith(b"Rar!\x1a\x07\x00"):
                crc, kind, _flags, size = struct.unpack_from("<HBHH", data, 7)
                if (kind != 0x73 or size < 13 or size > len(data) - 7
                        or zlib.crc32(memoryview(data)[9:7 + size]) & 0xFFFF != crc):
                    raise ValueError("invalid main header")
            else:
                crc = struct.unpack_from("<I", data, 8)[0]
                size, position = 0, 12
                for shift in range(0, 70, 7):
                    byte = data[position]
                    position += 1
                    size |= (byte & 0x7F) << shift
                    if not byte & 0x80:
                        break
                else:
                    raise ValueError("invalid header length")
                if (size > len(data) - position
                        or zlib.crc32(memoryview(data)[12:position + size]) != crc):
                    raise ValueError("invalid main header")
    except (ValueError, IndexError, struct.error):
        raise UploadContractError("submission_archive_invalid") from None
