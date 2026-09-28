"""Pure in-memory byte-cache tests; source access remains an outer-layer check."""
from concurrent.futures import ThreadPoolExecutor
import math

import pytest

from backend.domain.errors import RecognitionError
from backend.recognition.cache_identity import RecognitionCacheIdentityV1, native_cache_identity, render_cache_identity
from backend.recognition.local_cache import MAX_BYTES, MAX_ENTRIES, MAX_TTL_SECONDS, RecognitionByteCache
from backend.recognition.models import RecognitionSourceRefV1
from backend.tools.pdf_evidence import PdfIndexRequest, PdfRenderRequest


def source(**changes):
    values = dict(owner_id="owner", scope="assignment_source", business_id="task", stored_file_id="source-file",
                  input_sha256="a" * 64, content_type="application/pdf")
    values.update(changes)
    return RecognitionSourceRefV1(**values)


def context(page=1, *, original=None, render=False):
    original = original or source()
    identity = (render_cache_identity(original, PdfRenderRequest(page_number=page)) if render else
                native_cache_identity(original, PdfIndexRequest(start_page=page)))
    return dict(identity=identity, source=original, authorized_owner_id=original.owner_id)


def cache(**changes):
    now = [100.0]
    return RecognitionByteCache(clock=lambda: now[0], **changes), now


def assert_invalid(call):
    with pytest.raises(RecognitionError) as caught:
        call()
    assert caught.value.code == "recognition_artifact_invalid"
    assert "PRIVATE_BODY" not in str(caught.value)


def test_default_and_exact_constructor_limits():
    instance = RecognitionByteCache(max_bytes=MAX_BYTES, ttl_seconds=MAX_TTL_SECONDS, max_entries=MAX_ENTRIES)
    assert instance.size_bytes == instance.entry_count == 0
    assert instance.put(**context(), value=b"native")
    assert instance.get(**context()) == b"native"


@pytest.mark.parametrize("field,value", [
    ("max_bytes", 0), ("max_bytes", -1), ("max_bytes", MAX_BYTES + 1), ("max_bytes", 1.0), ("max_bytes", True),
    ("max_entries", 0), ("max_entries", -1), ("max_entries", MAX_ENTRIES + 1), ("max_entries", 1.0), ("max_entries", True),
    ("ttl_seconds", 0), ("ttl_seconds", -1), ("ttl_seconds", MAX_TTL_SECONDS + 1), ("ttl_seconds", True),
    ("ttl_seconds", float("nan")), ("ttl_seconds", float("inf")), ("ttl_seconds", -float("inf")),
    ("ttl_seconds", "1800"), ("ttl_seconds", 10 ** 1000), ("clock", None),
])
def test_invalid_constructor_configuration_is_rejected(field, value):
    with pytest.raises(ValueError, match="invalid recognition cache limits"):
        RecognitionByteCache(**{field: value})


def test_exact_bytes_boundary_and_oversized_values_do_not_evict_other_entries():
    instance, _ = cache(max_bytes=4)
    assert instance.put(**context(), value=b"1234")
    assert instance.size_bytes == 4
    assert not instance.put(**context(2), value=b"12345")
    assert instance.get(**context()) == b"1234"
    assert instance.entry_count == 1
    assert not instance.put(**context(), value=b"12345")
    assert instance.get(**context()) is None
    assert instance.size_bytes == instance.entry_count == 0


def test_native_and_render_share_the_total_lru_budget():
    instance, _ = cache(max_bytes=6)
    assert instance.put(**context(), value=b"abc")
    assert instance.put(**context(render=True), value=b"123")
    assert instance.size_bytes == 6 and instance.entry_count == 2
    assert instance.get(**context()) == b"abc"
    assert instance.put(**context(2), value=b"xyz")
    assert instance.get(**context(render=True)) is None
    assert instance.get(**context()) == b"abc" and instance.get(**context(2)) == b"xyz"
    assert instance.size_bytes == 6 and instance.entry_count == 2


def test_entry_cap_bounds_even_empty_payloads():
    instance, _ = cache(max_bytes=1, max_entries=2)
    assert instance.put(**context(), value=b"")
    assert instance.put(**context(2), value=b"")
    assert instance.put(**context(3), value=b"")
    assert instance.get(**context()) is None
    assert instance.get(**context(2)) == instance.get(**context(3)) == b""
    assert instance.entry_count == 2 and instance.size_bytes == 0


