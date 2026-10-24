"""Role/content/limit behavior; no application startup or external services."""
from __future__ import annotations

import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from PIL import Image


# Independent expected values, not imported from the policy being tested.
ROLE_EXTENSIONS = {
    "course_knowledge": ("pdf", "txt", "md", "markdown"),
    "problem_reference": ("pdf", "txt", "md", "markdown", "docx", "jpg", "jpeg", "png"),
    "rubric": ("pdf", "txt", "md"),
    "programming_tests": ("pdf", "txt", "md", "json"),
    "student_submission": ("pdf", "txt", "md", "markdown", "docx", "jpg", "jpeg", "png", "py", "c", "cc", "cpp"),
}
OTHER_EXTENSIONS = ("csv", "tsv", "rst", "pptx", "bmp", "tif", "tiff", "webp", "exe", "zip", "rar", "7z", "tar")


def policy():
    return importlib.import_module("backend.services.upload_contract")


def office_package(*, missing=None):
    members = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>',
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as package:
        for name, body in members.items():
            if name != missing:
                package.writestr(name, body)
    return out.getvalue()


def payload(extension):
    if extension == "pdf":
        # Real native PDF fixture, not PDF bytes masquerading as every format.
        import fitz
        with fitz.open() as doc:
            doc.new_page().insert_text((72, 72), "Teacher original source")
            return doc.tobytes()
    if extension in {"png", "jpg", "jpeg"}:
        out = io.BytesIO()
        Image.new("RGB", (1, 1)).save(out, format="PNG" if extension == "png" else "JPEG")
        return out.getvalue()
    if extension == "docx":
        return office_package()
    if extension == "json":
        return b'{"cases":[{"input":"1","expected_output":"2"}]}'
    if extension in {"py", "c", "cc", "cpp"}:
        return b"# teacher text; never executed\n"
    return "Teacher original text\n完整原文".encode()


@pytest.mark.parametrize("role,extension", [(role, ext) for role, exts in ROLE_EXTENSIONS.items() for ext in exts])
def test_each_role_accepts_only_its_documented_formats(role, extension):
    data = payload(extension)
    result = policy().validate_upload(role, "original." + extension, data)
    assert result.file_name == "original." + extension
    assert result.extension == extension
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.media_type


@pytest.mark.parametrize("role,extension", [
    (role, ext) for role, exts in ROLE_EXTENSIONS.items()
    for ext in sorted(set(OTHER_EXTENSIONS).union(*(set(value) for value in ROLE_EXTENSIONS.values())) - set(exts))
])
def test_disallowed_formats_are_rejected_before_mime_can_grant_access(role, extension):
    module = policy()
    with pytest.raises(module.UploadContractError) as caught:
        module.validate_upload(role, "source." + extension, b"plain text cannot grant a new role")
    assert caught.value.code == ("submission_source_type_not_allowed" if role == "student_submission" else "source_type_not_allowed")


def test_unknown_role_is_not_a_wide_default():
    module = policy()
    with pytest.raises(module.UploadContractError) as caught:
        module.validate_upload("unknown", "source.txt", b"text")
    assert caught.value.code == "invalid_source_role"


def test_corrupt_docx_member_is_rejected_not_just_its_directory():
    module = policy()
    data = office_package()
    # ZIP_STORED fixture: preserve the directory but corrupt a required member's CRC.
    data = data.replace(b"<w:document", b"!w:document", 1)
    with pytest.raises(module.UploadContractError) as caught:
        module.validate_upload("problem_reference", "source.docx", data)
    assert caught.value.code == "source_docx_invalid"


@pytest.mark.parametrize("malformation", ["non_xml", "unsupported_compression"])
def test_docx_required_members_must_be_readable_office_xml(malformation):
    module = policy()
    if malformation == "non_xml":
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as package:
            for name in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
                package.writestr(name, b"ordinary archive content, not Office XML")
        data = out.getvalue()
    else:
        data = bytearray(office_package())
        # Preserve all members and sizes but use an unsupported ZIP method.
        offset = data.index(b"PK\x01\x02")
        data[offset + 10:offset + 12] = (99).to_bytes(2, "little")
        data = bytes(data)
    with pytest.raises(module.UploadContractError) as caught:
        module.validate_upload("problem_reference", "renamed.docx", data)
    assert caught.value.code == "source_docx_invalid"


def test_image_policy_projects_and_enforces_the_lower_ocr_byte_limit(monkeypatch):
    from backend.config import settings
    module = policy()
    data = payload("png")
    monkeypatch.setattr(settings, "ocr_max_image_bytes", len(data))
    assert module.get_upload_contract()["limits"]["image_max_bytes"] == len(data)
    assert module.validate_upload("student_submission", "source.png", data).extension == "png"
    with pytest.raises(module.UploadContractError) as caught:
        module.validate_upload("student_submission", "source.png", data + b"x")
    assert caught.value.code == "submission_source_too_large"
    assert caught.value.details["max_bytes"] == len(data)


