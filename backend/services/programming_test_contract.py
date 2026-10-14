"""Read an explicit Python entry-point signature without executing source text."""
import re


def required_function_name(stem: str) -> str | None:
    names = set(re.findall(r"(?m)^\s*def\s+([A-Za-z_]\w*)\s*\(", stem))
    # Multiple definitions can include helpers; never guess the entry point.
    return next(iter(names)) if len(names) == 1 else None
