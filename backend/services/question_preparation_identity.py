"""Pure frozen-input identities shared by execution and manual projection."""
import hashlib
import json
from typing import Any, Mapping, Sequence


def source_fingerprint(payload: dict) -> str:
    selected = {
        key: payload.get(key)
        for key in (
            "sha256", "filename", "content_type", "source_kind",
            "library_material_id", "structure_mode", "role",
            "extraction_hint", "save_to_library", "targets", "recognition_options", "enable_material_ocr",
            "base_workflow_revision", "recognition_provider_id",
            "recognition_configuration_fingerprint",
        )
    }
    if payload.get("defer_recognition") is True:
        selected["defer_recognition"] = True
    # For an upload/inline source, this ID is an output of optional library
    # retention, assigned after input_hash was frozen. Only a library-selected
    # source uses the material ID as input identity. This also preserves retry
    # compatibility for previously saved uploads without weakening their
    # content, source-file, provider or prepared-text checks.
    if selected["source_kind"] != "library":
        selected["library_material_id"] = None
    return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()


def question_preparation_input_hash(
    *,
    ordered_source_inputs: Sequence[Mapping[str, Any]],
    logical_input_revision: int,
    replace_confirmed: bool,
    generation_policy: str,
    score_policy: Mapping[str, Any],
    recognition_provider_id: str,
    provider_configuration_fingerprint: str,
    acknowledged_restart_from: str | None = None,
    generation_language: str | None = None,
) -> str:
    """Hash the ordered logical input independently of retry claim revisions."""

    return hashlib.sha256(
        json.dumps(
            {
                "question_preparation_version": 2,
                "sources": list(ordered_source_inputs),
                "base_revision": logical_input_revision,
                "replace_confirmed": replace_confirmed,
                "generation_policy": generation_policy,
                **({"generation_language": generation_language} if generation_language else {}),
                "score_policy": dict(score_policy),
                "recognition_provider_id": recognition_provider_id,
                "provider_configuration_fingerprint": (
                    provider_configuration_fingerprint
                ),
                **({"acknowledged_restart_from": acknowledged_restart_from}
                   if acknowledged_restart_from else {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
