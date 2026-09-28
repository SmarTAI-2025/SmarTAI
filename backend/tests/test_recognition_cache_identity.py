import hashlib

import pytest
from pydantic import ValidationError

from backend.agents.recognition_agent import RecognitionReadRequestV1
from backend.domain.errors import RecognitionError
from backend.recognition.cache_identity import (
    RecognitionCacheIdentityV1, canonical_digest, final_cache_identity,
    model_cache_identity, native_cache_identity, render_cache_identity,
)
from backend.recognition.engine import EngineLocateInputV1, EngineReadInputV1, EngineRepairInputV1, LocatorImageV1
from backend.recognition.models import NormalizedRegionV1, RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.planner import EngineCapabilitiesV1
from backend.recognition.repair_response import RepairContextV1
from backend.tools.pdf_evidence import ImagePrepareRequest, PdfContactSheetRequest, PdfIndexRequest, PdfPagesRequest, PdfRenderRequest


def source(**changes):
    return RecognitionSourceRefV1(owner_id="owner", scope="assignment_source", business_id="task",
                                  content_type="application/pdf", input_sha256=hashlib.sha256(b"source").hexdigest()).model_copy(update=changes)


def caps(**changes):
    return EngineCapabilitiesV1(route_id="selected", fingerprint="route-model-v1", visual_inputs=["page_image", "document"],
                                semantic_repair=True, response_recheck=True, target_location=True,
                                max_locator_images=2, region_reads=True).model_copy(update=changes)


def unit(**changes):
    return EngineReadInputV1(purpose="problems", input_mode="page_image", page_number=1,
                             content_type="image/png", payload=b"pixels").model_copy(update=changes)


def model_key(request=None, **changes):
    values = dict(source=source(), request=request or unit(), capabilities=caps(),
                  policy=RecognitionPolicyV1(), prompt_version="reader-v1")
    values.update(changes)
    return model_cache_identity(**values)


def test_native_key_is_owner_source_request_scoped_but_not_business_name_scoped():
    request = PdfIndexRequest(targets=["1.1.5"])
    original = native_cache_identity(source(), request)
    assert original == native_cache_identity(source(original_name="other.pdf", business_id="other", stored_file_id="new"), request)
    assert original.key != native_cache_identity(source(owner_id="another"), request).key
    assert original.key != native_cache_identity(source(input_sha256="f" * 64), request).key
    assert original.key != native_cache_identity(source(), request, tool_version="next").key
    assert RecognitionCacheIdentityV1.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize("command", [PdfIndexRequest(start_page=2), PdfIndexRequest(window_pages=2),
                                     PdfIndexRequest(targets=["1"]), PdfPagesRequest(pages=[1])])
def test_native_request_parameters_invalidate_identity(command):
    assert native_cache_identity(source(), PdfIndexRequest()).key != native_cache_identity(source(), command).key


@pytest.mark.parametrize("command", [PdfRenderRequest(page_number=2), PdfRenderRequest(page_number=1, scale=1),
                                     PdfRenderRequest(page_number=1, region=(0, 0, 0.5, 1)),
                                     PdfContactSheetRequest(pages=[1])])
def test_render_geometry_and_operation_are_distinct(command):
    assert render_cache_identity(source(), PdfRenderRequest(page_number=1)).key != render_cache_identity(source(), command).key


def test_contact_sheet_labels_scale_and_image_crops_invalidate():
    sheet = render_cache_identity(source(), PdfContactSheetRequest(pages=[1, 3]))
    assert sheet != render_cache_identity(source(), PdfContactSheetRequest(pages=[1, 2]))
    assert sheet != render_cache_identity(source(), PdfContactSheetRequest(pages=[1, 3], tile_long_edge=512))
    image = source(content_type="image/png")
    assert render_cache_identity(image, ImagePrepareRequest(content_type="image/png")) != render_cache_identity(
        image, ImagePrepareRequest(content_type="image/png", region=(0, 0, 0.5, 1)))


@pytest.mark.parametrize("change", [dict(purpose="submissions"), dict(page_number=2), dict(payload=b"other pixels"),
                                    dict(region=NormalizedRegionV1(x1=0.5)), dict(max_output_tokens=512)])
def test_model_request_all_semantic_parameters_and_actual_pixels_invalidate(change):
    assert model_key().key != model_key(unit(**change)).key


