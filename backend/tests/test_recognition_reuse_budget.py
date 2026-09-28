"""Cached evidence consumes logical limits, never a new dispatch's token budget."""
import base64
from concurrent.futures import ThreadPoolExecutor
import gzip
import json

import pytest
from pydantic import ValidationError

from backend.domain.errors import RecognitionError
from backend.recognition.budget import BudgetSnapshot, LogicalBudgetSnapshotV2, RecognitionBudget
from backend.recognition.models import RecognitionCandidateV1
from backend.tests.test_recognition_budget import Clock, context
from backend.tests.test_recognition_legacy_artifacts import LEGACY_FIXTURES


def make_budget(*, bounded=True, **policy):
    source, policy, caps = context(**policy)
    caps.target_location = True
    caps.bounded_output_tokens = bounded
    caps.candidate_kind = "vision" if bounded else "ocr"
    clock = Clock()
    return RecognitionBudget(source, policy, caps, clock=clock), clock, (source, policy, caps)


def reply(*, unknown=False, **changes):
    fields = dict(kind="vision", status="ok", text="PRIVATE literal source", provider_route_id="PRIVATE_ROUTE",
                  input_tokens=None if unknown else 1000, output_tokens=None if unknown else 500)
    fields.update(changes)
    return RecognitionCandidateV1(**fields)


def cached(value, key="p1", *, kind="initial", candidate=None):
    return value.register_cached_success(kind, region_keys=() if kind == "locator" else (key,),
                                         candidate=candidate if candidate is not None else reply())


def dispatched(value, key="p1", *, kind="initial", outcome="ok", tokens=1):
    ticket = value.reserve(kind, tokens, region_keys=() if kind == "locator" else (key,))
    if outcome is not None:
        value.settle(ticket, 2, tokens, outcome=outcome)
    return ticket


def test_old_snapshot_fields_and_fresh_serialization_are_unchanged():
    historical = json.loads(gzip.decompress(base64.b64decode(LEGACY_FIXTURES["assembly"]["base64"])))
    old_fields = set(historical["payload"]["raw"]["budget"])
    assert set(BudgetSnapshot.model_fields) == old_fields
    value, _, _ = make_budget()
    before = value.snapshot().model_dump_json()
    logical = value.logical_snapshot()
    assert logical.total_calls == logical.cache_hits == logical.region_count == 0
    assert value.snapshot().model_dump_json() == before
    assert BudgetSnapshot.model_validate_json(before).model_dump_json() == before
    dispatched(value)
    current, logical = value.snapshot(), value.logical_snapshot()
    for field in ("locator_calls", "initial_calls", "empty_recovery_calls", "patch_calls", "read_calls", "total_calls"):
        assert getattr(current, field) == getattr(logical, field)
    assert logical.cache_hits == 0 and set(current.model_dump()) == old_fields


@pytest.mark.parametrize("kind", ["locator", "initial"])
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("bounded", [False, True])
def test_cached_known_or_unknown_usage_is_not_current_spend(kind, unknown, bounded):
    value, _, _ = make_budget(bounded=bounded, max_output_tokens=1)
    value.start_phase("locator" if kind == "locator" else "read")
    before = value.snapshot().model_dump_json()
    result = cached(value, kind=kind, candidate=reply(unknown=unknown, kind="vision" if bounded else "ocr"))
    assert result is None
    assert value.snapshot().model_dump_json() == before
    assert value.output_limit() == (1 if bounded else 4096)
    logical = value.logical_snapshot()
    assert logical.cache_hits == logical.total_calls == 1
    assert logical.initial_cache_hits == logical.initial_calls == (kind == "initial")
    assert logical.locator_cache_hits == logical.locator_calls == (kind == "locator")
    assert logical.region_count == (kind == "initial")


@pytest.mark.parametrize("outcome", [None, "ok"])
def test_existing_current_charge_is_neither_refunded_nor_augmented_by_hit(outcome):
    value, _, _ = make_budget(max_output_tokens=10)
    dispatched(value, "paid", outcome=outcome, tokens=10)
    before = value.snapshot().model_dump_json()
    cached(value, "reused", candidate=reply(unknown=True))
    assert value.snapshot().model_dump_json() == before
    assert value.logical_snapshot().initial_calls == 2 and value.snapshot().initial_calls == 1
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        value.reserve("initial", 1, region_keys=("next-paid",))
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        value.reserve("patch", 1, region_keys=("reused",))


