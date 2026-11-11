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
_CONFIG_KEYS = ("county", "county_extent", "in_county_agencies", "d4h_env", "deck_folder")
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

    for k in ("county", "d4h_env", "deck_folder"):
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


_NAME_STOPWORDS = frozenset({"not", "recorded", "unknown", "none", "unk"})


def name_tokens(text: str) -> list[str]:
    """Name words (3+ letters, any script) for the text check. Placeholders like [not recorded] are not names."""
    text = re.sub(r"\[[^\]]*\]", " ", text or "")
    return [t for t in re.findall(r"[^\W\d_][^\W\d_'-]{2,}", text) if t.casefold() not in _NAME_STOPWORDS]


def _fold(text: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


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
        out.subject_names = name_tokens(person.split(";")[0])
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


# Turbo writes Person Assisted as a placeholder at dispatch, so it only suggests a find.
_OUTCOMES = {"Life Saved": "Found", "Person Assisted": "[confirm] Found", "Deceased": "Found deceased",
             "Not Located": "Not found"}


def lpb_rows_by_number(export_csv: Path) -> dict[str, dict[str, str]]:
    from isrid_submission import d4h_number, read_export
    return {d4h_number(r["Mission #"]): r for r in read_export(export_csv)}


def _found_status(lpb: dict) -> str:
    """Person Assisted is Turbo's dispatch placeholder; with find evidence in the export it is a find."""
    status = _OUTCOMES.get(lpb.get("Outcome", "").strip(), FILL_IN)
    evidence = any(lpb.get(k, "").strip() not in ("", "-") for k in ("Find Coordinates", "Search Time"))
    return "Found" if status == "[confirm] Found" and evidence else status


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
    notes: list[str] = field(default_factory=list)


def build_facts(incident: dict, involved: list[dict], lpb_row: dict | None,
                tag_titles: dict[int, str], cfg: dict) -> IncidentFacts:
    turbo = parse_turbo_description(incident.get("description"))
    coords = (incident.get("location") or {}).get("coordinates") or [0, 0]
    pls = None if not coords or (coords[0] == 0 and coords[1] == 0) else (coords[1], coords[0])
    name = incident.get("referenceDescription") or ""
    ma, confident = guess_mutual_aid(name, turbo.turbo_record, pls, cfg)
    lpb = lpb_row or {}
    distance = " ".join(x for x in (lpb.get("Distance From IPP", "").strip(),
                                    lpb.get("Direction From IPP", "").strip()) if x and x != "-")
    names = list(turbo.subject_names)
    for person in involved or []:
        names += name_tokens(person.get("name") or "")
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
        found_status=_found_status(lpb),
        distance=distance or FILL_IN,
        specialty=specialty_teams([t["id"] for t in incident.get("tags") or []], tag_titles),
        map_id=turbo.map_id,
        pls=pls,
        subject_names=list(dict.fromkeys(names)),
    )


# ── drafts.md ────────────────────────────────────────────────────────────────

class DraftError(RecapError):
    """drafts.md cannot be read back. The message names the incident header, never its text."""


FACT_KEYS = ("date", "event", "attendees", "hours", "volunteer_hours", "cancelled", "mutual_aid",
             "environment", "found", "distance", "specialty", "map", "pls")
DRAFT = "[draft]"
_SECTIONS = {"Summary": "summary", "Highlight": "highlight", "Lessons learned": "lessons",
             "The disruption": "disruption", "Sources and conflicts": None}
_SECTION_KEYS = {k.casefold(): v for k, v in _SECTIONS.items()}


@dataclass
class IncidentDraft:
    number: str
    title: str
    facts: dict[str, str]
    summary: str = ""
    highlight: str = ""
    lessons: list[str] = field(default_factory=list)
    disruption: list[str] = field(default_factory=list)


@dataclass
class MonthDraft:
    month: str
    incidents: list[IncidentDraft]
    excluded: list[str]


