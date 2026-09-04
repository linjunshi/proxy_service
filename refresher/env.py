"""How this stack reads its environment -- one rule, shared by both sidecar programs.

A value that is missing or malformed refuses the process a start and names itself,
rather than falling back to a number the operator never wrote in .env.
"""

from __future__ import annotations

import os
import re


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set")
    return value


def optional(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def whole_number(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value


def pattern(name: str, default: str) -> re.Pattern[str]:
    return _compile(name, os.environ.get(name, "").strip() or default)


def optional_pattern(name: str) -> re.Pattern[str] | None:
    """An unset exclude means exclude nothing -- an empty pattern would match every name."""
    raw = os.environ.get(name, "").strip()
    return _compile(name, raw) if raw else None


def _compile(name: str, expression: str) -> re.Pattern[str]:
    try:
        return re.compile(expression)
    except re.error as error:
        raise ValueError(f"{name} is not a valid regex: {error}") from None
