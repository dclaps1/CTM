"""QA scorecard used when managers review a call."""
from __future__ import annotations

from collections.abc import Mapping

MAX_POINTS = 5

CRITERIA: list[tuple[str, str]] = [
    ("greeting", "Greeting & introduction"),
    ("discovery", "Discovery / qualifying questions"),
    ("knowledge", "Product & service knowledge"),
    ("objections", "Objection handling"),
    ("close", "Close / clear next step"),
    ("tone", "Tone, empathy & professionalism"),
]


def score_form(form: Mapping[str, str]) -> tuple[dict[str, int], float | None]:
    """Read `c_<key>` fields (0-5, blank = N/A). Returns (criteria, percent) or percent None if nothing scored."""
    scored: dict[str, int] = {}
    for key, _label in CRITERIA:
        value = str(form.get(f"c_{key}", "")).strip()
        if not value:
            continue
        try:
            points = int(value)
        except ValueError:
            continue
        scored[key] = max(0, min(MAX_POINTS, points))
    if not scored:
        return scored, None
    return scored, round(100 * sum(scored.values()) / (MAX_POINTS * len(scored)), 1)


def to_ctm_score(percent: float) -> int:
    """Map a 0-100 QA percent onto CTM's 1-5 sale score."""
    return max(1, min(5, round(percent / 20)))
