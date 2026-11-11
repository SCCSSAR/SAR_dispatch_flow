# Monthly Incident Recap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code skill plus a local script that draft one month's General Team incident slides into an editable `drafts.md`, then render the edited drafts and drawn maps into a new `.pptx`.

**Architecture:** Three stdlib-plus-PIL modules in `backend/` (facts and drafts, maps, text check) do everything deterministic and are tested in the build's Step 0 pytest. A fourth module writes the `.pptx` with `python-pptx`, which lives only in a local venv, so its tests skip where it is absent. The skill runs collect → Claude drafts narrative from AARs and Slack → Bill edits → render.

**Tech Stack:** Python 3 stdlib (`urllib`, `json`, `zoneinfo`, `math`), Pillow (already a backend dependency), `python-pptx` in a venv under gitignored `research/`, OpenStreetMap standard tiles.

**Spec:** `docs/superpowers/specs/2026-11-10-monthly-incident-recap-design.md`

## Global Constraints

- The repo is PUBLIC. Every test fixture is synthetic: D4H numbers start with 9, places, streets and names are invented. No real incident value appears in any committed file.
- All incident output goes under `research/monthly-recap/<YYYY-MM>/` (gitignored). Nothing in `research/` is committed.
- Never overwrite: `drafts.md` and `<YYYY-MM> Incident Recap.pptx` are created only when absent. A second collect writes `drafts-2.md`, `drafts-3.md`, …; render refuses when the `.pptx` exists.
- Reads only. D4H and CalTopo credentials from sccssar-dev Secret Manager with the gcloud configuration from `deploy.env` (`ENV_NAME=sccssar-dev`). Never personal-dev.
- Never call the Google Drive connector; Bill moves the file into Drive himself.
- Never change CalTopo sharing. CalTopo is read with signed `GET /api/v1/map/<id>/since/0` only.
- OSM tiles: `https://tile.openstreetmap.org/{z}/{x}/{y}.png`, User-Agent `SCCSSAR-monthly-recap/1.0 (local, low volume)`, cached under `research/monthly-recap/tiles/z/x/y.png`, never refetched when cached, every map image carries `© OpenStreetMap contributors`.
- Style: orange `#F1592A`, headings font `Oswald`, body font `Public Sans`, 16:9 (13.333 in × 7.5 in), text boxes, tables and pictures only.
- Slide text never contains: subject name, DOB, phone, email, agency Event #, house-numbered address, responder names. Map pictures MAY contain CalTopo marker titles verbatim (Bill: "map labels only"); marker descriptions are never drawn.
- Range rings, tracks, assignments, clues and all CalTopo Shapes are never drawn.
- Every new pin is mutation-checked: reintroduce the defect, confirm the test goes red, restore (CLAUDE.md "A pin must READ production and SURVIVE MUTATION"). Record the mutation count in the PR body.
- Work only in the worktree `$WT` = `/private/tmp/claude-501/-Users-billburns-src-SAR-dispatch-flow/069a58d3-8cb5-4a51-b98c-e718a8c75a62/scratchpad/wt-recap` (outside the repo, created 2026-11-10; recreate with `git worktree add` if gone); never switch branches in the shared main checkout.
- Session Rules #2 and #14: `git branch --show-current` as its own call before staging; `checkout && add && commit` in one call; never `git add -A`; never self-merge; never run `build-*.sh`.

## Review Focus

