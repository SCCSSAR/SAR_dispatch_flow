"""Monthly General Team incident recap: collect, draft, check and render one month's slides.

Run by the `monthly-recap` skill (.claude/skills/monthly-recap/SKILL.md). Everything this module
writes goes under research/monthly-recap/<YYYY-MM>/, which is gitignored: it holds incident detail.
Reads production D4H and CalTopo only, and never writes to either.

Team-specific values (county, its extent, in-county agency names, the deploy.env environment) come
from research/monthly-recap/config.json, copied from the shipped template, so the code names no team.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DEFAULT_ROOT = Path("research/monthly-recap")
CONFIG_TEMPLATE = ".claude/skills/monthly-recap/config.template.json"
_CONFIG_KEYS = ("county", "county_extent", "in_county_agencies", "d4h_env")
FILL_IN = "[fill in]"


class RecapError(Exception):
    """A refusal the dispatcher has to act on. Messages never carry incident values."""


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    """Read and validate the team's config. Keys starting with `_` are notes and are dropped."""
    if not path.exists():
        raise RecapError(f"no team config at {path}: copy {CONFIG_TEMPLATE} there and fill it in")
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = {k: v for k, v in raw.items() if not k.startswith("_")}
    problems = [f"{k}: missing" for k in _CONFIG_KEYS if k not in cfg]
    problems += [f"{k}: unknown key" for k in cfg if k not in _CONFIG_KEYS]

    def text_ok(v):
        return isinstance(v, str) and v.strip() and "<" not in v

    for k in ("county", "d4h_env"):
        if k in cfg and not text_ok(cfg[k]):
            problems.append(f"{k}: must be filled in")
    if "in_county_agencies" in cfg and not (isinstance(cfg["in_county_agencies"], list)
                                            and cfg["in_county_agencies"]
                                            and all(text_ok(a) for a in cfg["in_county_agencies"])):
        problems.append("in_county_agencies: a non-empty list of agency names as event names start")
    ext = cfg.get("county_extent")
    if "county_extent" in cfg and not (isinstance(ext, list) and len(ext) == 2
                                       and all(isinstance(p, list) and len(p) == 2
                                               and all(isinstance(x, (int, float)) for x in p) for p in ext)
                                       and ext[0][0] != ext[1][0] and ext[0][1] != ext[1][1]):
        problems.append("county_extent: two [lat, lng] corners")
    if problems:
        raise RecapError(f"team config {path} is not usable:\n  " + "\n  ".join(problems))
    return cfg


# ── dates and hours ──────────────────────────────────────────────────────────

def month_window(month: str) -> tuple[datetime, datetime]:
    m = re.fullmatch(r"(\d{4})-(\d{2})", month)
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise RecapError(f"month must be YYYY-MM, got {month!r}")
    y, mo = int(m.group(1)), int(m.group(2))
    start = datetime(y, mo, 1, tzinfo=PT)
    end = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=PT)
    return start, end


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def incident_hours(starts_at: str | None, ends_at: str | None) -> float | None:
    """Hours from start to end, or None when D4H has not been closed out (end missing or == start)."""
    if not starts_at or not ends_at or starts_at == ends_at:
        return None
    hours = (_utc(ends_at) - _utc(starts_at)).total_seconds() / 3600
    return hours if hours > 0 else None


def volunteer_hours_label(attendees: int, hours: float | None) -> str:
    if hours is None:
        return f"{attendees} × not closed out"
    return f"{attendees} × {hours:.1f} h = {round(attendees * hours)}"


# ── record classification ────────────────────────────────────────────────────

_SUFFIX_RE = re.compile(r"\[[0-9a-f]{4}\]")
_TEST_RE = re.compile(r"\btest\b", re.IGNORECASE)
_CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)


def test_reason(reference_description: str) -> str | None:
    if _SUFFIX_RE.search(reference_description or ""):
        return "[abcd] personal-dev suffix"
    if _TEST_RE.search(reference_description or ""):
        return "named as a test"
    return None


test_reason.__test__ = False  # not a pytest test, despite the name


def is_cancelled(reference_description: str) -> bool:
    return bool(_CANCELLED_RE.search(reference_description or ""))


def _inside(point: tuple[float, float], extent: list[list[float]]) -> bool:
    (a_lat, a_lng), (b_lat, b_lng) = extent
    lat, lng = point
    return min(a_lat, b_lat) <= lat <= max(a_lat, b_lat) and min(a_lng, b_lng) <= lng <= max(a_lng, b_lng)


def guess_mutual_aid(reference_description: str, turbo_record: bool,
                     pls: tuple[float, float] | None, cfg: dict) -> tuple[bool, bool]:
    """(is mutual aid, confident). An unconfident guess is shown to the dispatcher as [confirm]."""
    name = (reference_description or "").strip()
    if "M/A" in name.upper():
        return True, True
    upper = name.upper()
    if any(upper == a.upper() or upper.startswith(a.upper() + " ") for a in cfg["in_county_agencies"]):
        return False, True
    if pls is not None and not _inside(pls, cfg["county_extent"]):
        return True, True
    return True, False


