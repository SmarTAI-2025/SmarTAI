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

    @model_validator(mode="before")
    @classmethod
    def zero_has_one_value(cls, value):
        if type(value) in (int, float, str) and value in (0, "0"):
            return {"points": 0}
        return value

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
    max_deduction: Points | None = Field(default=None, exclude_if=lambda v: v is None)
    source_quote: str = Field(default="", max_length=4000)

    # Needed only for teacher clauses that cannot be resolved by the deterministic reader.
    field_sources: dict[Literal["maximum", "minimum", "deduct_per_error", "max_deduction"], Text] = Field(
        default_factory=dict, exclude_if=lambda v: not v
    )
    inferred_method_cap: bool = Field(default=False, exclude_if=lambda v: not v)

    def needs_rate(self, part_points) -> bool:
        return self.deduct_per_error is None and self.minimum.resolve(part_points) < self.maximum.resolve(part_points)

    @model_validator(mode="after")
    def check_deduction_cap(self):
        fixed_zero = self.maximum.points == self.minimum.points == 0
        if self.max_deduction is not None and self.deduct_per_error is None and not fixed_zero:
            raise ValueError("Specify the per-error deduction for this cumulative deduction cap.")
        return self


class Criterion(Frozen):
    id: Key
    criterion: Text
    source_quote: str = Field(default="", max_length=4000, exclude_if=lambda v: not v)


class PointStep(Criterion):
    required_conditions: tuple[Text, ...] = Field(default=(), max_length=20, exclude_if=lambda v: not v)
    follow_through_pts: Points | None = Field(default=None, exclude_if=lambda v: v is None)
    total_pts: Points = Field(gt=0)
    answer_refs: tuple[Key, ...] = Field(default=(), exclude_if=lambda v: not v)
    follow_through_keep: float | None = Field(
        default=None, ge=0, lt=1, allow_inf_nan=False
    )


    @model_validator(mode="after")
    def check_follow_through(self):
        if self.follow_through_pts is not None and (
            self.follow_through_keep is not None or self.follow_through_pts >= self.total_pts
        ):
            raise ValueError("Supply only one follow-through award, smaller than the step budget.")
        return self

    def follow_through_award(self, *, ai_default=False):
        if self.follow_through_pts is not None:
            return Fraction(str(self.follow_through_pts))
        rate = self.follow_through_keep
        if rate is None:
            rate = 0.5 if ai_default else 0
        return Fraction(str(rate)) * Fraction(str(self.total_pts))


