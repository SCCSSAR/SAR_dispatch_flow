# Quarterly ISRID submission — design

Status: approved, 2026-11-10. "The team" is the SAR team running the skill. "The dispatcher" is whoever
on the team runs the batch. Lines marked *(builder's call)* are implementation choices, not team
decisions.

## Why

A SAR team using Dispatch Turbo and D4H wants to give its lost-person incidents to ISRID, Robert
Koester's International Search & Rescue Incident Database, every quarter: through D4H's Lost Behavior
export first, and later through state databases such as Cal OES's. Each record that reaches ISRID
improves the statistics that range rings and search planning are built on.

The D4H export alone is not good enough to send as it is:

- Some columns are blank that the team can fill from records it already keeps, such as Incident Type
  and Scenario.
- One column is wrong by design: D4H exports the state in County.
- Times are rounded to whole hours.
- The free-text Comment holds detail that must not be sent: place names and the names of third parties.

The skill closes that gap. It also keeps an exact archive of every file sent.

## Goals

1. Begin from the CSV that D4H exports from the Lost Behavior tab.
2. Fill a blank column with a value the team holds, from D4H, CalTopo, Slack or the after-action
   report, when ISRID defines that element. When D4H holds a value that the after-action report shows
   is wrong, list both and let the dispatcher choose which is sent. This applies to every ISRID
   element, not only the 13 Minimum fields.
3. Put ISRID elements that have no CSV column into the Comment field.
4. Write a de-identified copy under a unique, dated name, so the archive shows exactly what was sent.
5. Report an internal quality metric: the percentage of records in the batch that meet all 13
   Minimum fields. Give it twice, once for the export as D4H produced it and once for the file sent,
   so the gain from enrichment is visible. The metric goes in the summary and the manifest, and never
   in the file sent.
6. Keep every team-specific value out of the public code (see Team config).

## Non-goals

- **A scheduled, unattended run.** D4H has no API route for the Lost Behavior export. A spike checked
  this on 2026-11-10 against API spec 7.6.1, with invented-route and invented-value controls, and
  D4H's help article 494 says delivery cannot be automated. A person downloads the CSV. A scheduled
  task can come later, as a reminder that hands off to this skill.
- **Sending the file.** The dispatcher emails the de-identified CSV to ISRID from a team account,
  after the team's leadership has reviewed it. The skill never sends, uploads or shares anything.
- **Writing to D4H.** The skill reads D4H and never changes it. A value it fills in the CSV is also
  listed as a correction for a person to make in D4H, because D4H stays the system of record.
- **State database submissions.** These come later, from the same archive.
- **Shared storage.** Files stay in gitignored `research/`. The team copies them to its own shared
  drive by hand.

## Anti-goals (it fails if it does any of these, even when it technically works)

- It sends anything that identifies a subject, reporting party, witness or responder.
- It sends the agency's Event # or the D4H Mission # text, either of which links to a police record.
- It fills a cell by inference with nothing behind it. Every filled cell names its source, and the
  dispatcher approves it.
- It overwrites an earlier batch.
- It puts PII, or one team's specifics, in the public repo, including test fixtures.

## Team config

Every team-specific value lives in `research/isrid-submissions/config.json` (gitignored), copied
from `.claude/skills/isrid-quarterly/config.template.json`:

| Key | Use |
|---|---|
| `team_slug` | The outgoing file name, `isrid-<team_slug>-<quarter>-<run>.csv` |
| `county` | Written into County for every in-county row, because D4H exports the state there |
| `places` | Every place in the county as D4H's City column spells it; a City not on the list is flagged as possible mutual aid |
| `eligible_from` | Incidents dated before this day are never selected: the first day the team's records are good enough to publish |
| `d4h_env` | The `deploy.env` environment whose gcloud configuration reads production D4H |

The script refuses to run until every `<…>` placeholder is replaced, and copies the config into each
batch's worklist, so a batch records the rules it was built under and `finalize` uses exactly what
`prepare` used.

## The workflow

1. **The dispatcher downloads** the export from D4H: Operations > Incidents > Lost Behavior.
   Unapproved incidents (drafts) are left out of the export, so approve every incident first.
2. **The dispatcher runs the skill**, giving it the CSV path. The batch is named after the quarter
   the run happens in, for example `2026-Q4`.
3. **Stage 1, by script.** The script:
   - copies the raw file into the batch folder and records its sha256;
   - selects every row whose D4H number is not in an earlier batch's `SENT.md`. A batch is "approved
     and not yet sent", not "dated in the quarter", so an incident approved late lands in the next
     batch;
   - never selects an incident dated before the config's `eligible_from`. Earlier incidents are
     counted in the summary and never sent. A date that can't be read is not cut: it stays in front
     of the dispatcher;
   - flags each row as in-county, mutual aid or check.

   County cannot be used for the flag, because D4H puts the state in it. The flag comes from City
   checked against the config's `places`, and from a blank County or City *(builder's call: a
   heuristic is enough, because the dispatcher confirms every flag)*. A mutual-aid callout can look
   like a typo, so only rows the dispatcher confirms as in-county and not tests go forward.
