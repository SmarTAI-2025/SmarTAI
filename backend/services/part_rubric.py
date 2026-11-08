"""Frozen scoring budgets and source-bound alternative-method policies."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Points = Annotated[float, Field(ge=0, le=10000, allow_inf_nan=False)]
Key = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[\w.():-]+$")]
Text = Annotated[str, Field(min_length=1, max_length=12000)]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Amount(Frozen):
    points: Points | None = Field(default=None, exclude_if=lambda v: v is None)
    fraction: str | None = Field(
        default=None, exclude_if=lambda v: v is None, pattern=r"^\d{1,6}/[1-9]\d{0,5}$"
    )

    @model_validator(mode="after")
    def check_unit(self):
        if (self.points is None) == (self.fraction is None):
            raise ValueError("Use exactly one of points or fraction.")
        if self.fraction and Fraction(self.fraction) > 1:
            raise ValueError("A fraction of the maximum cannot exceed one.")
        return self

    def resolve(self, maximum) -> Fraction:
        return (
            Fraction(self.fraction) * Fraction(str(maximum))
            if self.fraction
            else Fraction(str(self.points))
        )


class Alternative(Frozen):
    maximum: Amount = Field(default_factory=lambda: Amount(fraction="1/1"))
    minimum: Amount = Field(default_factory=lambda: Amount(points=0))
    deduct_per_error: Points | None = Field(default=None, gt=0)
    source_quote: str = Field(default="", max_length=4000)


class Criterion(Frozen):
    id: Key
    criterion: Text
    source_quote: str = Field(default="", max_length=4000, exclude_if=lambda v: not v)


class PointStep(Criterion):
    total_pts: Points = Field(gt=0)
    answer_refs: tuple[Key, ...] = Field(default=(), exclude_if=lambda v: not v)
    follow_through_keep: float | None = Field(
        default=None, ge=0, lt=1, allow_inf_nan=False
    )


class Penalty(Criterion):
    deduct_pts: Points = Field(gt=0)
    count_by: Literal["independent_error", "wrong_step", "target_item"] = (
        "independent_error"
    )
    limit: int | None = Field(default=None, gt=0, le=100)


class RubricPart(Frozen):
    id: Key
    label: str | None = Field(default=None, max_length=64)
    stem: str | None = Field(default=None, max_length=100000)
    origin_overrides: dict[str, Literal["teacher", "ai"]] = Field(
        default_factory=dict, exclude_if=lambda v: not v
    )
    answer_refs: tuple[Key, ...] = Field(default=(), exclude_if=lambda v: not v)
    total_pts: Points = Field(gt=0)
    steps: tuple[PointStep, ...] = Field(
        default=(), max_length=100, exclude_if=lambda v: not v
    )
    deductions: tuple[Penalty, ...] = Field(
        default=(), max_length=100, exclude_if=lambda v: not v
    )
    alternative: Alternative = Field(default_factory=Alternative)

    @model_validator(mode="after")
    def check_budget(self):
        if bool(self.steps) == bool(self.deductions):
            raise ValueError(
                f"{self.id}: supply steps or deductions, exactly one nonempty list."
            )
        if (
            any(rule.count_by == "target_item" for rule in self.deductions)
            and not self.answer_refs
        ):
            raise ValueError(
                f"{self.id}: target_item deductions require answer_refs identifying the targets."
            )
        if self.steps:
            total = sum(Decimal(str(step.total_pts)) for step in self.steps)
            if total != Decimal(str(self.total_pts)):
                raise ValueError(
                    f"{self.id}: step budgets total {total}, required {self.total_pts:g}."
                )
        if len({i.id for i in (*self.steps, *self.deductions)}) != len(
            self.steps or self.deductions
        ):
            raise ValueError(f"{self.id}: duplicate scoring item ID.")
        if (
            not 0
            <= self.alternative.minimum.resolve(self.total_pts)
            <= self.alternative.maximum.resolve(self.total_pts)
            <= self.total_pts
        ):
            raise ValueError(
                f"{self.id}: alternative method bounds must fit the part budget."
            )
        return self


class Floor(Frozen):
    pts: Points | None = Field(default=None, exclude_if=lambda v: v is None)
    fraction: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def check_unit(self):
        if (self.pts is None) == (self.fraction is None):
            raise ValueError("Use exactly one question floor unit.")
        return self

    def resolve(self, maximum):
        return (
            Fraction(str(self.pts))
            if self.pts is not None
            else Fraction(str(self.fraction)) * Fraction(str(maximum))
        )


class AnswerSegment(Frozen):
    id: Key
    text: Text
    origin: Literal["teacher", "ai"]


class PartRubric(Frozen):
    id: Key
    label: str = Field(max_length=120)
    stem: str = Field(max_length=100000)
    total_pts: Points = Field(gt=0)
    base_unit_pts: Points = Field(default=1, gt=0)
    rubric_version: int = Field(default=1, ge=1)
    rubric_origin: Literal["teacher", "ai"] = "ai"
    teacher_rubric: str | None = Field(
        default=None, max_length=100000, exclude_if=lambda v: v is None
    )
    answer_segments: tuple[AnswerSegment, ...] = Field(
        default=(), max_length=200, exclude_if=lambda v: not v
    )
    parts: tuple[RubricPart, ...] = Field(min_length=1, max_length=100)
    nonblank_floor: Floor = Field(default_factory=lambda: Floor(pts=0))
    # Warnings concern interpretation of the teacher's ordinary rules only.
    teacher_gaps: tuple[str, ...] = Field(default=(), max_length=20)
    low_confidence: bool = False

    @model_validator(mode="after")
    def unique_parts(self):
        if len({part.id for part in self.parts}) != len(self.parts):
            raise ValueError("Duplicate rubric part ID.")
        total = sum(Decimal(str(part.total_pts)) for part in self.parts)
        if total != Decimal(str(self.total_pts)):
            raise ValueError(
                f"Part budgets total {total}; required {self.total_pts:g}."
            )
        if self.nonblank_floor.resolve(self.total_pts) > Fraction(str(self.total_pts)):
            raise ValueError("Nonblank floor exceeds the question budget.")
        segments = {segment.id for segment in self.answer_segments}
        if len(segments) != len(self.answer_segments):
            raise ValueError("Duplicate reference-answer segment ID.")
        for part in self.parts:
            if not part.label and (len(self.parts) != 1 or part.stem is not None):
                raise ValueError(
                    "An unnumbered question needs one part with null label and stem."
                )
            references = {
                *part.answer_refs,
                *(ref for step in part.steps for ref in step.answer_refs),
            }
            if references - segments:
                raise ValueError(
                    f"{part.id}: missing referenced answer segments {sorted(references - segments)}."
                )
        return self


class AssignmentRubric(Frozen):
    schema_version: Literal[2]
    assignment_id: Key
    questions: tuple[PartRubric, ...]

    @model_validator(mode="after")
    def unique_ids(self):
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("Question IDs must be unique within the assignment.")
        return self


_ALT = re.compile(
    r"另类解法|其它解法|其他(?:正确|有效)?(?:解法|方法)|另解|不同(?:的)?(?:解法|方法)|非(?:标答|标准)(?:解法|方法)?|不(?:按|用|使用)(?:标答|标准|指定)(?:解法|方法)?|alternat(?:e|ive)\s+(?:valid\s+)?(?:methods?|solutions?)",
    re.I,
)
_AMOUNT = re.compile(
    r"(\d+(?:\.\d+)?\s*/\s*[1-9]\d*\s*(?:分(?!之)|points?)?|\d+(?:\.\d+)?\s*%|一半|半分|[二三四五六七八九十]分之[一二三四五六七八九]|\d+(?:\.\d+)?\s*(?:分(?!之)|points?)|零分)",
    re.I,
)
_SHORT_RESPONSE = re.compile(
    r"判断.{0,20}(?:真假|正误|正确与否|是否正确)|是否(?:正确|成立)|真假|true.{0,8}false|选择正确|select (?:the|all)",
    re.I,
)
_REQUIRES_REASON = re.compile(
    r"证明|推理过程|推导|论证|给出理由|说明理由|说明原因|解释原因|写出.{0,5}过程|show (?:your )?work|justify|prove|explain why",
    re.I,
)
_DEMANDS_REASON = re.compile(
    r"(?:并|须|需|且|必须|同时|给出|写出).{0,10}(?:说明|解释|理由|推导|推理|证明)|(?:provide|give|show|justify|explain).{0,14}(?:reason|proof|work|why)",
    re.I,
)


def check_response_obligation(stem, criterion, parts):
    if not _SHORT_RESPONSE.search(stem) or _REQUIRES_REASON.search(
        stem + " " + criterion
    ):
        return
    for part in parts:
        for step in part.steps:
            if _DEMANDS_REASON.search(step.criterion):
                raise ValueError(
                    f"{part.label or part.id}: the source requests only the decision. "
                    "A correct selection or true/false answer earns its allocation. "
                    "Remove invented proof requirements from the scored steps; keep derivations in the reference answer."
                )


_REQUIRED = re.compile(
    r"(?:必须|限定|要求|请|务必|只能|须)\s*(?:用|使用|采用).{1,40}?(?:法|定理|公式)|(?:^|[。；;\n(）)])\s*用.{1,28}?法(?:求|证|解|计算)|\b(?:must use|using (?:the )?method of|required method)\b",
    re.I,
)


def parse_amount(token: str) -> Amount:
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", token))
    if text in ("一半", "半分", "零分"):
        return (
            Amount(fraction="1/2")
            if text == "一半"
            else Amount(points=0.5 if text == "半分" else 0)
        )
    if "分之" in text:
        denominator, numerator = text.split("分之")
        digits = "零一二三四五六七八九十"
        return Amount(fraction=f"{digits.index(numerator)}/{digits.index(denominator)}")
    unit = re.search(r"分$|points?$", text, re.I)
    number = text[: unit.start()] if unit else text
    if "/" in number:
        f = Fraction(number)
        return (
            Amount(points=float(f))
            if unit
            else Amount(fraction=f"{f.numerator}/{f.denominator}")
        )
    if number.endswith("%"):
        f = Fraction(number[:-1]) / 100
        return Amount(fraction=f"{f.numerator}/{f.denominator}")
    if not unit:
        raise ValueError("Specify absolute points or a fraction of the part maximum.")
    return Amount(points=float(number))


def read_alternative(
    text: str, *, maximum: float, fallback: Alternative | None = None
) -> Alternative:
    """Only score-bearing clauses enter arithmetic. Quote originals, including typos."""
    policy = fallback or Alternative()
    values = {}
    for match in _AMOUNT.finditer(unicodedata.normalize("NFKC", text)):
        prefix = unicodedata.normalize("NFKC", text)[: match.start()]
        clause = re.split(r"[，,；;。\n]", prefix)[-1].casefold()
        amount = parse_amount(match.group())
        if re.search(r"(?:扣(?:除)?|deduct)\s*$", clause):
            if amount.points is None or amount.points <= 0:
                raise ValueError("Give a positive absolute deduction per error.")
            field, value = "deduct_per_error", amount.points
        elif re.search(r"最低|保底|至少|minimum|floor", clause):
            field, value = "minimum", amount
        elif (
            re.search(
                r"满分|最多|最高|最贵|给|至多|上限|封顶|maximum|up to|capped|worth|得|计|为|0分$|按",
                clause,
            )
            or amount.points == 0
            or _ALT.search(clause)
        ):
            field, value = "maximum", amount
        else:
            raise ValueError(
                "Cannot determine whether this amount is a cap, floor or deduction."
            )
        if field in values and values[field] != value:
            raise ValueError(f"Conflicting alternative {field}.")
        values[field] = value
    if not values and not re.search(
        r"满分|full (?:credit|marks)|同等|等价", text, re.I
    ):
        raise ValueError("Unclear alternative method policy.")
    policy = Alternative.model_validate(
        {**policy.model_dump(), **values, "source_quote": text}
    )
    if (
        not 0
        <= policy.minimum.resolve(maximum)
        <= policy.maximum.resolve(maximum)
        <= maximum
    ):
        raise ValueError("Alternative minimum, maximum and part budget conflict.")
    return policy


def alternative_clauses(text):
    return [s.strip() for s in re.split(r"[。；;\n]", text or "") if _ALT.search(s)]


def canonical_label(label, question_label=None):
    value = unicodedata.normalize("NFKC", label or "").strip().casefold()
    if question_label:
        prefix = re.escape(
            unicodedata.normalize("NFKC", str(question_label)).strip().casefold()
        )
        value = re.sub(rf"^{prefix}\s*(?=\()", "", value)
    match = re.fullmatch(
        r"(?:第\s*)?(?:小[问题]|(?:sub)?part\s+)?\(?([a-z0-9一二三四五六七八九十]+)\)?(?:\s*(?:小[问题]|问))?",
        value,
    )
    if not match:
        return value
    token = match[1]
    digits = "零一二三四五六七八九"
    if "十" in token:
        tens, _, units = token.partition("十")
        if (not tens or tens in digits) and (not units or units in digits):
            return str(
                (digits.index(tens) if tens else 1) * 10
                + (digits.index(units) if units else 0)
            )
    return str(digits.index(token)) if len(token) == 1 and token in digits else token


def _base_unit(parts, teacher):
    """Primary awards use the teacher's increment, or the finest required division of one point."""
    from math import gcd
    from functools import reduce

    stated = re.search(
        r"(?:最小(?:给分|计分|评分)?(?:单位|粒度)|(?:给分|计分|评分)粒度)\s*(?:为|是|[:：])?\s*(\d+(?:\.\d+)?)\s*分"
        r"|以\s*(\d+(?:\.\d+)?)\s*分\s*为(?:给分|计分|评分)?单位", teacher or "")
    values = [Decimal(str(item.total_pts)) for part in parts for item in part.steps]
    values += [Decimal(str(item.deduct_pts)) for part in parts for item in part.deductions]
    values += [Decimal(str(part.total_pts)) for part in parts]
    if stated:
        unit = Decimal(stated[1] or stated[2])
        if unit <= 0 or any(value % unit != 0 for value in values):
            raise ValueError(f"Ordinary step budgets and deductions must use the teacher's {unit}-point increment.")
        return float(unit)
    # Derived caps and follow-through are fractions of an award and do not
    # change its face value. The settlement engine applies them afterwards.
    places = max(0, *(-value.as_tuple().exponent for value in values))
    scale = 10 ** places
    return float(Decimal(reduce(gcd, (int(value * scale) for value in values), scale)) / scale)