def _fact_values(f: IncidentFacts) -> dict[str, str]:
    ma = "yes" if f.mutual_aid else "no"
    return {
        "date": f.date_pt,
        "event": f.event_name,
        "attendees": str(f.attendees),
        "hours": FILL_IN if f.hours is None else f"{f.hours:.1f}",
        "volunteer_hours": volunteer_hours_label(f.attendees, f.hours),
        "cancelled": "yes" if f.cancelled else "no",
        "mutual_aid": ma if f.ma_confident else f"[confirm] {ma}",
        "environment": f.environment,
        "found": f.found_status,
        "distance": f.distance,
        "specialty": ", ".join(f.specialty),
        "map": f.map_id or "",
        "pls": f"{f.pls[0]:.5f}, {f.pls[1]:.5f}" if f.pls else "",
    }


def render_drafts(month: str, facts: list[IncidentFacts], excluded: list[tuple[str, str]]) -> str:
    out = [f"# {month} incident recap — drafts", "",
           "Edit anything below. Keep the `## ` lines and the ```facts fences.", ""]
    for f in sorted(facts, key=lambda x: x.sort_key):
        out += [f"## {f.number} · {f.date_pt} · {f.event_name}", "```facts"]
        out += [f"{k}: {v}" for k, v in _fact_values(f).items()]
        out += ["```", "### Summary", DRAFT, "### Highlight", DRAFT, "### Lessons learned", f"- {DRAFT}",
                "### The disruption"] + ([f"- {DRAFT}"] if f.cancelled else [])
        out += ["### Sources and conflicts", "(not rendered)"] + [f"- {n}" for n in f.notes] + [""]
    out += ["## Excluded"] + [f"- {n} · {why}" for n, why in excluded] + [""]
    return "\n".join(out)


_HEADER_RE = re.compile(r"^## (\S+) · (\S+) · (.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*)$")


def _bullets(lines: list[str]) -> list[str]:
    """List items as written: '-', '*' or '1.' bullets; any other non-blank line continues the item above."""
    items: list[str] = []
    for line in lines:
        m = _BULLET_RE.match(line)
        if m:
            items.append(m.group(1).strip())
        elif line.strip():
            if items:
                items[-1] = f"{items[-1]} {line.strip()}"
            else:
                items.append(line.strip())
    return items


def parse_drafts(text: str) -> MonthDraft:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    m = next((re.match(r"^# (\d{4}-\d{2}) incident recap", l) for l in lines if l.startswith("# ")), None)
    if not m:
        raise DraftError("no '# YYYY-MM incident recap' month header on the first heading line")
    blocks, current = [], None
    for line in lines:
        if line.startswith("## "):
            current = [line]
            blocks.append(current)
        elif current is not None:
            current.append(line)
    incidents, excluded = [], []
    for block in blocks:
        header = block[0][3:].strip()
        if header == "Excluded":
            excluded = [l[2:].strip() for l in block[1:] if l.startswith("- ")]
            continue
        hm = _HEADER_RE.match(block[0])
        if [l.strip() for l in block].count("```facts") > 1:
            raise DraftError(f"incident '{header}' runs into the next one: a '## ' header line is missing")
        if not hm or "```facts" not in [l.strip() for l in block]:
            raise DraftError(f"incident '{header}' has no ```facts block")
        start = [l.strip() for l in block].index("```facts")
        try:
            end = next(i for i in range(start + 1, len(block)) if block[i].strip() == "```")
        except StopIteration:
            raise DraftError(f"incident '{header}' has an unclosed ```facts block") from None
        facts = {}
        for l in block[start + 1:end]:
            if ": " in l or l.endswith(":"):
                k, _, v = l.partition(":")
                facts[k.strip()] = v.strip()
        sections: dict[str, list[str]] = {}
        seen: set[str] = set()
        name = None
        for l in block[end + 1:]:
            if l.startswith("### "):
                key = l[4:].strip().casefold()
                if key not in _SECTION_KEYS:
                    raise DraftError(f"incident '{header}': unknown section '### {l[4:].strip()}'")
                if key in seen:
                    raise DraftError(f"incident '{header}' repeats '### {l[4:].strip()}': a '## ' header line is missing")
                seen.add(key)
                name = _SECTION_KEYS[key]
                if name:
                    sections[name] = []
            elif name:
                sections[name].append(l)
        inc = IncidentDraft(number=hm.group(1), title=hm.group(3).strip(), facts=facts)
        inc.summary = "\n".join(sections.get("summary", [])).strip("\n").strip()
        inc.highlight = "\n".join(sections.get("highlight", [])).strip("\n").strip()
        inc.lessons = _bullets(sections.get("lessons", []))
        inc.disruption = _bullets(sections.get("disruption", []))
        incidents.append(inc)
    return MonthDraft(month=m.group(1), incidents=incidents, excluded=excluded)