4. **Stage 2, enrichment, done by Claude.** For each row, Claude reads the D4H API (the incident and
   its involved person) and that incident's after-action report in `research/after-action/`. Slack
   and CalTopo values come only through the after-action report. Incidents from before Turbo have no
   after-action report, so they are enriched from D4H alone. For each blank column Claude proposes a
   value with its source, and the dispatcher approves or rejects it. Where D4H and the after-action
   report disagree, Claude shows both values and their sources, and the dispatcher picks one. Every
   filled cell goes on a "fix in D4H too" list.
5. **Stage 3, the comment, done by Claude.** Claude rebuilds each Comment from structured facts,
   following the template below, and the dispatcher approves each one. The D4H free-text comment
   never reaches the file sent.
6. **Stage 4, by script.** The script de-identifies the rows, checks every outgoing cell, computes
   the quality metric and writes the batch files.
7. **Hand-off.** The skill tells the dispatcher:
   - where the batch folder is, and that it is ready for review;
   - that the summary is the file to review with the team's leadership;
   - that, once leadership approves, they email the `isrid-*.csv` to ISRID;
   - to record the send when it is done (see Archive).

## Field map

The full element-by-element map is in `field-map.md`, inside the skill folder. In short:

- **CSV columns** that ISRID defines are filled when blank, if the team holds the value.
- **ISRID elements the team holds that have no column** go in the Comment:
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

`field-map.md` is adapted from *ISRID Data Standards 2016* (70 pp.), which is licensed CC BY-NC-SA
4.0. The file carries that attribution and licence, separate from the repo's BSD licence *(builder's
call)*. The standard is read once, to write `field-map.md`, and is not needed when the skill runs.

## Comment template