def test_historical_overrun_does_not_poison_current_budget_but_real_overrun_still_does():
    value, _, _ = make_budget(max_output_tokens=10)
    cached(value)
    assert not value.snapshot().output_limit_exceeded and value.output_limit() == 10
    ticket = value.reserve("patch", 1, region_keys=("p1",))
    value.settle(ticket, 10, 2, outcome="ok")
    assert value.snapshot().output_limit_exceeded
    cached(value, "another-hit")
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        dispatched(value, "another-paid")


def test_cached_initial_has_one_patch_but_never_empty_recovery_or_second_initial():
    value, _, _ = make_budget()
    cached(value)
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        value.reserve("empty_recovery", 1, region_keys=("p1",))
    ticket = value.reserve("patch", 10, region_keys=("p1",))
    value.settle(ticket, 3, 4, outcome="ok")
    assert value.snapshot().initial_calls == 0 and value.snapshot().patch_calls == 1
    assert value.snapshot().input_tokens == 3 and value.snapshot().output_tokens == 4
    logical = value.logical_snapshot()
    assert logical.initial_calls == logical.patch_calls == logical.initial_cache_hits == 1
    assert logical.read_calls == logical.total_calls == 2 and logical.region_count == 1
    for kind in ("patch", "empty_recovery", "initial"):
        with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
            value.reserve(kind, 1, region_keys=("p1",))
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        cached(value)


def test_cached_group_counts_one_initial_and_preserves_each_original_region_once():
    value, _, _ = make_budget()
    value.register_cached_success("initial", region_keys=("p1", "p2", "p3"), candidate=reply())
    assert value.logical_snapshot().initial_calls == 1 and value.logical_snapshot().region_count == 3
    dispatched(value, "p2", kind="patch")
    dispatched(value, "p1", kind="patch")
    before = value.logical_snapshot()
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        value.register_cached_success("initial", region_keys=("p3", "new"), candidate=reply())
    assert value.logical_snapshot() == before
    dispatched(value, "new")
    assert value.logical_snapshot().region_count == 4


@pytest.mark.parametrize("outcome", [None, "failed", "empty"])
def test_cached_success_cannot_replace_a_pending_failed_or_empty_initial(outcome):
    value, _, _ = make_budget()
    dispatched(value, outcome=outcome)
    before = value.logical_snapshot()
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        cached(value)
    assert value.logical_snapshot() == before and not before.cache_hits
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        value.reserve("patch", 1, region_keys=("p1",))
    if outcome == "empty":
        dispatched(value, kind="empty_recovery")
    else:
        with pytest.raises(RecognitionError, match="recognition_request_invalid"):
            value.reserve("empty_recovery", 1, region_keys=("p1",))


@pytest.mark.parametrize("cached_first", [False, True])
@pytest.mark.parametrize("kind", ["locator", "initial"])
def test_dispatch_and_cache_share_each_logical_call_cap(cached_first, kind):
    value, _, _ = make_budget(max_locator_calls=1, max_initial_calls=1)
    if cached_first:
        cached(value, kind=kind)
        next_call = lambda: dispatched(value, "p2", kind=kind)
    else:
        dispatched(value, kind=kind)
        next_call = lambda: cached(value, "p2", kind=kind)
    before = value.logical_snapshot()
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        next_call()
    assert value.logical_snapshot() == before


@pytest.mark.parametrize("cached_first", [False, True])
def test_shared_region_limit_counts_cached_and_dispatched_regions(cached_first):
    value, _, _ = make_budget(max_regions=1)
    (cached if cached_first else dispatched)(value)
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        (dispatched if cached_first else cached)(value, "p2")
    assert value.logical_snapshot().region_count == 1


def test_read_total_caps_cache_hits_and_extras_together():
    value, _, _ = make_budget(max_calls=2)
    cached(value)
    dispatched(value, kind="patch")
    for action in (lambda: cached(value, "p2"), lambda: dispatched(value, "p2")):
        with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
            action()
    assert value.logical_snapshot().read_calls == 2 and value.snapshot().read_calls == 1