def bind_parts(rubric: PartRubric, problem: dict) -> PartRubric:
    """Bind the model proposal to known question scope and authoritative teacher text."""
    from backend.services.question_structure import _allocation_marker_pattern
    from backend.services.math_source_evidence import match_source_quote

    structure = problem.get("question_structure") or {}
    parts = structure.get("subparts") or []
    labels = [p["label"] for p in parts] or [""]
    proposed = {
        canonical_label(p.label, problem.get("number")): p for p in rubric.parts
    }
    wanted = {canonical_label(label) for label in labels}
    missing = wanted - proposed.keys()
    if missing or proposed.keys() != wanted or len(proposed) != len(rubric.parts):
        raise ValueError(
            f"Missing subparts: {', '.join(sorted(missing)) or 'none'}. Required labels: {labels}."
        )
    maximum = float(problem.get("max_score") or 10)
    total = sum(Decimal(str(p.total_pts)) for p in rubric.parts)
    if total != Decimal(str(maximum)) or rubric.total_pts != maximum:
        raise ValueError(f"Part budgets total {total}, required {maximum:g}.")
    if rubric.nonblank_floor.resolve(maximum) > maximum:
        raise ValueError("Nonblank floor exceeds question maximum.")
    text = problem.get("criterion") or ""
    teacher = (
        text
        if problem.get("criterion_source") != "ai"
        and "criterion" not in (problem.get("ai_completion_provenance") or {})
        else ""
    )
    alternates = alternative_clauses(teacher)
    ordinary = teacher
    for clause in alternates:
        ordinary = ordinary.replace(clause, "")
    expected = dict(problem.get("teacher_subpart_points") or {})
    # Source clauses constrain individual fields. A subtotal, a step budget and a
    # conditional award in the same sentence are three different quantities.
    per_part = re.search(r"每(?:个)?小问\s*(\d+(?:\.\d+)?)\s*分", ordinary)
    if per_part:
        for label in labels:
            expected.setdefault(label, per_part[1])
    for label in filter(None, labels):
        marker = _allocation_marker_pattern(label)
        declared = re.search(
            marker + r"\s*(?:满分|共计|共|计|占|为)?\s*(\d+(?:\.\d+)?)\s*分", ordinary
        )
        if declared:
            expected.setdefault(label, declared[1])
        sequence = re.search(
            marker + r"\s*(?:按|分步为)\s*([\d.]+(?:[、,，]\s*[\d.]+)+)\s*分", ordinary
        )
        if sequence:
            stated = [Decimal(n.strip()) for n in re.split("[、,，]", sequence[1])]
            actual = [
                Decimal(str(step.total_pts))
                for step in proposed[canonical_label(label)].steps
            ]
            if stated != actual:
                raise ValueError(
                    f"{label}: preserve ordered teacher step budgets {sequence[1]}."
                )
    check_response_obligation(problem.get("stem", ""), teacher, rubric.parts)
    verify_teacher_numbers(teacher, rubric, maximum)
    gaps = list(rubric.teacher_gaps) if teacher else []
    mentioned = {
        label
        for label in labels
        if label and re.search(_allocation_marker_pattern(label), ordinary, re.I)
    }
    if (
        mentioned
        and len(mentioned) < len(labels)
        and not re.search(
            r"每(?:一|个)?小问|各(?:小)?问|all parts|each part", ordinary, re.I
        )
    ):
        gaps.append(
            "教师标准未明确涵盖的小问 / Parts without an explicit teacher rule: "
            + ", ".join(label for label in labels if label not in mentioned)
        )
    fixed = []
    for index, label in enumerate(labels):
        p = proposed[canonical_label(label)]
        if label in expected and Decimal(str(expected[label])) != Decimal(
            str(p.total_pts)
        ):
            raise ValueError(
                f"Teacher assigned {label} {expected[label]} points; restore that budget."
            )
        stem = parts[index]["stem"] if parts else problem.get("stem", "")
        shared_stem = structure.get("shared_stem", "") if parts else ""
        required = _REQUIRED.search(stem) or _REQUIRED.search(shared_stem)
        floor = rubric.nonblank_floor.resolve(maximum)
        if required and _REQUIRED.search(shared_stem):
            cap = floor / Fraction(str(maximum))
            alternative_max = Amount(fraction=f"{cap.numerator}/{cap.denominator}")
        elif required:
            alternative_max = Amount(
                points=float(min(floor, Fraction(str(p.total_pts))))
            )
        else:
            alternative_max = Amount(fraction="1/1")
        policy = Alternative(
            maximum=alternative_max,
            source_quote=(
                (stem if _REQUIRED.search(stem) else shared_stem) if required else ""
            ),
        )
        applicable = []
        for clause in alternates:
            named = [
                l
                for l in labels
                if l and re.search(_allocation_marker_pattern(l), clause, re.I)
            ]
            if not named or label in named:
                applicable.append(clause)
        if applicable:
            policy = read_alternative(
                "；".join(applicable), maximum=p.total_pts, fallback=policy
            )
        for override in (problem.get("alternative_rules") or {}).values():
            if override["label"] == (label or None) and override["stem"] == (
                stem if parts else None
            ):
                policy = read_alternative(override["text"], maximum=p.total_pts)
        if not teacher:
            p = p.model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(update={"source_quote": ""}) for step in p.steps
                    ),
                    "deductions": tuple(
                        rule.model_copy(update={"source_quote": ""})
                        for rule in p.deductions
                    ),
                }
            )
        for item in (*p.steps, *p.deductions):
            if item.source_quote and not match_source_quote(teacher, item.source_quote):
                raise ValueError(
                    "A supplied scoring rule must quote its teacher source exactly."
                )
        fixed.append(
            p.model_copy(
                update={
                    "label": label or None,
                    "stem": stem if parts else None,
                    "alternative": policy,
                }
            )
        )
    return rubric.model_copy(
        update={
            "id": problem.get("q_id") or rubric.id,
            "label": str(problem.get("number") or ""),
            "stem": (
                (structure.get("shared_stem") or "")
                if parts
                else problem.get("stem", "")
            ),
            "teacher_rubric": teacher or None,
            "rubric_origin": "teacher" if teacher else "ai",
            "parts": tuple(fixed),
            "base_unit_pts": _base_unit(fixed, teacher),
            "teacher_gaps": tuple(dict.fromkeys(gaps)),
            "low_confidence": bool(teacher and rubric.low_confidence),
        }
    )


