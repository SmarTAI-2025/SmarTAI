"""Per-field identity fallbacks for a source-backed submission."""
from pathlib import PurePosixPath


def identity_filename(filename: str | None) -> str:
    """Return the upload's leaf within the identity field limit, keeping its extension."""
    path = PurePosixPath((filename or "").replace("\\", "/"))
    if len(path.name) <= 160:
        return path.name
    suffix = path.suffix
    if len(suffix) >= 160:
        return path.name[:160]
    return path.stem[:160 - len(suffix)] + suffix


def filename_identity_fallbacks(student: dict, filename: str | None) -> set[str]:
    leaf = identity_filename(filename)
    placeholders = {"", "[Unknown Student]", filename, leaf, PurePosixPath((filename or "").replace("\\", "/")).name}
    return {field for field in ("stu_id", "stu_name") if str(student.get(field) or "").strip() in placeholders}


def apply_filename_fallbacks(student: dict, filename: str) -> set[str]:
    fields = filename_identity_fallbacks(student, filename)
    if fields:
        fallback = identity_filename(filename)
        student.update(dict.fromkeys(fields, fallback))
        student["identity_status"] = "needs_review"
    return fields
