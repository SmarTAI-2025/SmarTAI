"""Explicit source policies and their negation/scope boundaries."""
import pytest

from backend.services.process_credit_policy import process_credit_policy


@pytest.mark.parametrize(("criterion", "mode"), [
    ("每步 1 分", "allowed"),
    ("选项 2 分、解释 2 分", "allowed"),
    ("按步骤给分，每步2分。", "allowed"),
    ("写出简要过程（2分），答案（3分）。", "allowed"),
    ("过程0.5分，答案3.5分。", "allowed"),
    ("过程正确得2分，答案2分", "allowed"),
    ("步骤正确给2分，答案2分", "allowed"),
    ("1. 过程2分；2. 答案2分", "allowed"),
    ("过程0分、解释2分、选项2分", "allowed"),
    ("不计过程分", "forbidden"),
    ("只看最终答案", "forbidden"),
    ("解释0分、选项4分", "forbidden"),
    ("过程2分；本题只看最终答案。", "forbidden"),
    ("答案错误就是0分", "forbidden"),
    ("步骤不给分", "forbidden"),
    ("", "unspecified"),
    ("写出解题过程", "unspecified"),
    ("请检查步骤是否正确", "unspecified"),
    ("不允许按步骤给分", "forbidden"),
    ("不禁止过程分", "unspecified"),
    ("如果答案正确，过程2分", "unspecified"),
    ("只在答案错误时不计过程分", "unspecified"),
    ("Explanation 2 points; final answer 2 points.", "allowed"),
    ("Do not award process credit.", "forbidden"),
    ("Only grade the final answer.", "forbidden"),
])
def test_distinguishes_explicit_policies_from_mentions(criterion, mode):
    assert process_credit_policy(criterion).mode == mode


def test_no_working_marks_does_not_disable_multiple_choice_subset_marks():
    policy = process_credit_policy("不计过程分；全选对4分，部分选对2分，选错0分")
    assert policy.mode == "forbidden" and not policy.all_or_nothing
    assert process_credit_policy("答案错误就是0分").all_or_nothing


def test_component_error_does_not_override_an_independent_explanation_budget():
    policy = process_credit_policy("选项错不得分，解释2分")
    assert policy.mode == "allowed" and not policy.all_or_nothing


def test_separate_condition_does_not_erase_an_explicit_unconditional_rule():
    policy = process_credit_policy("不计过程分，只看最终答案，答案错误就是0分；若未作答也记0分")
    assert policy.mode == "forbidden" and policy.all_or_nothing
