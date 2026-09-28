from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from backend.recognition.models import NormalizedRegionV1, RecognitionPolicyV1
from backend.recognition.planner import (
    EngineCapabilitiesV1,
    PageObservationV1,
    RecognitionPlanRequestV1,
    RecognitionPlanV1,
    plan_recognition,
)


def engine(**changes):
    return EngineCapabilitiesV1(**{
        "route_id": "owner-selected-route", "fingerprint": "frozen-model-profile-v1",
        "visual_inputs": ["page_image"], "region_reads": True,
        "semantic_repair": True, "response_recheck": True, **changes,
    })


def request(*pages, purpose="problems", **changes):
    return RecognitionPlanRequestV1(**{
        "purpose": purpose, "scope": "document", "total_pages": len(pages),
        "requested_pages": list(range(1, len(pages) + 1)), "observations": list(pages), **changes,
    })


def native(number=1, **changes):
    return PageObservationV1(**{
        "page_number": number, "native_quality": "clean", "native_char_count": 100, **changes,
    })


@pytest.mark.parametrize("purpose", ["problems", "reference", "rubric", "test_cases", "knowledge"])
def test_clean_native_prose_needs_no_provider(purpose):
    plan = plan_recognition(request(native(), purpose=purpose), engine=None)
    assert plan.decisions[0].action == "native"
    assert plan.initial_calls == plan.reserved_extra_calls == 0
    assert plan.recognition_complete is False
    assert plan.decisions[0].retain_native is True


def test_student_text_layer_does_not_hide_crossouts_or_imply_correctness():
    plan = plan_recognition(request(native(), purpose="submissions"), engine=engine())
    assert plan.decisions[0].action == "visual"
    assert "submission_fidelity" in plan.decisions[0].reason_codes
    assert plan.decisions[0].regions == [NormalizedRegionV1()]


@pytest.mark.parametrize("risk", ["math", "table", "diagram", "handwriting", "layout", "damaged_text"])
def test_native_text_never_suppresses_risky_visual_content(risk):
    plan = plan_recognition(request(native(risks=[risk])), engine=engine())
    assert plan.decisions[0].action == "visual"
    assert f"risk_{risk}" in plan.decisions[0].reason_codes


def test_known_blank_is_not_empty_ocr_failure_or_a_billable_request():
    plan = plan_recognition(request(PageObservationV1(page_number=1, verified_blank=True)), engine=engine())
    assert plan.decisions[0].action == "blank"
    assert plan.initial_calls == 0


def test_scan_with_text_only_route_is_blocked_not_replaced_by_hidden_fallback():
    selected = engine(visual_inputs=[], region_reads=False, semantic_repair=False)
    plan = plan_recognition(request(PageObservationV1(page_number=1)), engine=selected)
    assert plan.decisions[0].action == "blocked"
    assert "visual_capability_unavailable" in plan.decisions[0].reason_codes
    assert plan.initial_calls == 0
    assert plan.route_id == selected.route_id


def test_ocr_only_engine_cannot_semantically_patch_or_automatically_resubmit():
    selected = engine(visual_inputs=["document"], region_reads=False, semantic_repair=False, response_recheck=False)
    plan = plan_recognition(request(PageObservationV1(page_number=1)), engine=selected)
    assert plan.decisions[0].input_mode == "document"
    assert not plan.decisions[0].repair_allowed
    assert plan.reserved_extra_calls == 0


def test_localized_missing_image_regions_augment_retained_native_text():
    regions = [NormalizedRegionV1(x1=0.4), NormalizedRegionV1(x0=0.6)]
    plan = plan_recognition(request(native(risks=["diagram"], regions=regions, regions_cover_all_risks=True)), engine=engine())
    assert plan.decisions[0].regions == regions
    assert plan.initial_calls == 2
    assert "localized_risks" in plan.decisions[0].reason_codes


@pytest.mark.parametrize("changes,purpose", [
    ({"regions_cover_all_risks": False}, "problems"),
    ({"risks": ["layout"]}, "knowledge"),
    ({"native_quality": "suspect"}, "problems"),
    ({}, "submissions"),
])
def test_unsafe_crop_uses_whole_page_context(changes, purpose):
    page = native(**{
        "risks": ["math"], "regions": [NormalizedRegionV1(x1=0.5)],
        "regions_cover_all_risks": True, **changes,
    })
    plan = plan_recognition(request(page, purpose=purpose), engine=engine())
    assert plan.decisions[0].regions == [NormalizedRegionV1()]
    assert "full_page_context" in plan.decisions[0].reason_codes


def test_force_visual_is_an_explicit_policy_choice():
    plan = plan_recognition(request(native()), engine=engine(), policy=RecognitionPolicyV1(force_visual=True))
    assert plan.initial_calls == 1
    assert "forced_visual" in plan.decisions[0].reason_codes


def test_long_book_batch_keeps_every_unprocessed_page_visible():
    plan = plan_recognition(request(*(native(i) for i in range(1, 1001)), purpose="knowledge"), engine=None)
    assert len(plan.decisions) == 1000
    assert sum(page.action == "native" for page in plan.decisions) == 24
    assert sum(page.action == "deferred" for page in plan.decisions) == 976
    assert plan.initial_calls == 0
    assert not plan.recognition_complete


def test_budget_does_not_drop_half_a_page_or_schedule_unbounded_retry():
    regions = [NormalizedRegionV1(x1=0.4), NormalizedRegionV1(x0=0.6)]
    plan = plan_recognition(
        request(native(risks=["diagram"], regions=regions, regions_cover_all_risks=True), native(2)),
        engine=engine(), policy=RecognitionPolicyV1(max_initial_calls=1),
    )
    assert [page.action for page in plan.decisions] == ["deferred", "native"]
    assert plan.initial_calls == 0


