# Quarterly ISRID submission — design

Status: approved by Bill, 2026-11-10. Lines marked *(my call)* are the builder's choices, not
decisions Bill made.

## Why

SCCSSAR wants to give its lost-person incidents to ISRID, Robert Koester's International Search &
Rescue Incident Database, every quarter. The aim agreed with Leadership on 2026-11-09 is to publish
through D4H's Lost Behavior export first, and later through the Cal OES database. Each SCCSSAR record
that reaches ISRID improves the statistics our own range rings and planning are built on.

The D4H export alone is not good enough to send as it is:

- Some columns are blank that we can fill from records we already keep, such as Incident Type and
  Scenario.
- One column is wrong by design. D4H exports the state in County.
- Times are rounded to whole hours.
- The free-text Comment holds detail we must not send: place names and the names of third parties.

The skill closes that gap. It also keeps an exact archive of every file sent.

## Goals

1. Begin from the CSV that D4H exports from the Lost Behavior tab.
2. Fill a blank column with a value we hold, from D4H, CalTopo, Slack or the after-action report,
   when ISRID defines that element. When D4H holds a value that the after-action report shows is
   wrong, list both and let Bill choose which is sent (Bill, 2026-11-10). This applies to every ISRID element, not only the 13 Minimum
   fields.
3. Put ISRID elements that have no CSV column into the Comment field.
4. Write a de-identified copy under a unique, dated name, so the archive shows exactly what was
   sent.
5. Report an internal quality metric: the percentage of records in the batch that meet all 13
   Minimum fields. Give it twice, once for the export as D4H produced it and once for the file we
   send, so the gain from enrichment is visible. The metric goes in the Leadership summary and the
   manifest, and never in the file we send.

## Non-goals

- **A scheduled, unattended run.** D4H has no API route for the Lost Behavior export. Spike 29
  checked this on 2026-11-10 against spec 7.6.1, using invented-route and invented-value controls.
  D4H's help article 494 also says delivery cannot be automated. A person downloads the CSV. A
  scheduled task can come later, as a reminder that hands off to this skill.
- **Sending the file.** Bill emails the de-identified CSV to Koester himself, from his SAR account,
  after Leadership has reviewed it. The skill never sends, uploads or shares anything.
- **Writing to D4H.** The skill reads D4H and never changes it. A value it fills in the CSV is also
  listed as a correction for a person to make in D4H, because D4H stays the system of record.
- **The Cal OES submission.** That comes later, from the same archive.
- **Shared storage.** Files stay in gitignored `research/` for now. Bill copies them to the
  Leadership Google Drive by hand.

## Anti-goals (it fails if it does any of these, even when it technically works)

- It sends anything that identifies a subject, reporting party, witness or responder.
- It sends the agency's Event # or the D4H Mission # text, either of which links to a police
  record.
- It fills a cell by inference with nothing behind it. Every filled cell names its source, and Bill
  approves it.
- It overwrites an earlier batch.
- It puts PII in the public repo, including test fixtures.

## The workflow

1. **Bill downloads** the export from D4H: Operations > Incidents > Lost Behavior. Unapproved
   incidents (drafts) are left out of the export, so approve every incident before downloading.
2. **Bill runs the skill**, giving it the CSV path. The batch is named after the quarter the run
   happens in, for example `2026-Q4`.
3. **Stage 1, by script.** The script:
   - copies the raw file into the batch folder and records its sha256;
   - selects every row whose D4H number is not in an earlier batch's `SENT.md` (Bill, 2026-11-10).
     A batch is "approved and not yet sent", not "dated in the quarter". An incident approved late
     lands in the next batch, and the first batch covers the whole 2026 backlog;
   - flags each row as in-county, mutual aid or test.

   County cannot be used for the flag, because D4H puts the state in it. The flag comes from City
   checked against a list of Santa Clara County places, and from a blank County or City *(my call:
   a heuristic is enough, because Bill confirms every flag)*.

   Bill confirms each flag, because a mutual-aid callout can look like a typo. Only rows that are
   in-county and not tests go forward.
4. **Stage 2, enrichment, done by Claude.** For each row, Claude reads the D4H API (the incident and
   its involved person) and that incident's after-action report in `research/after-action/`.
   Slack and CalTopo values come only through the after-action report. Incidents from before
   Turbo have no after-action report, so they are enriched from D4H alone. For each blank column,
   Claude proposes a value with its source, and Bill approves or rejects each one. Where D4H and the
   after-action report disagree, Claude shows both values and their sources, and Bill picks the one
   that is sent. County is always
   set to `Santa Clara` for an in-county row. Every filled cell goes on a "fix in D4H too" list.
