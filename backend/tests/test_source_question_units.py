from backend.services.source_question_units import source_question_units, source_page_groups


def test_contiguous_saved_pages_close_only_the_previous_question():
    units = source_question_units({1: "Name: Learner\nQuestion 1: First part",
                                   2: "Continuation\nQuestion 2: Incomplete tail"})
    assert [(unit.number, unit.complete, unit.closure) for unit in units] == [
        ("1", True, "next_heading"), ("2", False, "open")]
    assert "Continuation" in units[0].text and units[0].pages == (1, 2)
    finished = source_question_units({1: "Question 1: First\nQuestion 2: Final"}, source_complete=True)
    assert finished[-1].complete and finished[-1].closure == "source_end"


def test_gap_and_repeated_headings_do_not_close_a_fragment():
    units = source_question_units({1: "Question 1: Beginning", 3: "Question 2: Later"})
    assert not any(unit.complete for unit in units)
    repeated = source_question_units({1: "Question 1: First\nQuestion 2: Next",
                                     2: "Question 1: Repeated"}, source_complete=True)
    assert all(not unit.complete and unit.closure == "repeated_heading" for unit in repeated if unit.number == "1")


def test_solution_steps_do_not_create_question_boundaries_and_unknown_headings_do_not_prove_completion():
    assert source_question_units({1: "1. first calculation\n2. second calculation"}) == []
    units = source_question_units({1: "Question 1: Answer\nQuestion 99: Appendix"}, known_numbers={"1"})
    assert len(units) == 1 and not units[0].complete


def test_independent_complete_question_after_a_gap_can_finish_before_the_source():
    units = source_question_units({1: "Question 1: Cut off", 3: "Question 2: Complete\nQuestion 3: Tail"})
    assert [(unit.number, unit.complete) for unit in units] == [("1", False), ("2", True), ("3", False)]


def test_later_cross_page_answer_keeps_its_whole_visible_span_without_inferred_opening():
    pages = {1: "Question 1: Answer", 3: "Question 2: Earlier fragment",
             4: "Question 3: Opening", 5: "Question 3: （续）\nEnding"}
    units = source_question_units(pages, check_opening=True, source_end_page=5)
    assert [(unit.number, unit.pages, unit.closure) for unit in units] == [
        ("1", (1,), "open"), ("2", (3,), "unverified_opening"), ("3", (4, 5), "unverified_opening"),
    ]
    assert not source_page_groups(pages, check_opening=True, source_end_page=5)[-1].complete


def test_known_and_unknown_headings_after_a_gap_do_not_prove_an_answer_starts_there():
    pages = {1: "Question 1: Answer", 3: "Question 99: Unrecognized label\nQuestion 2: Fragment\nQuestion 3: Full"}
    units = source_question_units(pages, check_opening=True, known_numbers={"1", "2", "3"}, source_end_page=3)
    assert [(unit.number, unit.closure) for unit in units] == [
        ("1", "open"), ("2", "unverified_opening"), ("3", "unverified_opening"),
    ]


def test_page_groups_keep_two_questions_in_each_request_and_close_after_lookahead():
    pages = {1: "**1.** First question.\n**2.** Second question.",
             2: "3. Third question.\n4. Fourth question."}
    assert [(group.pages, group.complete) for group in source_page_groups(pages)] == [
        ((1,), True), ((2,), False)]
    assert [(group.pages, group.complete) for group in source_page_groups(pages, source_complete=True)] == [
        ((1,), True), ((2,), True)]


def test_cross_page_question_keeps_both_pages_in_the_same_request():
    pages = {1: "1. First question.\n2. Prove the following statement:",
             2: "Every element satisfies the given condition.\n3. Third question.\n4. Fourth question."}
    groups = source_page_groups(pages, source_complete=True)
    assert len(groups) == 1 and groups[0].pages == (1, 2) and groups[0].complete
    assert groups[0].text == pages[1] + "\n\n" + pages[2]


def test_cross_page_groups_do_not_truncate_three_page_question():
    pages = {1: "Question 1: Beginning", 2: "Continuation", 3: "Final condition.\nQuestion 2: Next"}
    assert [(group.pages, group.complete) for group in source_page_groups(pages)] == [((1, 2, 3), False)]
    assert source_page_groups(pages, source_complete=True)[0].complete


def test_missing_page_does_not_join_unrelated_sides_or_mark_cut_off_question_complete():
    pages = {1: "1. A cut off question:", 3: "2. Later question.\n3. Last question."}
    assert [(group.pages, group.complete) for group in source_page_groups(pages)] == [
        ((1,), False), ((3,), False)]


def test_exposition_and_repeated_exercise_numbers_are_not_required_question_ids():
    pages = {1: "Introductory exposition without exercises.",
             2: "1. First exercise.\n2. Second exercise.",
             3: "1. A new section's first exercise.\n2. Its second exercise."}
    groups = source_page_groups(pages, source_complete=True)
    assert [group.pages for group in groups] == [(1,), (2,), (3,)]
    assert all(group.complete for group in groups)