def test_mixed_maximum_locator_reads_and_extras_preserve_current_only_accounting():
    value, _, _ = make_budget()
    for i in range(6):
        cached(value, kind="locator")
        dispatched(value, kind="locator")
        cached(value, f"cached-{i}")
        dispatched(value, f"paid-{i}", outcome="empty" if i < 2 else "ok")
    for i in range(2):
        dispatched(value, f"paid-{i}", kind="empty_recovery")
    for i in range(4):
        dispatched(value, f"cached-{i}", kind="patch")
    logical, current = value.logical_snapshot(), value.snapshot()
    assert logical.locator_calls == logical.initial_calls == 12
    assert logical.empty_recovery_calls == 2 and logical.patch_calls == 4
    assert logical.read_calls == 18 and logical.total_calls == 30 and logical.cache_hits == 12
    assert current.total_calls == 18 and current.initial_calls == current.locator_calls == 6
    assert current.input_tokens == 36 and current.output_tokens == 18
    for action in (lambda: cached(value, kind="locator"), lambda: cached(value, "new"),
                   lambda: dispatched(value, "cached-4", kind="patch")):
        with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
            action()


@pytest.mark.parametrize("name", ["response_recheck", "semantic_repair", "enable_repair"])
def test_cached_initial_does_not_invent_patch_capability(name):
    source, policy, caps = context()
    setattr(policy if name == "enable_repair" else caps, name, False)
    value = RecognitionBudget(source, policy, caps, clock=Clock())
    cached(value)
    with pytest.raises(RecognitionError, match="recognition_input_unsupported"):
        dispatched(value, kind="patch")
    assert value.snapshot().total_calls == 0


@pytest.mark.parametrize("candidate", [
    reply(status="empty", text=""), reply(status="error", text=""), reply(status="not_run", text=""),
    reply(safe_error_code="provider_auth_failed"), reply(finish_reason="refused"), reply(finish_reason="length"),
    reply(warning_codes=["provider_refused"]), reply(warning_codes=["output_truncated"]),
])
def test_non_success_or_explicitly_partial_candidate_cannot_create_logical_credit(candidate):
    value, _, _ = make_budget()
    with pytest.raises(RecognitionError, match="recognition_response_invalid"):
        cached(value, candidate=candidate)
    assert value.logical_snapshot().total_calls == value.snapshot().total_calls == 0


@pytest.mark.parametrize("candidate", [reply(provider_route_id="another"), reply(kind="ocr"), reply(kind="native")])
def test_candidate_route_and_kind_must_match_frozen_capabilities(candidate):
    value, _, _ = make_budget()
    with pytest.raises(RecognitionError, match="recognition_route_changed"):
        cached(value, candidate=candidate)
    assert value.logical_snapshot().cache_hits == 0


@pytest.mark.parametrize("capability", ["none", "no_visual", "no_locator"])
def test_unsupported_capabilities_cannot_register_cached_model_output(capability):
    source, policy, caps = context()
    if capability == "none":
        caps = None
    elif capability == "no_visual":
        caps.visual_inputs = []
        caps.semantic_repair = caps.response_recheck = False
    value = RecognitionBudget(source, policy, caps, clock=Clock())
    with pytest.raises(RecognitionError, match="recognition_input_unsupported"):
        cached(value, kind="locator" if capability == "no_locator" else "initial")


def test_candidate_revalidation_rejects_mutation_and_success_does_not_retain_aliases():
    value, _, _ = make_budget()
    candidate = reply()
    candidate.normalized_text = "invented different text"
    with pytest.raises(RecognitionError, match="recognition_response_invalid"):
        cached(value, candidate=candidate)
    candidate = reply()
    cached(value, candidate=candidate)
    candidate.status, candidate.text, candidate.provider_route_id = "error", "", "another"
    dispatched(value, kind="patch")
    assert value.logical_snapshot().initial_cache_hits == 1
    assert value.snapshot().total_calls == 1