def render_parts(rubric, language="zh"):
    zh = language == "zh"
    unit = "分" if zh else " points"
    lines = []
    for part in rubric.parts:
        heading = " ".join(value for value in (part.label, part.stem) if value)
        if heading:
            lines.append(heading)
        prefix = f"{part.label} " if part.label else ""
        for step in part.steps:
            lines.append(f"- {prefix}[{step.total_pts:g}{unit}] {step.criterion}")
        if part.deductions:
            lines.append(
                f"{prefix}{'满分' if zh else 'Maximum '}{part.total_pts:g}{unit}"
            )
            for rule in part.deductions:
                lines.append(
                    f"- {rule.criterion} ({'扣' if zh else 'deduct '}{rule.deduct_pts:g}{unit})"
                )
    return "\n".join(lines)


def alternative_text(part, zh=True):
    policy = part.alternative

    def amount(value):
        if value.points is not None:
            return f"{value.points:g}分" if zh else f"{value.points:g} points"
        return (
            f"本问满分的 {value.fraction}"
            if zh
            else f"{value.fraction} of the part maximum"
        )

    if zh:
        loss = (
            f"每个独立错误扣{policy.deduct_per_error:g}分。"
            if policy.deduct_per_error
            else "部分完成时按已达到的评分目标给分。"
        )
        return f"其他解法最高得{amount(policy.maximum)}，最低{amount(policy.minimum)}。{loss}"
    loss = (
        f"Deduct {policy.deduct_per_error:g} points per independent error."
        if policy.deduct_per_error
        else "Award partial credit for the goals achieved."
    )
    return f"Alternative methods: maximum {amount(policy.maximum)}, minimum {amount(policy.minimum)}. {loss}"


