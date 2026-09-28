from concurrent.futures import ThreadPoolExecutor
import copy

import pytest

from backend.domain.errors import RecognitionError
from backend.recognition.budget import BudgetTicket, RecognitionBudget
from backend.recognition.models import RecognitionPolicyV1, RecognitionSourceRefV1
from backend.recognition.planner import EngineCapabilitiesV1


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def context(**policy):
    return (
        RecognitionSourceRefV1(owner_id="PRIVATE_OWNER", scope="assignment_source", business_id="PRIVATE_TASK",
                               stored_file_id="PRIVATE_FILE", original_name="PRIVATE_NAME",
                               content_type="image/png", input_sha256="a" * 64),
        RecognitionPolicyV1(**policy),
        EngineCapabilitiesV1(route_id="PRIVATE_ROUTE", fingerprint="PRIVATE_FINGERPRINT", visual_inputs=["page_image"],
                             response_recheck=True, semantic_repair=True, bounded_output_tokens=True),
    )


def budget(**policy):
    return RecognitionBudget(*context(**policy), clock=Clock())


def initial(value, key="p1", tokens=100, outcome=None):
    ticket = value.reserve("initial", tokens, region_keys=(key,))
    if outcome:
        value.settle(ticket, 10, 1, outcome=outcome)
    return ticket


@pytest.mark.parametrize("index,field,changed,code", [
    (0, "owner_id", "OTHER", "recognition_source_mismatch"),
    (0, "scope", "submission_source", "recognition_source_mismatch"),
    (0, "business_id", "OTHER", "recognition_source_mismatch"),
    (0, "input_sha256", "b" * 64, "recognition_source_mismatch"),
    (0, "stored_file_id", "OTHER", "recognition_source_mismatch"),
    (1, "max_initial_calls", 1, "recognition_plan_changed"),
    (1, "force_visual", True, "recognition_plan_changed"),
    (1, "read_seconds", 2, "recognition_plan_changed"),
    (1, "max_locator_calls", 1, "recognition_plan_changed"),
    (2, "route_id", "OTHER", "recognition_route_changed"),
    (2, "fingerprint", "OTHER", "recognition_route_changed"),
    (2, "response_recheck", False, "recognition_route_changed"),
    (2, "bounded_output_tokens", False, "recognition_route_changed"),
])
def test_context_is_complete_frozen_and_owner_bound(index, field, changed, code):
    original = context()
    saved = tuple(item.model_copy(deep=True) for item in original)
    value = RecognitionBudget(*original, clock=Clock())
    setattr(original[index], field, changed)
    with pytest.raises(RecognitionError) as exc:
        value.assert_context(*original)
    assert exc.value.code == code and "PRIVATE" not in str(exc.value)
    value.assert_context(*saved)


def test_capability_lists_are_copied_and_mutated_models_revalidated():
    source, policy, caps = context()
    value = RecognitionBudget(source, policy, caps, clock=Clock())
    caps.visual_inputs.append("document")
    initial(value)
    with pytest.raises(RecognitionError, match="recognition_route_changed"):
        value.assert_context(source, policy, caps)
    policy.max_calls = 999
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        RecognitionBudget(source, policy, caps)
    source.input_sha256 = "PRIVATE_NOT_A_HASH"
    with pytest.raises(RecognitionError, match="recognition_request_invalid") as exc:
        value.assert_context(source, policy, caps)
    assert "PRIVATE" not in str(exc.value)


def test_native_only_has_zero_usage_without_inventing_capabilities():
    source, policy, _ = context()
    value = RecognitionBudget(source, policy, None, clock=Clock())
    value.assert_context(source, policy, None)
    snapshot = value.snapshot()
    assert snapshot.total_calls == 0 and snapshot.usage_complete
    assert snapshot.input_tokens == snapshot.output_tokens == 0
    with pytest.raises(RecognitionError, match="recognition_input_unsupported"):
        initial(value)


def test_one_global_deadline_and_phase_start_never_reset_or_clip_to_per_call():
    clock = Clock()
    value = RecognitionBudget(*context(total_seconds=900), clock=clock)
    assert value.remaining() == 900
    clock.now = 100
    assert value.remaining("locator") == 360
    clock.now = 200
    value.start_phase("locator")
    assert value.remaining("locator") == 260
    clock.now = 450
    assert value.remaining("read") == 450
    clock.now = 460
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        value.start_phase("locator")
    assert value.remaining("read") == 440
    clock.now = 900
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        value.remaining()
    snapshot = value.snapshot()
    assert snapshot.global_remaining_seconds == snapshot.locator_remaining_seconds == snapshot.read_remaining_seconds == 0
    assert snapshot.duration_ms == 900_000


