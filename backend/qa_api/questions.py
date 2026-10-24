"""The question payload stored in questions.question (JSONB).

The database no longer checks it (meeting decision), so every write path validates it
here first. A, B, C and D are option labels only; "answer" is the actual text of the
correct option, e.g. "New Delhi". A single letter is valid only when it really is an
option's text.
"""

from typing import Any

from .config import LIMITS
from .errors import FieldError

KEYS = ("A", "B", "C", "D")


def _text(value: Any, name: str, limit: int, errors: list[FieldError]) -> str:
    if not isinstance(value, str):
        errors.append(FieldError(name, "must be a string"))
    elif not value.strip():
        errors.append(FieldError(name, "must not be blank"))
    elif len(value.strip()) > limit:
        errors.append(FieldError(name, f"must be at most {limit} characters"))
    else:
        return value.strip()
    return ""


def parse_question_payload(value: Any, prefix: str = "question") -> tuple[dict[str, Any] | None, list[FieldError]]:
    """Checks {question, options: {A, B, C, D}, answer} exactly and returns a trimmed copy.

    The answer must equal one option's text after trimming. Option values must be distinct
    after trimming, because answers are stored as text only (a recommended rule, not one
    stated in the meeting).
    """
    errors: list[FieldError] = []
    if not isinstance(value, dict):
        return None, [FieldError(prefix, "must be an object")]
    for k in value:
        if k not in ("question", "options", "answer"):
            errors.append(FieldError(f"{prefix}.{k}", "unknown field"))
    question = _text(value.get("question"), f"{prefix}.question", LIMITS["question"], errors)
    options: dict[str, str] = {}
    raw = value.get("options")
    if not isinstance(raw, dict):
        errors.append(FieldError(f"{prefix}.options", "must be an object with keys A, B, C and D"))
    else:
        for k in raw:
            if k not in KEYS:
                errors.append(FieldError(f"{prefix}.options.{k}", "unknown field"))
        for k in KEYS:
            if k not in raw:
                errors.append(FieldError(f"{prefix}.options.{k}", "is required"))
            else:
                options[k] = _text(raw[k], f"{prefix}.options.{k}", LIMITS["option"], errors)
        values = [v for v in options.values() if v]
        if len(values) == 4 and len(set(values)) != 4:
            errors.append(FieldError(f"{prefix}.options", "option values must be distinct"))
    answer = _text(value.get("answer"), f"{prefix}.answer", LIMITS["option"], errors)
    if answer and isinstance(raw, dict) and answer not in options.values():
        errors.append(FieldError(f"{prefix}.answer", "must equal the text of one of the options"))
    if errors:
        return None, errors
    return {"question": question, "options": {k: options[k] for k in KEYS}, "answer": answer}, []