def test_original_context_mutation_is_not_a_route_or_policy_change_inside_budget():
    value, _, (source, policy, caps) = make_budget()
    caps.route_id = "changed"
    with pytest.raises(RecognitionError, match="recognition_route_changed"):
        value.assert_context(source, policy, caps)
    with pytest.raises(RecognitionError, match="recognition_route_changed"):
        cached(value, candidate=reply(provider_route_id=caps.route_id))
    cached(value)
    assert value.logical_snapshot().cache_hits == 1


@pytest.mark.parametrize("kind,phase_limit", [("locator", 360), ("initial", 600)])
def test_registration_starts_phase_once_and_later_hits_cannot_reset_deadline(kind, phase_limit):
    value, clock, _ = make_budget(total_seconds=900)
    clock.now = 10
    cached(value, kind=kind)
    clock.now = 20
    cached(value, "p2", kind=kind)
    clock.now = 10 + phase_limit
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        cached(value, "p3", kind=kind)
    assert value.logical_snapshot().cache_hits == 2 and value.snapshot().total_calls == 0


def test_global_deadline_blocks_hits_before_any_phase_can_start():
    value, clock, _ = make_budget(total_seconds=5)
    clock.now = 5
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        cached(value)
    assert value.logical_snapshot().cache_hits == 0


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True, -1])
def test_invalid_or_backward_clock_cannot_register_a_hit(now):
    value, clock, _ = make_budget()
    clock.now = now
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        cached(value)
    assert value.logical_snapshot().cache_hits == 0


@pytest.mark.parametrize("kind,keys", [
    ("patch", ("p1",)), ("empty_recovery", ("p1",)), ([], ()), ("initial", ()),
    ("initial", ["p1"]), ("initial", ("p1", "p1")), ("initial", ("",)),
    ("initial", (" " * 2,)), ("initial", ("x" * 513,)), ("initial", (1,)),
    ("initial", tuple(str(i) for i in range(25))), ("locator", ("p1",)),
])
def test_invalid_registration_does_not_add_any_credit(kind, keys):
    value, _, _ = make_budget()
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        value.register_cached_success(kind, region_keys=keys, candidate=reply())
    assert value.logical_snapshot().total_calls == value.snapshot().total_calls == 0


def test_cache_and_dispatch_race_for_one_region_atomically():
    value, _, _ = make_budget()
    def attempt(index):
        try:
            (cached if index % 2 else dispatched)(value)
            return True
        except RecognitionError as error:
            assert error.code == "recognition_budget_exhausted"
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(attempt, range(24)))
    assert sum(accepted) == 1
    assert value.logical_snapshot().initial_calls == value.logical_snapshot().region_count == 1
    assert value.snapshot().initial_calls + value.logical_snapshot().initial_cache_hits == 1


def test_parallel_cache_registration_cannot_overrun_combined_call_cap():
    value, _, _ = make_budget(max_initial_calls=3)
    def attempt(index):
        try:
            cached(value, f"p{index}")
            return True
        except RecognitionError as error:
            assert error.code == "recognition_budget_exhausted"
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(attempt, range(24)))
    assert sum(accepted) == 3
    assert value.logical_snapshot().initial_cache_hits == value.logical_snapshot().region_count == 3
    assert value.snapshot().total_calls == 0


def test_logical_snapshot_is_detached_serializable_and_contains_no_identifiers():
    value, _, _ = make_budget()
    cached(value, "PRIVATE_REGION")
    logical = value.logical_snapshot()
    serialized = logical.model_dump_json()
    assert "PRIVATE" not in serialized
    assert LogicalBudgetSnapshotV2.model_validate_json(serialized) == logical
    logical.cache_hits = 0
    assert value.logical_snapshot().cache_hits == 1


@pytest.mark.parametrize("field,changed", [("read_calls", 0), ("total_calls", 0), ("cache_hits", 0),
                                         ("initial_cache_hits", 2), ("region_count", 0), ("patch_calls", True)])
def test_logical_serialization_rejects_inconsistent_or_coerced_summary(field, changed):
    value, _, _ = make_budget()
    cached(value)
    data = value.logical_snapshot().model_dump()
    data[field] = changed
    with pytest.raises(ValidationError):
        LogicalBudgetSnapshotV2.model_validate(data)