@pytest.mark.parametrize("phase,limit", [("locator", 360), ("read", 600)])
def test_phase_deadline_is_independent_of_larger_global_limit(phase, limit):
    clock = Clock()
    value = RecognitionBudget(*context(total_seconds=900), clock=clock)
    value.start_phase(phase)
    clock.now = limit
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        value.remaining(phase)
    assert value.remaining() == 900 - limit


def test_global_deadline_starts_at_construction_and_applies_to_reserve():
    clock = Clock()
    value = RecognitionBudget(*context(total_seconds=5), clock=clock)
    clock.now = 5
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        initial(value)
    assert value.snapshot().total_calls == 0


def test_late_settlement_keeps_known_spend_after_timeout():
    clock = Clock()
    value = RecognitionBudget(*context(total_seconds=5), clock=clock)
    ticket = initial(value)
    clock.now = 6
    snapshot = value.settle(ticket, 30, 20, outcome="ok")
    assert snapshot.output_tokens == 20 and snapshot.duration_ms == 6000
    with pytest.raises(RecognitionError, match="recognition_timeout"):
        initial(value, "p2")


def test_known_usage_releases_only_unused_reservation_not_spent_tokens():
    value = budget(max_output_tokens=100)
    ticket = initial(value)
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        value.output_limit()
    snapshot = value.settle(ticket, 40, 30, outcome="ok")
    assert snapshot.usage_complete and snapshot.known_output_tokens == snapshot.charged_output_tokens == 30
    assert snapshot.reserved_output_tokens == 0 and value.output_limit(4096) == 70
    initial(value, "p2", tokens=70)
    assert value.snapshot().charged_output_tokens == 100


@pytest.mark.parametrize("bounded", [True, False])
def test_unknown_usage_is_not_zero_even_for_ocr_without_token_control(bounded):
    source, policy, caps = context(max_output_tokens=150)
    caps.bounded_output_tokens = bounded
    caps.candidate_kind = "ocr" if not bounded else "vision"
    value = RecognitionBudget(source, policy, caps, clock=Clock())
    ticket = initial(value)
    snapshot = value.settle(ticket, None, None, outcome="ok")
    assert snapshot.bounded_output_tokens is bounded and not snapshot.usage_complete
    assert snapshot.input_tokens is None and snapshot.output_tokens is None
    assert snapshot.unknown_input_calls == snapshot.unknown_output_calls == 1
    assert snapshot.reserved_output_tokens == (100 if bounded else 0)
    assert snapshot.charged_output_tokens == (100 if bounded else None)
    assert snapshot.known_input_tokens == snapshot.known_output_tokens == 0
    assert snapshot.pending_calls == 0 and value.output_limit() == (50 if bounded else 4096)


def test_unbounded_ocr_can_use_all_twelve_calls_without_fake_token_budget():
    source, policy, caps = context(max_output_tokens=1)
    caps.bounded_output_tokens = False
    caps.candidate_kind = "ocr"
    value = RecognitionBudget(source, policy, caps, clock=Clock())
    for index in range(12):
        assert value.output_limit() == 4096
        ticket = initial(value, f"p{index}", tokens=4096)
        value.settle(ticket, None, None, outcome="ok")
    snapshot = value.snapshot()
    assert snapshot.initial_calls == snapshot.settled_calls == 12
    assert snapshot.unknown_input_calls == snapshot.unknown_output_calls == 12
    assert snapshot.input_tokens is None and snapshot.output_tokens is None
    assert snapshot.known_input_tokens == snapshot.known_output_tokens == snapshot.reserved_output_tokens == 0
    assert snapshot.charged_output_tokens is None and not snapshot.bounded_output_tokens
    assert not snapshot.usage_complete and not snapshot.output_limit_exceeded
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        initial(value, "p13", tokens=4096)
    caps.bounded_output_tokens = True
    with pytest.raises(RecognitionError, match="recognition_route_changed"):
        value.assert_context(source, policy, caps)
    assert value.output_limit() == 4096


