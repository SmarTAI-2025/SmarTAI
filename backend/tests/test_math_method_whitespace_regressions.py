"""Presentation whitespace must not create work or change part ownership."""
import pytest

from backend.services.math_grading_context import math_plans_context
from backend.skills import math_grading as grading
from backend.tests.test_math_method_review import provider, revision
from backend.tests.test_math_method_premise_regressions import (
    assess, premise_case,
    test_heading_without_work_cannot_turn_a_premise_into_credit as _blank_case,
    test_line_start_coefficient_does_not_displace_clear_later_heading as _coefficient_case,
    test_delimited_formula_heading_keeps_correct_initial_judgment as _formula_case,
)


BLANK_TEX = [
    r'$\quad$', r'$\qquad$', r'$\enspace$', r'$\thinspace$', r'$\medspace$',
    r'$\thickspace$', r'$\negthinspace$', r'$\,$', r'$\;$', r'$\!$', r'$~$',
    r'$\hspace{2em}$', r'$\hspace*{2em}$', r'$\kern 2em$', r'$\mkern-2mu$',
    r'$\phantom{x=2}$', r'$\hphantom{\frac{4}{2}}$', r'$\vphantom{x}$',
    r'$\text{\qquad}$', r'${\displaystyle\quad}$', r'$\mathrm{}$',
    r'$\begin{aligned}\quad&\quad\\\end{aligned}$', r'$\color{red}\quad$',
    '$% x=2\n$', r'$\quad\quad$',
]


@pytest.mark.asyncio
@pytest.mark.parametrize('body', BLANK_TEX)
@pytest.mark.parametrize('stage', ['initial', 'joint'])
async def test_latex_layout_cannot_fill_an_empty_part(body, stage):
    await _blank_case(body, False, stage)


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [r'$\quad 2x=4$', r'$2x=4\qquad$', r'$\text{2x=4}$',
    r'$\frac{4}{2}$', r'$\pi$', r'$\emptyset$', r'$\sqrt{4}$',
    r'$\phantom{x}\,2x=4$', r'$\begin{aligned}2x&=4\end{aligned}$',
    r'$\color{red}2x=4$', r'$\mathrm{x}=2$', r'$\lnot$'])
@pytest.mark.parametrize('stage', ['initial', 'joint'])
async def test_latex_layout_preserves_actual_cited_content(body, stage):
    p, a, plan, experts, original, second = premise_case(body=body)
    if stage == 'initial':
        candidate = original.model_copy(update={'part_decisions': [original.part_decisions[0], second]})
        score, calls = await assess(p, a, plan, candidate)
        assert score == 20 and len(calls) == 1
    else:
        main, calls = provider('primary', {'revisions': [revision(second)]})
        with math_plans_context({p.q_id: plan}), grading.math_review_scope():
            result = await grading.review_math_experts(p, a, experts, main)
        assert result[0].score == 20 and len(calls) == 1


HSPACE = ['', ' ', '\t', '\u3000', '\u00a0', '\u2009']


@pytest.mark.asyncio
@pytest.mark.parametrize('space', HSPACE)
@pytest.mark.parametrize('expression', ['x{0}={0}4', 'x{0}+{0}1=3', 'x{0}^{0}2=4', '{0}x=4'])
async def test_formula_spacing_does_not_change_coefficient_ownership(space, expression):
    await _coefficient_case('(2)' + expression.format(space), '(2)因式分解得两根')


@pytest.mark.asyncio
@pytest.mark.parametrize('space', HSPACE)
@pytest.mark.parametrize('separator', ['\n', '\r', '\u2028', '；'])
async def test_unicode_indent_keeps_real_formula_headings(space, separator):
    await _formula_case('x = 2', separator + space)


def test_first_formula_heading_is_not_displaced_by_an_inline_mention():
    from backend.services.math_source_evidence import _labelled_ranges
    _, _, plan, _, _, _ = premise_case()
    text = '(1)x = 2；(1) 代入所得结果。\n(2)2x=4'
    ranges = _labelled_ranges(text, plan.part_rubric.parts)
    assert ranges['p'] == (0, text.index('\n(2)') + 1)