@pytest.mark.parametrize("policy", [
    RecognitionPolicyV1(max_calls=1),
    RecognitionPolicyV1(max_patches=0),
    RecognitionPolicyV1(enable_repair=False),
])
def test_disabled_or_exhausted_repair_is_not_advertised(policy):
    plan = plan_recognition(request(PageObservationV1(page_number=1)), engine=engine(), policy=policy)
    assert not plan.decisions[0].repair_allowed


def test_full_book_requires_every_page_but_partial_scope_is_explicit():
    with pytest.raises(ValidationError, match="every page"):
        request(native(), total_pages=500)
    plan = plan_recognition(request(native(), total_pages=500, scope="pages"), engine=None)
    assert len(plan.decisions) == 1


def test_target_localization_failure_is_preserved_not_invented():
    req = request(native(), scope="targets", requested_targets=["1.1.5", "1.2.16"], unlocated_targets=["1.2.16"])
    plan = plan_recognition(req, engine=engine())
    assert plan.unlocated_targets == ["1.2.16"]
    assert plan.requested_targets == ["1.1.5", "1.2.16"]
    assert plan.scope == "targets"
    assert not plan.recognition_complete


def test_plan_freezes_scope_policy_and_engine_without_aliasing_inputs():
    req = request(native(), scope="pages", total_pages=500)
    caps = engine()
    policy = RecognitionPolicyV1(max_calls=3)
    plan = plan_recognition(req, engine=caps, policy=policy)
    policy.max_calls = 18
    caps.visual_inputs.clear()
    assert plan.total_pages == 500
    assert plan.scope == "pages"
    assert plan.requested_pages == [1]
    assert plan.policy.max_calls == 3
    assert plan.engine_capabilities.visual_inputs == ["page_image"]
    assert plan.strategy_status == "uncalibrated"


def test_deserialized_plan_cannot_exceed_its_frozen_page_budget():
    payload = plan_recognition(request(native(1), native(2)), engine=None).model_dump()
    payload["policy"]["max_detail_pages"] = 1
    with pytest.raises(ValidationError, match="page budget"):
        RecognitionPlanV1.model_validate(payload)


def test_deserialized_plan_cannot_grant_an_ocr_only_engine_semantic_repair():
    selected = engine(visual_inputs=["document"], region_reads=False, semantic_repair=False, response_recheck=False)
    payload = plan_recognition(request(PageObservationV1(page_number=1)), engine=selected).model_dump()
    payload["decisions"][0]["repair_allowed"] = True
    with pytest.raises(ValidationError, match="unavailable repair"):
        RecognitionPlanV1.model_validate(payload)
    payload["reserved_extra_calls"] = 1
    with pytest.raises(ValidationError, match="recheck capability"):
        RecognitionPlanV1.model_validate(payload)


@pytest.mark.parametrize("change", ["input", "region", "retry"])
def test_deserialized_plan_cannot_expand_capabilities_or_region_budget(change):
    regions = [NormalizedRegionV1(x1=0.4), NormalizedRegionV1(x0=0.6)]
    page = native(risks=["diagram"], regions=regions, regions_cover_all_risks=True)
    payload = plan_recognition(request(page), engine=engine()).model_dump()
    if change == "input":
        payload["decisions"][0]["input_mode"] = "document"
    elif change == "region":
        payload["policy"]["max_regions"] = 1
    else:
        payload["policy"].update(max_patches=0, max_empty_recoveries=0)
    with pytest.raises(ValidationError):
        RecognitionPlanV1.model_validate(payload)


def test_missing_index_page_is_blocked_without_spending_a_call():
    req = request(native(), observations=[])
    plan = plan_recognition(req, engine=engine())
    assert plan.decisions[0].reason_codes == ["page_not_indexed"]
    assert plan.initial_calls == 0


@pytest.mark.parametrize("changes", [
    {"native_char_count": 0}, {"verified_blank": True},
    {"regions_cover_all_risks": True}, {"risks": ["math", "math"]},
])
def test_inconsistent_observations_fail_closed(changes):
    with pytest.raises(ValidationError):
        native(**changes)


def test_planning_is_deterministic_bounded_and_round_trippable():
    rng = random.Random(17)
    for _ in range(60):
        pages = [
            native(i, risks=rng.choice([[], ["math"], ["layout", "diagram"]]))
            for i in range(1, rng.randint(1, 60) + 1)
        ]
        req = request(*pages, purpose=rng.choice(["knowledge", "problems", "submissions"]))
        policy = RecognitionPolicyV1(
            max_detail_pages=rng.randint(1, 24), max_regions=rng.randint(1, 24),
            max_initial_calls=rng.randint(0, 12), max_calls=rng.randint(0, 18),
        )
        plan = plan_recognition(req, engine=engine(), policy=policy)
        assert plan == plan_recognition(req, engine=engine(), policy=policy)
        assert RecognitionPlanV1.model_validate_json(plan.model_dump_json()) == plan
        assert len(plan.decisions) == len(pages)
        assert plan.initial_calls <= min(policy.max_initial_calls, policy.max_calls, policy.max_regions)
        assert plan.initial_calls + plan.reserved_extra_calls <= policy.max_calls
        assert sum(page.action in {"native", "blank", "visual"} for page in plan.decisions) <= policy.max_detail_pages