class Penalty(Criterion):
    exclusive_group: Key | None = Field(default=None, exclude_if=lambda value: value is None)
    deduct_pts: Points = Field(gt=0)
    max_deduction: Points | None = Field(default=None, exclude_if=lambda v: v is None)
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
        if not self.steps and not self.deductions:
            raise ValueError(
                f"{self.id}: supply funded steps or deduction rules."
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
        if len({i.id for i in (*self.steps, *self.deductions)}) != len(self.steps) + len(self.deductions):
            raise ValueError(f"{self.id}: duplicate scoring item ID.")
        if (
            not 0
            <= self.alternative.minimum.resolve(self.total_pts)
            <= self.alternative.maximum.resolve(self.total_pts)
            <= Fraction(str(self.total_pts))
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

    def missing_alternative_rates(self):
        return [p.id for p in self.parts if p.alternative.needs_rate(p.total_pts)]

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
    r"(\d+(?:\.\d+)?\s*/\s*[1-9]\d*\s*(?:分(?!之)|points?)?|\d+(?:\.\d+)?\s*%|一半|半分|[二三四五六七八九十]分之[一二三四五六七八九]|(?:\d+(?:\.\d+)?|\.\d+)\s*(?:分(?!之)|points?)|零分)",
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


def _requires_method(text):
    for clause in re.split(r"[，,。；;\n]", text or ""):
        for match in _REQUIRED.finditer(clause):
            if re.search(r"(?:不|并非|没有|无需|不必)\s*$|\bnot\s+$", clause[:match.start()], re.I):
                continue
            if not re.search(r"任意|任何|自选|自由选择|不限|any method", match[0], re.I):
                return True
    return False


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


_ZERO_AWARD = r"不得分|不给分|不予(?:给)?分|不计分|不记分|no (?:credit|marks?|points?)"
_FULL_AWARD = r"满分(?!\s*(?:的|[数值]|(?:为|是|[:：])?\s*\d+\s*分))|full (?:credit|marks)"
_POLICY_TOKEN = re.compile(rf"(?P<amount>{_AMOUNT.pattern})|(?P<zero>{_ZERO_AWARD})|(?P<full>{_FULL_AWARD})", re.I)
_LOSS_CAP = re.compile(r"(?:(?:累计|合计|一共|总共)?(?:最多|至多|最高)|上限(?:为)?|累计|合计)\s*(?:可)?扣(?:除)?\s*$|"
                       r"(?:累计|总)扣分(?:上限)?(?:为|是)?\s*$|(?:maximum|total|cumulative) deduction(?: of)?\s*$", re.I)


_NOT_FULL = re.compile(r"(?:不(?:能|可|允许|应|得)?(?:给予|给|予|得|计|获得)?\s*满分|"
                       r"(?:not(?: (?:given|receive|award))?|no)\s+full (?:credit|marks))", re.I)


class UninterpretedAlternative(ValueError):
    """A clause needs semantic interpretation; explicit contradictions still fail."""


def _alternative_fields(text):
    """Parse only the fields actually stated by the teacher."""
    values = {}
    normalized = unicodedata.normalize("NFKC", text)
    for match in _POLICY_TOKEN.finditer(normalized):
        prefix = normalized[:match.start()]
        clause = re.split(r"[，,；;。\n]|\.(?=\s|$)", prefix)[-1].casefold()
        token = match.group()
        if match["full"]:
            if re.search(r"(?:[/％%]\d*的?|分之[一二三四五六七八九]的?|of(?: the)?)\s*$", clause):
                continue
            if _NOT_FULL.search(clause + token):
                continue
            field, value = "maximum", Amount(fraction="1/1")
        elif match["zero"]:
            field, value = "maximum", Amount(points=0)
        elif re.search(r"[-−负]\s*$", clause):
            raise ValueError("Scoring amounts cannot be negative.")
        elif _LOSS_CAP.search(clause):
            amount = parse_amount(token)
            if amount.points is None:
                raise ValueError("Specify the cumulative deduction cap in points.")
            field, value = "max_deduction", amount.points
        elif re.search(r"(?:扣(?:除)?|deduct)\s*$", clause):
            amount = parse_amount(token)
            if amount.points is None or amount.points <= 0:
                raise ValueError("Give a positive absolute deduction per error.")
            field, value = "deduct_per_error", amount.points
        elif re.search(r"最低|保底|至少|minimum|floor", clause):
            field, value = "minimum", parse_amount(token)
        elif (
            re.search(
                r"满分|最多|最高|最贵|给|至多|上限|封顶|maximum|up to|capped|worth|得|计|为|0分$|按",
                clause,
            )
            or parse_amount(token).points == 0
            or _ALT.search(clause)
        ):
            field, value = "maximum", parse_amount(token)
        else:
            raise UninterpretedAlternative(
                "Cannot determine whether this amount is a cap, floor or deduction."
            )
        if field in values and values[field] != value:
            raise ValueError(f"Conflicting alternative {field}.")
        values[field] = value
    if not values and not _NOT_FULL.search(text):
        if re.search(r"同等(?:给分|评分|待遇)|等同(?:于)?满分", text):
            values["maximum"] = Amount(fraction="1/1")
        else:
            raise UninterpretedAlternative("Unclear alternative method policy.")

    return values


def read_alternative(text: str, *, maximum: float, fallback: Alternative | None = None) -> Alternative:
    fields = _alternative_fields(text)
    return _checked_alternative(fallback or Alternative(), fields, text, maximum)


def _checked_alternative(policy, values, text, maximum):
    policy = Alternative.model_validate(
        {**policy.model_dump(), **values, "source_quote": text}
    )
    if (
        not 0
        <= policy.minimum.resolve(maximum)
        <= policy.maximum.resolve(maximum)
        <= Fraction(str(maximum))
    ):
        raise ValueError("Alternative minimum, maximum and part budget conflict.")
    if _NOT_FULL.search(text) and policy.maximum.resolve(maximum) == Fraction(str(maximum)):
        raise UninterpretedAlternative("Specify a maximum below full credit for the restricted alternative.")
    return policy


def alternative_clauses(text):
    # Keep a policy's monetary continuations together across punctuation.
    # A new named part or an ordinary-rule heading ends the previous scope.
    text = text or ""
    spans = []
    active = False
    for item in re.finditer(r"(?:(?![，,、]\s*(?:本题|整题|非空|只要有作答))[^。；;\n])+", text):
        clause = item.group().strip()
        if _ALT.search(clause):
            spans.append(item.span())
            active = True
        elif active and re.match(
            r"(?:每(?:个?独立)?错|每[一每]?处|累计|合计|至多扣|最多扣|最高扣|最低|保底|"
            r"deduct\b|maximum deduction\b|minimum\b)", clause, re.I
        ):
            spans[-1] = (spans[-1][0], item.end())
        else:
            active = False
    return [text[start:end].strip() for start, end in spans]


def _main_clause(clause):
    marker = _ALT.search(clause)
    prefix = clause[:marker.start()] if marker else ""
    return prefix if re.search(r"[，,:：]|分|points?\b", prefix, re.I) else ""


def ordinary_criterion(text):
    for clause in alternative_clauses(text):
        text = text.replace(clause, _main_clause(clause), 1)
    return text


def _teacher_alternative_fields(text, proposed):
    """Recognized amounts override the proposal. Semantic-only wording needs source evidence."""
    try:
        fields = _alternative_fields(text)
    except UninterpretedAlternative:
        if not proposed.field_sources:
            raise
        fields = {}
        # Still bind each independently recognizable clause deterministically.
        for clause in re.split(r"[。；;，,\n]", text):
            if clause.strip():
                try:
                    for name, amount in _alternative_fields(clause).items():
                        if name in fields and fields[name] != amount:
                            raise ValueError(f"Conflicting alternative {name}.")
                        fields[name] = amount
                except UninterpretedAlternative:
                    continue
    from backend.services.math_source_evidence import match_source_quote
    for name, quote in proposed.field_sources.items():
        if match_source_quote(text, quote):
            try:
                recognized = _alternative_fields(quote)
            except UninterpretedAlternative:
                recognized = {}
            if recognized and name not in recognized:
                continue
            value = getattr(proposed, name)
            if value is not None:
                fields.setdefault(name, value)
    if not fields and not _NOT_FULL.search(text):
        raise UninterpretedAlternative("Identify each explicit teacher field and quote the supporting clause.")
    return fields


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


def bind_alternative(part, *, label, stem, structure, floor, question_points, teacher, override, binary=False):
    """Bind each field once for both a model proposal and a teacher's edited text."""
    from backend.services.question_structure import _allocation_marker_pattern

    labels = [item["label"] for item in structure.get("subparts", [])]
    shared_stem = structure.get("shared_stem", "") if labels else ""
    teacher_clauses = []
    ordinary = ordinary_criterion(teacher)
    for clause in alternative_clauses(teacher):
        named = [name for name in labels if re.search(_allocation_marker_pattern(name), clause, re.I)]
        if not named or label in named:
            teacher_clauses.append(clause)
    teacher_method = ""
    for clause in re.split(r"[。；;\n]", ordinary):
        # Only explicit requirements placed on the student constrain another method.
        if _requires_method(clause):
            named = [name for name in labels if re.search(_allocation_marker_pattern(name), clause, re.I)]
            if not named or label in named:
                teacher_method = clause.strip()
                break
    proposed = part.alternative
    policy_text = "；".join(clause[_ALT.search(clause).start():] for clause in teacher_clauses)
    fields = _teacher_alternative_fields(policy_text, proposed) if policy_text else {}
    if override is not None:
        fields.update(_teacher_alternative_fields(override, proposed))
    source = override or "；".join(teacher_clauses)
    restriction = shared_stem if _requires_method(shared_stem) else stem if _requires_method(stem) else teacher_method
    inferred = False
    share = floor / Fraction(str(question_points))
    part_floor = share * Fraction(str(part.total_pts))
    # Materialize exact decimal awards (including zero); keep the exact fraction
    # for recurring values rather than changing a teacher's award by rounding.
    decimal_points = float(part_floor)
    share_amount = (Amount(points=decimal_points) if Fraction(str(decimal_points)) == part_floor
                    else Amount(fraction=f"{share.numerator}/{share.denominator}"))
    if restriction and "maximum" not in fields:
        fields["maximum"] = share_amount
        fields.setdefault("minimum", share_amount)
        inferred = bool(source)
    elif "maximum" not in fields and proposed.maximum.resolve(part.total_pts) < part_floor:
        raise ValueError(f"{part.id}: AI alternative.maximum must be between its share of the "
                         f"nonblank floor ({float(part_floor):g}) and its total ({part.total_pts:g}).")
    from backend.services.math_source_evidence import match_source_quote
    sources = {key: quote for key, quote in proposed.field_sources.items()
               if key in fields and source and match_source_quote(source, quote)}
    # An explicit per-error rate applies to each independent error until the
    # stated floor. An extra cumulative cap would silently change that rate.
    if "deduct_per_error" in fields and "max_deduction" not in fields:
        fields["max_deduction"] = None
    defaults = Alternative(field_sources=sources, maximum=proposed.maximum,
                           minimum=Amount(points=0) if proposed.inferred_method_cap else proposed.minimum,
                           deduct_per_error=proposed.deduct_per_error or (part.total_pts if binary else None),
                           max_deduction=proposed.max_deduction)
    fields["inferred_method_cap"] = inferred
    return _checked_alternative(defaults, fields, source or restriction or "", part.total_pts)


_RESULT_ONLY = re.compile(
    r"(?:只需|只须|仅需|只填|只写|仅填写|仅给出)\s*(?:最终|最后)?(?:结果|答案|数值)|"
    r"(?:求|确定|计算|统计)[^。！？\n]{0,75}(?:语句的?(?:频度|执行次数)|执行了?多少次)|"
    r"\b(?:final (?:answer|value) only|(?:determine|find|give) the (?:frequency|execution count) "
    r"of the (?:@[- ]marked|marked) statement)\b", re.I,
)


# A shared instruction followed by a list of inputs is source evidence of a
# repeated task. Local instructions can introduce different demands or weights.
_PARALLEL_LIST = re.compile(r"下列|以下|各(?:个|项|式|题|句|程序|函数)|分别|逐[一项个]|\b(?:each|following)\b", re.I)
_TASK_VERB = re.compile(
    r"计算|求出|求解|求值|判断|确定|判定|证明|简化|化简|写出|指出|列出|翻译|分类|画出|解释|说明|推导|设计|分析|比较|"
    r"(?:^|[，。；：\s])求|\b(?:calculate|compute|solve|evaluate|determine|decide|classify|prove|"
    r"simplify|identify|translate|find|state|derive|explain|compare|sketch|design|justify|show)\b", re.I)
_WEIGHT_IN_SOURCE = re.compile(
    r"(?:\d|[零一二三四五六七八九十两半])\s*(?:分(?![钟子母解析布配式])|points?\b|marks?\b|pts?\b|[%％])|"
    r"权重|加权|占比|分值|配分|加倍|权数|\b(?:weights?|weighted|double credit|extra credit)\b", re.I)


def parallel_part_budgets(problem):
    """Allocate unweighted parallel inputs in source order, using displayable hundredths."""
    structure = problem.get("question_structure") or {}
    parts = structure.get("subparts") or []
    if len(parts) < 2 or problem.get("teacher_subpart_points"):
        return {}
    shared = structure.get("shared_stem", "")
    # A whole-question maximum does not prescribe relative part weights.
    header = re.sub(r"(?:本题|整题|全题)?(?:满分|共计|总分)\s*[:：]?\s*[\d.]+\s*分|"
                    r"\b(?:total(?: of)?|out of)\s*[\d.]+\s*(?:points?|marks?)\b", "", shared, flags=re.I)
    source = " ".join([header, problem.get("criterion") or "", *(part["stem"] for part in parts)])
    if _WEIGHT_IN_SOURCE.search(source):
        return {}
    if not (_PARALLEL_LIST.search(shared) and _TASK_VERB.search(shared)):
        return {}
    if any(_TASK_VERB.search(part["stem"]) for part in parts):
        return {}
    hundredths = Decimal(str(problem.get("max_score") or 10)) * 100
    if hundredths != hundredths.to_integral_value() or hundredths < len(parts):
        return {}
    base, remainder = divmod(int(hundredths), len(parts))
    return {part["label"]: (base + (index < remainder)) / 100 for index, part in enumerate(parts)}


def bind_parts(rubric: PartRubric, problem: dict, *, previous: PartRubric | None = None, language="zh") -> PartRubric:
    """Bind the model proposal to known question scope and authoritative teacher text."""
    from backend.services.question_structure import _allocation_marker_pattern
    from backend.services.math_source_evidence import match_source_quote

    from backend.services.true_false_answers import is_decision_only

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
    if rubric.nonblank_floor.resolve(maximum) > Fraction(str(maximum)):
        raise ValueError("Nonblank floor exceeds question maximum.")
    text = problem.get("criterion") or ""
    teacher = (
        text
        if problem.get("criterion_source") != "ai"
        and "criterion" not in (problem.get("ai_completion_provenance") or {})
        else ""
    )
    teacher = "\n".join(filter(None, [teacher, problem.get("teacher_notes")]))
    ordinary = ordinary_criterion(teacher)
    expected = dict(problem.get("teacher_subpart_points") or {})
    parallel = parallel_part_budgets(problem)
    from backend.services.math_rubric_plan import _FLOOR_RULE, _source_clauses
    teacher_primary = any(not _FLOOR_RULE.fullmatch(clause) for clause in _source_clauses(ordinary))
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
    from backend.services.teacher_point_allocations import validate_teacher_point_items
    anchors = validate_teacher_point_items(rubric, ordinary)
    capped_rules = verify_teacher_numbers(teacher, rubric, maximum)
    gaps = list(rubric.teacher_gaps) if teacher else []
    from backend.services.teacher_point_allocations import bind_step_conditions
    conditions, unresolved = bind_step_conditions(rubric.parts, ordinary, anchors)
    gaps.extend(unresolved)
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
    overrides = {(item["label"], item["stem"]): item["text"]
                 for item in (problem.get("alternative_rules") or {}).values()}
    binary = is_decision_only(problem.get("stem", ""), teacher)
    shared_stem = structure.get("shared_stem", "") if parts else problem.get("stem", "")
    prior_parts = {part.id: part for part in previous.parts} if (
        previous and previous.stem == shared_stem and previous.total_pts == maximum
        and previous.nonblank_floor.resolve(maximum) == rubric.nonblank_floor.resolve(maximum)
    ) else {}
    fixed = []
    for index, label in enumerate(labels):
        p = proposed[canonical_label(label)]
        if p.id in capped_rules:
            p = p.model_copy(update={"deductions": tuple(
                rule.model_copy(update={"max_deduction": capped_rules[p.id][rule.id]})
                if rule.id in capped_rules[p.id] else rule for rule in p.deductions
            )})
        if label in parallel and Decimal(str(parallel[label])) != Decimal(str(p.total_pts)):
            raise ValueError(f"Shared task with unweighted parallel parts: use budgets {parallel}. "
                             "Adjust the steps and policies under each part to its budget.")
        if label in expected and Decimal(str(expected[label])) != Decimal(
            str(p.total_pts)
        ):
            raise ValueError(
                f"Teacher assigned {label} {expected[label]} points; restore that budget."
            )
        stem = parts[index]["stem"] if parts else problem.get("stem", "")
        instruction = shared_stem + "\n" + stem if parts else stem
        result_only = (not teacher_primary and not binary and len(p.steps) == 1 and _RESULT_ONLY.search(instruction)
                       and not _REQUIRES_REASON.search(instruction) and not _requires_method(instruction)
                       and not re.search(r"过程|理由|说明|步骤|并(?:给出|分析|比较|绘|画)|"
                                         r"\b(?:reasoning|derivation|explain|steps|and (?:show|give|compare))\b|"
                                         r"(?:不能|不可|不得|不要|禁止|勿)\s*(?:只|仅)|\bnot\s+(?:the\s+)?final",
                                         instruction, re.I))
        policy = bind_alternative(p, label=label, stem=stem, structure=structure,
            floor=rubric.nonblank_floor.resolve(maximum), question_points=maximum, teacher=teacher,
            override=overrides.get((label or None, stem if parts else None)), binary=binary)
        prior = prior_parts.get(p.id)
        if (prior and prior.label == (label or None) and prior.stem == (stem if parts else None)
                and prior.total_pts == p.total_pts and prior.alternative.source_quote == policy.source_quote
                and prior.alternative.inferred_method_cap == policy.inferred_method_cap):
            # A semantic edit of the main rubric is not an edit of the other
            # route. Rebind its saved values to current teacher constraints.
            policy = bind_alternative(prior, label=label, stem=stem, structure=structure,
                floor=rubric.nonblank_floor.resolve(maximum), question_points=maximum, teacher=teacher,
                override=overrides.get((label or None, stem if parts else None)), binary=binary)
        if policy.needs_rate(p.total_pts):
            raise ValueError(f"{p.id}: supply a positive alternative.deduct_per_error in points "
                             "for this part. Preserve the main rubric and explicit teacher constraints.")
        if p.steps and any((p.id, rule.id) not in anchors for rule in p.deductions):
            raise ValueError(f"{p.id}: mixed parts need an explicit teacher allocation for each penalty.")
        if anchors:
            def bind_item(item):
                anchor = anchors.get((p.id, item.id))
                if anchor is None:
                    return item
                condition, quote = anchor
                # The condition seen by the expert is the teacher's own target,
                # even when a proposal paraphrased or mislabeled the matching leaf.
                return item.model_copy(update={"criterion": condition, "source_quote": quote})
            p = p.model_copy(update={
                "steps": tuple(bind_item(item) for item in p.steps),
                "deductions": tuple(bind_item(item) for item in p.deductions),
            })
        if not teacher_primary:
            p = p.model_copy(
                update={
                    **({"origin_overrides": {**p.origin_overrides, "criterion": "ai"}} if teacher else {}),
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
            if (item.source_quote and (p.id, item.id) not in anchors
                    and not match_source_quote(teacher, item.source_quote)):
                raise ValueError(
                    "A supplied scoring rule must quote its teacher source exactly."
                )
        # New snapshots materialize the conditional award before any student
        # work is seen. Immutable historic snapshots can still contain a rate.
        normalized_steps = []
        for step in p.steps:
            step = step.model_copy(update={"required_conditions": conditions.get((p.id, step.id), ())})
            if result_only:
                # The accepted answer owns the result. A second generated
                # formula in the only funded leaf can disagree at a boundary.
                criterion = ("按题设给出与本问标答等价的结果，满足题目规定的范围、形式与精度。"
                             if language == "zh" else "Give a result equivalent to the adopted answer for this "
                             "part over the stated domain, in the requested form and precision.")
                step = step.model_copy(update={"criterion": criterion})
            award = step.follow_through_award(ai_default=not teacher_primary)
            if Fraction(str(float(award))) == award:
                step = step.model_copy(update={"follow_through_pts": float(award), "follow_through_keep": None})
            normalized_steps.append(step)
        fixed.append(
            p.model_copy(
                update={
                    "label": label or None,
                    "stem": stem if parts else None,
                    "alternative": policy,
                    "steps": tuple(normalized_steps),
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


def render_parts(rubric, language="zh", *, full_policy=False):
    zh = language == "zh"
    unit = "分" if zh else " points"
    lines = []
    if full_policy:
        floor = rubric.nonblank_floor.resolve(rubric.total_pts)
        lines.append(f"本题满分 {rubric.total_pts:g} 分。整题未作答得 0 分。整题有书写时，保底分最低 {float(floor):g} 分。"
                     if zh else f"Maximum {rubric.total_pts:g} points. Blank: 0. Nonblank minimum: {float(floor):g} points.")
        lines.append(f"标答解法评分标准（配分单位 {rubric.base_unit_pts:g} 分）" if zh
                     else f"Reference method (allocation unit {rubric.base_unit_pts:g} points)")
    for part in rubric.parts:
        if full_policy:
            heading = f"{part.label or ''} 满分 {part.total_pts:g} 分".strip() if zh else f"{part.label or ''} Maximum {part.total_pts:g} points".strip()
        else:
            heading = " ".join(value for value in (part.label, part.stem) if value)
        if heading:
            lines.append(heading)
        prefix = f"{part.label} " if part.label else ""
        for step in part.steps:
            entry = f"- {prefix}[{step.total_pts:g}{unit}] {step.criterion}"
            if step.required_conditions:
                entry += "（" + "；".join(step.required_conditions) + "）" if zh else " (" + "; ".join(step.required_conditions) + ")"
            if full_policy:
                points = step.follow_through_award(ai_default=rubric.rubric_origin == "ai")
                entry += f"（沿用错值且本步正确时折算 {float(points):g} 分）" if zh else f" (follow-through: {float(points):g} points)"
            lines.append(entry)
        if part.deductions:
            if not full_policy:
                lines.append(f"{prefix}{'满分' if zh else 'Maximum '}{part.total_pts:g}{unit}")
            for rule in part.deductions:
                cap = ""
                if rule.max_deduction is not None:
                    cap = f"，累计最多扣{rule.max_deduction:g}分" if zh else f", maximum deduction {rule.max_deduction:g} points"
                if full_policy and rule.limit is not None:
                    cap += f"，最多 {rule.limit} 次" if zh else f", at most {rule.limit} events"
                if full_policy:
                    counting = {"target_item": ("按作答目标", "per target"), "wrong_step": ("按错误步骤", "per wrong step"),
                                "independent_error": ("按独立错误", "per independent error")}
                    cap += ("，" if zh else ", ") + counting[rule.count_by][0 if zh else 1]
                if rule.exclusive_group:
                    cap += "，同一目标只取一档" if zh else ", choose one loss for this goal"
                lines.append(
                    f"- {rule.criterion} ({'扣' if zh else 'deduct '}{rule.deduct_pts:g}{unit}{cap})"
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

    if policy.inferred_method_cap:
        ceiling = float(policy.maximum.resolve(part.total_pts))
        # The explicit number makes the editable cap understandable even for a shared floor.
        maximum_text = f"{ceiling:g}分" if zh else f"{ceiling:g} points"
    else:
        maximum_text = amount(policy.maximum)
    if zh:
        loss = (
            f"每个独立错误扣{policy.deduct_per_error:g}分。"
            if policy.deduct_per_error
            else ("本问另解按固定分数结算。" if not policy.needs_rate(part.total_pts) else "每个独立错误的扣分额待补全。")
        )
        if policy.max_deduction is not None:
            loss += f"累计最多扣{policy.max_deduction:g}分。"
        return f"其他解法最高得{maximum_text}，最低{amount(policy.minimum)}。{loss}"
    loss = (
        f"Deduct {policy.deduct_per_error:g} points per independent error."
        if policy.deduct_per_error
        else ("Fixed score for this alternative." if not policy.needs_rate(part.total_pts) else "Per-error deduction pending.")
    )
    if policy.max_deduction is not None:
        loss += f" Maximum deduction {policy.max_deduction:g} points."
    return f"Alternative methods: maximum {maximum_text}, minimum {amount(policy.minimum)}. {loss}"


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
per supported error; they do not sum to total_pts), unless the teacher explicitly combines funded
steps with named deductions. For a mixed part include both lists and quote each additional penalty.
For explicit conditions that withhold a step's award, include the exact source clauses in that
step's required_conditions. Record ambiguous targets in teacher_gaps. Reference solutions can contain detailed
unscored steps for either kind. Distinguish method-independent requested conclusions from a required technique. Scope main steps to the reference strategy and its minor variants. In a generated proof rubric, each
criterion names the mathematical obligation and acceptable evidence. Put sample computations,
chosen notation and examples in the reference answer. Equivalent notation within the same strategy
must satisfy the same scoring goals. A substantively different strategy uses only the independent
alternative policy, including for partially correct work.
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
For an AI-authored proof step, describe the result a student must establish and the evidence
that suffices. A cited witness, equation or definition can entail a conclusion by immediate
substitution on its whole domain. Do not make separately writing that substitution or restating
its conclusion a condition for any credit unless the source specifically requests it.
Withhold the award for an unsupported assertion, an invalid inference or an uncovered case.
Every explicit ordinary award must map to its own source condition, correct part and exact value.
Quote the corresponding teacher allocation (condition and amount) on that precise scoring item.
Extract explicit source rules with exact source_quote evidence. When an alternative is described in
teacher prose, use alternative.field_sources for each expressly stated field with its exact supporting
quote (maximum, minimum, deduct_per_error or max_deduction). Never attribute an AI default to the teacher. Only fill missing ordinary
rules using AI; report which teacher fields were unclear or missing in teacher_gaps.
Do not report the absence of alternative-method rules or the use of AI as a defect.
Always propose the part's independent alternative.deduct_per_error (positive absolute points)
when its maximum and minimum differ, including when the teacher gave only the main rubric.
Choose a rate appropriate to this part before seeing student work. Use the default maximum
and minimum unless constrained by the question or teacher. Do not invent a cumulative cap
or source quotation. Never encode the reference steps in the alternative.
Preserve teacher caps, floors, exemptions and counting scopes. For follow-through, use follow_through_pts for the
absolute award on a sound step using an earlier wrong result. Convert a stated fraction using
that step's own budget. The earned award replaces the full step award. Use only one representation.
max_deduction is the cumulative loss in points; deduct_pts or deduct_per_error is the loss per event.
A count limit is a number of events, not a point amount.
Ratios of the maximum (一半, 最多给1/3, 50%) are fractions, e.g. {"fraction":"1/3"}.
Absolute amounts (0.5分, 半分) use {"points":0.5}. Never turn a proportion into a point.
"""


def question_floor_amounts(ordinary, maximum):
    """Only question-level minima can award points to an unrelated nonblank response."""
    from backend.services.math_rubric_plan import _AMOUNT, _FLOOR_RULE, _amount_value, _source_clauses

    floors = set()
    for sentence in re.split(r"[。；;\n]", ordinary):
        previous = ""
        for clause in _source_clauses(sentence):
            match = _FLOOR_RULE.fullmatch(clause)
            if match and (match[1] or not previous or _AMOUNT.search(previous)
                          or re.fullmatch(r"(?:本题|整题)?(?:非空(?:作答|答案)?|只要有作答|有书写)", previous)):
                floors.add(_amount_value(match[2], match[3] or "分", maximum))
            previous = clause
    return floors


def verify_teacher_numbers(teacher, rubric, maximum):
    """Verify unambiguous teacher constraints within the part named by the source."""
    from backend.services.question_structure import _allocation_marker_pattern

    ordinary = unicodedata.normalize("NFKC", ordinary_criterion(teacher))
    floors = question_floor_amounts(ordinary, maximum)
    if (floors or {Decimal(0)}) != {rubric.nonblank_floor.resolve(maximum)}:
        raise ValueError("Preserve the explicit question floor; only a nonblank question minimum applies here.")
    capped_rules = {}
    stated_rates = set()
    for m in re.finditer(
        r"(?:扣(?:除)?|(?:maximum|total|cumulative) deduction(?: of)?|deduct)\s*(\d+(?:\.\d+)?|\.\d+)\s*(?:分|points?)", ordinary, re.I
    ):
        clause = re.split(r"[。；;\n]", ordinary[: m.end()])[-1]
        eligible = [
            p
            for p in rubric.parts
            if p.label and re.search(_allocation_marker_pattern(p.label), clause, re.I)
        ] or list(rubric.parts)
        if _LOSS_CAP.search(ordinary[:m.start(1)].casefold()):
            cap = Decimal(m[1])
            candidates = [(p, d) for p in eligible for d in p.deductions]
            if len(candidates) != 1:
                candidates = [(p, d) for p, d in candidates if d.source_quote and m.group() in d.source_quote
                              and d.source_quote in clause]
            if len(candidates) != 1:
                raise ValueError(f"Identify the deduction rule subject to the cumulative {cap}-point cap.")
            part, rule = candidates[0]
            previous = capped_rules.setdefault(part.id, {}).get(rule.id, rule.max_deduction)
            if ((previous is not None and Decimal(str(previous)) != cap)
                    or (rule.limit is not None and Decimal(str(rule.deduct_pts)) * rule.limit < cap)):
                raise ValueError(f"Preserve the cumulative deduction cap of {cap} points for {rule.id}.")
            capped_rules[part.id][rule.id] = float(cap)
            continue
        matching = {(p.id, d.id) for p in eligible for d in p.deductions
                    if Decimal(str(d.deduct_pts)) == Decimal(m[1])}
        stated_rates.update(matching)
        if not matching:
            raise ValueError(f"Teacher deduction {m[1]} must be preserved in its part.")

    for part in rubric.parts:
        for rule in part.deductions:
            if ((part.id, rule.id) in stated_rates and rule.max_deduction is not None
                    and rule.id not in capped_rules.get(part.id, {})):
                raise ValueError(f"{part.id}/{rule.id}: the teacher specified a rate without a cumulative deduction cap.")

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
        all_later = re.search(r"(?:后续|后面|后传)(?:的)?(?:各|每|所有|全部)?(?:个)?(?:步骤|各步|每步)|"
                              r"(?:all|each|every) (?:later|subsequent|downstream) step", clause, re.I)
        applicable = [step for part in scoped for step in part.steps]
        if all_later:
            if re.search(r"每个?小问|各小问|each part", clause, re.I):
                applicable = [step for part in scoped for step in part.steps[1:]]
            else:
                applicable = applicable[1:]

        def matches(step):
            return ((step.follow_through_keep is not None or step.follow_through_pts is not None)
                    and step.follow_through_award() == amount.resolve(step.total_pts))

        if not applicable or not (all if all_later else any)(matches(step) for step in applicable):
            raise ValueError(
                f"Preserve the teacher's follow-through credit {match[1]} in its designated step."
            )
    return capped_rules
