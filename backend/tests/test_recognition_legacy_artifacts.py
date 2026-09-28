"""Static PR101 schema-1 bytes, independent of today's fixture/model builders."""
import base64
import gzip
import hashlib
import json

import pytest

from backend.recognition.artifact_codec import decode_artifact, encode_artifact


# Captured from PR101 commit 40b1c2bfa73fec51ceeaf38c6756ef47a6281110.
# All content is synthetic: artifact_codec test helpers for visual/locator/repair;
# assembly has two model reads, one known (10/2) and one unknown usage outcome.
# Runtime generation is deliberately forbidden: V2/default-field changes must
# not regenerate these historical bytes or silently revise their SHA values.
LEGACY_FIXTURES = {
    "visual_read": {
        "encoded_sha256": "f4c044df285760a044da673b1254ef61de11d80197ef672640242d26b76a9035",
        "payload_sha256": "d3f317ad541bb28f4b8720d36b5b5c0c88bc1f413b29d28b682c6738bd0bf45b",
        "identity_key": "7b676cf2a7cb1a98b019b509281786ae5bd67ec463aa6732dac313fabbb2efdc",
        "base64": (
            "H4sIAAAAAAAAE61UWW7bQAy9SqBv25XkJYqB3qA3aIPBLJQ98CzqLI6VwHcvOYoXBAX64c6PZYoiH997nI9KepcCl6naVtHykLhe"
            "BJB+53TS3i0wont6Pau0Apd0GqvtRyX5wIU2mAORxT1v1xssIB882ATcTjtgvXY7CEPQjoD1wb+Dw7eGjxAwcNQxc4MB/+YgMK0w"
            "Vh4xNPDALSQId8DgwUNlvdFyvJVUDx4qGbwdEjsiVKSa5tSnlAPMjw29zWHwETCMecKAjRiMPgcJjERDMVgaB0rgw4DgOAn2bVD9"
            "Le+Klj94sGTy3txhFT47BWqO/eba8l1BfSb6R+O5mkzilFY8Af1RORSADOfYumzMDMd1Ou5ZAB6p5BTUbsg4mD+Aw8SmnlUH7dSk"
            "OXWeVdYrMJd054PlRr+DYglO6RL2Od2XaQvZR3RwYAHfwWQZuUeCqWTkPTAIwQekVsGlSkw85UjeOhABpX71Q6O3uHk6PX1/mreL"
            "X/T9Gw8OHVs+xvyfr0hEIYUNAciQSX+d0JY+SBcmldTqitqinliNNhJ/DCBDu2n0ku2yFSgDtmler3wzMSYohNW32FV+8eDB1p8Y"
            "UMlTXW3rBbY5NdiPHsZLZJwiZ0r/nSEmlOWLFKv6ZYPMZmF1jMUPfGRw0hG57bmJMKsy3j2TQLnGc+cqdvMC7j85p+zR13HVsl82"
            "z1ytV40QbdevRPfc1mq5EWuxlrXsOiGbftUsRfui2k5sulZunpedULXoV2saN8o9WH7ze3PZKWJAZBQGYpxAJh7JHv/cyUn3/7mS"
            "Pmi8LZEIhzce0TWU1fvrtRiln3Ah6ztnCennQORzH1CpXptpMcin5/MfGPEVGxwGAAA="
        ),
    },
    "locator": {
        "encoded_sha256": "fac80889f1fb4e4f947c2801f9a377077a86580d5a97636d800fe6cfb27369e2",
        "payload_sha256": "d91ddb32ac7d3845b482a2541b21885cf9d0186e210dee87782101545cbe5754",
        "identity_key": "7b676cf2a7cb1a98b019b509281786ae5bd67ec463aa6732dac313fabbb2efdc",
        "base64": (
            "H4sIAAAAAAAAE61W0XLqOAz9lTt+TnsTCCHhL/blvrRMxolF8NSxc22nhcvw73vsAIFuZ/eh6xkGLMvSkXQkc2Kt0d7y1rMNcz23"
            "nstnS63ptPTS6GdI5C4cJ0wK0l76I9ucWMsH3kgFHXK12/PFqoCB9psLTkh3UlO9k7ojO1ipA7CdNX9I41TxI1kI3qUbuYLAfGiy"
            "tRSQxZ8QDdzynjzZO2D0zRXMGiXb42xSfHMFk9b0g6/fARWpDnHKgx8tPb1n4XS0g3EEMfQaRb2D0JnRtlSHoqEYtT8OQYEPA8Dx"
            "ULCfg9jNeje0/JsLJr0x6g5rY0YtSDzB35PseRdRn0P6j8pwEUhi6fdIzpOozeiHEXDNG2nHNnlaFQmO3aj8xCYtpOCewkaMNkZS"
            "I+CNHpVKkBct3b62xF3wPQmlvjeZpQl7k1pM5AgQE9YbQeqqro3tuZJ/gMbTwV/Fn5AtYlXeQXVbW5zRxK12j0oEk47vqCZrjUUN"
            "BF2tOM/96AIJ30Kmon12emXKTFVxr2zz4wUCz21HPuxe2V/ZK0vwPV2eZLdMTEcD8jrdzbZhTwGZbmlS/oVAQYwfwdB5e4bnD241"
            "OidiA5yXLQryHw3u4LKOOE3oHqndQG2oWfQNG/A8x4H96YYCFmcIsfPmG7eMzLVNLtFDCPXzNraqA0kfkjcV+RYBm/EF//WoQUG5"
            "kxQaKJ7NiF1rBqqNVke2Te7YN7mN1uA4gGv31POZzBlEpD6Hjdy5PU0XTwwjhSMMHvn62H6R/j8H3bEvs40Oeb6lLGF7kt0e52Va"
            "gn4DTWwPmcJV3Kyj00s6az32DWBes/oF8EfqSUXIFihAogO2dVFOwimIyXc9GMzV2DTpc3px1DTmUA/yQCqolslykazXRVKm6fYB"
            "SnQa95a6COIFRrJ0keZpmRXLRbFapkUC2bIqHj45hFVZratVlZXLYr3MiyqIqjS9+2SxdDo04DVTMYiL7EMKv7+KjL/kLr1E/jmI"
            "8i6GqBC6Gfijlcc0BD56GFRXBlzV4KzM58E2z9Tmmyu2gBubXjoX5x0/1nSQDhHvuHJ05/My2eY2/QcYUWVCNMsFb9diWearJi8X"
            "OMqzZpGV5ardVSLNyoIWWSqIyvW6xK9sla/ahlbrVc6+Jld8RgLlm9HhXXZumoeeu7cL2f/1GZpm9P/5Chkr8QcBVdJ45GFyHOJr"
            "8+U/gTgPglvkt9N9QHoJKLSNsej23ZUUYY6fz38DMa9z0g8JAAA="
        ),
    },
    "repair": {
        "encoded_sha256": "3ec7c8898ff75df95bdd67b0bf6c67a1a2b0f80dc6683564e84b5abc0ebb6aee",
        "payload_sha256": "0ad22a445545ce2874184f24ebe39e10bb6a70910e595845037d4b37dcf1660b",
        "identity_key": "d58b36ca6b272e0c94f42f9c5c8d53e3cd233b7f193c77e4f0decadba56db790",
        "base64": (
            "H4sIAAAAAAAAE7VV23LbOAz9lQyf7VRS5Vw8s7+xL02HQ5GQzIlEcnlxrWb87wVI35Im7XTc5YPGBkjw4BwAfGHSmuiFjGzNwiR8"
            "FPrWg7SD0VFbc4sW3ZN7wbQCE3Wc2fqFSeFEp0fcA4GHjWhWdxhAXrnwEjCDNsB7bQbwzmtDwHpvv4NB7yhm8GhwIsoN/rffDHiu"
            "FZryTzQ54cUEEfwFLrhyUVg7ajmfQ6orF4X0dnKRbxEqMk1p6l1MHpbbmrzJOxuAkvW2G2EKaAw2eQmcNEMteJwdbRDOIThBen1y"
            "qj/vO6EVVy4MGa0dL7B2NhkFaon3LfUkhox6T/TPoxWKaiSbc7G8hpvtn5wZMOoG9LBBiduqWhQHdx5Iw5jvMWkcKSjaTZo6Er8+"
            "XcK7OUJACx0+2k45d1eurNAFFIapcg+YtedBihF4g1s8DNn7wnYVW1e3iGRXIyT6MR8tc7Hs/xdhvmkVN2zdVBTfw38JQgTFbYou"
            "Ief2GUwgd/tA7pDGWBrYKK1EzAKpVJLkWGQHyrEBddhgviKcddDmMmSNmT1rQ8231bksFmyyCsbjdmP9JEb9HdFE2MWj+S2y3Alb"
            "Tbx69EHpZ7nB6qeQQfTAwXvrse4VHKOEKGIK1PjPVJ05Pnt5YgpkBvPE1jdP7BnAcUSXxPjEFmigjcW1u/nnZtkU60EWIBRGQtnw"
            "L8bBxrsJesBoe6JaeIODKePAq798RcZzce8ypx301sMh10N8mpshJDidYThtTZBeu8w4Hu+xeSP7inyhCNtX5xv2TvG/Nxj+tAyd"
            "MIVmV1VVvQz4rTBMYepNBockf/FIUJtoVA+CsybQuDyqgAcuJGC5rn66IA/tgF3xSlLMjvJUP20uNXkm9HD7qaJpggTwW1B5c3b2"
            "KCy+VjNPBmeY7jU6kfEgNzCJ81irF29LAS++rAMi46OCTN2EUuc2EjOHnQ4IuxdjgAVLyFQhPGWqz5OSH1qoAGXvDLJKqKYRbbta"
            "tSsJzcN9Wz+0fdNCB58foa667k7cV491BavH1UO7qj7fq7bDj+zru7uKBtmHeeayTQGf3BAKvigCsf/bJ6bMgr85yKzXQ64Ogw84"
            "MeXyS/LuKx+kLbgCqTIR0kNCNBmwCxVKPpZRQvLs9z8AyXRHh+oIAAA="
        ),
    },
    "assembly": {
        "encoded_sha256": "05f59294ea7cd0ac666ef0d9abc19ed7c9e884ea648834a2c684268c806c7ad0",
        "payload_sha256": "8f16402eba1b0815fd707fd807b50fb658d94f0f1acd9a7b8fb817d271821bcc",
        "identity_key": "35ae565a153a61c9db3e84afd5fe5a0e173894164efa529550fc7b29a4349442",
        "base64": (
            "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"
        ),
    },
}