5. **Stage 3, the comment, done by Claude.** Claude rebuilds each Comment from structured facts,
   following the template below, and Bill approves each comment. The D4H free-text comment never
   reaches the file we send.
6. **Stage 4, by script.** The script de-identifies the rows, computes the quality metric and writes
   the batch files.
7. **Hand-off.** The skill tells Bill:
   - where the batch folder is, and that it is ready for his review;
   - that the Leadership summary is the file to review with Leadership;
   - that, after Leadership approves, he emails the `isrid-*.csv` to Koester;
   - to record the send when it is done (see Archive).

## Field map

The full element-by-element map is in `field-map.md`, inside the skill folder. In short:

- **CSV columns** that ISRID defines are filled when blank, if we hold the value.
- **ISRID elements we hold that have no column** go in the Comment:
  - outcome counts and subject status;
  - exact last-seen, notified and found times;
  - IPP type and any revised PLS;
  - investigative reason;
  - mission cause;
  - find resource;
  - incident outcome;
  - contact method;
  - destination and direction of travel, when known;
  - injuries;
  - resources (tasks, dogs, aircraft or UAS, personnel);
  - total incident time;
  - height, weight and build.
- **Elements ISRID derives** from coordinates and times are not computed here: ecoregion division,
  land cover, elevation, track offset, light.
- **Held but never sent:** the agency's Event #.

`field-map.md` derives from *ISRID Data Standards 2016*, which is licensed CC BY-NC-SA 4.0. The file
carries that attribution and licence, separate from the repo's BSD licence *(my call)*. The
edition used is the 70-page 2016 PDF that Bill holds. It is read once, to write `field-map.md`, and
is not needed when the skill runs.

## Comment template

Bill decided on 2026-11-10 to rebuild the comment from facts rather than edit D4H's free text. Each
part is one short clause, in this order, and is left out when we don't hold it *(my call on the
order and wording)*:

```
Outcome: 1 well. Last seen 2026-01-15 18:40; SAR notified 2026-01-15 21:05; found 2026-01-16 02:15.
IPP: point last seen (later sighting, not residence). Cause: <ISRID cause>. Found by: SAR ground.
Incident outcome: closed by search. Resources: 6 tasks, 1 dog, 1 UAS, 18 personnel. Subject: 5'6", 140 lb, slim.
```

Times use local time with no zone label, matching the Incident Date column. Free text never comes
from a form, Slack post or D4H note without Bill approving it. The template only ever takes values
from the ISRID option lists, plus numbers and times.

## De-identification rules

Bill agreed these rules on 2026-11-10. They follow ISRID's own policy: no PII, nothing restricted in a
limited data set under 45 CFR §164.514(e), and mission numbers removed from any public version.