def test_replacement_accounts_old_bytes_once_and_refreshes_only_on_write():
    instance, now = cache(max_bytes=5, ttl_seconds=10)
    instance.put(**context(), value=b"12345")
    now[0] += 5
    instance.put(**context(), value=b"ab")
    assert instance.size_bytes == 2 and instance.entry_count == 1
    now[0] += 9
    assert instance.get(**context()) == b"ab"
    now[0] += 1
    assert instance.get(**context()) is None and instance.size_bytes == 0


def test_get_changes_lru_order_but_never_extends_absolute_ttl():
    instance, now = cache(ttl_seconds=10)
    instance.put(**context(), value=b"abc")
    now[0] = 109.999
    assert instance.get(**context()) == b"abc"
    now[0] = 110
    assert instance.get(**context()) is None
    assert instance.entry_count == instance.size_bytes == 0


def test_stats_prune_expired_entries_even_without_get():
    instance, now = cache(ttl_seconds=10)
    instance.put(**context(), value=b"old")
    now[0] += 2
    instance.put(**context(2), value=b"newer")
    now[0] = 110
    assert instance.size_bytes == 5 and instance.entry_count == 1
    now[0] = 112
    assert instance.entry_count == 0 and instance.size_bytes == 0


@pytest.mark.parametrize("field,value", [
    ("scope", "submission_source"), ("business_id", "other-task"), ("stored_file_id", "other-file"),
])
def test_same_owner_hash_identity_is_still_bound_to_source_scope_and_file(field, value):
    instance, _ = cache()
    first = context()
    second = context(original=source(**{field: value}))
    assert first["identity"] == second["identity"]
    instance.put(**first, value=b"body")
    assert instance.get(**second) is None


def test_owner_and_source_hash_isolation_even_when_payloads_match():
    instance, _ = cache()
    first, other_owner, other_hash = context(), context(original=source(owner_id="other")), context(original=source(input_sha256="b" * 64))
    instance.put(**first, value=b"body")
    assert instance.get(**other_owner) is None and instance.get(**other_hash) is None
    instance.put(**other_owner, value=b"other body")
    assert instance.clear_owner(owner_id="owner", authorized_owner_id="owner") == 1
    assert instance.get(**other_owner) == b"other body"


@pytest.mark.parametrize("owner", ["other", "", None, True])
def test_explicit_authorized_owner_is_required_on_every_content_boundary(owner):
    instance, _ = cache()
    values = context()
    values["authorized_owner_id"] = owner
    assert_invalid(lambda: instance.get(**values))
    assert_invalid(lambda: instance.put(**values, value=b"PRIVATE_BODY"))
    assert_invalid(lambda: instance.invalidate_source(source=values["source"], authorized_owner_id=owner))
    assert_invalid(lambda: instance.clear_owner(owner_id="owner", authorized_owner_id=owner))


@pytest.mark.parametrize("field,value", [
    ("owner_id", "other"), ("source_sha256", "b" * 64), ("source_content_type", "image/png"),
    ("parameters_sha256", "invalid"), ("tool_version", ""),
])
def test_mutated_identity_is_revalidated_before_use(field, value):
    instance, _ = cache()
    values = context()
    setattr(values["identity"], field, value)
    assert_invalid(lambda: instance.put(**values, value=b"body"))
    assert_invalid(lambda: instance.get(**values))


@pytest.mark.parametrize("stored_file_id", [None, "", "   "])
def test_original_file_identity_is_mandatory(stored_file_id):
    instance, _ = cache()
    values = context(original=source(stored_file_id=stored_file_id))
    assert_invalid(lambda: instance.get(**values))
    assert_invalid(lambda: instance.put(**values, value=b"body"))
    assert_invalid(lambda: instance.invalidate_source(source=values["source"], authorized_owner_id="owner"))


@pytest.mark.parametrize("layer", ["visual", "patch", "final"])
def test_model_layers_are_not_accepted_by_local_media_cache(layer):
    instance, _ = cache()
    values = context()
    values["identity"] = RecognitionCacheIdentityV1(
        layer=layer, owner_id="owner", source_sha256="a" * 64, source_content_type="application/pdf",
        parameters_sha256="b" * 64, tool_version="v1", purpose="problems", engine_fingerprint="fake",
        capabilities_sha256="c" * 64, policy_sha256="d" * 64, prompt_version="v1",
    )
    assert_invalid(lambda: instance.get(**values))
    assert_invalid(lambda: instance.put(**values, value=b"body"))