def supplied_notation(stem):
    """Return an explicit notation convention; an assertion to prove is not a given."""
    stops = ".!?。！？"
    for match in re.finditer(
        r"\b(?:may|can) be (?:replaced|denoted|written) by\b|"
        r"\b(?:denote|represent) the same (?:[a-z]+ ){0,2}(?:input|variable|quantity|object)\b|"
        r"\bunder the (?:given )?(?:identification|correspondence|substitution)\b|"
        r"(?:可(?:以)?用.{1,50}?(?:代替|表示)|在(?:给定|上述)(?:对应|代换)下|分别记作)",
        stem,
        re.I,
    ):
        left = max(stem.rfind(c, 0, match.start()) for c in stops) + 1
        end = min(len(stem), match.end() + 400)
        right = next((i + 1 for i in range(match.end(), end) if stem[i] in stops), end)
        clause = stem[max(left, match.start() - 400) : right].strip()
        # A parenthetical 'where ...' may follow the main proof obligation.
        context = re.split(
            r"\bwhere\b|其中|这里", stem[left : match.end()], flags=re.I
        )[-1]
        if re.search(
            r"\b(?:prove|show|verify|whether|if|only if|not|cannot)\b|"
            r"证明|说明是否|是否|不得|不可以",
            context,
            re.I,
        ) or clause.endswith(("?", "？")):
            continue
        return clause
    return None