1. **A month with an incident not yet closed out** (`endsAt == startsAt`): expect "not closed out" in the hours cell and a `[fill in]` in drafts, never `0 h` or a crash. Pinned in Task 2.
2. **A `drafts.md` Bill edited by hand**: blank lines, a deleted lesson, an extra bullet, Windows line endings, a missing optional section. Expect the parse to keep his text exactly and fail loudly only on a missing incident header or facts block. Pinned in Task 3.
3. **A CalTopo map with no markers we draw, or no map at all** (hand-entered M/A, map in another team): expect a placeholder box, not an empty white map or a crash. Pinned in Task 4.
4. **A subject name that is also a common word or street** (e.g. a surname equal to the event's street): expect the check to flag it anyway; Bill decides. Name tokens shorter than 3 letters are ignored so initials do not trip it. Pinned in Task 5.
5. **A second render after Bill already moved the file to Drive**: the local `.pptx` still exists, so render refuses and names the file; Bill deletes or renames to regenerate. Pinned in Task 6.

---

### Task 1: Spike the two unverified D4H reads (no repo code)

**Files:**
- Create: `experiments/d4h/28_recap_incident_list_and_tags.py` (gitignored; never `git add -f`)

**Interfaces:**
- Produces: two facts recorded in this plan's Task 2 constants before Task 2 starts: `D4H_INCIDENT_LIST_PARAMS` (the query that lists incidents for a date window, and its pagination keys) and whether `GET /v3/team/1775/tags` returns tag titles (`TAG_TITLES_FROM_API: bool`).

- [ ] **Step 1: Write the spike** reading `BASE_URL`/`TEAM_ID` from `backend/d4h.py` via `ast` (the ISRID skill's snippet), token from `d4h-access-token` (sccssar-dev). Probe: (a) `incidents?after=<ISO>&before=<ISO>` for 2026-10-01..2026-11-01 PT; (b) the same with an INVENTED param `zzwindow=<ISO>` as the negative control; (c) the unfiltered list's `totalSize`; (d) `tags`; (e) an invented path `zztags` as its negative control. Print counts, keys and pagination fields only; never print descriptions or names.
- [ ] **Step 2: Run it.** Expected to conclude: the window filter is real only if (a)'s count < (c)'s and (b)'s equals (c)'s. If the filter is not real, the plan's fallback is: page the unfiltered list and filter on `startsAt` locally.
- [ ] **Step 3: Record both answers** in Task 2's constants block below (edit this plan file), plus the page-size key.

**Recorded 2026-11-10 (spike 28):** `incidents?after=<ISO UTC>&before=<ISO UTC>` filters (October 2026 PT window → 8 records vs `totalSize` 588 unfiltered, all starts inside the window); an invented param returns 400 (strict schema), so `D4H_INCIDENT_LIST_PARAMS = ("after", "before")`. Pagination: `page` (0-based) and `size`; responses carry `page`, `pageSize` (default 250), `totalSize`. Still filter `startsAt` locally against the PT window. `GET tags` returns `{id, title}` for 21 tags including `e-bike` (an invented path 404s), so `TAG_TITLES_FROM_API = True`.

### Task 2: Incident facts (`backend/monthly_recap.py`)

**Files:**
- Create: `backend/monthly_recap.py`
- Test: `backend/test_monthly_recap.py`

**Interfaces:**
- Consumes: `backend/d4h.py` constants via `ast` (`BASE_URL`, `TEAM_ID`, `TAG_*`), never `import d4h` (it needs `httpx`).
- Produces:
  - `PT = ZoneInfo("America/Los_Angeles")`
  - `month_window(month: str) -> tuple[datetime, datetime]` — `"2026-10"` → aware PT start of 10-01, start of 11-01.
  - `incident_hours(starts_at: str, ends_at: str) -> float | None` — ISO UTC strings; `None` when equal (not closed out).
  - `volunteer_hours_label(attendees: int, hours: float | None) -> str` — `"10 × 6.2 h = 62"`; `"10 × not closed out"` when hours is `None`. Hours to 1 dp, total rounded to whole hours.
  - `test_reason(reference_description: str) -> str | None` — `"[abcd] personal-dev suffix"` for `\[[0-9a-f]{4}\]`, `"named as a test"` for `\btest\b` (case-insensitive), else `None`.
  - `is_cancelled(reference_description: str) -> bool` — `\bCANCELL?ED\b`, case-insensitive.
  - `IN_COUNTY_AGENCIES: frozenset[str]` — every key AND value of `main._AGENCY_DISPLAY` read via `ast` (main.py says that dict canonicalizes in-county agencies only), plus `{"SCCSO", "SAN JOSE", "SANTA CLARA"}`: the two raw city forms reach event names un-normalized (ops#874). Do not hand-type other abbreviations; an unknown agency is an unconfident M/A guess that Bill confirms.
  - `guess_mutual_aid(reference_description: str, turbo_record: bool) -> tuple[bool, bool]` — returns `(is_ma, confident)`. `"M/A"` in the name → `(True, True)`; not a Turbo record → `(True, False)`; leading agency token in `IN_COUNTY_AGENCIES` → `(False, True)`; otherwise `(True, False)`.
  - `parse_turbo_description(html: str) -> TurboLines` — dataclass with `map_id: str | None` (from `CalTopo Map ID:` → last path segment of `caltopo.com/m/<id>`), `environment: str | None` (text after `Environment:`, up to `·`), `subject_names: list[str]` (tokens ≥ 3 letters from the `Missing Person:` line, before `;`), `agency: str | None` (from `Agency:`). Splits on `<br />`, `<br>` and newlines. `turbo_record` = `map_id is not None or agency is not None`.
  - `NON_SPECIALTY_TAG_TITLES = frozenset({"Search Management", "Transport", "Training", "Training - Mandatory", "Training - Optional", "Academy", "SO", "Compliance", "501c3", "General Team Hours"})` and `TAG_DISPLAY = {"Canine": "K9"}`.
  - `specialty_teams(tag_ids: list[int], tag_titles: dict[int, str]) -> list[str]` — titles from the `GET tags` map (Task 1), excluded titles dropped, renamed by `TAG_DISPLAY`, sorted; an id missing from the map → `"tag <id>"`.
  - `lpb_rows_by_number(export_csv: Path) -> dict[str, dict[str, str]]` — keyed by the 5-digit D4H number (reuse `isrid_submission.d4h_number` and `read_export` if PR #107 is on main; otherwise copy those two functions, citing the source, and note it for dedupe).
  - `environment_label(population_density: str | None, turbo_environment: str | None) -> str` — LPB `Urban`/`Suburban` → `"Urban"`, `Rural`/`Wilderness` → `"Wilderness"`; else Turbo's line containing `Urban` → `"Urban"`, `Rural` → `"Wilderness"`; else `"[fill in]"`.
  - `IncidentFacts` dataclass: `number, date_pt (MM-DD), sort_key (aware datetime), event_name, attendees, hours, cancelled, mutual_aid, ma_confident, environment, found_status, distance, specialty: list[str], map_id, pls: tuple[float, float] | None (lat, lng), subject_names` and `build_facts(incident: dict, involved: list[dict], lpb_row: dict | None, tag_titles: dict[int, str]) -> IncidentFacts`. `found_status` = LPB `Outcome` mapped `Life Saved`/`Person Assisted` → `"Found"`, `Deceased` → `"Found deceased"`, `Not Located` → `"Not found"`, absent → `"[fill in]"`; `distance` = LPB `Distance From IPP` + `Direction From IPP` verbatim, else `"[fill in]"`. `pls` from `incident["location"]["coordinates"]` (lng first), `None` for `[0, 0]`. `subject_names` also takes each involved person's `name`.

- [ ] **Step 1: Write failing tests** `TestMonthWindow`, `TestIncidentHours` (`("2026-10-02T22:49:00.000Z","2026-10-03T05:00:00.000Z")` → `6.18…`; equal → `None`), `TestVolunteerHoursLabel` (`(10, 6.183)` → `"10 × 6.2 h = 62"`; `(7, None)` → `"7 × not closed out"`), `TestTestReason`, `TestIsCancelled` (`"MPD Elmwood CANCELLED"` true, `"MPD Cancelino"` false), `TestGuessMutualAid` (one case per branch), `TestParseTurboDescription` (synthetic HTML with every line; and one with no Turbo lines → all `None`), `TestSpecialtyTeams` (`Search Management` and `Transport` absent; `Canine` → `"K9"`; `e-bike` kept; unknown id → `"tag 99"`), `TestEnvironmentLabel`, `TestBuildFacts` (`[0, 0]` → `pls is None`; LPB row present vs absent).
- [ ] **Step 2: Run** `python3 -m pytest -q backend/test_monthly_recap.py` — expect FAIL (module not found).
- [ ] **Step 3: Implement** the interfaces above in `backend/monthly_recap.py`.
- [ ] **Step 4: Run** the same command — expect all PASS; then `python3 -m pytest -q backend/` — expect no new failures.
- [ ] **Step 5: Mutation-check** `incident_hours` (return `0.0` for equal), `is_cancelled` (drop `\b`), `guess_mutual_aid` (`"M/A"` branch removed), the support-tag exclusion in `specialty_teams`: each must turn a test red. Restore.
- [ ] **Step 6: Commit.** All work happens in the worktree `$WT` (outside the repo, already on `docs/monthly-incident-recap-design`); the main checkout is shared with other sessions and never changes branch. First commit renames the branch so the PR carries spec, plan and code:
```bash
cd "$WT" && git branch -m docs/monthly-incident-recap-design feat/monthly-incident-recap && git branch --show-current
```
```bash
cd "$WT" && git add backend/monthly_recap.py backend/test_monthly_recap.py && git commit -m "feat: monthly recap incident facts"
```
Later tasks commit the same way: `cd "$WT" && git add <their files> && git commit -m …` in one call, after a separate `git branch --show-current`.

### Task 3: `drafts.md` write and parse

**Files:**
- Modify: `backend/monthly_recap.py`
- Test: `backend/test_monthly_recap.py`

**Interfaces:**
- Consumes: `IncidentFacts` from Task 2.
- Produces:
  - `IncidentDraft` dataclass: `facts: dict[str, str]` (ordered), `summary: str`, `highlight: str`, `lessons: list[str]`, `disruption: list[str]`, `number: str`, `title: str`.
  - `MonthDraft` dataclass: `month: str`, `incidents: list[IncidentDraft]`, `excluded: list[str]`.
  - `render_drafts(month: str, facts: list[IncidentFacts], excluded: list[tuple[str, str]]) -> str`
  - `parse_drafts(text: str) -> MonthDraft` — raises `DraftError(message)` naming the incident header when a facts block is missing.
  - `next_drafts_path(folder: Path) -> Path` — `drafts.md`, else `drafts-2.md`, `drafts-3.md`, …
- Format (exact; the skill tells Bill not to edit lines starting `## ` or the fence lines):

````markdown
# 2026-10 incident recap — drafts

Edit anything below. Keep the `## ` lines and the ```facts fences.

## 98765 · 10-02 · SCCSO Elmwood
```facts
date: 10-02
event: SCCSO Elmwood
attendees: 10
hours: 6.2
volunteer_hours: 10 × 6.2 h = 62
cancelled: no
mutual_aid: no
environment: Urban
found: Found
distance: 1.2mi NE
specialty: K9, UAS
map: ABC1234
pls: 37.30000, -121.90000
```
### Summary
[draft]
### Highlight
[draft]
### Lessons learned
- [draft]
### The disruption
### Sources and conflicts
(not rendered)

## Excluded
- 98000 · personal-dev suffix
````
  `### The disruption` is filled only for cancelled incidents. `### Sources and conflicts` is never rendered. A field that needs Bill shows `[fill in]` or `[confirm] <guess>` (M/A when `ma_confident` is false).

- [ ] **Step 1: Write failing tests** `TestDraftsRoundTrip` (render then parse returns equal facts and sections), `TestParseBillsEdits` (CRLF line endings; a lesson deleted; a fourth bullet added; `### The disruption` absent; text with `:` and `—` inside) keeping his text exactly, `TestParseRejects` (a `## ` incident with no facts fence → `DraftError` whose message contains the header), `TestNextDraftsPath` (empty dir → `drafts.md`; with `drafts.md` → `drafts-2.md`; with both → `drafts-3.md`), `TestConfirmMarker` (unconfident M/A renders `mutual_aid: [confirm] yes`).
- [ ] **Step 2: Run** `python3 -m pytest -q backend/test_monthly_recap.py -k "Draft or Confirm"` — expect FAIL.
- [ ] **Step 3: Implement** the interfaces.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Mutation-check** `next_drafts_path` (always return `drafts.md`) and the facts-missing error (return an empty facts dict): tests go red. Restore.
- [ ] **Step 6: Commit** in `$WT` (`git add … && git commit -m "feat: monthly recap drafts file"`).

### Task 4: Map pictures (`backend/recap_map.py`)

**Files:**
- Create: `backend/recap_map.py`
- Test: `backend/test_recap_map.py`

**Interfaces:**
- Consumes: nothing from earlier tasks; marker symbols from `backend/caltopo.py` constants via `ast` (`SYMBOL_LKP`, `SYMBOL_RESIDENCE`, `SYMBOL_STAGING_OFFICER`, `SYMBOL_STAGING_ALT`).
- Produces:
  - `Marker` dataclass: `role: str` (`"lkp" | "residence" | "cp" | "staging" | "find"`), `title: str`, `lat: float`, `lng: float`.
  - `FIND_TITLE_RE = re.compile(r"\b(subject|mp)\s+(found|location)\b", re.I)` (same pattern as the AAR skill).
  - `select_markers(features: list[dict]) -> list[Marker]` — only `class == "Marker"`; find by title first; then symbol: LKP, residence, cp; `SYMBOL_STAGING_ALT` only when the title matches `^\d+\.\s`. Never reads `description`.
  - `TileFetcher = Callable[[int, int, int], bytes]`; `cached_osm_fetcher(cache_dir: Path) -> TileFetcher` (urllib, the Global Constraints User-Agent, writes `z/x/y.png`, reads cache first).
  - `lnglat_to_world_px(lng: float, lat: float, zoom: int) -> tuple[float, float]` (Web Mercator, 256-px tiles).
  - `choose_zoom(points: list[tuple[float, float]], width: int, height: int, min_zoom: int, max_zoom: int, margin: float = 0.15) -> int` — the largest zoom where all points fit inside the margin.
  - `render_map(markers: list[Marker], width: int, height: int, fetch: TileFetcher, out: Path, *, extent: list[tuple[float, float]] | None = None, min_zoom: int = 11, max_zoom: int = 17) -> Path` — fits `markers + extent`; draws tiles, then one symbol per role (LKP black placemark, residence house, cp red flag, staging blue dot, find orange star) with its title in a white-backed label, then `© OpenStreetMap contributors` bottom-right. PNG.
  - `COUNTY_EXTENT = [(37.484, -122.202), (36.896, -121.208)]` (Santa Clara County bounding corners, lat/lng) and `COUNTY_REF = (37.23, -121.70)`.
  - `render_overview(pins: list[tuple[int, float, float, bool]], fetch, out) -> Path` — `(number, lat, lng, cancelled)`; numbered orange pins, grey when cancelled; extent = pins plus `COUNTY_EXTENT`; `max_zoom=12`.
  - `miles_between(a: tuple[float, float], b: tuple[float, float]) -> float` (great-circle, 3958.8 mi radius).
  - Incident map sizes: 1200×900 px; overview 1000×1000 px.
- Per-incident extent rules (the skill passes `extent`): found → markers only; cancelled → markers plus a 0.75 mi box around the PLS; M/A → markers plus `COUNTY_EXTENT` with `min_zoom=7`. No markers and no PLS → the caller draws a placeholder; `render_map` raises `ValueError("nothing to draw")`.

- [ ] **Step 1: Write failing tests** with a fake fetcher that returns a solid 256×256 PNG and records calls: `TestSelectMarkers` (synthetic features: a `Shape`, a clue `point` titled "Glove", a `point` titled "2. Example Park", an LKP, a residence, a cp, a `point` titled "Subject Found" → exactly 5 markers with roles `staging, lkp, residence, cp, find`; and a feature with a `description` whose text never appears in any `Marker`), `TestWorldPx` (lng 0, lat 0, zoom 0 → (128, 128)), `TestChooseZoom` (two points 1 km apart at 37°N in 1200×900 → zoom ≥ 14; points spanning the county → ≤ 10), `TestRenderMap` (output exists, size 1200×900, the bottom-right 300×40 region is not tile-coloured, i.e. attribution drawn; fetch called only for tiles inside the frame), `TestRenderMapNothingToDraw`, `TestCachedFetcher` (second call for the same tile does not hit the network function — inject the opener), `TestOverview` (grey pixel at a cancelled pin's position), `TestMilesBetween` (SF to LA ≈ 347 mi ± 2).
- [ ] **Step 2: Run** `python3 -m pytest -q backend/test_recap_map.py` — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expect PASS; `python3 -m pytest -q backend/` no new failures.
- [ ] **Step 5: Mutation-check**: let `select_markers` keep every `point`; drop the attribution draw; make the cache always miss. Each turns a test red. Restore.
- [ ] **Step 6: Commit.**

### Task 5: Text check (`backend/monthly_recap.py`)

**Files:**
- Modify: `backend/monthly_recap.py`
- Test: `backend/test_monthly_recap.py`

**Interfaces:**
- Consumes: `isrid_submission._PATTERNS` (phone, email, street address, agency Event #) — **requires PR #107 merged to main; if it is not, stop and ask Bill** rather than copying the patterns.
- Produces:
  - `DOB_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")`
  - `slide_text_hits(text: str, subject_names: list[str]) -> list[str]` — kinds hit, from `{"phone", "email", "address", "event_number", "dob", "subject_name"}`; never the value. Name tokens < 3 letters ignored; match is whole-word, case-insensitive.
  - `check_month(draft: MonthDraft, names_by_number: dict[str, list[str]]) -> list[str]` — `"<number> <section>: <kinds>"` per hit over every rendered field (title, facts shown on the slide, summary, highlight, lessons, disruption).

- [ ] **Step 1: Write failing tests** `TestSlideTextHits` (one positive per kind with invented values; `"10 × 6.2 h = 62"` and `"1.2mi NE"` are clean; a subject surname equal to the event's street word still hits — Review Focus 4; a two-letter name token does not), `TestCheckMonth` (the hit message names number and section and does not contain the value).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Mutation-check**: remove `DOB_RE`; make the name match case-sensitive; include the value in the message. Each turns a test red. Restore.
- [ ] **Step 6: Commit.**

### Task 6: The `.pptx` writer (`backend/recap_pptx.py`)

**Files:**
- Create: `backend/recap_pptx.py`
- Create: `.claude/skills/monthly-recap/requirements.txt` (`python-pptx==1.0.2`)
- Test: `backend/test_recap_pptx.py` (starts with `pptx = pytest.importorskip("pptx")`)

**Interfaces:**
- Consumes: `MonthDraft`, `IncidentDraft` (Task 3); map PNG paths from Task 4.
- Produces:
  - `ORANGE = "F1592A"`, `GREY = "9A9A9A"`, `HEADING_FONT = "Oswald"`, `BODY_FONT = "Public Sans"`.
  - `build_deck(draft: MonthDraft, overview_png: Path | None, maps: dict[str, Path | None], map_links: dict[str, str], out: Path) -> Path` — raises `FileExistsError(str(out))` when `out` exists, before building anything.
  - Slide 1 (summary): title `OCTOBER 2026 · INCIDENTS`; table columns `#`, `Date`, `Event`, `Volunteer hours`, `Att.`; in-county rows numbered 1..n in date order, M/A rows `M/A`, cancelled rows grey text with `— cancelled` appended to the event; totals row `<n> incidents (<c> cancelled, <m> M/A)`, sum of volunteer-hour totals (only rows with closed-out hours; if any row is not closed out, the cell reads `<sum>+`), sum of attendees; overview picture on the right.
  - Incident slides in date order: left 6.3 in column with summary, `Highlight:` line, a fact strip (one text box, chips separated by `  ·  `: found, environment, distance, volunteer hours, specialty, `M/A: yes`/`no`), `LESSONS LEARNED` (or `THE DISRUPTION` then `LESSONS LEARNED` when cancelled), and an empty `TEAM TAKEAWAYS` heading; right column the map picture with its click action set to `https://caltopo.com/m/<map>` and a caption text box `Open the live map` hyperlinked to the same URL; no map → a grey rectangle labelled `Space for a photo`. Cancelled slides use a grey left bar; others orange. `[draft]` lines are dropped; a slide with no lessons gets none.
- [ ] **Step 1: Write failing tests** `TestBuildDeckRefusesOverwrite` (Review Focus 5: `out` exists → `FileExistsError`, file bytes unchanged), `TestSummarySlide` (row count = incidents + header + totals; M/A row's first cell is `M/A`; totals text), `TestIncidentSlide` (title text; the picture's `click_action.hyperlink.address` equals the CalTopo URL; `TEAM TAKEAWAYS` present; `[draft]` absent), `TestCancelledSlide` (`THE DISRUPTION` before `LESSONS LEARNED`), `TestNoMapPlaceholder`, `TestFonts` (every run's font name is `Oswald` or `Public Sans`).
- [ ] **Step 2: Set up the venv and run:** `python3 -m venv research/monthly-recap/.venv && research/monthly-recap/.venv/bin/pip install -q -r .claude/skills/monthly-recap/requirements.txt pillow pytest && research/monthly-recap/.venv/bin/python -m pytest -q backend/test_recap_pptx.py` — expect FAIL. Also `python3 -m pytest -q backend/test_recap_pptx.py` — expect `1 skipped`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the venv command — expect PASS; system `python3 -m pytest -q backend/` — expect no new failures and this file skipped.
- [ ] **Step 5: Mutation-check**: drop the overwrite guard; drop the hyperlink; render `[draft]` lines. Each turns a test red. Restore.
- [ ] **Step 6: Commit.**

### Task 7: CLI wiring (`backend/monthly_recap.py`)

**Files:**
- Modify: `backend/monthly_recap.py`
- Test: `backend/test_monthly_recap.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `collect(month: str, out_root: Path, get_d4h: Callable[[str], dict], get_caltopo: Callable[[str], dict], lpb_export: Path | None) -> Path` — lists the month's incidents (Task 1's query), skips `test_reason` hits into `excluded`, fetches involved persons (`incident-involved-persons?incident_id=<id>`) and each map's `since/0`, writes `research/monthly-recap/<YYYY-MM>/d4h-<number>.json`, `caltopo-<map>.json`, `names.json` (number → subject name tokens; used only by the check), and the drafts file at `next_drafts_path`. A not-closed-out incident gets `hours: [fill in]`. An M/A incident with a PLS gets `distance: ≈ <N> mi from the county centre` (`recap_map.miles_between(COUNTY_REF, pls)`, whole miles). Returns the drafts path.
  - `render(drafts: Path, fetch: TileFetcher, build: Callable = recap_pptx.build_deck) -> Path` — parses drafts, runs `check_month` (any hit → raise `RecapError` listing the hits, nothing written), renders the overview from every in-county incident's PLS (M/A excluded) and one map per incident by the Task 4 extent rules, then calls `build`. Output `research/monthly-recap/<YYYY-MM>/<YYYY-MM> Incident Recap.pptx`.
  - `main(argv) -> int` — `collect --month YYYY-MM [--lpb-export PATH]` and `render --drafts PATH`. Network getters read tokens from env `D4H_ACCESS_TOKEN`, `CALTOPO_CREDENTIAL_ID`, `CALTOPO_CREDENTIAL_SECRET`; CalTopo signing as in the AAR skill (HMAC-SHA256 of `"GET <path>\n<expires>\n"`), retry 5xx twice, never 4xx.
- [ ] **Step 1: Write failing tests** with fake getters and synthetic incidents: `TestCollect` (an `[abcd]` record lands in `## Excluded`; the drafts file is created; a second collect creates `drafts-2.md` and leaves `drafts.md` byte-identical; a not-closed-out incident shows `[fill in]` hours — Review Focus 1), `TestRenderRefusesOnHit` (a phone in a lesson → `RecapError`, no `.pptx` written), `TestRenderNoMap` (Review Focus 3: an incident with `map: ` empty and no PLS gets a placeholder, render succeeds — run under the venv, `importorskip("pptx")`).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both suites (system and venv) — expect PASS / skip as before.
- [ ] **Step 5: Mutation-check**: let collect overwrite `drafts.md`; let render write before the check. Each turns a test red. Restore.
- [ ] **Step 6: Commit.**

### Task 8: The skill (`.claude/skills/monthly-recap/SKILL.md`) and PR

**Files:**
- Create: `.claude/skills/monthly-recap/SKILL.md`
- Modify: `CLAUDE.md` "Things this file names that you may not have" table: one row for `monthly-recap` (shipped in repo; output local-only).

**Interfaces:**
- Consumes: the CLI from Task 7, run from the repo root as `research/monthly-recap/.venv/bin/python backend/monthly_recap.py collect|render`.
- The skill's sections, in order:
  1. Scope and hard rules (Global Constraints, restated briefly; production only; never Drive connector; never overwrite).
  2. Setup: create the venv if absent (Task 6 command); auth preflight with `--configuration` from `deploy.env` for sccssar-dev; export the three secrets.
  3. Collect: ask Bill for the month and, optionally, the path of a fresh D4H Lost Behavior export; run `collect`.
  4. Draft: for each incident, read its AAR (`research/after-action/*/report.md` matched by D4H number or event name) — debrief notes, "For dispatchers and Plans", Find data — and its Slack channel (search private, archived channels by the incident date; read the whole channel). Fill Summary (1–2 sentences, de-identified, one memorable highlight in Highlight), Lessons (≤ 3, about what happened, never a person), and for cancelled incidents The disruption (YES count, how far people got, when it was cancelled). Fill `[fill in]` facts the AAR answers (found, distance, environment) and say so in Sources and conflicts. List every disagreement there, with the value chosen and why. Never invent: leave `[fill in]`.
  5. Review: tell Bill the drafts path; stop until he says he has finished editing.
  6. Render: run `render`; on `RecapError` show the hits (kinds and places only) and ask Bill to edit; on success give the `.pptx` path and remind him to move it into the team's monthly-review Drive folder and import its slides into the master deck.
- [ ] **Step 1: Write the skill** to the outline above, no PII, no Drive folder path (it is Bill's to name each time).
- [ ] **Step 2: Run** `python3 -m pytest -q backend/` — expect the suite green with the pptx tests skipped. `LC_ALL=C grep -rn -E '[0-9]{3}[ -][0-9]{3}-[0-9]{4}|@sccssar' .claude/skills/monthly-recap backend/*recap*` — expect no output.
- [ ] **Step 3: Review** the whole branch with a fresh reviewer (the six-question rubric does not apply — no handler code; review for correctness, the overwrite guarantees and the text check).
- [ ] **Step 4: Commit, push, open the PR** (`gh pr create`), body lists the mutation counts per task and says no build or deploy is involved. Never self-merge.

### Task 9: October by hand (acceptance)

Not code. Runs only after Bill merges the PR (or on the branch, with his OK).

- [ ] **Step 1:** Run the skill for `2026-10` with Bill's latest Lost Behavior export.
- [ ] **Step 2:** Bill edits `drafts.md`; render.
- [ ] **Step 3:** Open the `.pptx`, look at every slide (convert to PNG via the venv or open in Keynote/Slides), and check: 8 rows in the summary (3 cancelled, 3 M/A), 359 h ± rounding if every incident is closed out, pins only for in-county incidents, each map shows only the four marker roles and the OSM credit, each link opens the right CalTopo map.
- [ ] **Step 4:** Report to Bill what he still needs to fill in, and what the run got wrong, as the input for the next round.
