from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.recognition.locator import NativeLocatorRequestV1, locate_native_targets
from backend.recognition.models import RecognitionSourceRefV1
from backend.tools.pdf_evidence import PdfDetailPage, PdfIndexPage


def source():
    return RecognitionSourceRefV1(owner_id="owner-one", scope="assignment_source", business_id="task-one",
                                  stored_file_id="file-one", content_type="application/pdf", input_sha256="a" * 64)


def detail(number, text, *, parts=None):
    blocks = []
    offset = 0
    for index, part in enumerate(parts if parts is not None else [text]):
        if part:
            blocks.append(dict(order_index=index, kind="text", text=part,
                               native_char_start=offset, native_char_end=offset + len(part),
                               region=[0, index / 100, 1, (index + 1) / 100]))
        offset += len(part)
    return PdfDetailPage(page_number=number, page_index=number - 1, native_text=text, blocks=blocks,
                         width_points=600, height_points=800, rotation=0,
                         observation=dict(page_number=number, native_char_count=len(text),
                                          native_quality="clean" if text else "missing", verified_blank=False))


def summary(page, targets=()):
    values = page.model_dump(exclude={"page_index", "native_text", "blocks"})
    values["target_matches"] = list(targets)
    return PdfIndexPage.model_validate(values)


def locate(targets, pages=(), *, index=(), hints=None, total=1000):
    return locate_native_targets(NativeLocatorRequestV1(
        source=source(), total_pages=total, targets=list(targets), detail_pages=list(pages),
        index_pages=list(index), page_hints=hints or {},
    ))


def test_literal_boundaries_never_match_number_prefixes():
    result = locate(["1", "1.2", "Q1"], [detail(499, "11. 1.20 1.2.3 Q11 A1\n"),
                                              detail(500, "1. Prove\n1.2 Find\nQ1. Explain\n")])
    assert result.selected_pages == [500]
    assert result.missing_targets == result.ambiguous_targets == []
    assert [location.status for location in result.locations] == ["located"] * 3
    assert result.inspected_detail_pages == [499, 500]
    assert result.inspected_index_pages == []


@pytest.mark.parametrize("context,target", [
    ("Sec 1.1", "1.1.5"), ("Sec. 1 . 1", "1.1.5"),
    ("Section 2.7", "2.7.5"), ("Chapter 2\nSection 7", "2.7.5"),
    ("Chap. 4", "4.5"), ("第 2 章\n第 7 节", "2.7.5"),
])
def test_general_section_or_chapter_plus_local_exercise(context, target):
    page = detail(15, f"5. Prove the stated condition.\n{context}\n")
    result = locate([target], [page])
    location = result.locations[0]
    assert location.status == "located" and result.selected_pages == [15]
    assert location.reason_codes == ["native_section_local_label"]
    assert "local_label" in {proof.kind for proof in location.evidence}
    for proof in location.evidence:
        assert proof.matched_text == page.native_text[proof.native_char_start:proof.native_char_end]
        assert proof.page_number == 15 and proof.block_order_indices == [0]
    assert result.source == source()


@pytest.mark.parametrize("label", ["(5)", "（5）", "5 .", "Exercise 5.", "Problem 5)", "Question 5:"])
def test_common_local_question_labels(label):
    result = locate(["2.3.5"], [detail(7, f"{label} Find x.\nSection 2.3\n")])
    assert result.selected_pages == [7]


def test_composite_evidence_has_priority_over_incidental_full_id_reference():
    result = locate(["2.3.5"], [detail(2, "See 2.3.5 for comparison.\n"),
                                       detail(5, "5. Prove this.\nSec. 2.3\n")])
    assert result.selected_pages == [5]
    assert result.locations[0].candidate_pages == [2, 5]


def test_composite_label_does_not_override_strong_full_id_question_on_another_page():
    result = locate(["2.3.5"], [detail(1, "2.3.5 Prove A.\n"),
                                        detail(2, "5. Prove B.\nSec 2.3\n")])
    assert result.ambiguous_targets == ["2.3.5"] and result.selected_pages == []
    assert result.locations[0].candidate_pages == [1, 2]
    assert result.locations[0].resolved_scope == "none"


def test_inline_decimal_or_cross_reference_is_not_a_verified_question_label():
    page = detail(3, "Take x = 1.2 and see Exercise 1.2.\n")
    result = locate(["1.2"], [page])
    assert result.selected_pages == [] and result.missing_targets == ["1.2"]
    assert result.locations[0].candidate_pages == [3]
    assert "native_literal_requires_question_context" in result.locations[0].reason_codes
    assert len(result.locations[0].evidence) == 2