| Column or element | Rule |
|---|---|
| Mission # | Replaced with the 5-digit D4H number alone. The rest of the text (agency, street) is dropped |
| Comment | Replaced with the rebuilt comment. The D4H text is kept only in the private enriched file |
| Agency Event # | Never sent |
| Names, DOB, addresses, phone numbers, clothing | Never sent. None of them has an ISRID column, and the comment template cannot carry them |
| IPP and Find coordinates | Full precision. ISRID is a restricted research database, not a public one (Bill) |
| Incident Date | Full date and time (Bill) |
| Exact times in the comment | Kept (Bill) |
| Age | Sent as a number *(my call: ISRID's Age field, already allowed in a limited data set)* |
| Height, weight, build | Sent in the comment. Low sensitivity once the rest is removed (Bill) |
| City | Sent as D4H has it |

**Every column has a rule.** The outgoing file keeps all 34 export columns, in D4H's order and with
D4H's header row, because D4H built the export for ISRID's import *(my call)*. The script holds an
allow-list of all 34 headers and refuses an export whose header differs, so a column D4H adds later
cannot pass through unreviewed. Each column falls into one of three groups:

- **Pass:** a pick-list or number with no free text. Incident Date, Incident Type, County, Subject
  Category, # Subjects, Involvement, Age, Sex, the four time columns, Distance and Direction From
  IPP, Find Feature, Outcome, Elevation Change, Population Density, the six coordinate columns, Eco
  Region, Environment, Equipment, Experience, Fitness, Scenario, Terrain.
- **Rewrite:** Mission # and Comment, as in the table above.
- **Free text, shown for review:** City and Weather. Weather is D4H free text of up to about 65
  characters. Both go out only after Bill has seen them in the Leadership summary, and only after
  they pass the forbidden-pattern check.

The hour columns keep D4H's whole-hour format (`73hrs 0m`). Exact times go in the comment instead
(Bill).

The script checks the file against these rules before writing it. If a forbidden pattern turns up in
any outgoing cell (a phone-number shape, an email, a street-address shape, the Event # pattern, or the
original Mission # text), the run stops and the file is not written.

## Quality metric

A record meets the Minimum when each of the 13 App. B fields has a value or is legitimately not
applicable. For example, find coordinates do not apply to a subject who was never found, provided the
outcome says so *(my call on what counts as not applicable)*.

The score is out of 13, not out of the 10 CSV columns those fields fall into. Two rules keep it
honest. Both were found by re-scoring the 2026-11-09 Leadership deck against its own export:

- **The four outcome counts (# well, # injured, # DOA, # no trace) are met only when the comment
  states the condition.** D4H's Outcome value `Person Assisted` does not say whether the subject was
  well or injured (Koester p. 53). The deck credited them when the record showed a find, and the
  after-action report confirmed it. That works for an internal review, but ISRID receives only the
  file, so the file itself has to state the condition.
- **A placeholder is not a value.** Sex `o` is what Turbo writes when the form gives no sex, so it is
  scored as missing unless the after-action report confirms it. The same goes for any default a
  D4H pick-list starts on, such as Eco-Region `Dry`.

The summary shows:

- the percentage meeting the Minimum, as exported and as sent;
- for each record, the fields still missing;
- which Silver fields are filled. Silver is the 12 fields of App. C, added on top of the 13. It is
  shown, not scored.

## Archive

```
research/isrid-submissions/
  2026-Q4/
    20261110-1432/                          one folder per run; the script refuses an existing folder
      raw-d4h-export.csv                    byte copy of what was downloaded
      enriched-PRIVATE.csv                  all columns, D4H comments kept, sources noted
      isrid-sccssar-2026-Q4-20261110-1432.csv   de-identified; the file Bill emails
      leadership-summary.md                 counts, quality metric, per-record gaps, the comments
      fix-in-d4h.md                         every filled cell, so D4H can be corrected
      manifest.json                         sha256 of each file, rules version, D4H numbers, changed cells
      SENT.md                               written only after Bill sends: date, to, sha256 sent
```

The file name carries the quarter and the run stamp, so a re-run never collides. `SENT.md` is the only
file added after the run, and only when Bill says the file has gone. It is also the only thing that
makes a record count as sent. Several runs in one quarter are fine (the 2026-11-10 validation run
is one): a run that is never sent has no `SENT.md`, so its records stay selectable *(my call)*. A
record that has already been sent is never selected again. Re-sending a corrected record is out of
scope until ISRID says how it wants corrections.

## How it is built

- **The skill**, at `.claude/skills/isrid-quarterly/`, holds `SKILL.md` (the steps above, including the
  interview for stages 2 and 3) and `field-map.md`.
- **The script** is plain Python using only the standard library, and does the deterministic stages:
  copy, filter, de-identify, the forbidden-pattern check, the metric, the files and the manifest. It
  lives in `backend/` so that the build's existing pytest step runs its tests *(my call: the build
  only runs `pytest backend`)*.
- **The tests** use synthetic rows only, and no real incident. Each de-identification rule gets a pin,
  and each pin is mutation-tested, following CLAUDE.md's "a pin must survive mutation" row.
- **Validation:** one run by hand against the 2026-11-09 export.

## The Turbo fallback (noted, not built)

A server-side job in Turbo hits the same limit: it can read only what the API exposes. It could
assemble the incident, the involved person and the IPP, but never the Lost Behavior tab fields (search
time, find feature, scenario, terrain), which have no API route. It would also need encrypted, access-
controlled storage for a file that holds subject data, with its own IAM, retention and security review.
It is not worth building while the export has to be downloaded by hand anyway.

## Decisions log

- Bill, 2026-11-10: coordinates at full precision; exact times in the comment; the comment is
  rebuilt, and Bill reviews it; archive in `research/` only, copied to the Leadership Drive by
  hand; height, weight and build included; every in-county, non-test record is sent, complete or
  not; % meeting the Minimum tracked internally; agency Event # never sent; Bill emails the file
  to Koester after Leadership review; batch = approved and not yet sent; conflicts corrected, with
  Bill choosing each.
- Builder's calls, marked *(my call)* above: keep all 34 columns in D4H's order; the comment order
  and wording; what counts as not applicable for the metric; Age sent as a number; the
  mutual-aid heuristic; the script in `backend/`; the licence on `field-map.md`; unsent runs stay
  selectable.
