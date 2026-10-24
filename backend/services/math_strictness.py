"""Authenticated defaults; mathematical importance is decided once in the plan."""
from typing import Literal

Adjustment = Literal["minor_omission", "follow_through"]


def strictness_policy(strictness: int) -> str:
    return (
        f"Default mathematical scoring at strictness {strictness}/100: ONLY where the teacher "
        "criterion/notes do not specify a policy, a non-core minor omission loses its standard "
        f"deduction × {strictness}/100; correct dependent work using an erroneous intermediate "
        f"value keeps its otherwise-earned credit × (100−{strictness})/100. "
        "Determine importance and point budgets once in the shared question plan, before seeing "
        "answers. A core mathematical obligation is never a minor omission just because it "
        "concerns a special case. The first actual error and new independent errors are scored "
        "normally; independent correct work remains credited. Explicit teacher amounts/policies "
        "take precedence, including exemptions, required correctness and follow-through rules. "
        "Do not invent student work or give blank answers credit."
    )


LEDGER_INSTRUCTION = (
    "Default strictness accounting: item.default_adjustments lists eligible defaults fixed in "
    "the shared plan. An empty list means no default adjustment. In points mode, award.score "
    "is the earned credit EXCLUDING the conditional portion. For an eligible issue add "
    "adjustment=minor_omission|follow_through and conditional_credit (the additional credit at "
    "strictness 0); the program adds conditional_credit*(100-plan.strictness)/100 exactly once. "
    "Do NOT pre-scale either number. Their sum cannot exceed item.points. Cite a refuted omission "
    "for minor_omission; for follow_through, cite actual correct work on the wrong value and set "
    "caused_by to its refuted ancestor claim. A new independent mistake is not follow-through. "
    "In deductions mode, adjustment selects an eligible default; the program scales that "
    "standard deduction by plan.strictness/100. Follow-through requires the same caused_by "
    "dependency; minor_omission requires omission evidence. Explicit exemptions/no-repeat still "
    "win. Otherwise omit these fields and score normally. Never apply both defaults to the same "
    "lost credit. If partial credit is disabled, no default conditional credit applies. "
    "In holistic/direct scoring apply the same numeric defaults within the original rubric."
)