def test_same_numbered_example_and_exercise_are_ambiguous_without_structural_proof():
    page = detail(1, "(5) A numbered example.\n5. Prove an exercise.\nSec. 1.1\n")
    result = locate(["1.1.5"], [page])
    assert result.ambiguous_targets == ["1.1.5"] and result.selected_pages == [1]
    assert result.locations[0].resolved_scope == "page_only"
    assert "same_page_occurrences_require_full_page_context" in result.locations[0].reason_codes
    hinted = locate(["1.1.5"], [page], hints={"1.1.5": [1]})
    assert hinted.selected_pages == [1] and hinted.locations[0].status == "hinted"


def test_same_page_conflicting_sections_do_not_assign_local_label_by_first_hit():
    result = locate(["2.3.5"], [detail(6, "Sec. 2.3\n5. Prove this.\nSec. 2.4\n")])
    assert result.selected_pages == [6]
    assert result.ambiguous_targets == ["2.3.5"]
    assert result.locations[0].resolved_scope == "page_only"


def test_repeated_same_context_is_not_a_second_exercise():
    result = locate(["2.3.5"], [detail(6, "Sec. 2.3\n5. Prove this.\nSec. 2.3\n")])
    assert result.selected_pages == [6]


def test_native_section_id_alone_is_not_a_question_match():
    result = locate(["1.2"], [detail(1, "Sec. 1.2\nNo question label here.\n")])
    assert result.selected_pages == [] and result.missing_targets == ["1.2"]


def test_local_number_and_inline_section_reference_do_not_define_section_context():
    result = locate(["6.3.5"], [detail(2, "5. Prove this; see Section 6.3.\nChap. 6\n")])
    assert result.selected_pages == []


def test_no_implicit_section_inheritance_or_adjacent_page_expansion():
    result = locate(["1.1.5", "1.1.11"], [detail(1, "5. Prove this.\nSec 1.1\n"),
                                                 detail(2, "11. Another exercise.\nChap. 1\n")])
    assert result.selected_pages == [1]
    assert result.missing_targets == ["1.1.11"]


def test_duplicates_need_hint_and_do_not_choose_first_page():
    pages = [detail(1, "1.2 Question A\n"), detail(3, "1.2 Question B\n")]
    result = locate(["1.2"], pages)
    assert result.selected_pages == [] and result.ambiguous_targets == ["1.2"]
    assert result.locations[0].candidate_pages == [1, 3]
    hinted = locate(["1.2"], pages, hints={"1.2": [3]})
    assert hinted.selected_pages == [3] and hinted.locations[0].status == "hinted"
    assert {proof.page_number for proof in hinted.locations[0].evidence} == {3}


def test_same_page_duplicates_remain_ambiguous_without_explicit_hint():
    page = detail(5, "1.2 First\n1.2 Second\n")
    result = locate(["1.2"], [page])
    assert result.ambiguous_targets == ["1.2"] and result.selected_pages == [5]
    assert result.locations[0].resolved_scope == "page_only"
    hinted = locate(["1.2"], [page], hints={"1.2": [5]})
    assert hinted.selected_pages == [5]
    assert "native_candidates_ambiguous_within_hint" in hinted.locations[0].reason_codes
    assert len(hinted.locations[0].evidence) == 2


def test_same_page_ambiguity_does_not_hide_unchecked_index_candidate_on_another_page():
    page = detail(5, "1.2 First\n1.2 Second\n")
    result = locate(["1.2"], [page], index=[summary(detail(9, "1.2 Another\n"), ["1.2"])])
    assert result.selected_pages == [] and result.ambiguous_targets == ["1.2"]
    assert result.locations[0].resolved_scope == "none"


def test_explicit_hint_can_select_uninspected_or_scanned_pages_without_claiming_match():
    result = locate(["1.1.5"], hints={"1.1.5": [999, 1000]})
    assert result.selected_pages == [999, 1000]
    assert result.inspected_detail_pages == []
    assert result.locations[0].status == "hinted"
    assert result.locations[0].evidence == []
    assert "hint_not_native_verified" in result.locations[0].reason_codes


def test_only_explicit_native_continuation_expands_selection():
    result = locate(["1.2"], [detail(1, "1.2 Prove this.\n"),
                                      detail(2, "Question 1.2 (continued)\nThe last condition.\n"),
                                      detail(3, "A paragraph without an identity.\n")])
    assert result.selected_pages == [1, 2]
    assert "explicit_native_continuation" in result.locations[0].reason_codes
    assert any(proof.kind == "continuation" and proof.matched_text == "continued"
               for proof in result.locations[0].evidence)


def test_composite_continuation_needs_its_own_native_section_evidence():
    result = locate(["2.3.5"], [detail(1, "5. Prove this.\nSec. 2.3\n"),
                                        detail(2, "5. (continued)\nSec. 2.3\n"),
                                        detail(3, "5. (continued)\nChap. 2\n")])
    assert result.selected_pages == [1, 2]


