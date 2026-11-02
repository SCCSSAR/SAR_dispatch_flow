"""Range-ring distances for the LPB Range Ring Analysis.

The public default is deliberately generic: the same static rings for every
subject category, labelled as not being statistics. A team with the right to use
a statistical dataset supplies it through the RANGE_RING_TABLE environment
variable, mounted from the `range-ring-table` Secret Manager secret, so the table
never enters this public repository. docs/architecture.md has the JSON format and
the steps to load it.

Stdlib only, so it stays importable under local pytest.
"""
import json
import logging
import os
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

ENV_VAR = "RANGE_RING_TABLE"
MAX_RING_RADIUS_MI = 50.0

_KM_PER_MILE = 1.609344
_TEXT_ONLY_SUFFIX = " (text only)"
_ROW_KEYS = {"label", "miles", "draw"}


class Ring(NamedTuple):
    label: str
    miles: float
    draw: bool = True


class RingTable(NamedTuple):
    source: str
    categories: dict


_GENERIC_LABEL = "generic planning ring"
_GENERIC_RINGS = [Ring(_GENERIC_LABEL, 0.25), Ring(_GENERIC_LABEL, 0.5), Ring(_GENERIC_LABEL, 1.0)]

DEFAULT_RING_TABLE = RingTable(
    source="generic rings, not statistics for this category",
    categories={
        category: _GENERIC_RINGS
        for category in (
            "Dementia", "Mentally Ill", "Despondent", "Substance Intoxication",
            "Child 1-3", "Child 4-6", "Child 7-9", "Child 10-12",
            "Hiker", "Climber",
        )
    },
)


def _single_line_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\n" not in value


def _parse_row(row) -> Ring:
    if not isinstance(row, dict) or set(row) - _ROW_KEYS:
        raise ValueError("row shape")
    label, miles, draw = row.get("label"), row.get("miles"), row.get("draw", True)
    if not _single_line_text(label):
        raise ValueError("row label")
    # bool is an int in Python, and `"miles": true` would draw a 1-mile ring.
    if isinstance(miles, bool) or not isinstance(miles, (int, float)) or miles <= 0:
        raise ValueError("row miles")
    if not isinstance(draw, bool):
        raise ValueError("row draw")
    if draw and miles > MAX_RING_RADIUS_MI:
        raise ValueError("row radius")
    return Ring(label, float(miles), draw)


def parse_ring_table(raw: str) -> Optional[RingTable]:
    """The table in `raw`, or None when `raw` means "no override".

    Raises ValueError on anything malformed. Its messages name the rule broken
    and never the value, but callers still must not log them verbatim: a
    JSONDecodeError is a ValueError too.
    """
    if not raw.strip() or raw.strip() == "{}":
        return None
    data = json.loads(raw)
    if not isinstance(data, dict) or not _single_line_text(data.get("source")):
        raise ValueError("table source")
    categories = data.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise ValueError("table categories")
    parsed = {}
    for name, rows in categories.items():
        if not _single_line_text(name) or not isinstance(rows, list) or not rows:
            raise ValueError("category")
        parsed[name] = [_parse_row(row) for row in rows]
    return RingTable(data["source"], parsed)


def load_ring_table() -> RingTable:
    """The override from the environment, else the default. Never raises: a bad
    secret must cost the override, not the dispatch."""
    try:
        table = parse_ring_table(os.environ.get(ENV_VAR, ""))
    except (ValueError, RecursionError) as e:
        # Class name only. The table may be licensed, and logs are not its place.
        logger.error("Range ring table | mode=invalid error=%s", type(e).__name__)
        return DEFAULT_RING_TABLE
    if table is None:
        logger.info("Range ring table | mode=default")
        return DEFAULT_RING_TABLE
    logger.info("Range ring table | mode=override categories=%d", len(table.categories))
    return table


def _format_miles(miles: float) -> str:
    """0.25 -> "0.25", 0.5 -> "0.5", 1.0 -> "1.0"."""
    text = f"{miles:.2f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _distance_line(ring: Ring) -> str:
    km = ring.miles * _KM_PER_MILE
    label = ring.label if ring.draw else ring.label + _TEXT_ONLY_SUFFIX
    return f"- {_format_miles(ring.miles)} miles ({km:.1f} km) — {label}"


def render_prompt_block(table: RingTable) -> str:
    """Item 2 of the LPB section of both Gemini prompts."""
    lines = [
        f"2. Range Ring Distances ({table.source}):",
        "   Output the heading line above exactly as written, then copy the distance "
        "lines listed below for the chosen category, exactly as written and miles "
        "first. Do NOT output the category name again. Do NOT change, round, add or "
        "omit a line, and do NOT recall distances from training data.",
    ]
    for category, rings in table.categories.items():
        lines.append(f"   {category}:")
        lines.extend(f"   {_distance_line(ring)}" for ring in rings)
    return "\n".join(lines)