PART_INSTRUCTION = """
For a written math, proof or concept rubric, return part_rubric using the question schema.
Derive the required response from the exact stem and teacher rules. For true/false, selection,
fill-in or final-value tasks, a correct response earns its budget unless written reasoning was
explicitly requested. For decision-only true/false questions include expected_truth_by_part in the enclosing
rubric_definition: a JSON boolean for each part ID, checked against the original proposition.
A detailed reference solution does not add a student obligation. For a
sorting question assess the requested ordering and equivalent growth classes. Give separately
checkable outcomes separate steps when partial progress is scorable.
Always include parts (one unnumbered part with null label/stem for an unsplit question).
Preserve the original question and all explicit teacher policies. For each known part give
total_pts and exactly one of steps (budgets sum to total_pts) or deductions (positive amounts
per supported error; they do not sum to total_pts). Reference solutions can contain detailed
unscored steps for either kind. Make goals method-independent unless the task requires a method. In a generated proof rubric, each
criterion names the mathematical obligation and acceptable evidence. Put sample computations,
chosen notation and the reference route in the reference answer. For each AI-authored step,
check whether a different sufficient argument could earn its points as written. If a student
would have to pretend to use the sample method, rewrite the step around the conclusion to establish.
For a universally quantified claim, retain the original domain in the scored obligation. Do not append
a new sufficient shortcut to the criterion. A proof on a proper subset is a case result; the remaining
cases must be established for full credit. Preserve explicit source cases and teacher policies.
If a proof can make meaningful
partial progress, budget distinct, separately assessable obligations before seeing student work.
A change of names supplied by the question, rewriting a definition, or restating an already proved
equivalence cannot consume an independent budget. Separate a genuinely new implication, condition
or construction; the goal must admit a meaningful difference in demonstrated understanding.
Do not give multiple budgets to repeats of the same inference. State which hypotheses/cases are
substantive. A valid direct implication or equivalence chain may meet more than one independent goal.
Extract explicit source rules with exact source_quote evidence. Only fill missing ordinary
rules using AI; report which teacher fields were unclear or missing in teacher_gaps.
Do not report the absence of alternative-method rules or the use of AI as a defect.
Preserve teacher caps, floors, exemptions, follow-through fractions and counting scopes.
Ratios of the maximum (一半, 最多给1/3, 50%) are fractions, e.g. {"fraction":"1/3"}.
Absolute amounts (0.5分, 半分) use {"points":0.5}. Never turn a proportion into a point.
"""


