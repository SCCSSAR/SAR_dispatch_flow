"""Quarterly ISRID batch: turn D4H's Lost Behavior export into a de-identified, archived file.

Design: docs/superpowers/specs/2026-11-10-isrid-quarterly-submission-design.md
Skill:  .claude/skills/isrid-quarterly/SKILL.md

Standard library only, no network. Output goes under --root (default research/isrid-submissions,
which is gitignored). Messages name columns and D4H numbers, never cell values: the export holds
subject data.

    python3 backend/isrid_submission.py prepare <export.csv> [--root DIR]
    python3 backend/isrid_submission.py finalize <batch-dir>
    python3 backend/isrid_submission.py mark-sent <batch-dir> --to <address> [--on YYYY-MM-DD]
"""

import csv
import re
from pathlib import Path

RULES_VERSION = "1"

# D4H's Lost Behavior export header, in D4H's order (34 columns, measured 2026-11-09).
# An export whose header differs is refused, so a column D4H adds later cannot pass unreviewed.
EXPORT_HEADER = (
    "Mission #", "Incident Date", "Incident Type", "County", "City", "Subject Category",
    "# Subjects", "Involvement", "Age", "Sex", "Search Time", "Lost Time", "Distance From IPP",
    "Direction From IPP", "Find Feature", "Outcome", "Elevation Change", "Mobility Time",
    "Population Density", "IPP Coordinates", "IPP Lat", "IPP Lng", "Find Coordinates", "Find Lat",
    "Find Lng", "Eco Region", "Environment", "Weather", "Equipment", "Experience", "Fitness",
    "Scenario", "Terrain", "Comment",
)
REWRITE_COLUMNS = ("Mission #", "Comment")
REVIEW_COLUMNS = ("City", "Weather")
COORDINATE_COLUMNS = ("IPP Coordinates", "IPP Lat", "IPP Lng",
                      "Find Coordinates", "Find Lat", "Find Lng")
IN_COUNTY = "Santa Clara"


class SubmissionError(Exception):
    """A refusal. The message names columns and D4H numbers, never values."""


def read_export(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = tuple(next(reader, ()))
        if header != EXPORT_HEADER:
            extra = [h for h in header if h not in EXPORT_HEADER]
            missing = [h for h in EXPORT_HEADER if h not in header]
            moved = [h for i, h in enumerate(header)
                     if h in EXPORT_HEADER and i < len(EXPORT_HEADER) and EXPORT_HEADER[i] != h]
            raise SubmissionError(
                "export header differs from D4H's known 34 columns: "
                f"unknown {extra}, missing {missing}, out of order {moved}")
        return [dict(zip(EXPORT_HEADER, rec)) for rec in reader]


_D4H_NUMBER_RE = re.compile(r"(\d{5})(?!\d)")


def d4h_number(mission: str) -> str:
    m = _D4H_NUMBER_RE.match(mission.strip())
    if not m:
        raise SubmissionError("Mission # does not start with a 5-digit D4H number")
    return m.group(1)


_STREET_TYPES = ("St", "Street", "Ave", "Avenue", "Rd", "Road", "Dr", "Drive", "Way", "Ct", "Court",
                 "Blvd", "Ln", "Lane", "Pl", "Cir", "Pkwy", "Hwy")
_PATTERNS = (
    re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),                              # phone
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),                                        # email
    re.compile(r"\b\d{1,6}\s+(?:[A-Z][\w'.-]*\s+){1,4}(?:%s)\b" % "|".join(_STREET_TYPES)),  # address
    re.compile(r"\b\d{2}-\d{3}-\d{3,4}\b"),                                        # agency Event #
)


def _mission_words(mission_raw: str) -> list[re.Pattern]:
    rest = _D4H_NUMBER_RE.sub("", mission_raw.strip(), count=1)
    words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", rest)}
    return [re.compile(r"\b%s\b" % re.escape(w), re.IGNORECASE) for w in sorted(words)]


def forbidden_hits(row: dict[str, str], mission_raw: str) -> list[str]:
    mission_words = _mission_words(mission_raw)
    hits = []
    for col in EXPORT_HEADER:
        if col in COORDINATE_COLUMNS or col == "Incident Date":
            continue
        value = row.get(col, "")
        found = any(p.search(value) for p in _PATTERNS)
        if not found and col not in ("City", "County"):
            found = any(p.search(value) for p in mission_words)
        if found:
            hits.append(col)
    return hits


def deidentify_row(row: dict[str, str], comment: str) -> dict[str, str]:
    out = dict(row)
    out["Mission #"] = d4h_number(row["Mission #"])
    out["Comment"] = comment
    out["County"] = IN_COUNTY
    return out