def test_input_models_are_snapshotted_as_immutable_keys_without_aliases():
    instance, _ = cache()
    values = context()
    instance.put(**values, value=b"original")
    values["source"].stored_file_id = "different-file"
    values["identity"].tool_version = "changed"
    assert instance.get(**values) is None
    assert instance.get(**context()) == b"original"
    assert "original" not in repr(instance)


@pytest.mark.parametrize("value", [bytearray(b"mutable"), memoryview(b"body"), "text", None, 1])
def test_only_immutable_bytes_are_retained(value):
    instance, _ = cache()
    assert_invalid(lambda: instance.put(**context(), value=value))
    assert instance.entry_count == 0


def test_invalidate_clears_all_operations_for_only_the_exact_source():
    instance, _ = cache()
    same_source = [context(), context(2), context(render=True)]
    others = [context(original=source(business_id="other-task")), context(original=source(stored_file_id="other-file")),
              context(original=source(input_sha256="b" * 64)), context(original=source(scope="submission_source"))]
    for values in [*same_source, *others]:
        instance.put(**values, value=b"body")
    assert instance.invalidate_source(source=source(), authorized_owner_id="owner") == 3
    assert all(instance.get(**values) is None for values in same_source)
    assert all(instance.get(**values) == b"body" for values in others)
    assert instance.entry_count == 4 and instance.size_bytes == 16
    assert instance.clear_owner(owner_id="owner", authorized_owner_id="owner") == 4
    assert instance.size_bytes == instance.entry_count == 0


@pytest.mark.parametrize("invalid_time", [float("nan"), float("inf"), -float("inf"), True, "100", None, 99.0, 10 ** 1000])
def test_invalid_and_regressing_clock_discards_entries_without_extending_expiry(invalid_time):
    instance, now = cache()
    instance.put(**context(), value=b"body")
    now[0] = invalid_time
    assert instance.get(**context()) is None
    now[0] = 200
    assert instance.size_bytes == instance.entry_count == 0
    assert instance.put(**context(), value=b"fresh")
    assert instance.get(**context()) == b"fresh"


def test_clock_failure_fails_put_closed_and_does_not_leak_exception():
    state = [False]

    def clock():
        if state[0]:
            raise RuntimeError("PRIVATE_BODY")
        return 100.0

    instance = RecognitionByteCache(clock=clock)
    assert instance.put(**context(), value=b"body")
    state[0] = True
    assert not instance.put(**context(2), value=b"new")
    assert instance.get(**context()) is None
    assert instance.size_bytes == instance.entry_count == 0


def test_clock_precision_that_cannot_represent_ttl_is_not_cached():
    instance = RecognitionByteCache(clock=lambda: 1e308)
    assert not instance.put(**context(), value=b"body")
    assert instance.size_bytes == instance.entry_count == 0


def test_concurrent_distinct_puts_gets_have_exact_accounting():
    instance, _ = cache(max_bytes=4096, max_entries=64)
    values = [context(number + 1) for number in range(64)]

    def write(number):
        payload = bytes([number]) * (number + 1)
        assert instance.put(**values[number], value=payload)
        assert instance.get(**values[number]) == payload

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(64)))
    assert instance.entry_count == 64
    assert instance.size_bytes == sum(range(1, 65))


def test_concurrent_evictions_invalidations_and_owner_clear_keep_budget_consistent():
    instance, _ = cache(max_bytes=64, max_entries=8)
    values = [context(page + 1, original=source(owner_id="owner" if page % 2 else "other")) for page in range(24)]

    def change(number):
        selected = values[number % len(values)]
        instance.put(**selected, value=bytes([number % 256]) * 8)
        instance.get(**selected)
        if number % 11 == 0:
            instance.invalidate_source(source=selected["source"], authorized_owner_id=selected["authorized_owner_id"])
        if number % 17 == 0:
            instance.clear_owner(owner_id=selected["authorized_owner_id"], authorized_owner_id=selected["authorized_owner_id"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(change, range(400)))
    retained = [instance.get(**item) for item in values]
    assert instance.entry_count == sum(value is not None for value in retained) <= 8
    assert instance.size_bytes == sum(len(value) for value in retained if value is not None) <= 64
    assert math.isfinite(instance.size_bytes)