The team decided on 2026-11-10 to rebuild the comment from facts rather than edit D4H's free text.
Each part is one short clause, in this order, and is left out when the team doesn't hold it
*(builder's call on the order and wording)*:

```
Outcome: 1 well. Last seen 2026-01-15 18:40; SAR notified 2026-01-15 21:05; found 2026-01-16 02:15.
IPP: point last seen (later sighting, not residence). Cause: <ISRID cause>. Found by: SAR ground.
Incident outcome: closed by search. Resources: 6 tasks, 1 dog, 1 UAS, 18 personnel. Subject: 5'6", 140 lb, slim.
```

The script collapses a comment to one line before it is written. Times use local time with no zone
label, matching the Incident Date column. Free text never comes from a form, Slack post or D4H note
without the dispatcher approving it. The template only ever takes values from the ISRID option lists,
plus numbers and times.

## De-identification rules

These were agreed on 2026-11-10. They follow ISRID's own policy: no PII, nothing restricted in a
limited data set under 45 CFR §164.514(e), and mission numbers removed from any public version.

| Column or element | Rule |
|---|---|
| Mission # | Replaced with the 5-digit D4H number alone. The rest of the text (agency, street) is dropped |
| Comment | Replaced with the rebuilt comment. The D4H text is kept only in the private enriched file |
| Agency Event # | Never sent |
| Names, DOB, addresses, phone numbers, clothing | Never sent. None of them has an ISRID column, and the comment template cannot carry them |
| IPP and Find coordinates | Full precision. ISRID is a restricted research database, not a public one |
| Incident Date | Full date and time |
| Exact times in the comment | Kept |
| Age | Sent as a number *(builder's call: ISRID's Age field, already allowed in a limited data set)* |
| Height, weight, build | Sent in the comment. Low sensitivity once the rest is removed |
| City | Sent as D4H has it, after the dispatcher has seen it |

**Every column has a rule.** The outgoing file keeps all 34 export columns, in D4H's order and with
D4H's header row, because D4H built the export for ISRID's import *(builder's call)*. The script holds
an allow-list of all 34 headers and refuses an export whose header differs, so a column D4H adds later
cannot pass through unreviewed. It also refuses a row with the wrong number of cells.

- **Shape-checked:** Incident Date and the six coordinate columns. Each must be a date or a
  coordinate, so free text (a mis-copied map marker description) cannot ride out in them.
- **Pass:** pick-lists and numbers. Incident Type, Subject Category, # Subjects, Involvement, Age,
  Sex, the four time columns, Distance and Direction From IPP, Find Feature, Outcome, Elevation
  Change, Population Density, Eco Region, Environment, Equipment, Experience, Fitness, Scenario,
  Terrain.
- **Rewrite:** Mission #, Comment and County.
- **Free text, shown for review:** City and Weather. Both go out only after the dispatcher has seen
  them in the summary.

The hour columns keep D4H's whole-hour format (`73hrs 0m`). Exact times go in the comment instead.

Before writing anything, the script checks every outgoing cell for a phone number (several shapes,
including local and international), an email, a street address (any case, with numbered streets),
an agency Event # shape, and, in Comment and Weather only, any word of four or more letters from the
original Mission # text. Pick-lists are not checked for event-name words, because words like `Fair`
and `Rock` are also street names. A hit stops the run; the refusal names the row and column, never
the value.

## Quality metric

A record meets the Minimum when each of the 13 App. B fields has a value or is legitimately not
applicable. For example, find coordinates do not apply to a subject who was never found, provided the
outcome says so *(builder's call on what counts as not applicable)*.

The score is out of 13, not out of the 10 CSV columns those fields fall into. Two rules keep it
honest:

- **The four outcome counts (# well, # injured, # DOA, # no trace) are met only when the comment
  states the condition.** D4H's Outcome value `Person Assisted` does not say whether the subject was
  well or injured (Koester p. 53). An internal review may credit them when the record shows a find,
  but ISRID receives only the file, so the file itself has to state the condition.
- **A placeholder is not a value.** Sex `o` is what Turbo writes when the form gives no sex, so it is
  scored as missing unless it is confirmed. The same goes for any default a D4H pick-list starts on,
  such as Eco-Region `Dry`.

The summary shows the percentage meeting the Minimum as exported and as sent, and for each record
the Minimum score and missing fields, and the Silver score out of 25 (the 13 plus App. C's 12) with
the missing Silver fields.

## Archive

```
research/isrid-submissions/
  config.json                               the team config (see above)
  2026-Q4/
    20261110-1432/                          one folder per run; the script refuses an existing folder
      raw-d4h-export.csv                    byte copy of what was downloaded
      worklist.json                         rows selected, flags, the config used
      decisions.json                        the dispatcher's approved values and comments
      enriched-PRIVATE.csv                  all columns, D4H comments kept, sources noted
      isrid-<team_slug>-2026-Q4-20261110-1432.csv   de-identified; the file that is emailed
      leadership-summary.md                 counts, quality metric, per-record gaps, the comments
      fix-in-d4h.md                         every filled cell, so D4H can be corrected
      manifest.json                         sha256 of each file, rules version, D4H numbers, changed cells
      SENT.md                               written only after the send: date, to, sha256 sent
```

The file name carries the quarter and the run stamp, so a re-run never collides. `SENT.md` is the only
file added after the run, and only once the file has gone; it is the only thing that makes a record
count as sent. Several runs in one quarter are fine: a run that is never sent has no `SENT.md`, so
its records stay selectable *(builder's call)*. A record already sent is never selected again, and
`mark-sent` refuses a batch whose records another batch already sent. Re-sending a corrected record is
out of scope until ISRID says how it wants corrections.

## How it is built

- **The skill**, at `.claude/skills/isrid-quarterly/`, holds `SKILL.md` (the steps above, including
  the interview for stages 2 and 3), `field-map.md` and `config.template.json`.
- **The script**, `backend/isrid_submission.py`, is plain Python using only the standard library. It
  does the deterministic stages: copy, filter, de-identify, the checks, the metric, the files and the
  manifest. It lives in `backend/` so that the build's existing pytest step runs its tests *(builder's
  call: the build only runs `pytest backend`)*.
- **The tests** use synthetic rows and invented places only. Each de-identification rule gets a pin,
  and each pin is mutation-tested, following CLAUDE.md's "a pin must survive mutation" row.
- **Validation:** one run by hand against a real export, with the dispatcher making every decision.

## The Turbo fallback (noted, not built)

A server-side job in Turbo hits the same limit: it can read only what the API exposes. It could
assemble the incident, the involved person and the IPP, but never the Lost Behavior tab fields (search
time, find feature, scenario, terrain), which have no API route. It would also need encrypted, access-
controlled storage for a file that holds subject data, with its own IAM, retention and security review.
It is not worth building while the export has to be downloaded by hand anyway.

## Decisions log

- Team decisions, 2026-11-10:
  - coordinates at full precision, and exact times in the comment;
  - the comment is rebuilt from facts, and the dispatcher approves each one;
  - the archive lives in `research/` only, and is copied to a shared drive by hand;
  - height, weight and build are included;
  - every in-county, non-test record is sent, complete or not, and the % meeting the Minimum is
    tracked internally;
  - the agency Event # is never sent;
  - the dispatcher emails the file after leadership review;
  - a batch = approved and not yet sent;
  - conflicts are corrected, with the dispatcher choosing each;
  - a publishing cutoff date (`eligible_from`);
  - no team-specific value in the public code.
- Builder's calls, marked *(builder's call)* above:
  - keep all 34 columns in D4H's order;
  - the comment order and wording;
  - what counts as not applicable for the metric;
  - Age sent as a number;
  - the mutual-aid heuristic;
  - the script in `backend/`;
  - the licence on `field-map.md`;
  - unsent runs stay selectable.
