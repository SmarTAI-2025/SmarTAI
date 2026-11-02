"""Canonical upload metadata and role policy (no application startup)."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import math
import struct
from pathlib import PurePosixPath
from types import MappingProxyType
import zipfile
import zlib
from xml.etree import ElementTree


MIB = 1024 * 1024
KNOWLEDGE_MAX_BYTES = 64 * MIB
QUESTION_SOURCE_MAX_BYTES = 5 * MIB
INDIVIDUAL_SUBMISSION_MAX_BYTES = 64 * MIB
COURSE_MATERIAL_FILENAME_MAX_CHARACTERS = 240

ROLE_EXTENSIONS = MappingProxyType({
    "course_knowledge": frozenset({"pdf", "txt", "md", "markdown"}),
    "problem_reference": frozenset({"pdf", "txt", "md", "markdown", "docx", "jpg", "jpeg", "png", "bmp", "tif", "tiff", "webp"}),
    "rubric": frozenset({"pdf", "txt", "md", "markdown", "jpg", "jpeg", "png", "bmp", "tif", "tiff", "webp"}),
    "programming_tests": frozenset({"pdf", "txt", "md", "markdown", "json", "jpg", "jpeg", "png", "bmp", "tif", "tiff", "webp"}),
    "student_submission": frozenset({"pdf", "txt", "md", "markdown", "docx", "jpg", "jpeg", "png", "bmp", "tif", "tiff", "webp", "py", "c", "cc", "cpp", "csv", "tsv", "rst"}),
})
_BINARY_MEDIA = {
    "pdf": "application/pdf", "png": "image/png",
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "bmp": "image/bmp", "tif": "image/tiff", "tiff": "image/tiff", "webp": "image/webp",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
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
                "allow_native_text": role == "student_submission",
            }
            for role, extensions in ROLE_EXTENSIONS.items()
        },
        "limits": limits,
    }


def _validate_docx(data: bytes, limits: dict[str, int]) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            infos = package.infolist()
            names = [entry.filename for entry in infos]
            if len(infos) > limits["archive_max_files"] or not _OFFICE_MEMBERS.issubset(names):
                raise ValueError("office structure")
            if sum(entry.file_size for entry in infos) > limits["archive_max_expanded_bytes"]:
                raise ValueError("office size")
            for name in _OFFICE_MEMBERS:
                if names.count(name) != 1:
                    raise ValueError("ambiguous office member")
                info = package.getinfo(name)
                if info.is_dir() or info.flag_bits & 1 or info.file_size <= 0:
                    raise ValueError("unreadable office member")
                if info.file_size > limits["archive_max_member_bytes"]:
                    raise ValueError("office member size")
                # Read only required, bounded metadata/document members to
                # verify decompression/CRC. This is not a DOCX text reader.
                with package.open(info) as stream:
                    member = stream.read(limits["archive_max_member_bytes"] + 1)
                    if len(member) != info.file_size:
                        raise ValueError("office member length")
                # A renamed ordinary ZIP containing three arbitrary payloads
                # is not an Office package. Check structure, never extract text.
                root = ElementTree.fromstring(member)
                expected = {
                    "[Content_Types].xml": {"{http://schemas.openxmlformats.org/package/2006/content-types}Types"},
                    "_rels/.rels": {"{http://schemas.openxmlformats.org/package/2006/relationships}Relationships"},
                    "word/document.xml": {
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document",
                        "{http://purl.oclc.org/ooxml/wordprocessingml/main}document",
                    },
                }
                if root.tag not in expected[name]:
                    raise ValueError("office XML root")
    except (OSError, ValueError, RuntimeError, NotImplementedError, EOFError,
            ElementTree.ParseError, zlib.error, zipfile.BadZipFile, zipfile.LargeZipFile):
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


def validate_upload(role: str, file_name: str, data: bytes) -> ValidatedUpload:
    """Validate one ordinary file. ZIP container handling is a separate boundary."""
    extensions = ROLE_EXTENSIONS.get(role)
    if extensions is None:
        raise UploadContractError("invalid_source_role")
    extension = PurePosixPath(file_name).suffix.lower().lstrip(".")
    if extension not in extensions and role != "student_submission":
        raise UploadContractError("source_type_not_allowed", role=role)
    limits = _limits()
    if len(file_name) > limits["source_filename_max_characters"]:
        raise UploadContractError("source_filename_too_long")
    if not data:
        raise UploadContractError("submission_source_empty" if role == "student_submission" else "source_empty")
    max_bytes = _role_max_bytes(role, limits)
    if extension in {"png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"}:
        max_bytes = min(max_bytes, limits["image_max_bytes"])
    if len(data) > max_bytes:
        raise UploadContractError("submission_source_too_large" if role == "student_submission" else "source_too_large", max_bytes=max_bytes)

    if extension == "docx":
        _validate_docx(data, limits)
    from backend.tools.file_processing import inspect_upload_content
    inspection = inspect_upload_content(data, file_name)
    # Keep main's native-text student input without decoding an over-limit body.
    # This grants reading, never code execution or another business role.
    native_student_text = (role == "student_submission" and not inspection.mismatch
                           and inspection.content_type.startswith("text/"))
    if extension not in extensions and not native_student_text:
        raise UploadContractError("submission_source_type_not_allowed", role=role)
    media_type = inspection.content_type
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