@pytest.mark.parametrize("requested", [0, -1, True, 1.5, 32769])
def test_unbounded_output_hint_still_requires_valid_requested_value(requested):
    source, policy, caps = context()
    caps.bounded_output_tokens = False
    value = RecognitionBudget(source, policy, caps, clock=Clock())
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        value.output_limit(requested)


@pytest.mark.parametrize("bounded", [True, False])
def test_cancellation_or_uncertain_submit_remains_pending_and_cannot_be_reissued(bounded):
    source, policy, caps = context(max_output_tokens=200)
    caps.bounded_output_tokens = bounded
    value = RecognitionBudget(source, policy, caps, clock=Clock())
    initial(value)
    snapshot = value.snapshot()
    assert snapshot.pending_calls == 1 and snapshot.reserved_output_tokens == (100 if bounded else 0)
    assert snapshot.input_tokens is None and snapshot.output_tokens is None
    value.start_phase("read")
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        initial(value)
    for kind in ("patch", "empty_recovery"):
        with pytest.raises(RecognitionError, match="recognition_request_invalid"):
            value.reserve(kind, 50, region_keys=("p1",))
    assert value.snapshot().total_calls == 1


@pytest.mark.parametrize("bounded", [True, False])
def test_reported_overrun_applies_only_to_bounded_route_and_preserves_actual_usage(bounded):
    source, policy, caps = context(max_output_tokens=1000)
    caps.bounded_output_tokens = bounded
    value = RecognitionBudget(source, policy, caps, clock=Clock())
    snapshot = value.settle(initial(value, tokens=20), 30, 21, outcome="ok")
    assert snapshot.known_output_tokens == snapshot.output_tokens == 21
    assert snapshot.known_input_tokens == snapshot.input_tokens == 30
    assert snapshot.charged_output_tokens == (21 if bounded else None)
    assert snapshot.output_limit_exceeded is bounded
    if bounded:
        with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
            value.output_limit(1)
        with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
            initial(value, "p2", tokens=1)
    else:
        assert snapshot.usage_complete and value.output_limit(1) == 1
        initial(value, "p2", tokens=1)


def test_settlement_is_exactly_once_and_tickets_bind_to_budget_instance():
    first, second = budget(), budget()
    ticket, other_ticket = initial(first), initial(second)
    for wrong in (ticket, BudgetTicket(), copy.copy(other_ticket), object()):
        with pytest.raises(RecognitionError, match="recognition_request_invalid"):
            second.settle(wrong, 1, 1, outcome="ok")
    second.settle(other_ticket, 1, 1, outcome="ok")
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        second.settle(other_ticket, 0, 0, outcome="empty")
    assert first.snapshot().pending_calls == 1
    assert second.snapshot().known_output_tokens == 1


@pytest.mark.parametrize("outcome,kind,allowed", [
    ("ok", "patch", True), ("ok", "empty_recovery", False),
    ("empty", "patch", False), ("empty", "empty_recovery", True),
    ("failed", "patch", False), ("failed", "empty_recovery", False),
])
def test_extra_requires_matching_completed_initial_outcome(outcome, kind, allowed):
    value = budget()
    initial(value, outcome=outcome)
    if allowed:
        value.reserve(kind, 10, region_keys=("p1",))
    else:
        with pytest.raises(RecognitionError, match="recognition_request_invalid"):
            value.reserve(kind, 10, region_keys=("p1",))


@pytest.mark.parametrize("kind,capability,enabled", [
    ("empty_recovery", "response_recheck", False),
    ("patch", "response_recheck", False), ("patch", "semantic_repair", False),
    ("patch", "enable_repair", False),
])
def test_extra_respects_frozen_capabilities_and_policy(kind, capability, enabled):
    source, policy, caps = context()
    setattr(policy if capability == "enable_repair" else caps, capability, enabled)
    value = RecognitionBudget(source, policy, caps, clock=Clock())
    initial(value, outcome="empty" if kind == "empty_recovery" else "ok")
    with pytest.raises(RecognitionError, match="recognition_input_unsupported"):
        value.reserve(kind, 10, region_keys=("p1",))


def test_group_initial_covers_each_region_once_and_extra_shares_region_quota():
    value = budget()
    ticket = value.reserve("initial", 100, region_keys=("p1", "p2", "p3"))
    value.settle(ticket, 10, 1, outcome="ok")
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        value.reserve("initial", 10, region_keys=("p3", "p4"))
    extra = value.reserve("patch", 10, region_keys=("p2",))
    value.settle(extra, 2, 1, outcome="ok")
    for kind in ("patch", "empty_recovery"):
        with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
            value.reserve(kind, 10, region_keys=("p2",))
    value.reserve("patch", 10, region_keys=("p1",))
    assert value.snapshot().initial_calls == 1 and value.snapshot().patch_calls == 2