def test_extension_normalization_preserves_original_bytes_hash():
    data = b"\xef\xbb\xbfprint(1)\r\n"
    result = policy().validate_upload("student_submission", "Original.PY", data)
    assert result.extension == "py"
    assert result.file_name == "Original.PY"
    assert result.sha256 == "41c1b808cc296dd7303431470c8ce2bf765dfac06f3935329ada15946f7433b9"


@pytest.mark.parametrize("extension,data", [("txt", b"%PDF-1.7\n"), ("pdf", b"not a pdf"), ("png", b"plain text"), ("py", b"PK\x03\x04not source"), ("json", b"%PDF-1.7\n")])
def test_extension_content_disagreement_is_rejected(extension, data):
    module = policy()
    role = "programming_tests" if extension == "json" else "student_submission"
    with pytest.raises(module.UploadContractError) as caught:
        module.validate_upload(role, "source." + extension, data)
    assert caught.value.code == "submission_source_content_type_mismatch"


@pytest.mark.parametrize("missing", ["[Content_Types].xml", "_rels/.rels", "word/document.xml"])
def test_docx_requires_office_members_not_just_a_zip_signature(missing):
    module = policy()
    with pytest.raises(module.UploadContractError) as caught:
        module.validate_upload("student_submission", "source.docx", office_package(missing=missing))
    assert caught.value.code == "source_docx_invalid"


def test_docx_corrupt_zip_is_a_controlled_error():
    module = policy()
    with pytest.raises(module.UploadContractError) as caught:
        module.validate_upload("problem_reference", "source.docx", b"PK\x03\x04broken")
    assert caught.value.code == "source_docx_invalid"


@pytest.mark.parametrize("data", [b'{"x":NaN}', b'{"x":[Infinity]}', b'{"x":-Infinity}', b'{"x":[1e999]}', b'{broken'])
def test_test_json_rejects_invalid_and_non_finite_nested_values(data):
    module = policy()
    with pytest.raises(module.UploadContractError) as caught:
        module.validate_upload("programming_tests", "tests.json", data)
    assert caught.value.code == "programming_tests_json_invalid"


def test_json_strings_are_data_not_a_keyword_blacklist():
    result = policy().validate_upload("programming_tests", "tests.json", b'{"input":"import os; eval(x)","expected_output":"Infinity"}')
    assert result.media_type == "application/json"


def test_role_byte_limit_and_published_limit_use_stricter_existing_setting(monkeypatch):
    from backend.tools import file_processing
    monkeypatch.setattr(file_processing, "SUBMISSION_UPLOAD_MAX_BYTES", 16)
    module = policy()
    assert module.validate_upload("student_submission", "source.txt", b"x" * 16).extension == "txt"
    with pytest.raises(module.UploadContractError) as caught:
        module.validate_upload("student_submission", "source.txt", b"x" * 17)
    assert caught.value.code == "submission_source_too_large"
    assert module.get_upload_contract()["limits"]["submission_upload_max_bytes"] == 16


def test_contract_distinguishes_effective_entrypoint_limits_and_does_not_leak_mutable_state():
    module = policy()
    contract = module.get_upload_contract()
    assert contract["limits"]["knowledge_max_bytes"] == 64 * 1024 * 1024
    assert contract["limits"]["question_source_max_bytes"] == 5 * 1024 * 1024
    assert contract["limits"]["individual_submission_max_bytes"] == 64 * 1024 * 1024
    assert contract["limits"]["submission_upload_max_bytes"] == 100 * 1024 * 1024
    assert contract["limits"]["archive_max_files"] == 500
    assert contract["limits"]["archive_max_expanded_bytes"] == 100 * 1024 * 1024
    assert contract["limits"]["archive_max_member_bytes"] == 5 * 1024 * 1024
    assert contract["limits"]["source_filename_max_characters"] == 512
    for role, extensions in ROLE_EXTENSIONS.items():
        assert contract["roles"][role]["extensions"] == sorted(extensions)
    json.dumps(contract, allow_nan=False)
    contract["roles"]["rubric"]["extensions"].append("png")
    assert module.get_upload_contract()["roles"]["rubric"]["extensions"] == ["md", "pdf", "txt"]


def test_policy_module_import_does_not_start_application():
    # This must run before any other test has imported the policy/app. A new
    # interpreter prevents an already-loaded module making the check vacuous.
    check = subprocess.run(
        [sys.executable, "-B", "-c", "import sys; import backend.services.upload_contract; "
         "assert 'backend.main' not in sys.modules; "
         "assert not any(name == 'backend.db' or name.startswith('backend.db.') for name in sys.modules)"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=20,
    )
    assert check.returncode == 0, check.stdout + check.stderr
