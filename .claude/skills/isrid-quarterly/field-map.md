# ISRID 2016 elements vs the D4H Lost Behavior export

Adapted from *ISRID Data Standards 2016*, Robert J. Koester, dbS Productions, licensed
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/). Changes: the element list
is reduced to the ones this team can hold, and each element is mapped to the D4H export column and to
where we keep the value. **This file is licensed CC BY-NC-SA 4.0**, not under the repository's BSD
licence. Page numbers refer to the 2016 edition (70 pp.): Minimum = Appendix B, p. 68; Silver =
Appendix C, p. 70.

**Tier:** **M** = Minimum (13 fields). **S** = one of the 12 Silver additions; Silver is the Minimum
plus these 12, 25 fields in all. Blank = neither.

## A. Elements with a column in the D4H export

Fill the column when it is blank and we hold the value, and correct it when the after-action report
shows D4H's value is wrong. The dispatcher approves each change, and each one goes on `fix-in-d4h.md`.

| ISRID element | Tier | D4H column(s) | Where we hold it when D4H is blank or wrong |
|---|---|---|---|
| Incident Type | M | Incident Type | AAR entry sheet |
| Incident date/time (SAR notified) | M | Incident Date | D4H start = when the team was paged (Turbo writes the Everbridge send time) |
| Subject Category | M | Subject Category | Turbo summary, LPB section |
| IPP coordinates | M | IPP Coordinates / Lat / Lng | AAR Step 2.5c; CalTopo LKP marker |
| Find coordinates | M | Find Coordinates / Lat / Lng | AAR Step 2.5c; CalTopo find marker |
| Scenario | M | Scenario | AAR entry sheet. D4H spells it `Investigation`; ISRID says `Investigative` |
| Comments | M | Comment | rebuilt by the skill (section B) |
| Found date/time, via search hours | M | Search Time | AAR; Slack find post. D4H holds whole hours only |
| Last-seen date/time, via time lost | M | Lost Time | AAR; intake form. D4H holds whole hours only |
| Incident Environment | S | Environment | AAR Step 2.5f |
| Population Density | S | Population Density | AAR Step 2.5f |
| Terrain | S | Terrain | AAR Step 2.5f |
| Age | S | Age | D4H involved person |
| Sex | S | Sex | Form or AAR. Turbo writes `o` (Other) when the form gives none: confirm it, or fill the real value |
| Physical Fitness | S | Fitness | AAR |
| Experience | S | Experience | AAR |
| Mobility (hours) | S | Mobility Time | AAR. Blank when unknown; never estimate it |
| Number involved | | # Subjects | D4H |
| City | | City | D4H. Free text: shown to the dispatcher before it is sent |
| County | | County | The config's `county` for every in-county row. D4H exports the state here by design |
| Ecoregion domain | | Eco Region | AAR Step 2.5f: your county's Bailey domain. `Dry` is the pick-list default, so check it |
| Equipment | | Equipment | AAR |
| Distance from IPP | | Distance From IPP | AAR Step 2.5c. D4H's display string, with units that switch (`mi`, `yd`) |
| Find bearing | | Direction From IPP | AAR Step 2.5c |
| Find feature | | Find Feature | AAR |
| Elevation change | | Elevation Change | ISRID derives it from coordinates when blank |
| Weather | | Weather | D4H free text: shown to the dispatcher before it is sent |
| Mission number | | Mission # | Cut to the 5-digit D4H number |
| Subject status | S (part) | Outcome | D4H's options do not say well or injured, so the comment carries it |

The D4H-only column `Involvement` has no ISRID element and passes through unchanged.

## B. Elements we hold that have no column, so they go in the Comment

| ISRID element | Tier | Source | Note |
|---|---|---|---|
| # well / # injured / # DOA / # no trace | M | AAR outcome | `Outcome: 1 well.` The Minimum counts these only when the comment states them |
| Subject Status | S | same | the same clause covers it |
| Last-seen, notified and found times | M (precision) | intake form, Everbridge send, Slack find post | the main gain: D4H's whole hours lose up to 30 minutes each way |
| IPP type, revised PLS and reason | | AAR Step 2.5c | e.g. "IPP: point last seen (later sighting, not residence)" |
| Investigative reason | | AAR | only when Scenario is Investigative |
| Mission cause | | AAR; D4H involved-person `cause` (API only) | D4H's list is injury-cause style (`INTENTIONAL_SELF`), not ISRID's: map by hand |
| Find resource | | Slack; AAR | public, law enforcement, SAR ground, K9, air |
| Incident outcome | | AAR | closed by search, by the public, by investigation |
| Contact method | | intake | nearly always "reported missing" |
| Destination; direction of travel | S | AAR, rarely known | only when known: "Destination: …", "Direction of travel: …" |
| Subject sub-category; group type | | intake; AAR | optional |
| Injury or illness; rescue method | | D4H involved-person `injuries`; AAR | only when injured |
| Resources: tasks, dogs, aircraft or UAS, personnel | | CalTopo assignments; D4H attendance and animal attendance | member durations are placeholders, so leave out manhours |
| Total incident time | | D4H start → end | only when the end time is real |
| Height, weight, build | | intake form description | low sensitivity once the rest is removed (team decision, 2026-11-10) |

## C. Elements ISRID derives, or that we do not hold

- **ISRID derives these from coordinates and times:** ecoregion division, land cover, land owner, IPP
  classification, elevation, track offset, light, notify hours, and find feature (NLCD) as a fallback.
  The Silver score lists track offset as never held for this reason.
- **ISRID assigns these:** data source, key number.
- **We do not hold these:** disaster-related and disaster type, subject activity, mental fitness,
  clothing rating, personality, survival training, IPP and find accuracy as numbers, direction-of-travel
  basis, decision point, found secondary, detectability, mobility and responsiveness, lost strategy,
  signalling, number saved, treatment by, injured searcher or near miss, vehicles, distance driven,
  emergent volunteers, equipment loss, total cost.
- **We hold this but never send it:** the agency's incident number (Event #), because it links to a
  police record.
- **Left out on purpose:** D4H's involved-person `areaKnowledge` (familiar or unfamiliar). No ISRID
  element matches it. Experience is about the activity, not the area.