@pytest.mark.parametrize("change", [dict(source=source(owner_id="another")), dict(source=source(input_sha256="f" * 64)),
                                    dict(capabilities=caps(fingerprint="next-model")), dict(capabilities=caps(route_id="other-route")),
                                    dict(capabilities=caps(bounded_output_tokens=True)), dict(policy=RecognitionPolicyV1(force_visual=True)),
                                    dict(prompt_version="reader-v2"), dict(tool_version="worker-v2")])
def test_model_frozen_context_invalidates(change):
    assert model_key().key != model_key(**change).key


def test_locator_target_order_pages_and_every_payload_invalidate():
    request = EngineLocateInputV1(purpose="problems", targets=["1", "2"], images=[
        LocatorImageV1(page_numbers=[1, 2], payload=b"sheet-one"), LocatorImageV1(page_numbers=[3], payload=b"sheet-two")])
    original = model_key(request)
    for replacement in (
        request.model_copy(update={"targets": ["2", "1"]}),
        request.model_copy(update={"images": [request.images[0], LocatorImageV1(page_numbers=[4], payload=b"sheet-two")]}),
        request.model_copy(update={"images": [request.images[0], LocatorImageV1(page_numbers=[3], payload=b"changed")]}),
    ):
        assert original != model_key(replacement)
    assert "sheet-one" not in original.model_dump_json()


def test_repair_context_not_just_image_is_part_of_patch_key():
    context = RepairContextV1(purpose="problems", span_id="p0001-s0001", page_number=1, region=NormalizedRegionV1(),
                              native_text="x", visual_text="y", before_text="x", issue_codes=["source_conflict"])
    request = EngineRepairInputV1(**unit().model_dump(exclude={"max_output_tokens"}), payload=b"pixels", repair_context=context)
    original = model_key(request)
    assert original.layer == "patch"
    for change in (dict(before_text="y"), dict(issue_codes=["unclear_source"]), dict(native_text="z", before_text="z")):
        assert original != model_key(request.model_copy(update={"repair_context": context.model_copy(update=change)}))
    assert original != model_key(unit(max_output_tokens=2048))


def test_document_page_mapping_is_not_just_first_page():
    a = unit(input_mode="document", content_type="application/pdf", document_pages=[1, 2])
    b = a.model_copy(update={"document_pages": [1, 3]})
    assert model_key(a) != model_key(b)


def test_final_identity_includes_scope_hints_policy_and_source_binding():
    request = RecognitionReadRequestV1(source=source(), purpose="problems", scope="targets", targets=["1"])
    original = final_cache_identity(request, capabilities=caps(), prompt_version="workflow-v1")
    for change in (dict(targets=["2"]), dict(page_hints={"1": [2]}), dict(search_start_page=2),
                   dict(search_window_pages=12), dict(purpose="submissions"), dict(source=source(business_id="another")),
                   dict(policy=RecognitionPolicyV1(force_visual=True))):
        assert original != final_cache_identity(request.model_copy(update=change), capabilities=caps(), prompt_version="workflow-v1")
    assert original != final_cache_identity(request, capabilities=None, prompt_version="workflow-v1")
    assert original != final_cache_identity(request, capabilities=caps(), prompt_version="workflow-v2")


@pytest.mark.parametrize("call", [
    lambda: native_cache_identity(source(content_type="image/png"), PdfIndexRequest()),
    lambda: native_cache_identity(source(), PdfPagesRequest(operation="export_pages", pages=[1])),
    lambda: render_cache_identity(source(), ImagePrepareRequest(content_type="image/png")),
    lambda: render_cache_identity(source(content_type="image/png"), PdfRenderRequest(page_number=1)),
    lambda: model_key(capabilities=caps(visual_inputs=[])),
    lambda: model_key(unit(page_number=0)),
    lambda: model_key(source=source(owner_id="")),
    lambda: model_key(prompt_version=""),
])
def test_mutated_invalid_requests_fail_safely(call):
    with pytest.raises(RecognitionError) as exc:
        call()
    assert exc.value.code == "recognition_request_invalid"


def test_local_layers_cannot_accept_model_or_secret_fields():
    identity = native_cache_identity(source(), PdfIndexRequest()).model_dump()
    for changes in (dict(purpose="knowledge"), dict(engine_fingerprint="model"), dict(api_key="secret")):
        with pytest.raises(ValidationError):
            RecognitionCacheIdentityV1.model_validate({**identity, **changes})
    assert canonical_digest({"a": 1, "b": 2}) == canonical_digest({"b": 2, "a": 1})
    with pytest.raises(ValueError):
        canonical_digest({"value": float("nan")})