def next_drafts_path(folder: Path) -> Path:
    if not (folder / "drafts.md").exists():
        return folder / "drafts.md"
    n = 2
    while (folder / f"drafts-{n}.md").exists():
        n += 1
    return folder / f"drafts-{n}.md"


# ── the slide text check ─────────────────────────────────────────────────────

def _text_patterns() -> list[tuple[str, re.Pattern]]:
    """The ISRID batch's outgoing-text patterns, reused rather than copied (one set to maintain)."""
    from isrid_submission import _PATTERNS, _STREET_TYPES
    if len(_PATTERNS) != 7:
        raise RecapError("isrid_submission._PATTERNS changed shape: update _text_patterns")
    kinds = ("phone", "phone", "phone", "phone", "email", "address", "event_number")
    # ISRID's address pattern ignores case, which flags slide prose ("2 teams walked the creek trail").
    # On slides every word up to the street type must be capitalised, as an address is written.
    address = re.compile(r"\b\d{1,6}[A-Za-z]?\s+(?:[A-Z0-9][\w'-]*\s+){1,4}(?:%s)\b" % "|".join(_STREET_TYPES))
    pats = [address if k == "address" else p for k, p in zip(kinds, _PATTERNS)]
    return list(zip(kinds, pats)) + [("dob", DOB_RE)]


DOB_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
SLIDE_FACT_KEYS = ("event", "volunteer_hours", "environment", "found", "distance", "specialty")


def slide_text_hits(text: str, subject_names: list[str]) -> list[str]:
    """Kinds of disallowed content in text bound for a slide. Never returns the matched value."""
    kinds = []
    for kind, pattern in _text_patterns():
        if pattern.search(text) and kind not in kinds:
            kinds.append(kind)
    folded = _fold(text)
    for name in subject_names:
        if len(name) >= 3 and re.search(rf"\b{re.escape(_fold(name))}\b", folded):
            kinds.append("subject_name")
            break
    return kinds


def check_month(draft: MonthDraft, names_by_number: dict[str, list[str]]) -> list[str]:
    hits = []
    for inc in draft.incidents:
        names = names_by_number.get(inc.number, [])
        fields = [("title", inc.title)] + [(k, inc.facts.get(k, "")) for k in SLIDE_FACT_KEYS]
        fields += [("summary", inc.summary), ("highlight", inc.highlight),
                   ("lessons", "\n".join(inc.lessons)), ("disruption", "\n".join(inc.disruption))]
        for where, text in fields:
            found = slide_text_hits(text, names)
            if found:
                hits.append(f"{inc.number} {where}: {', '.join(found)}")
    return hits


# ── collect and render ───────────────────────────────────────────────────────

_PAGE_SIZE = 250
# A map id becomes a file name and part of a CalTopo URL path, so only this shape is ever used.
MAP_ID_RE = re.compile(r"[A-Za-z0-9]{5,10}")


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _list_month(get_d4h, month: str) -> list[dict]:
    """Every incident D4H lists for the month window, paged, then re-checked against the PT window."""
    start, end = month_window(month)
    out, page = [], 0
    while True:
        body = get_d4h(f"incidents?after={_iso_utc(start)}&before={_iso_utc(end)}&size={_PAGE_SIZE}&page={page}")
        results = body.get("results") or []
        out += results
        page += 1
        if not results or len(out) >= int(body.get("totalSize") or 0):
            break
    return [i for i in out if start <= _utc(i["startsAt"]).astimezone(PT) < end]


