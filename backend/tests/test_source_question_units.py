from backend.services.source_question_units import source_question_units


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