# ── Turbo's description lines ────────────────────────────────────────────────

@dataclass
class TurboLines:
    map_id: str | None = None
    environment: str | None = None
    subject_names: list[str] = field(default_factory=list)
    agency: str | None = None

    @property
    def turbo_record(self) -> bool:
        return self.map_id is not None or self.agency is not None


def _line_value(lines: list[str], label: str) -> str | None:
    for line in lines:
        m = re.match(rf"\s*{re.escape(label)}:[^\S\n]*(.*)$", line)
        if m:
            return m.group(1).strip()
    return None


def parse_turbo_description(html: str | None) -> TurboLines:
    if not html:
        return TurboLines()
    text = re.sub(r"</?p>", "", html)
    lines = re.split(r"<br\s*/?>|\n", text)
    out = TurboLines()
    map_line = _line_value(lines, "CalTopo Map ID")
    if map_line:
        m = re.search(r"/m/([A-Za-z0-9]+)", map_line)
        out.map_id = m.group(1) if m else None
    env = _line_value(lines, "Environment")
    if env:
        out.environment = env.split("·")[0].strip()
    person = _line_value(lines, "Missing Person")
    if person:
        out.subject_names = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", person.split(";")[0])
    out.agency = _line_value(lines, "Agency") or None
    return out


# ── tags, environment, LPB ───────────────────────────────────────────────────

NON_SPECIALTY_TAG_TITLES = frozenset({
    "Search Management", "Transport", "Training", "Training - Mandatory", "Training - Optional",
    "Academy", "SO", "Compliance", "501c3", "General Team Hours",
})
TAG_DISPLAY = {"Canine": "K9"}


def specialty_teams(tag_ids: list[int], tag_titles: dict[int, str]) -> list[str]:
    out = []
    for tid in tag_ids:
        title = tag_titles.get(tid)
        if title is None:
            out.append(f"tag {tid}")
        elif title not in NON_SPECIALTY_TAG_TITLES:
            out.append(TAG_DISPLAY.get(title, title))
    return sorted(out)


def environment_label(population_density: str | None, turbo_environment: str | None) -> str:
    pd = (population_density or "").strip()
    if pd in ("Urban", "Suburban"):
        return "Urban"
    if pd in ("Rural", "Wilderness"):
        return "Wilderness"
    env = turbo_environment or ""
    if "Urban" in env:
        return "Urban"
    if "Rural" in env or "Wilderness" in env:
        return "Wilderness"
    return FILL_IN


_OUTCOMES = {"Life Saved": "Found", "Person Assisted": "Found", "Deceased": "Found deceased",
             "Not Located": "Not found"}


def lpb_rows_by_number(export_csv: Path) -> dict[str, dict[str, str]]:
    from isrid_submission import d4h_number, read_export
    return {d4h_number(r["Mission #"]): r for r in read_export(export_csv)}


# ── facts ────────────────────────────────────────────────────────────────────

@dataclass
class IncidentFacts:
    number: str
    date_pt: str
    sort_key: datetime
    event_name: str
    attendees: int
    hours: float | None
    cancelled: bool
    mutual_aid: bool
    ma_confident: bool
    environment: str
    found_status: str
    distance: str
    specialty: list[str]
    map_id: str | None
    pls: tuple[float, float] | None
    subject_names: list[str]


def build_facts(incident: dict, involved: list[dict], lpb_row: dict | None,
                tag_titles: dict[int, str], cfg: dict) -> IncidentFacts:
    turbo = parse_turbo_description(incident.get("description"))
    coords = (incident.get("location") or {}).get("coordinates") or [0, 0]
    pls = None if not coords or (coords[0] == 0 and coords[1] == 0) else (coords[1], coords[0])
    name = incident.get("referenceDescription") or ""
    ma, confident = guess_mutual_aid(name, turbo.turbo_record, pls, cfg)
    lpb = lpb_row or {}
    distance = " ".join(x for x in (lpb.get("Distance From IPP", "").strip(),
                                    lpb.get("Direction From IPP", "").strip()) if x)
    names = list(turbo.subject_names)
    for person in involved or []:
        names += re.findall(r"[A-Za-z][A-Za-z'-]{2,}", person.get("name") or "")
    start = _utc(incident["startsAt"]).astimezone(PT)
    return IncidentFacts(
        number=str(incident.get("reference", "")),
        date_pt=start.strftime("%m-%d"),
        sort_key=start,
        event_name=name,
        attendees=int(incident.get("countAttendance") or 0),
        hours=incident_hours(incident.get("startsAt"), incident.get("endsAt")),
        cancelled=is_cancelled(name),
        mutual_aid=ma,
        ma_confident=confident,
        environment=environment_label(lpb.get("Population Density"), turbo.environment),
        found_status=_OUTCOMES.get(lpb.get("Outcome", "").strip(), FILL_IN),
        distance=distance or FILL_IN,
        specialty=specialty_teams([t["id"] for t in incident.get("tags") or []], tag_titles),
        map_id=turbo.map_id,
        pls=pls,
        subject_names=list(dict.fromkeys(names)),
    )