def test_full_id_start_can_have_composite_local_continuation():
    result = locate(["2.3.5"], [detail(1, "2.3.5 Prove this.\n"),
                                        detail(2, "5. (continued)\nSec. 2.3\n")])
    assert result.selected_pages == [1, 2]
    assert result.locations[0].status == "located"


def test_continuation_without_start_is_unlocated_not_complete():
    result = locate(["1.2"], [detail(2, "1.2 (continued)\n")])
    assert result.missing_targets == ["1.2"] and result.selected_pages == []
    assert result.locations[0].candidate_pages == [2]


def test_index_candidates_require_details_and_unchecked_duplicates_block_uniqueness():
    page = detail(5, "1.2 Question\n")
    result = locate(["1.2"], [page], index=[summary(page, ["1.2"]),
                                          summary(detail(9, "1.2 Other\n"), ["1.2"])])
    assert result.needs_detail_targets == ["1.2"]
    assert result.selected_pages == [9]
    assert result.locations[0].candidate_pages == [5, 9]


def test_index_false_positive_does_not_override_inspected_native_detail():
    page = detail(5, "11. Another number\n")
    result = locate(["1"], [page], index=[summary(page, ["1"])])
    assert result.missing_targets == ["1"] and result.selected_pages == []


def test_unlocated_in_small_native_window_never_claims_whole_book_absence():
    page = detail(500, "")
    result = locate(["1.2"], [page], index=[summary(page)], total=1000)
    assert result.missing_targets == ["1.2"]
    assert result.inspected_index_pages == result.inspected_detail_pages == [500]
    assert result.locations[0].reason_codes == [
        "target_not_located_in_inspected_native_scope", "target_location_needs_hint",
    ]


def test_selection_budget_never_silently_takes_first_24_candidates():
    index = [summary(detail(number, "1.2 Here\n"), ["1.2"]) for number in range(1, 26)]
    result = locate(["1.2"], index=index)
    assert result.selection_limit_exceeded and result.selected_pages == []
    assert result.locations[0].status == "needs_hint"
    assert result.locations[0].candidate_pages == list(range(1, 26))
    assert result.locations[0].selected_pages == []


def test_union_selection_budget_is_shared_across_targets():
    index = [summary(detail(number, f"Q{number} Here\n"), [f"Q{number}"]) for number in range(1, 27)]
    result = locate([f"Q{number}" for number in range(1, 27)], index=index)
    assert result.selection_limit_exceeded and result.selected_pages == []
    assert all(not location.selected_pages for location in result.locations)


def test_excessive_matches_are_explicitly_ambiguous_with_bounded_evidence():
    result = locate(["1.2"], [detail(1, "1.2 repeated\n" * 100)])
    assert result.ambiguous_targets == ["1.2"]
    assert len(result.locations[0].evidence) == 64
    assert "native_match_limit_exceeded" in result.locations[0].reason_codes


def test_codepoint_offsets_and_block_geometry_are_raw_even_across_text_blocks():
    text = "条件\nExercise 1.2 Question\n"
    page = detail(8, text, parts=["条件\nExercise 1.", "2 Question\n"])
    result = locate(["1.2"], [page])
    proof = result.locations[0].evidence[0]
    assert proof.matched_text == "1.2"
    assert proof.native_char_start == text.index("1.2")
    assert proof.block_order_indices == [0, 1]
    assert proof.region == (0, 0, 1, 0.02)


@pytest.mark.parametrize("changes", [
    {"targets": ["1", "1"]}, {"targets": [" "]}, {"targets": ["1\n2"]},
    {"page_hints": {"other": [1]}}, {"page_hints": {"1": [2, 1]}},
    {"page_hints": {"1": [1001]}}, {"page_hints": {"1": list(range(1, 26))}},
    {"detail_pages": [detail(2, "a"), detail(1, "b")]},
    {"detail_pages": [detail(1, "a")] * 25},
    {"index_pages": [summary(detail(1, "a"))] * 501},
])
def test_input_scope_and_capacity_contracts_fail_closed(changes):
    values = dict(source=source(), targets=["1"], total_pages=1000)
    values.update(changes)
    with pytest.raises(ValidationError):
        NativeLocatorRequestV1(**values)


def test_disagreeing_index_detail_snapshots_are_rejected():
    with pytest.raises(ValidationError, match="snapshots disagree"):
        locate(["1"], [detail(1, "a")], index=[summary(detail(1, "different"))])


def test_snapshot_revalidation_and_output_do_not_alias_caller():
    request = NativeLocatorRequestV1(source=source(), total_pages=10, targets=["1.2"],
                                     detail_pages=[detail(1, "1.2 Question\n")])
    result = locate_native_targets(request)
    request.targets.append("other")
    request.source.owner_id = "another-owner"
    assert result.requested_targets == ["1.2"] and result.source.owner_id == "owner-one"
    request.targets.append("other")
    with pytest.raises(ValidationError):
        locate_native_targets(request)