def _canonical_hash(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _field_paths(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield path + (key,)
            yield from _field_paths(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _field_paths(item, path + (str(index),))


@pytest.mark.parametrize("kind", list(LEGACY_FIXTURES))
def test_pr101_static_gzip_reencodes_without_hash_or_field_drift(kind):
    fixture = LEGACY_FIXTURES[kind]
    blob = base64.b64decode(fixture["base64"], validate=True)
    assert hashlib.sha256(blob).hexdigest() == fixture["encoded_sha256"]
    # This small, checked-in gzip is trusted fixture data, not user input.
    historical = json.loads(gzip.decompress(blob))
    assert historical["contract"] == "smartai.recognition.artifact"
    assert historical["schema_version"] == 1 and historical["payload_kind"] == kind
    assert _canonical_hash(historical["payload"]) == fixture["payload_sha256"]

    restored = decode_artifact(blob)
    assert restored.schema_version == 1
    assert restored.payload_sha256 == fixture["payload_sha256"]
    assert restored.identity.key == fixture["identity_key"]
    current = restored.model_dump(mode="json")
    assert set(_field_paths(current)) == set(_field_paths(historical))
    assert current == historical
    assert _canonical_hash(current["payload"]) == fixture["payload_sha256"]
    encoded = encode_artifact(restored)
    # gzip OS headers and deflate output vary by platform/zlib version. The
    # immutable fixture SHA above and canonical uncompressed bytes must not drift.
    assert gzip.decompress(encoded) == gzip.decompress(blob)
    assert encode_artifact(restored) == encoded


@pytest.mark.parametrize("kind", ["visual_read", "locator", "repair"])
def test_legacy_per_call_model_usage_and_v1_payload_stay_literal(kind):
    payload = decode_artifact(base64.b64decode(LEGACY_FIXTURES[kind]["base64"], validate=True)).payload
    candidate = payload.candidate if kind == "visual_read" else payload.result.candidate
    assert candidate.status == "ok" and candidate.provider_route_id == "chosen"
    assert candidate.input_tokens == 10 and candidate.output_tokens == 2
    assert payload.requested_output_tokens == (2048 if kind == "repair" else 4096)
    assert not payload.submission_may_exist


def test_legacy_aggregate_keeps_known_and_unknown_model_usage_separate():
    envelope = decode_artifact(base64.b64decode(LEGACY_FIXTURES["assembly"]["base64"], validate=True))
    assembly = envelope.payload
    assert assembly.schema_version == assembly.raw.schema_version == assembly.raw.read_batch.schema_version == 1
    assert assembly.repair_execution is None
    assert [unit.candidate.text for unit in assembly.raw.read_batch.units] == ["Literal x = -2.", "Literal y = 3."]
    first, second = [unit.candidate for unit in assembly.raw.read_batch.units]
    assert (first.input_tokens, first.output_tokens) == (10, 2)
    assert second.input_tokens is second.output_tokens is None
    budget = assembly.raw.budget
    assert budget.total_calls == budget.initial_calls == budget.settled_calls == 2
    assert budget.known_input_tokens == 10 and budget.known_output_tokens == 2
    assert budget.unknown_input_calls == budget.unknown_output_calls == 1
    assert budget.input_tokens is budget.output_tokens is None
    assert not budget.usage_complete
    for usage in (assembly.raw.read_batch.usage, assembly.document.usage):
        assert usage.total_calls == 2 and not usage.usage_complete
        assert usage.input_tokens is usage.output_tokens is None


def test_legacy_repair_is_preserved_without_becoming_a_successful_cache_entry():
    envelope = decode_artifact(base64.b64decode(LEGACY_FIXTURES["repair"]["base64"], validate=True))
    assert envelope.payload.result.decision == "keep_visual"
    assert envelope.payload.result.final_text == "x = -2"
    assert not envelope.cacheable_success
