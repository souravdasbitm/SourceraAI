"""Normalize an English step so different test data shares one cached plan.

Bindings keep the original spelling. Stored plans hold tokens ({q1}, {email1}, {n1})
and the runner puts the real values back at replay time.
"""
import copy
import re

_QUOTED = re.compile(r'"([^"]*)"|\'([^\']*)\'')
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")


def normalize_step(text: str) -> str:
    normalized, _ = tokenize_step(text)
    return normalized


def bindings_for(text: str) -> list[tuple[str, str]]:
    _, bindings = tokenize_step(text)
    return bindings


def tokenize_step(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (normalized step, [(token, original value), ...] in appearance order)."""
    raw = text or ""
    spans: list[tuple[int, int, str]] = []

    def _covered(start: int, end: int) -> bool:
        return any(start < e and end > s for s, e, _ in spans)

    for match in _QUOTED.finditer(raw):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        spans.append((match.start(), match.end(), value))
    for match in _EMAIL.finditer(raw):
        if not _covered(match.start(), match.end()):
            spans.append((match.start(), match.end(), match.group(0)))
    for match in _NUMBER.finditer(raw):
        if not _covered(match.start(), match.end()):
            spans.append((match.start(), match.end(), match.group(0)))
    spans.sort(key=lambda item: item[0])

    counters = {"q": 0, "email": 0, "n": 0}
    bindings: list[tuple[str, str]] = []
    pieces: list[str] = []
    cursor = 0
    for start, end, value in spans:
        pieces.append(raw[cursor:start].lower())
        if raw[start] in "\"'":
            kind = "q"
        elif "@" in value:
            kind = "email"
        else:
            kind = "n"
        counters[kind] += 1
        token = f"{{{kind}{counters[kind]}}}"
        bindings.append((token, value))
        pieces.append(token)
        cursor = end
    pieces.append(raw[cursor:].lower())
    normalized = re.sub(r"\s+", " ", "".join(pieces).strip())
    return normalized, bindings


def _replace_fields(step: dict, pairs: list[tuple[str, str]], reverse: bool) -> dict:
    step = copy.deepcopy(step)
    ordered = sorted(pairs, key=lambda item: len(item[0 if reverse else 1]), reverse=True)
    for field in ("value", "url", "description"):
        current = step.get(field)
        if not isinstance(current, str):
            continue
        for token, raw in ordered:
            if reverse:
                if token and token in current:
                    current = current.replace(token, raw)
            elif raw and raw in current:
                current = current.replace(raw, token)
        step[field] = current
    return step


def mask_text(text: str, bindings: list[tuple[str, str]]) -> str:
    """Mask secrets in free text that would otherwise land in a plan file."""
    masked = text or ""
    for token, raw in sorted(bindings, key=lambda item: len(item[1]), reverse=True):
        if raw:
            masked = masked.replace(raw, token)
    return masked


def mask_steps(steps: list, bindings: list[tuple[str, str]]) -> list:
    """Replace live test data with tokens before writing a plan to disk."""
    return [_replace_fields(step, bindings, reverse=False) for step in steps]


def reinject_steps(steps: list, bindings: list[tuple[str, str]]) -> list:
    """Put the current step's real values back into a stored plan."""
    return [_replace_fields(step, bindings, reverse=True) for step in steps]
