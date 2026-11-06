"""Local witnesses for overlapping deductions. No source is executed here."""
import ast
import re
from decimal import Decimal


# A self-contained, unconditional rubric clause with an explicit source pattern.
# More complex prose can suggest a second look, but cannot prove a point limit.
_CALL = r"[A-Za-z_][A-Za-z_0-9(\t ]*?\(\s*\.\.\.\s*\)[)\t ]*"
_CAP = re.compile(
    rf"(?:^|[。；;\n])\s*`?(?P<pattern>{_CALL})`?\s*"
    r"(?:([\u4e00-\u9fff]{0,30})只扣([\u4e00-\u9fff]{0,24})\s*(?P<zh>\d+(?:\.\d+)?)\s*分"
    r"|(?:costs|loses) (?:only|at most) (?P<en>\d+(?:\.\d+)?) points?)"
    r"(?=\s*(?:[。；;.\n]|[,，]\s*(?:不得|不能|不可|禁止|do not|never)|$))", re.I,
)
_CASE = re.compile(r"(?:(?:用例|样例)\s*|\b(?:test\s+)?case\s*#?)([1-9]\d?)", re.I)


def _source_delta(pattern, source, reference, entry):
    """Remove one unary call named in the rubric and compare the entire AST to the checked reference."""
    if not reference or not entry or max(len(source), len(reference)) > 24000:
        return None
    try:
        rule = ast.parse(pattern, mode="eval").body
        src, ref = ast.parse(source), ast.parse(reference)
        # An expression mentioned in a comment, unused helper or dead branch
        # is not a witness for an executed call. Limit proof to a single return.
        if (len(src.body) != 1 or not isinstance(fn := src.body[0], ast.FunctionDef)
                or fn.name != entry or len(fn.body) != 1 or not isinstance(fn.body[0], ast.Return)):
            return None
        node = fn.body[0].value
        parent = fn.body[0]
        while isinstance(rule, ast.Call):
            if (not isinstance(rule.func, ast.Name) or len(rule.args) != 1 or rule.keywords
                    or not isinstance(node, ast.Call) or ast.dump(node.func) != ast.dump(rule.func)
                    or len(node.args) != 1 or node.keywords):
                return None
            arg = rule.args[0]
            if isinstance(arg, ast.Constant) and arg.value is Ellipsis:
                origin = ast.get_source_segment(source, node)
                if isinstance(parent, ast.Return):
                    parent.value = node.args[0]
                else:
                    parent.args[0] = node.args[0]
                if origin and len(origin) <= 240 and ast.dump(src) == ast.dump(ref):
                    return rule.func.id, origin
                return None
            rule, parent, node = arg, node, node.args[0]
    except (SyntaxError, ValueError, RecursionError):
        pass
    return None


def _grounded_awards(awards, case, name):
    # No interpretation of explanatory prose. Each deduction must cite the
    # changed operation, or both deductions must refer to the same actual
    # failed teacher case and at least one must name that operation.
    identifier = re.compile(rf"(?<![a-zA-Z_0-9]){re.escape(name)}(?![a-zA-Z_0-9])")
    texts = [a.desc[:240] for a in awards]
    names = [identifier.search(text) for text in texts]
    cases = [set(_CASE.findall(text)) == {str(case)} for text in texts]
    return texts if all(names) or (any(names) and all(cases)) else []


def uncited_deductions(awards, case, rule, source, reference, entry):
    """Suspicions request one check. Only a reproducible, capped source difference is a witness."""
    if len(awards) < 2:
        return None
    same_case = [a for a in awards if str(case) in _CASE.findall(a.desc)]
    matches = list(_CAP.finditer(rule))
    agreed = same_case if len(same_case) >= 2 else []
    if not matches and not agreed and source and len(source) <= 24000:
        # Shared, named calls can point to a common cause even when the rubric
        # uses free-form language. This check can only request a second look.
        try:
            names = {n.func.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        except (ValueError, SyntaxError, RecursionError):
            names = set()
        if any(_grounded_awards(awards, case, name) for name in names):
            agreed = awards
    proof = {}
    if len(matches) == 1:
        match, = matches
        clause = match.group(0).lstrip("。；;\n ").strip()
        if not re.search(r"如果|若|除非|当|但|否则|仅当|同时|另扣|不只扣", clause):
            cap = Decimal(match["zh"] or match["en"])
            spent = sum(Decimal(str(a.max_points)) - Decimal(str(a.score)) for a in awards)
            if spent > cap:
                agreed = awards
                delta = _source_delta(match["pattern"], source, reference, entry)
                # A separately chargeable requirement can concern the very same
                # expression. An open/independent allocation needs a teacher.
                if delta and not re.search(r"另(?:行)?扣|分别扣|独立(?:计|评|扣)分|\b(?:separately|independently)\b", rule, re.I):
                    name, expression = delta
                    grounds = _grounded_awards(awards, case, name)
                    if grounds:
                        proof = dict(governing_rule=clause, source_pattern=match["pattern"], source_quote=expression,
                                     reason_quotes=grounds, allowed=format(cap.normalize(), "f"), deducted=format(spent.normalize(), "f"))
    if not agreed:
        return None
    return {"kind": "capped_source_deduction_exceeded" if proof else "unattributed_shared_failure",
            "cases": [case], "steps": [a.step_no for a in agreed], "descriptions": [a.desc[:240] for a in agreed], **proof}