def test_locator_and_read_call_caps_are_separate_but_share_output_budget():
    value = budget()
    for _ in range(12):
        value.settle(value.reserve("locator", 1), 1, 1, outcome="ok")
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        value.reserve("locator", 1)
    for i in range(12):
        initial(value, str(i), tokens=1, outcome="empty" if i < 2 else "ok")
    for i in range(6):
        kind = "empty_recovery" if i < 2 else "patch"
        value.reserve(kind, 1, region_keys=(str(i),))
    snapshot = value.snapshot()
    assert snapshot.locator_calls == 12 and snapshot.read_calls == 18 and snapshot.total_calls == 30
    assert snapshot.empty_recovery_calls == 2 and snapshot.patch_calls == 4
    assert snapshot.charged_output_tokens == 30
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        initial(value, "new", tokens=1)


@pytest.mark.parametrize("policy,kind,keys", [
    ({"max_locator_calls": 0}, "locator", ()),
    ({"max_initial_calls": 0}, "initial", ("p1",)),
    ({"max_calls": 0}, "initial", ("p1",)),
    ({"max_regions": 1}, "initial", ("p1", "p2")),
    ({"max_output_tokens": 1}, "initial", ("p1",)),
])
def test_tighter_policy_caps_fail_without_reservation(policy, kind, keys):
    value = budget(**policy)
    with pytest.raises(RecognitionError, match="recognition_budget_exhausted"):
        value.reserve(kind, 2, region_keys=keys)
    assert value.snapshot().total_calls == 0


def test_parallel_reserve_is_atomic_for_output_limit_and_duplicate_regions():
    value = budget(max_output_tokens=25)

    def dispatch(index):
        try:
            return initial(value, f"p{index}", tokens=10)
        except RecognitionError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(dispatch, range(12)))
    assert sum(ticket is not None for ticket in results) == 2
    assert value.snapshot().charged_output_tokens == 20
    value = budget()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(dispatch, [0] * 12))
    assert sum(ticket is not None for ticket in results) == 1


@pytest.mark.parametrize("kind,tokens,keys", [
    ("wrong", 1, ()), ([], 1, ()), ("initial", True, ("p1",)),
    ("initial", 0, ("p1",)), ("initial", 32769, ("p1",)),
    ("initial", 1, ()), ("initial", 1, ["p1"]),
    ("initial", 1, ("p1", "p1")), ("initial", 1, ("",)),
    ("initial", 1, ([],)), ("locator", 1, ("p1",)),
    ("patch", 1, ("p1", "p2")),
])
def test_invalid_reservations_are_safe_and_do_not_change_ledger(kind, tokens, keys):
    value = budget()
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        value.reserve(kind, tokens, region_keys=keys)
    assert value.snapshot().total_calls == 0


@pytest.mark.parametrize("input_tokens,output_tokens,outcome", [
    (-1, 0, "ok"), (1, -1, "ok"), (True, 0, "ok"),
    (0, False, "ok"), (0, 1.5, "ok"), (0, 0, "cancelled"), (0, 0, []),
])
def test_invalid_settlement_keeps_pending_reservation(input_tokens, output_tokens, outcome):
    value = budget()
    ticket = initial(value)
    with pytest.raises(RecognitionError, match="recognition_response_invalid"):
        value.settle(ticket, input_tokens, output_tokens, outcome=outcome)
    assert value.snapshot().pending_calls == 1 and value.snapshot().reserved_output_tokens == 100


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True, -1])
def test_invalid_or_backward_clock_is_not_a_deadline_extension(now):
    clock = Clock()
    value = RecognitionBudget(*context(), clock=clock)
    clock.now = now
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        value.remaining()


def test_snapshot_is_detached_and_contains_no_source_route_or_region_identifiers():
    value = budget()
    initial(value, "PRIVATE_REGION")
    snapshot = value.snapshot()
    assert "PRIVATE" not in snapshot.model_dump_json()
    snapshot.charged_output_tokens = 0
    assert value.snapshot().charged_output_tokens == 100
    assert value.snapshot().locator_duration_ms is None
    with pytest.raises(RecognitionError, match="recognition_request_invalid"):
        value.remaining([])