def _extent_centre(cfg: dict) -> tuple[float, float]:
    (a_lat, a_lng), (b_lat, b_lng) = cfg["county_extent"]
    return (a_lat + b_lat) / 2, (a_lng + b_lng) / 2


def collect(month: str, root: Path, get_d4h, get_caltopo, lpb_export: Path | None, cfg: dict) -> Path:
    """Read the month from D4H and CalTopo into root/<month>/ and write the next drafts file."""
    from recap_map import miles_between
    folder = root / month
    folder.mkdir(parents=True, exist_ok=True)
    tag_titles = {t["id"]: t["title"] for t in get_d4h("tags").get("results") or []}
    lpb = lpb_rows_by_number(lpb_export) if lpb_export else {}
    facts, excluded, names = [], [], {}
    for inc in _list_month(get_d4h, month):
        ref = str(inc.get("reference", ""))
        reason = test_reason(inc.get("referenceDescription") or "")
        if reason:
            excluded.append((ref, reason))
            continue
        involved = get_d4h(f"incident-involved-persons?incident_id={inc['id']}").get("results") or []
        (folder / f"d4h-{ref}.json").write_text(json.dumps({"incident": inc, "involved": involved}, indent=2))
        f = build_facts(inc, involved, lpb.get(ref), tag_titles, cfg)
        if f.mutual_aid and f.ma_confident and f.pls and f.distance == FILL_IN:
            f.distance = f"≈ {round(miles_between(_extent_centre(cfg), f.pls))} mi from the county centre"
        if f.map_id:
            try:
                (folder / f"caltopo-{f.map_id}.json").write_text(json.dumps(get_caltopo(f.map_id), indent=2))
            except Exception as e:  # a missing map costs one picture, never the month
                f.notes.append(f"CalTopo map not read ({type(e).__name__}): the slide gets a PLS-only map")
        names[ref] = f.subject_names
        facts.append(f)
    (folder / "names.json").write_text(json.dumps(names, indent=2))
    path = next_drafts_path(folder)
    path.write_text(render_drafts(month, facts, excluded), encoding="utf-8")
    return path


def _pls(value: str) -> tuple[float, float] | None:
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\s*", value or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def _caltopo_base() -> str:
    import ast
    src = (Path(__file__).with_name("caltopo.py")).read_text()
    return next(n.value.value for n in ast.parse(src).body if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name) and t.id == "CALTOPO_BASE_URL")


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def refresh_volunteer_hours(draft: MonthDraft) -> None:
    """Fill volunteer hours for a row that was not closed out at collect time, once its hours are filled in.

    Only those rows: a closed-out row keeps collect's total, which used unrounded hours (8 x 12.65 h is
    101; recomputing from the drafts' 12.7 would say 102)."""
    for inc in draft.incidents:
        if "not closed out" not in inc.facts.get("volunteer_hours", ""):
            continue
        hours = _float(inc.facts.get("hours", ""))
        try:
            attendees = int(inc.facts.get("attendees", ""))
        except ValueError:
            continue
        if hours is not None:
            inc.facts["volunteer_hours"] = volunteer_hours_label(attendees, hours)


def open_items(draft: MonthDraft) -> list[str]:
    """Every fact still marked [fill in] or [confirm], by incident, for the hand-off."""
    return [f"{inc.number} {k}: {v}" for inc in draft.incidents for k, v in inc.facts.items()
            if FILL_IN in v or "[confirm]" in v]


