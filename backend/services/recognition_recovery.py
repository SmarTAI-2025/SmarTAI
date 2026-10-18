"""Read saved recognition evidence for manual handoff, never dispatch OCR.

Unlike the success cache, this path also retains incomplete reads and distinct
fragments from retries. The immutable original, owner, task and purpose must
match; a frozen list is revalidated when the durable worker consumes it.
"""
import hashlib
from dataclasses import dataclass

from backend.db import file_repository
from backend.domain.errors import RecognitionError
from backend.recognition.artifact_codec import MAX_COMPRESSED_BYTES, decode_artifact
from backend.services.recognition_artifacts import (
    ARTIFACT_KIND, RecognitionArtifactBindingV1, _load,
)
from backend.storage import get_storage


def merge_saved_text(previous: str, incoming: str) -> str:
    """Preserve distinct fragments without pretending they form a complete read."""
    previous, incoming = previous.strip(), incoming.strip()
    if not incoming or incoming in previous:
        return previous
    if not previous or previous in incoming:
        return incoming
    return previous + "\n\n" + incoming


@dataclass(frozen=True)
class SavedRecognition:
    text: str
    artifact_ids: list[str]
    missing_pages: list[int]


def saved_recognition(*, owner_id, task_id, stored_file_id, purpose, before,
                      artifact_ids=None, selected_pages=None):
    original = file_repository.get_file(file_id=stored_file_id, owner_id=owner_id)
    if original is None or original.assignment_id != task_id:
        raise RecognitionError("recognition_source_mismatch")
    storage = get_storage()
    binding = RecognitionArtifactBindingV1(link="assignment", business_id=task_id)
    frozen = artifact_ids is not None
    rows = ([file_repository.get_file(file_id=file_id, owner_id=owner_id) for file_id in artifact_ids]
            if frozen else file_repository.list_files(owner_id=owner_id, assignment_id=task_id))
    fragments, ids, missing = {}, [], set()
    # Prefer the assembled current text when it contains the same leaf text;
    # distinct earlier fragments remain available, even after another failure.
    for row in sorted(rows, key=lambda row: (row.created_at, row.id) if row else (0, "")):
        if (row is None or row.kind != ARTIFACT_KIND or row.assignment_id != task_id
                or row.owner_id != owner_id or row.created_at > before
                or row.availability_status != "available"):
            if frozen:
                raise RecognitionError("recognition_artifact_invalid")
            continue
        try:
            if not 0 < row.size_bytes <= MAX_COMPRESSED_BYTES:
                raise RecognitionError("recognition_artifact_invalid")
            with storage.open(row.storage_key) as stream:
                body = stream.read(MAX_COMPRESSED_BYTES + 1)
            if len(body) != row.size_bytes or hashlib.sha256(body).hexdigest() != row.sha256:
                raise RecognitionError("recognition_artifact_invalid")
            envelope = decode_artifact(body)
            if (envelope.source.stored_file_id != original.id
                    or envelope.source.input_sha256 != original.sha256
                    or (envelope.payload_kind != "native_detail" and envelope.identity.purpose != purpose)):
                if frozen:
                    raise RecognitionError("recognition_source_mismatch")
                continue
            envelope = _load(storage, row.id, envelope.source, envelope.identity, binding, owner_id)
        except (RecognitionError, FileNotFoundError):
            if frozen:
                raise
            continue
        blocks = []
        payload = envelope.payload
        if envelope.payload_kind in {"assembly", "assembly_v2"} and payload.document:
            document = payload.document
            missing.update(document.coverage.failed_pages + document.coverage.unprocessed_pages)
            blocks = [((page.page_number,), "\n\n".join(span.final_text for span in page.spans))
                      for page in document.pages]
            # Document-only outputs cannot be assigned fictitious page boundaries.
            blocks += [(tuple(unit.page_numbers), unit.candidate.text)
                       for unit in (payload.raw.read_batch.units if payload.raw.read_batch else [])
                       if unit.output_mapping == "document_only"]
        elif envelope.payload_kind == "visual_read":
            blocks = [(tuple(payload.page_numbers), payload.candidate.text)]
            if not payload.candidate.text.strip():
                missing.update(payload.page_numbers)
        elif envelope.payload_kind == "native_detail":
            blocks = [((page.page_number,), page.native_text) for page in payload.pages]
        else:
            continue
        ids.append(row.id)
        for pages, text in blocks:
            if selected_pages and not set(pages).intersection(selected_pages):
                continue
            if text.strip():
                fragments[pages] = merge_saved_text(fragments.get(pages, ""), text)
    if selected_pages:
        missing.intersection_update(selected_pages)
    text = "\n\n".join(f"[Saved source pages: {','.join(map(str, pages))}]\n{text}"
                         for pages, text in sorted(fragments.items()))
    if missing:
        text += "\n\n[Missing or incomplete source pages: " + ",".join(map(str, sorted(missing))) + "]"
    return SavedRecognition(text if fragments else "", ids, sorted(missing))