def test_out_of_order_continuation_waits_for_its_opening_page_without_duplicate_group():
    pages = {2: "Continuation of the earlier question.\n3. Another question.", 3: "4. Last question."}
    assert not source_page_groups(pages)[0].complete
    pages[1] = "1. First question.\n2. This question continues:"
    groups = source_page_groups(pages)
    assert groups[0].pages == (1, 2) and groups[0].complete


def test_failed_middle_page_does_not_make_actual_end_of_file_incomplete():
    groups = source_page_groups({1: "1. Cut off question:", 3: "2. A complete independent question."},
                                source_end_page=3)
    assert [(group.pages, group.complete) for group in groups] == [((1,), False), ((3,), True)]


def test_selected_pages_finished_does_not_make_missing_continuation_complete():
    groups = source_page_groups({1: "Question 1: Beginning.",
        3: "Continuation of missing page two.\nQuestion 2: Independent ending."},
        source_complete=True, source_end_page=3)
    assert not next(group for group in groups if group.pages == (3,)).complete


def test_failed_page_advice_only_uses_answers_on_adjacent_successful_pages():
    from backend.services.submission_incremental import answer_boundaries_on_read_pages, page_gap_review_flags

    pages = {1: "1.1.1 Earlier", 2: "1.1.2 First\n1.1.3 Last before the gap",
             4: "1.1.4 Between two gaps", 6: "1.1.5 First after the gap\n1.1.6 Later",
             7: "1.1.7 Final"}
    problems = {str(number): {"number": f"1.1.{number}"} for number in range(1, 8)}
    _, _, edges = answer_boundaries_on_read_pages(pages, problems, {
        "processed_pages": list(pages), "failed_pages": [3, 5], "total_pages": 7,
    })
    previous = "recognition_boundary_previous_page_failed"
    following = "recognition_boundary_next_page_failed"
    answers = [{"q_id": str(i), "content": content} for i, content in enumerate([
        "Earlier", "First", "Last before the gap", "Between two gaps", "First after the gap", "Later", "Final",
    ], 1)]
    assert page_gap_review_flags(pages, [3, 5], edges, answers) == {
        "3": [following], "4": [following, previous], "5": [previous],
    }


def test_empty_successful_page_separates_the_previous_answer_from_a_failure():
    from backend.services.submission_incremental import page_gap_review_flags

    assert page_gap_review_flags({1: "Question 1: Answer", 2: " \n"}, [3], {},
                                 [{"q_id": "q1", "content": "Answer"}]) == {}


def test_page_gap_excludes_only_a_matching_standalone_page_number_from_the_tail():
    from backend.services.submission_incremental import page_gap_review_flags

    answer = {"q_id": "q1", "content": "The proof begins here."}
    start = "1. The proof begins here."
    expected = {"q1": ["recognition_boundary_next_page_failed"]}
    for footer in ("- 1 -", "— 1 —", "Page 1", "第 1 页"):
        page = start + "\r\n" + footer + "\r\n"
        assert page_gap_review_flags({1: page}, [2], {}, [answer]) == expected
        assert page_gap_review_flags({1: page}, [2], {}, [
            {**answer, "content": answer["content"] + "\n" + footer},
        ]) == expected
        assert page_gap_review_flags({1: footer}, [2], {}, [{**answer, "content": footer}]) == {}
    for tail in (" - 1 -", "\n1", "\n- 2 -", "\n- 1 - x", "\n- 1 -\nMore work"):
        assert page_gap_review_flags({1: start + tail}, [2], {}, [answer]) == {}


def test_page_gap_excludes_only_a_matching_standalone_page_number_from_the_start():
    from backend.services.submission_incremental import page_gap_review_flags

    answer = {"q_id": "q2", "content": "The proof resumes here."}
    aliases = {"q2": ["2"]}
    body = "2. The proof resumes here."
    expected = {"q2": ["recognition_boundary_previous_page_failed"]}
    for header in ("Page 2", "- 2 -", "第 2 页"):
        page = f"\n {header}\r\n{body}"
        assert page_gap_review_flags({2: page}, [1], {}, [answer], aliases) == expected
        with_header = {**answer, "content": header + "\n" + body}
        assert page_gap_review_flags({2: page}, [1], {}, [with_header], aliases) == expected
        assert page_gap_review_flags({2: page}, [1], {}, [{**answer, "content": header}], aliases) == {}
    for prefix in ("Page 1\n", "Page 2 ", "Page 2\nUnmatched earlier work.\n"):
        assert page_gap_review_flags({2: prefix + body}, [1], {}, [answer], aliases) == {}
    assert page_gap_review_flags({2: "Page 2", 3: body}, [1], {}, [
        {**answer, "content": "Page 2\n" + body},
    ], aliases) == {}