def render(drafts: Path, fetch, cfg: dict, build=None, get_caltopo=None, notes: list[str] | None = None) -> Path:
    """Check the edited drafts, draw the maps and write the month's .pptx. Writes nothing on a refusal."""
    import math
    import recap_map as rm
    draft = parse_drafts(drafts.read_text(encoding="utf-8"))
    refresh_volunteer_hours(draft)
    folder = drafts.parent
    names_file = folder / "names.json"
    if not names_file.exists():
        raise RecapError(f"{names_file} is missing, so the subject-name check cannot run: run collect again")
    names = json.loads(names_file.read_text())
    missing = [inc.number for inc in draft.incidents if inc.number not in names]
    if missing:
        raise RecapError("no subject names on record for " + ", ".join(missing)
                         + ": add each to names.json, as [] only if the incident has no named subject")
    hits = check_month(draft, names)
    hits += [f"{inc.number} map: not a CalTopo map id (5-10 letters or digits)" for inc in draft.incidents
             if inc.facts.get("map", "").strip() and not MAP_ID_RE.fullmatch(inc.facts["map"].strip())]
    if hits:
        raise RecapError("slide text must not carry these; edit the drafts and render again:\n  " + "\n  ".join(hits))
    out = folder / f"{draft.month} Incident Recap.pptx"
    if out.exists():
        raise RecapError(f"{out} already exists: move or rename it to generate a new one")
    if build is None:
        import functools
        from recap_pptx import build_deck
        build = functools.partial(build_deck, notes=notes)
    extent = [tuple(p) for p in cfg["county_extent"]]
    base = _caltopo_base()
    maps, links, pins, n = {}, {}, [], 0
    for inc in draft.incidents:
        ma = "yes" in inc.facts.get("mutual_aid", "").lower()
        cancelled = "yes" in inc.facts.get("cancelled", "").lower()
        pls = _pls(inc.facts.get("pls", ""))
        map_id = inc.facts.get("map", "").strip()
        if not ma:
            n += 1
            if pls:
                pins.append((n, pls[0], pls[1], cancelled))
        markers = []
        if map_id:
            links[inc.number] = f"{base}/m/{map_id}"
            src = folder / f"caltopo-{map_id}.json"
            if not src.exists() and get_caltopo:  # e.g. the drafts now name the copy the field used
                try:
                    src.write_text(json.dumps(get_caltopo(map_id), indent=2))
                except Exception:
                    pass  # the slide falls back to a PLS-only map
            if src.exists():
                markers = rm.select_markers(json.loads(src.read_text())["result"]["state"]["features"])
        if not markers and pls:
            markers = [rm.Marker("lkp", "PLS", pls[0], pls[1])]
        kw = {}
        if ma:
            kw = {"extent": extent, "min_zoom": 5}
        elif cancelled and pls:
            dlat = 0.75 / 69.0
            dlng = dlat / max(0.2, math.cos(math.radians(pls[0])))
            kw = {"extent": [(pls[0] - dlat, pls[1] - dlng), (pls[0] + dlat, pls[1] + dlng)]}
        try:
            maps[inc.number] = rm.render_map(markers, 1200, 900, fetch, folder / f"map-{inc.number}.png", **kw)
        except ValueError:
            maps[inc.number] = None
    overview = rm.render_overview(pins, extent, fetch, folder / "overview.png")
    return build(draft, overview, maps, links, out)


# ── network getters and the command line ────────────────────────────────────

class HTTPStatusError(Exception):
    def __init__(self, code: int):
        super().__init__(f"HTTP {code}")
        self.code = code


def _module_constants(name: str) -> dict:
    import ast
    src = (Path(__file__).with_name(name)).read_text()
    return {t.id: n.value.value for n in ast.parse(src).body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}


def _urlopen_json(url: str, headers: dict | None = None) -> dict:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise HTTPStatusError(e.code) from None


def d4h_getter(token: str, opener=None):
    c = _module_constants("d4h.py")
    opener = opener or _urlopen_json

    def get(path: str) -> dict:
        return opener(f"{c['BASE_URL']}/team/{c['TEAM_ID']}/{path}", {"Authorization": "Bearer " + token})

    return get


