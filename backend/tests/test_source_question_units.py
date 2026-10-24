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
