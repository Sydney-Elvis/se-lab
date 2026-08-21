from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ParsedResponse:
    structured: dict[str, Any] | None
    raw_text: str
    parse_mode: str


def _extract_fenced_json(text: str) -> str | None:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    return None


def _extract_first_json_object(text: str) -> str | None:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        start = match.start()
        try:
            parsed, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return text[start : start + end]
    return None


def _normalize_confidence(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        normalized = value.strip().lower()
        try:
            return max(0.0, min(1.0, float(normalized)))
        except ValueError:
            pass
        words = {
            "very high": 0.95,
            "high": 0.90,
            "medium": 0.60,
            "moderate": 0.60,
            "low": 0.30,
            "very low": 0.10,
        }
        return words.get(normalized)
    return None


def _normalize_structured(parsed: dict[str, Any]) -> dict[str, Any]:
    confidence = _normalize_confidence(parsed.get("confidence"))
    if confidence is not None:
        parsed = dict(parsed)
        parsed["confidence"] = confidence
    return parsed


def _post_think_text(text: str) -> str:
    """Return the text after the last </think> tag, for reasoning models like deepseek-r1.

    R1 wraps its internal chain-of-thought in <think>...</think> and puts the
    actual answer after the closing tag.  Scanning for JSON inside the thinking
    block picks up wrong fragments; this extracts only the post-thinking output.
    """
    marker = "</think>"
    idx = text.rfind(marker)
    if idx == -1:
        return ""
    return text[idx + len(marker):].strip()


def parse_structured_response(text: str) -> ParsedResponse:
    post_think = _post_think_text(text)
    candidates = [
        # Prefer the post-think region for reasoning models — avoids picking up
        # JSON fragments from the internal chain-of-thought.
        ("post_think_json", post_think),
        ("post_think_fenced", _extract_fenced_json(post_think) or "" if post_think else ""),
        ("post_think_embedded", _extract_first_json_object(post_think) or "" if post_think else ""),
        # Standard extraction on the full text as fallback.
        ("json", text.strip()),
        ("fenced_json", _extract_fenced_json(text) or ""),
        ("embedded_json", _extract_first_json_object(text) or ""),
    ]
    for mode, candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return ParsedResponse(structured=_normalize_structured(parsed), raw_text=text, parse_mode=mode)
    return ParsedResponse(structured=None, raw_text=text, parse_mode="unstructured")