def caltopo_getter(credential_id: str, secret_b64: str, opener=None, sleep=None, now_ms=None):
    """Signed read of one map's features (read-only GET; sharing is never touched)."""
    import base64
    import hashlib
    import hmac
    import time
    import urllib.parse
    base = _caltopo_base()
    opener = opener or (lambda url: _urlopen_json(url))
    sleep = sleep or time.sleep
    now_ms = now_ms or (lambda: int(time.time() * 1000))
    key = base64.b64decode(secret_b64)

    def get(map_id: str) -> dict:
        if not MAP_ID_RE.fullmatch(map_id or ""):
            raise ValueError("not a CalTopo map id")
        path = f"/api/v1/map/{map_id}/since/0"
        for attempt in range(3):  # CalTopo returns transient 5xx (#706): retry those only
            exp = now_ms() + 120_000
            sig = base64.b64encode(hmac.new(key, f"GET {path}\n{exp}\n".encode(), hashlib.sha256).digest()).decode()
            q = urllib.parse.urlencode({"id": credential_id, "expires": exp, "signature": sig})
            try:
                return opener(f"{base}{path}?{q}")
            except HTTPStatusError as e:
                if e.code < 500 or attempt == 2:
                    raise
                sleep(1.5 * (attempt + 1))
        raise AssertionError("unreachable")

    return get


def handoff_message(deck: Path, cfg: dict) -> str:
    """The manual steps after a render: the script never touches Drive or the year's deck."""
    return "\n".join([
        "Next step (by hand):",
        f"  1. Move {deck.name} (in {deck.parent}) into {cfg['deck_folder']}.",
        "  2. Open the year's deck in Google Slides, choose File > Import slides, pick this file,",
        "     and import all of its slides. They go at the end; nothing already in the deck changes.",
        "  3. Add photos and corrections in Google Slides.",
    ])


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import sys
    ap = argparse.ArgumentParser(prog="monthly_recap", description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(DEFAULT_ROOT / "config.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="read D4H and CalTopo, write the next drafts file")
    c.add_argument("--month", required=True)
    c.add_argument("--lpb-export", type=Path)
    c.add_argument("--root", default=str(DEFAULT_ROOT))
    r = sub.add_parser("render", help="check the edited drafts and write the month's .pptx")
    r.add_argument("--drafts", required=True, type=Path)
    for p in (c, r):
        p.add_argument("--config", default=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        cfg = load_config(Path(args.config))
        if args.cmd == "collect":
            env = {k: os.environ.get(k) for k in ("D4H_ACCESS_TOKEN", "CALTOPO_CREDENTIAL_ID", "CALTOPO_CREDENTIAL_SECRET")}
            missing = [k for k, v in env.items() if not v]
            if missing:
                raise RecapError("missing environment: " + ", ".join(missing) + " (see the skill's Setup step)")
            path = collect(args.month, Path(args.root), d4h_getter(env["D4H_ACCESS_TOKEN"]),
                           caltopo_getter(env["CALTOPO_CREDENTIAL_ID"], env["CALTOPO_CREDENTIAL_SECRET"]),
                           args.lpb_export, cfg)
            print(f"drafts: {path}")
        else:
            from recap_map import cached_osm_fetcher
            fetch = cached_osm_fetcher(args.drafts.parent.parent / "tiles")
            cid, csec = os.environ.get("CALTOPO_CREDENTIAL_ID"), os.environ.get("CALTOPO_CREDENTIAL_SECRET")
            getter = caltopo_getter(cid, csec) if cid and csec else None
            notes: list[str] = []
            deck = render(args.drafts, fetch, cfg, get_caltopo=getter, notes=notes)
            print(f"deck: {deck}")
            if notes:
                print("continued onto a (cont.) slide, to keep text at 75% or larger:\n  " + "\n  ".join(notes))
            todo = open_items(parse_drafts(args.drafts.read_text(encoding="utf-8")))
            if todo:
                print("still to check on the slides:\n  " + "\n  ".join(todo))
            print(handoff_message(Path(deck), cfg))
    except RecapError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