def verify_teacher_numbers(teacher, rubric, maximum):
    """Verify unambiguous teacher constraints within the part named by the source."""
    from backend.services.question_structure import _allocation_marker_pattern

    ordinary = teacher
    for clause in alternative_clauses(teacher):
        ordinary = ordinary.replace(clause, "")
    ordinary = unicodedata.normalize("NFKC", ordinary)
    match = re.search(
        r"(?:最低|保底|至少)(?:得|给|为|是)?\s*(\d+(?:\.\d+)?\s*(?:分|%))", ordinary
    )
    if match and parse_amount(match[1]).resolve(
        maximum
    ) != rubric.nonblank_floor.resolve(maximum):
        raise ValueError(f"Preserve the explicit nonblank floor: {match[1]}.")
    if (
        not match
        and rubric.nonblank_floor.resolve(maximum)
        and not re.search(r"保底|最低|至少|floor|minimum", ordinary, re.I)
    ):
        raise ValueError("No teacher minimum was provided; use a zero nonblank floor.")
    for m in re.finditer(
        r"(?:扣(?:除)?|deduct)\s*(\d+(?:\.\d+)?)\s*(?:分|points?)", ordinary, re.I
    ):
        clause = re.split(r"[。；;\n]", ordinary[: m.end()])[-1]
        eligible = [
            p
            for p in rubric.parts
            if p.label and re.search(_allocation_marker_pattern(p.label), clause, re.I)
        ] or list(rubric.parts)
        if not any(
            Decimal(str(d.deduct_pts)) == Decimal(m[1])
            for p in eligible
            for d in p.deductions
        ):
            raise ValueError(f"Teacher deduction {m[1]} must be preserved in its part.")

    for clause in re.split(r"[。；;\n]", ordinary):
        if not re.search(r"沿用|后续|后传|follow.through|propagat", clause, re.I):
            continue
        match = re.search(
            r"(?:保留|折算为|retain|keep)\s*(" + _AMOUNT.pattern + ")", clause, re.I
        )
        if not match:
            continue
        amount = parse_amount(match[1])
        scoped = [
            p
            for p in rubric.parts
            if p.label and re.search(_allocation_marker_pattern(p.label), clause, re.I)
        ] or rubric.parts
        if not any(
            step.follow_through_keep is not None
            and Fraction(str(step.follow_through_keep)) * Fraction(str(step.total_pts))
            == amount.resolve(step.total_pts)
            for part in scoped
            for step in part.steps
        ):
            raise ValueError(
                f"Preserve the teacher's follow-through credit {match[1]} in its designated step."
            )
