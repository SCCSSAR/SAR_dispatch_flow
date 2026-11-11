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

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
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


# ── Scoring against ISRID Data Standards 2016 (App. B Minimum, App. C Silver) ──

_CONDITION_RE = re.compile(r"\b(well|injured|injury|DOA|deceased|dead|no trace)\b", re.IGNORECASE)
OUTCOME_COUNTS = ("# well", "# injured", "# DOA", "# no trace")
TRACK_OFFSET = "Track offset (ISRID derives)"


def _blank(value: str) -> bool:
    return value.strip() in ("", "-")


def states_condition(comment: str) -> bool:
    """True when the text says how the subject was found (well, injured, DOA, no trace).

    D4H's Outcome value `Person Assisted` does not (Koester p. 53), and ISRID receives only
    the file, so the four outcome counts are met only when the comment states the condition.
    """
    return bool(_CONDITION_RE.search(comment))


def score_minimum(row: dict[str, str], comment: str) -> tuple[int, list[str]]:
    no_trace = bool(re.search(r"\bno trace\b", comment, re.IGNORECASE))
    checks = [
        ("Incident Type", not _blank(row["Incident Type"])),
        ("SAR notified", not _blank(row["Incident Date"])),
        ("Found date/time", no_trace or not _blank(row["Search Time"])),
        ("Last seen date/time", not _blank(row["Lost Time"])),
        ("Subject Category", not _blank(row["Subject Category"])),
        ("IPP coordinates", not (_blank(row["IPP Lat"]) or _blank(row["IPP Lng"]))),
        ("Find coordinates", no_trace or not (_blank(row["Find Lat"]) or _blank(row["Find Lng"]))),
        ("Scenario", not _blank(row["Scenario"])),
    ]
    condition = states_condition(comment)
    checks += [(name, condition) for name in OUTCOME_COUNTS]
    checks.append(("Comment", bool(comment.strip())))
    missing = [name for name, ok in checks if not ok]
    return 13 - len(missing), missing


def score_silver(row: dict[str, str], comment: str, confirmed: set[str]) -> tuple[int, list[str]]:
    """Out of 25: the 13 Minimum fields plus the 12 App. C additions."""
    n, missing = score_minimum(row, comment)
    sex = row["Sex"].replace(",", "").strip()
    sex_ok = bool(sex) and (set(sex) != {"o"} or "Sex" in confirmed)
    checks = [(col, not _blank(row[col])) for col in
              ("Environment", "Population Density", "Terrain", "Age", "Fitness", "Experience")]
    checks += [
        ("Mobility", not _blank(row["Mobility Time"])),
        ("Sex", sex_ok),
        ("Subject Status", states_condition(comment)),
        ("Destination", bool(re.search(r"\bdestination\b", comment, re.IGNORECASE))),
        ("Direction of Travel", bool(re.search(r"\bdirection of travel\b", comment, re.IGNORECASE))),
        (TRACK_OFFSET, False),
    ]
    missing = missing + [name for name, ok in checks if not ok]
    return 25 - len(missing), missing


# ── prepare: select unsent rows and open a batch ─────────────────────────────

DEFAULT_ROOT = Path("research/isrid-submissions")

# Santa Clara County places, casefolded: the 15 incorporated cities plus unincorporated and
# district names D4H's City column carries. A heuristic only: Bill confirms every flag.
SCC_PLACES = frozenset(p.casefold() for p in (
    "Campbell", "Cupertino", "Gilroy", "Los Altos", "Los Altos Hills", "Los Gatos", "Milpitas",
    "Monte Sereno", "Morgan Hill", "Mountain View", "Palo Alto", "San Jose", "Santa Clara",
    "Saratoga", "Sunnyvale",
    "Mount Hamilton", "San Martin", "Alviso", "Stanford", "Coyote", "Lexington Hills",
    "Redwood Estates", "Holy City", "New Almaden",
))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def quarter_label(now: datetime) -> str:
    return f"{now.year}-Q{(now.month - 1) // 3 + 1}"


def sent_numbers(root: Path) -> set[str]:
    """D4H numbers recorded in any earlier batch's SENT.md. Only SENT.md counts as sent."""
    sent = set()
    for f in sorted(root.glob("*/*/SENT.md")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("D4H numbers:"):
                sent |= {n.strip() for n in line.split(":", 1)[1].split(",") if n.strip()}
    return sent


def flag_row(row: dict[str, str]) -> str:
    city, county = row["City"].strip(), row["County"].strip()
    if not city or not county:
        return "check"
    return "in-county" if city.casefold() in SCC_PLACES else "mutual-aid?"


def prepare(export: Path, root: Path, now: datetime) -> Path:
    rows = read_export(export)              # refuses a changed header before anything is written
    quarter, run = quarter_label(now), now.strftime("%Y%m%d-%H%M")
    batch = root / quarter / run
    if batch.exists():
        raise SubmissionError(f"batch folder already exists: {batch}")
    already = sent_numbers(root)
    batch.mkdir(parents=True)
    raw = batch / "raw-d4h-export.csv"
    raw.write_bytes(export.read_bytes())
    worklist = {"rules_version": RULES_VERSION, "raw_sha256": _sha256(raw),
                "quarter": quarter, "run": run, "rows": [], "already_sent": []}
    for i, row in enumerate(rows):
        line = i + 2                        # header is row 1
        try:
            number = d4h_number(row["Mission #"])
        except SubmissionError:
            worklist["rows"].append({"d4h": None, "line": line, "flag": "bad-mission-number",
                                     "incident_date": row["Incident Date"],
                                     "minimum_as_exported": score_minimum(row, row["Comment"])[0]})
            continue
        if number in already:
            worklist["already_sent"].append(number)
            continue
        worklist["rows"].append({"d4h": number, "line": line, "flag": flag_row(row),
                                 "incident_date": row["Incident Date"],
                                 "minimum_as_exported": score_minimum(row, row["Comment"])[0]})
    (batch / "worklist.json").write_text(json.dumps(worklist, indent=2) + "\n", encoding="utf-8")
    return batch


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="isrid_submission", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="select unsent rows from a D4H export into a new batch")
    p.add_argument("export", type=Path)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "prepare":
            batch = prepare(args.export, args.root, datetime.now())
            wl = json.loads((batch / "worklist.json").read_text(encoding="utf-8"))
            print(f"batch: {batch}")
            print(f"rows to decide: {len(wl['rows'])}; already sent: {len(wl['already_sent'])}")
            for flag, n in sorted(Counter(r["flag"] for r in wl["rows"]).items()):
                print(f"  {flag}: {n}")
    except SubmissionError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
