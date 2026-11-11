# Monthly incident recap for General Team meetings — design

Status: draft for Bill's review, 2026-11-10. Roadmap item (2) of the 2026-10-13 incident-review
roadmap. Sibling work: the quarterly ISRID export (PR #107).

## Why

The lead dispatcher presents the month's incidents at each General Team meeting, in a slot of about
ten minutes. Each incident gets one to three minutes: what happened, its lessons learned, then the room
is asked for further takeaways. Today those slides are written by hand from memory, Slack and D4H. The
facts already exist in D4H, the after-action (AAR) reports, the Slack incident channels and CalTopo, so
the slides can be drafted from them and the presenter's time goes into lessons, not data entry.

Kept month after month, the slides become the year's record of every incident and of what was shown to
the team.

## Goals

- One file per month holding that month's slides, ready for the presenter to correct and add to.
- Every incident the team responded to in that month gets a slide: found, not found, cancelled after the
  page, and mutual aid (M/A).
- The facts on each slide come from the system of record and say where they came from when two sources
  disagree.
- Each incident's summary, highlight and lessons are drafted for the presenter to edit, never presented
  unreviewed.
- Repeatable by hand first. A scheduled run comes later, once the output is accurate.

## Non-goals

- Writing to the team's master deck. The presenter joins each month's file into it by hand.
- Drawing search effort: tracks, assignments, clue markers, searcher spacing. CalTopo already shows
  these, and the slide links to the live map for that part of the discussion.
- Pre-page cancellations. A callout cancelled before Everbridge fires leaves no record the generator can
  read; the presenter adds it by hand.
- Range rings, for now (see the map section).

## Anti-goals (it fails if it does any of these, even when it technically works)

- It changes, reorders or overwrites any existing slide or any earlier month's file. Human edits must
  survive every run.
- It changes CalTopo sharing on any map. A permission downgrade was rejected (CLAUDE.md, Investigated
  and Rejected); the map picture is drawn from the map data instead.
- Incident material reaches a personal Google account. The Drive connector in Claude sessions is signed
  in to a personal account, so it is never used here.
- Anything with subject or incident detail lands in the public repository.

## Audience and disclosure (Bill, 2026-11-10)

The audience is the SAR team only: every member is background-checked and eligible to respond. For this
closed audience the slides may show the event name (which carries a street), agency, city, subject
category, outcome, distance and bearing from the PLS, and the CalTopo markers as they are, including the
residence marker and its position.

Never on a slide, in any text box: the subject's name, date of birth or phone; the agency's Event #; any
responder's name. Lessons are about what happened, never about a person, the same rule the AAR follows.
House-numbered addresses do not appear in slide text (summaries, lessons, fact strips). They MAY appear
inside the map pictures, because CalTopo marker titles carry them (`Residence — <address>`) and the
pictures draw titles as they are (Bill, 2026-11-10: "map labels only"). The text check therefore covers
text boxes only, by design.

## The workflow

1. **Collect.** For the chosen month, list D4H incidents whose start falls in that month (Pacific time).
   D4H team 1775 is shared by both environments, so the list also holds tests. Excluded by rule, and
   listed in `drafts.md` with the reason so Bill can overrule: a `referenceDescription` carrying the
   personal-dev `[abcd]` suffix, and any record whose name or AAR folder marks it as a test. Anything
   uncertain is included and flagged, never silently dropped. For each kept incident, gather the
   sources below into `research/monthly-recap/<YYYY-MM>/` (gitignored), one JSON per incident, plus the
   CalTopo map data.

   **Two collect paths.** A Turbo-dispatched record carries Turbo's lines in its D4H description
   (`CalTopo Map ID:`, `Environment:`, the event log). A hand-entered record, typically mutual aid, has
   none of them: its facts come from D4H's own fields (start, end, attendance, tags, location) and its
   Slack channel, found by searching channel names for the incident date. Fields neither path can fill
   are written as `[fill in]` in `drafts.md`, never guessed.
2. **Draft.** Write `research/monthly-recap/<YYYY-MM>/drafts.md`: one section per incident with the
   fact strip as computed, then a drafted summary (one or two sentences), one highlight, up to three
   lessons learned, and for a cancelled callout a "disruption" paragraph. Each drafted line cites its
   source (debrief notes, Slack message date and time, AAR section). Facts that disagree between sources
   are listed under the incident, with the value chosen and why.
3. **Review.** Bill edits `drafts.md` in his editor. The generator never rewrites it after he starts
   editing: a second draft run writes `drafts-2.md` and says so.
4. **Render.** Read the edited `drafts.md`, render the map pictures, run the text check (below), and
   write `research/monthly-recap/<YYYY-MM>/<YYYY-MM> Incident Recap.pptx`. If that file already exists,
   stop and ask; never overwrite.
5. **Hand over.** Bill drags the file into the team's monthly-review Drive folder and imports its slides
   into the master deck (Google Slides: File > Import slides). He corrects and adds photos there.

Writing straight into the synced Drive folder would need macOS Full Disk Access for Claude, which Bill
declined as too broad (2026-11-10). The local folder is the workaround until a narrower grant exists.

## Slides

Visual style follows the 2026-11-09 Leadership deck: house orange `#F1592A`, Oswald for headings,
Public Sans for body text, both Google Fonts so Slides renders them after import. Slides are built from
plain text boxes, tables and images only, which import cleanly into Google Slides. 16:9.

**1. Monthly summary (first slide of the month).**
- Title: `<MONTH YYYY> · INCIDENTS`.
- Left: chronological table. Columns: #, date, event name, volunteer hours, attendees. In-county
  incidents are numbered 1..n to match the map pins; M/A rows show `M/A` in the # column; cancelled rows
  are grey and say "cancelled". Volunteer hours are shown as `attendees × incident hours = total`
  (`10 × 6.2 h = 62`). A totals row: incident count (with cancelled and M/A counts), total volunteer
  hours, total attendees.
- Right: an overview map of the county with one numbered pin per in-county incident, at its PLS.
  Cancelled pins are grey. **M/A incidents get no pin**, because they would zoom the map out too far to
  show the in-county pattern (Bill).

**2. Incident slide (one per incident, in date order).**
- Title: `<MM-DD> · <EVENT NAME>`, plus `· M/A` or `— CANCELLED`.
- Left column: the summary with its highlight; a fact strip (found status, Urban or Wilderness, M/A or
  not, distance from the PLS, `attendees × hours = total`, specialty teams); "Lessons learned" (up to
  three); an empty "Team takeaways" heading for the meeting.
- Right column: the incident map picture (next section), with a clickable link to the live CalTopo map
  under it. Members who open it need their CalTopo team login; sharing is not changed.

**Variants.**
- **Cancelled after the page:** grey title bar; fact strip says when it was cancelled (after the page,
  en route, after staging and briefing). "The disruption" replaces "Lessons learned" as the first list:
  how many answered YES, how far people got, and anything notable about the stand-down. Lessons only if
  there are any. The map is centred on the PLS, because callouts cluster in the same areas.
- **Mutual aid:** the fact strip names the requesting county. The map shows the PLS or staging
  relative to Santa Clara County, because the discussion often starts with how far away it was. The
  slide states the straight-line distance from the county; drive time is left for the presenter to add.
  *(Builder's call; drive time would need a routing service.)*

## Map pictures (drawn by us)

**Why drawn.** CalTopo has no map-image endpoint reachable for a Secret map. Probed 2026-11-10
(spike 03, local-only): 12 candidate export, print and image paths all returned the same 404 as an
invented path, signed or not. The preview image Slack shows (`/pv/<map id>`, a 1200×1200 PNG) exists
only for maps with open sharing; for a Secret map a signed request and an invented map id both return
the same 500. The only route to CalTopo's own picture is the rejected permission downgrade.

**Base map: OpenStreetMap standard tiles** (Bill: the team's standard). Respect the OSM tile usage
policy: an identifying User-Agent, a local tile cache under the month folder, no bulk prefetch, and
the attribution "© OpenStreetMap contributors" on every picture. A month is a few dozen tiles.

**Drawn:** the markers that tell the story: LKP/PLS, residence, the command post (never the staging
alternates: Bill, 2026-11-10, after seeing seven of them crowd a cancelled callout's map),
and the find. Symbols follow Turbo's marker conventions; titles are drawn as they appear in CalTopo.
Marker descriptions are never drawn.

**Not drawn:** tracks, assignments, clue markers, shapes. Range rings are hidden for now: the rings on
existing maps came from an unsourced table (ops#870), and Bill wants rings only when they match the
subject's case, not generic ones. Turn them on only once rings come from the sourced table (the
override in PR #85) and the map shows that source.

**Extent:** fit the drawn markers with margin; a minimum zoom so a single-marker map (cancelled) still
shows the neighbourhood; for M/A, an extent that includes the county.

**Which map.** The map id comes from the `CalTopo Map ID:` line Turbo writes in the D4H description.
When the AAR says the field worked on a copy, use the AAR's map. Incidents with no map in our CalTopo
team (some M/A) get a placeholder box for a photo.

## Field sources (which wins)

| Slide field | Wins | Fallback / cross-check |
|---|---|---|
| Date, event name | D4H incident (`startsAt` in Pacific time, `referenceDescription`) | — |
| Attendees | D4H `countAttendance` | Higher than Slack YES counts is normal (management pre-add, late joins); D4H wins |
| Incident hours | D4H `endsAt − startsAt`, after close-out | If `endsAt == startsAt` the incident is not closed out: show "not closed out" and ask, never compute 0 |
| Specialty teams | D4H incident tags | Everbridge groups in the D4H event log |
| Found status | D4H Lost Behavior tab / Outcome, as entered and read back | AAR "Find data" |
| Distance and bearing from PLS | D4H Lost Behavior tab | AAR "Find data", computed from CalTopo; a disagreement over 0.1 mi is listed in `drafts.md` |
| Urban or Wilderness | D4H Population Density | Turbo's `Environment:` line in the D4H description |
| Mutual aid | Another county holds the incident (the agency is not in Santa Clara County) | Bill confirms in review |
| Cancelled, and when | Slack channel timeline | AAR operational sequence |
| Summary, highlight, lessons | AAR on-scene debrief notes and the Slack channel | The AAR's findings and "For dispatchers and Plans" lines; with nothing to draw on, a marked gap ("No lessons drafted: add or delete"). A missing AAR never stops the run |
| Map | CalTopo map data | — |

D4H's Lost Behavior tab is not readable through the D4H API (spec 7.5.2–7.6.1). Its values reach the
generator through D4H's Lost Behavior CSV export, the same file the ISRID skill reads, or the AAR's
"Find data" table. *(Builder's call: prefer the export when Bill has one for the month, else the AAR,
and say which.)*

Incident hours are approximate by design: attendance rows are 1-minute placeholders, so per-person
hours are not recorded, and `attendees × incident hours` overstates people who left early. The slide
shows the multiplication so the room can see what the number means.

## The text check

Before the .pptx is written, every text box is checked for: phone numbers, email addresses, the agency
Event # shape, dates of birth, house-numbered addresses, and the subject's name taken from that
incident's own D4H description. A hit stops the render and names the slide and box, never the value.
Reuse the patterns in `backend/isrid_submission.py` (PR #107) once merged rather than writing a second
set; event-name words are allowed here, unlike in the ISRID check.

Drafts and all source files stay in gitignored `research/monthly-recap/`. Nothing about an incident is
committed.

## How it is built

- A skill, `.claude/skills/monthly-recap/SKILL.md`, runs the collect → draft → review → render steps.
  Drafting (reading the AAR and Slack, writing summaries and lessons) is Claude's work inside the skill.
- A script does everything deterministic: D4H reads, CalTopo reads, hours arithmetic, tile fetch and map
  drawing, the text check, and writing the .pptx from the edited `drafts.md`. Tests go beside it.
- Reads only. D4H and CalTopo credentials come from the sccssar-dev Secret Manager, with that gcloud
  configuration from `deploy.env`; personal-dev is never used (Bill, 2026-11-10).
- Slack is read through the Slack connector, which is the team workspace. Archived incident channels
  stay readable: checked 2026-11-10, all nine October channels are listed as archived, and reading
  one returned its messages. This depends on Bill's account staying a member; if a post-incident clean-up removes him, the
  AAR's Step 2.6 event-log lines are the fallback.
- Library choices are the builder's. Note: system python has PIL but not `python-pptx`, `httpx` or
  `requests`, and the build scripts' Step 0 runs `pytest backend/`, so any test needing an extra
  library must skip cleanly where it is absent.

## Later

A scheduled monthly run, once October and November come out right by hand. It would draft and stop at
step 3, because the review is a person's job.

## Decisions log

**Bill's calls (2026-11-10)**
- Presenter is the lead dispatcher, ~10 minutes, lessons learned per incident, room takeaways after each.
- Lessons come from the on-scene debrief and the Slack channel.
- Cancelled callouts get a slide: the disruption to people who prepared or drove is the point.
- Event names with streets are fine for the team; raw CalTopo markers, including residence, are fine.
- Volunteer hours = attendees × incident hours, shown as the multiplication.
- One file per month into a Drive folder; Bill joins it into the master deck by hand.
- Full Disk Access declined; generator writes to a local folder and Bill moves the file.
- Summary slide: table plus county pin map; incident slide: map beside the text.
- M/A: map shows where it was relative to us; no M/A pins on the county overview.
- OpenStreetMap base map; markers only (LKP, residence, command post, find; no staging alternates); link to the live CalTopo map.
- Range rings only when they match the subject's case; hidden for now.
- Review happens in an editable markdown file; Bill corrects the slides again after generation.
- Test month: October 2026.
- Addresses: allowed in map labels, never in slide text.

**Builder's calls**
- The source-precedence table above.
- Cancelled-callout "disruption" content and the "Team takeaways" placeholder.
- M/A distance as straight-line from the county, drive time left to the presenter.
- Second draft runs write `drafts-2.md` rather than overwrite.
- Tests excluded by rule (`[abcd]` suffix, test-named records), listed with the reason for Bill to overrule.
- Hand-entered and M/A records get their own collect path; unfillable fields are `[fill in]`.
- No lessons to draw on → a marked gap, never a blocker.