def test_page_number_header_preserves_explicit_first_heading_evidence():
    from backend.services.submission_incremental import answer_boundaries_on_read_pages, page_gap_review_flags

    pages = {2: "Page 2\nQuestion 2: The original proof.\nQuestion 3: A separate answer."}
    problems = {"q2": {"number": "2"}, "q3": {"number": "3"}}
    _, _, headings = answer_boundaries_on_read_pages(pages, problems, {"failed_pages": [1], "total_pages": 2})
    answers = [{"q_id": "q2", "content": "Reformatted proof text."}]
    assert page_gap_review_flags(pages, [1], headings, answers) == {
        "q2": ["recognition_boundary_previous_page_failed"],
    }


def test_matched_fragments_can_bound_the_same_answer_on_both_sides_of_a_failed_page():
    from backend.services.submission_incremental import page_gap_review_flags

    pages = {1: "An earlier nonadjacent answer.", 2: "4. The opening of the proof.",
             4: "The last assertion of the proof.\n2. The next answer.",
             5: "A different nonadjacent answer."}
    answers = [
        {"q_id": "q1", "content": "An earlier nonadjacent answer."},
        {"q_id": "q2", "content": "The next answer."},
        {"q_id": "q3", "content": "A different nonadjacent answer."},
        {"q_id": "q4", "content": "The opening of the proof.\nThe last assertion of the proof."},
    ]
    assert page_gap_review_flags(pages, [3], {}, answers) == {
        "q4": ["recognition_boundary_next_page_failed", "recognition_boundary_previous_page_failed"],
    }


def test_page_gap_does_not_choose_between_questions_sharing_one_text_span():
    from backend.services.submission_incremental import page_gap_review_flags

    pages = {1: "2. The same short answer."}
    answers = [
        {"q_id": "q1", "content": "The same short answer."},
        {"q_id": "q2", "content": "The same short answer."},
    ]
    assert page_gap_review_flags(pages, [2], {}, answers) == {}


def test_page_gap_does_not_choose_between_overlapping_claims_to_the_edge():
    from backend.services.submission_incremental import page_gap_review_flags

    pages = {1: "2. The proof ends with the identity."}
    answers = [
        {"q_id": "q1", "content": "with the identity."},
        {"q_id": "q2", "content": "The proof ends with the identity."},
    ]
    assert page_gap_review_flags(pages, [2], {}, answers) == {}


def test_page_gap_does_not_choose_between_conflicting_heading_and_answer_evidence():
    from backend.services.submission_incremental import answer_boundaries_on_read_pages, page_gap_review_flags

    pages = {2: "Question 1: The original work."}
    problems = {"q1": {"number": "1"}, "q2": {"number": "2"}}
    _, _, headings = answer_boundaries_on_read_pages(pages, problems, {"failed_pages": [1]})
    answers = [{"q_id": "q2", "content": pages[2]}]
    assert page_gap_review_flags(pages, [1], headings, answers) == {}


def test_heading_alone_does_not_prove_the_last_answer():
    from backend.services.submission_incremental import answer_boundaries_on_read_pages, page_gap_review_flags

    pages = {1: "1. Another answer.\n1.2.3 Some OCR text."}
    problems = {"q1": {"number": "1"}, "q2": {"number": "1.2.3"}}
    answers = [
        {"q_id": "q1", "content": "Another answer."},
        {"q_id": "q2", "content": "Reformatted OCR text."},
    ]
    _, _, edges = answer_boundaries_on_read_pages(pages, problems, {"failed_pages": [2]})
    assert page_gap_review_flags(pages, [2], edges, answers) == {}


def test_page_gap_does_not_fall_back_to_explicit_headings_inside_the_page():
    from backend.services.submission_incremental import answer_boundaries_on_read_pages, page_gap_review_flags

    pages = {1: "Question 1: OCR before the last answer.\n2. Unmatched work at the end.",
             3: "3. Unmatched work at the beginning.\nQuestion 4: OCR after the first answer."}
    problems = {f"q{i}": {"number": str(i)} for i in range(1, 5)}
    answers = [{"q_id": q_id, "content": f"Different text for {q_id}."} for q_id in problems]
    _, _, edges = answer_boundaries_on_read_pages(pages, problems, {"failed_pages": [2]})
    assert page_gap_review_flags(pages, [2], edges, answers) == {}


def test_a_number_or_a_different_questions_heading_does_not_open_a_matched_answer():
    from backend.services.submission_incremental import page_gap_review_flags

    answers = [{"q_id": "q2", "content": "The proof resumes here."}]
    for prefix in ("2", "1."):
        assert page_gap_review_flags({2: f"{prefix}\nThe proof resumes here."}, [1], {}, answers, {"q2": ["2"]}) == {}
