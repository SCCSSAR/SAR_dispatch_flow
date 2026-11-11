# Quarterly ISRID Submission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code skill and a stdlib script that turn D4H's Lost Behavior export into a
de-identified, archived ISRID batch for the dispatcher to review, show the team's leadership and
email to ISRID.

> **Amended after review (2026-11-10):** every team-specific value this plan names (the county,
> its places, the cutoff date, the file-name slug, the D4H environment) moved out of the code into a
> gitignored team config. The spec's "Team config" section is authoritative; the values below that
> say `county` or `places` come from that config.

**Architecture:** One script, `backend/isrid_submission.py`, with three subcommands. `prepare`
selects the rows and writes a worklist. Claude and the dispatcher then fill `decisions.json` by hand in the
interview. `finalize` validates the decisions, de-identifies the rows, scores them and writes the
batch. `mark-sent` records the send. The skill (`.claude/skills/isrid-quarterly/SKILL.md`) runs the
interview between `prepare` and `finalize`.

**Tech Stack:** Python 3.11 standard library only (`csv`, `json`, `hashlib`, `re`, `pathlib`,
`argparse`, `datetime`). Tests run with pytest, which the build already runs in Step 0.

**Spec:** `docs/superpowers/specs/2026-11-10-isrid-quarterly-submission-design.md`

## Global Constraints

- The repo is PUBLIC. Fixtures are synthetic: no real D4H number, name, address or coordinate.
  Synthetic D4H numbers start `9`, for example `90001`.
- Script output goes only under the `--root` given, which defaults to `research/isrid-submissions`.
- Standard library only. Run as `python3 backend/isrid_submission.py <cmd>`, and import in tests as
  `from isrid_submission import ...`, the same pattern `test_egress_ip.py` uses.
- The script never writes a file that already exists, and never makes a network call.
- No PII in any `print` or log. Print counts, paths and D4H numbers only.
- D4H reads use the production environment only (the config's `d4h_env`, resolved through
  `deploy.env`). ISRID covers production records, so a non-production environment is never needed.
- `RULES_VERSION = "1"` is written into the manifest. Bump it whenever a de-identification rule
  changes.
- Rule #14 applies to every commit: chain `git checkout <branch> && git add <files> && git commit`
  in one Bash call, and run `git branch --show-current` first.

## Review Focus

1. **A D4H export with a new, renamed or reordered column.** Expected: `prepare` refuses and names
   the column. Pinned in Task 1.
2. **A Mission # that doesn't start with 5 digits**, for example a hand-entered incident. Expected:
   `finalize` refuses that row and names it, rather than sending the raw text. Pinned in Task 1.
3. **A comment the dispatcher approved that still contains a street address or phone number.** Expected:
   `finalize` stops, writes nothing and names the row and column, without printing the value.
   Pinned in Task 4.
4. **A worklist row with no decision.** Expected: `finalize` refuses and lists the D4H numbers.
   Pinned in Task 4.
5. **A run started twice in the same minute.** Expected: the second run refuses the existing
   folder. Pinned in Task 3.

---

### Task 1: Header allow-list and row-level de-identification

**Files:**
- Create: `backend/isrid_submission.py`
- Test: `backend/test_isrid_submission.py`

**Interfaces:**
- Produces:
  - `EXPORT_HEADER: tuple[str, ...]`, the 34 headers in D4H's order: `Mission #, Incident Date,
    Incident Type, County, City, Subject Category, # Subjects, Involvement, Age, Sex, Search Time,
    Lost Time, Distance From IPP, Direction From IPP, Find Feature, Outcome, Elevation Change,
    Mobility Time, Population Density, IPP Coordinates, IPP Lat, IPP Lng, Find Coordinates, Find Lat,
    Find Lng, Eco Region, Environment, Weather, Equipment, Experience, Fitness, Scenario, Terrain,
    Comment`.
  - `REVIEW_COLUMNS = ("City", "Weather")` and `REWRITE_COLUMNS = ("Mission #", "Comment")`.
  - `class SubmissionError(Exception)`. Every refusal raises it, with a message that names columns
    and D4H numbers, never values.
  - `read_export(path: Path) -> list[dict[str, str]]`. Reads `utf-8-sig` and raises
    `SubmissionError` unless the header equals `EXPORT_HEADER` exactly.
  - `d4h_number(mission: str) -> str`. Returns the leading `\d{5}` or raises `SubmissionError`.
  - `forbidden_hits(row: dict[str, str], mission_raw: str) -> list[str]`. Returns the names of the
    columns where a forbidden pattern appears.
  - `deidentify_row(row: dict[str, str], comment: str) -> dict[str, str]`. Sets `Mission #` to
    `d4h_number(...)`, `Comment` to `comment` and `County` to the config's county. Every other column is
    unchanged.

- [ ] **Step 1: Write the failing tests**

  `test_header_must_match_exactly`, parametrized over three cases (a column added at the end, two
  columns swapped, one renamed): `read_export` raises `SubmissionError`, and the message contains
  the offending header.

  `test_d4h_number`: `d4h_number("90001 2026-01-15 XXSO Example") == "90001"`.
  `d4h_number("SO-17")` raises.

  `test_deidentify_row` asserts:
  - `Mission #` becomes `"90001"`;
  - `Comment` becomes the supplied text;
  - `County` is the configured county;
  - the other 31 columns are byte-equal to the input.

  `test_forbidden_hits` is parametrized over each pattern. A synthetic value placed in `Comment`
  returns `["Comment"]` for each of:
  - a phone number, `408-555-0142`;
  - an email, `a@b.org`;
  - a street address, `123 Example Ave`;
  - an agency Event #, `26-001-071`;
  - the raw Mission # remainder `XXSO Example`, when `mission_raw` is `"90001 2026-01-15 XXSO
    Example"`.

  A clean row returns `[]`. So does a row whose only digit groups are its Incident Date and
  coordinates.

- [ ] **Step 2: Run it and watch it fail**

  Run: `python3 -m pytest -q backend/test_isrid_submission.py`. Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement the Interfaces above**

  The patterns:
  - phone: `\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b`;
  - email: `[\w.+-]+@[\w-]+\.[\w.]+`;
  - street address: a house number, then 1–4 capitalized words, then a street type taken from a
    fixed tuple (St, Street, Ave, Avenue, Rd, Road, Dr, Drive, Way, Ct, Court, Blvd, Ln, Lane, Pl,
    Cir, Pkwy, Hwy);
  - Event #: `\b\d{2}-\d{3}-\d{3,4}\b`;
  - the Mission # remainder: any word of 4 or more letters left after the leading number and date,
    matched case-insensitively as a whole word.

  `forbidden_hits` checks every column except the six coordinate columns and `Incident Date`.

- [ ] **Step 4: Run it and watch it pass.** Run the same command. Expected: all pass.

- [ ] **Step 5: Mutation-check each pin.** Make each of these changes by hand and confirm a test goes
  red, then restore it:
  - delete the header comparison;
  - return `mission` unchanged from `d4h_number`;
  - drop the `County` assignment;
  - remove each pattern in turn.

  Record "N mutations, 0 leaks" for the PR body.

- [ ] **Step 6: Commit.** `feat: ISRID export header allow-list and row de-identification`.

---

### Task 2: Minimum and Silver scoring

**Files:** Modify `backend/isrid_submission.py`, `backend/test_isrid_submission.py`.

**Interfaces:**
- Consumes: the row dicts from Task 1.
- Produces:
  - `states_condition(comment: str) -> bool`. Matches `well|injured|injury|DOA|deceased|dead|no
    trace` as whole words, case-insensitively.
  - `score_minimum(row, comment: str) -> tuple[int, list[str]]`. Returns the count met out of 13 and
    the names of the missing fields.
  - `score_silver(row, comment: str, confirmed: set[str]) -> tuple[int, list[str]]`. Returns a count
    out of 25 that includes the Minimum.

The 13 Minimum names, with how each one is met:

| Name | Met when |
|---|---|
| `Incident Type` | column non-blank |
| `SAR notified` | `Incident Date` non-blank |
| `Found date/time` | `Search Time` non-blank, or `no trace` in the comment |
| `Last seen date/time` | `Lost Time` non-blank |
| `Subject Category` | column non-blank |
| `IPP coordinates` | both `IPP Lat` and `IPP Lng` non-blank |
| `Find coordinates` | both `Find Lat` and `Find Lng` non-blank, or `no trace` in the comment |
| `Scenario` | column non-blank |
| `# well`, `# injured`, `# DOA`, `# no trace` | `states_condition(comment)` (all four together) |
| `Comment` | `comment.strip()` non-blank |

"Blank" means `""`, whitespace, or `-`.

The 12 Silver additions:

| Name | Met when |
|---|---|
| `Environment`, `Population Density`, `Terrain`, `Age`, `Fitness`, `Experience` | column non-blank |
| `Mobility` | `Mobility Time` non-blank |
| `Sex` | non-blank, and not `o` unless `"Sex"` is in `confirmed` |
| `Subject Status` | `states_condition(comment)` |
| `Destination` | `Destination` in the comment |
| `Direction of Travel` | `Direction of travel` in the comment |
| `Track offset` | never (ISRID derives it). Listed as `Track offset (ISRID derives)` |

- [ ] **Step 1: Write the failing tests.**

  `test_complete_row_scores_13`: a synthetic complete row with comment `"Outcome: 1 well."` scores
  `(13, [])`.

  `test_person_assisted_without_condition_loses_four`: the same row with comment `""` scores
  `(8, [...])`. The missing list contains exactly the four counts and `Comment`.

  `test_no_trace_excuses_find_fields`: blank `Search Time` and find coordinates, with comment
  `"Outcome: 1 no trace."`, still scores 13.

  `test_dash_is_blank`: `-` in `Scenario` counts as missing.

  `test_silver_sex_placeholder`: on a complete row with all Silver columns filled:
  - Sex `o` scores 21 and lists `Sex`;
  - with `confirmed={"Sex"}` it scores 22;
  - Sex `f` scores 22.

  `test_silver_matches_deck_count`: a row shaped like a typical complete record
  (Mobility blank, no destination or direction of travel) scores 21 of 25.

- [ ] **Step 2: Run it and watch it fail.**

- [ ] **Step 3: Implement the three functions from the tables above.**

- [ ] **Step 4: Run it and watch it pass.**

- [ ] **Step 5: Mutation-check each pin.**
  - Make `states_condition` return True.
  - Drop the `no trace` excuse.
  - Count `o` as filled.
  - Treat `-` as a value.

- [ ] **Step 6: Commit.** `feat: score ISRID rows against Minimum (13) and Silver (25)`.

---

### Task 3: `prepare`: select rows and open the batch

**Files:** Modify `backend/isrid_submission.py`, `backend/test_isrid_submission.py`.

**Interfaces:**
- Consumes: `read_export` and `d4h_number`.
- Produces:
  - The county's places, from the team config's `places` list, compared casefolded.
  - `quarter_label(now: datetime) -> str`, for example `"2026-Q4"`.
  - `sent_numbers(root: Path) -> set[str]`. Reads every `root/*/*/SENT.md`, taking the line
    starting `D4H numbers:`, comma-separated.
  - `flag_row(row, places) -> str`. Returns `"in-county"` when City is in `places`, `"check"` when City
    or County is blank, and `"mutual-aid?"` otherwise.
  - `prepare(export: Path, root: Path, now: datetime) -> Path`. It:
    - creates `root/<quarter>/<YYYYMMDD-HHMM>/`, raising if the folder exists;
    - copies the export byte for byte to `raw-d4h-export.csv`;
    - writes `worklist.json` and returns the folder.

  `worklist.json` holds:

  ```json
  {"rules_version": "1", "raw_sha256": "...", "quarter": "2026-Q4", "run": "20261110-1432",
   "rows": [{"d4h": "90001", "incident_date": "...", "flag": "in-county", "minimum_as_exported": 13}],
   "already_sent": ["90000"]}
  ```

  Rows whose D4H number is in `sent_numbers` go into `already_sent`, not `rows`. A row whose
  Mission # fails `d4h_number` goes into `rows` with `d4h: null`, its line number, and flag
  `"bad-mission-number"`. The worklist holds no City, Comment or other free text.

- CLI: `python3 backend/isrid_submission.py prepare <export.csv> [--root DIR]`. Prints the folder
  and the counts for each flag.

- [ ] **Step 1: Write the failing tests** (all on `tmp_path`):
  - `test_prepare_copies_raw_bytes`: the sha256 of the copy equals the source's, and equals
    `raw_sha256`.
  - `test_prepare_refuses_existing_folder`: a second call with the same `now` raises.
  - `test_prepare_skips_sent`: a planted `SENT.md` from an earlier batch moves `90001` into
    `already_sent`.
  - `test_flags`: parametrized over a listed place, an unlisted one, and a blank City.
  - `test_worklist_holds_no_free_text`: the worklist JSON string does not contain the fixture's City
    or Comment values.
  - `test_quarter_label`: Nov 10 → `2026-Q4`, Jan 1 → `2027-Q1`.

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Implement the Interfaces above.**

- [ ] **Step 4: Run them and watch them pass.**

- [ ] **Step 5: Mutation-check each pin.**
  - Skip the existence check.
  - Ignore `SENT.md`.
  - Put City into the worklist.

- [ ] **Step 6: Commit.** `feat: isrid_submission prepare selects unsent rows into a batch`.

---

### Task 4: `finalize`: apply decisions and write the batch

**Files:** Modify `backend/isrid_submission.py`, `backend/test_isrid_submission.py`.

**Interfaces:**
- Consumes: everything above.
- Produces: `finalize(batch: Path) -> None`. It reads `raw-d4h-export.csv`, `worklist.json` and
  `decisions.json`.

  `decisions.json` is written by Claude in the interview:

  ```json
  {"rows": {"90001": {"include": true,
                      "cells": {"Incident Type": {"value": "Search", "source": "AAR report"}},
                      "comment": "Outcome: 1 well. ...",
                      "confirmed": ["Sex"]},
            "90002": {"include": false, "reason": "mutual aid"}}}
  ```

  It refuses, writing nothing, if any of these is true:
  1. A worklist row has no decision.
  2. An included row has no `comment`.
  3. An included row has `d4h: null`.
  4. A `cells` key is not in `EXPORT_HEADER`, or is one of `Mission #`, `Comment` or `County`.
  5. A cell has no `source`.
  6. `forbidden_hits` is non-empty for any outgoing row. The message names the D4H number and the
     column.
  7. Any output file already exists.
  8. The raw file's sha256 no longer matches `raw_sha256`.

  On success it writes, in this order:
  - `enriched-PRIVATE.csv`: the raw row with the cells applied, plus `Comment (D4H)`, `Comment
    (sent)` and `Sources` columns;
  - `isrid-<team_slug>-<quarter>-<run>.csv`: the `deidentify_row` output, in `EXPORT_HEADER` order,
    UTF-8 with a BOM, matching D4H;
  - `fix-in-d4h.md`: one line per applied cell (D4H number, column, value, source);
  - `leadership-summary.md`:
    - the counts included and excluded, with each exclusion's reason;
    - `% meeting Minimum` as exported and as sent;
    - a table with one row per included record: D4H number, date, Minimum n/13 and missing,
      Silver n/25, City, Weather, and the comment sent;
  - `manifest.json`: `rules_version`, the sha256 of every file above plus the raw file, the
    `included` and `excluded` D4H numbers, the applied cells (number, column, value, source), and
    both percentages.

  The first applied cell's `was` value is taken from the raw file and kept in the enriched file
  only.

- CLI: `python3 backend/isrid_submission.py finalize <batch-dir>`. Prints the paths and both
  percentages.

- [ ] **Step 1: Write the failing tests** (each one builds a batch with `prepare` first):
  - `test_refuses_missing_decision` (rule 1);
  - `test_refuses_cell_without_source` (rule 5);
  - `test_refuses_rewrite_column_in_cells`, parametrized over `Mission #`, `Comment` and `County`
    (rule 4);
  - `test_forbidden_comment_writes_nothing`: the comment holds `123 Example Ave`, `finalize`
    raises, and the folder holds only `raw-d4h-export.csv`, `worklist.json` and `decisions.json`.
    The exception text does not contain `Example Ave`;
  - `test_refuses_tampered_raw` (rule 8);
  - `test_outgoing_file`: the header equals `EXPORT_HEADER`; the file holds only included rows;
    Mission # holds only the number; County reads the configured county; an applied cell appears;
  - `test_outgoing_has_no_d4h_comment`: the raw Comment's text appears in the enriched file and not
    in the outgoing one;
  - `test_metric_before_and_after`: a fixture where enrichment adds Incident Type and a comment
    shows 0% as exported and 100% as sent;
  - `test_manifest_hashes`: every sha256 in the manifest matches the file on disk.

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Implement `finalize`.** Build every output in memory, run all eight checks, and only
  then write the files.

- [ ] **Step 4: Run them and watch them pass.**

- [ ] **Step 5: Mutation-check each pin.**
  - Skip the forbidden check.
  - Write the raw comment out.
  - Drop rule 1.
  - Skip the sha256 re-check.
  - Write files before the checks.

- [ ] **Step 6: Commit.** `feat: isrid_submission finalize writes a de-identified batch`.

---

### Task 5: `mark-sent`

**Files:** Modify `backend/isrid_submission.py`, `backend/test_isrid_submission.py`.

**Interfaces:**
- Produces: `mark_sent(batch: Path, sent_on: date, to: str) -> Path`. It writes `SENT.md` with:
  - `Sent on: <date>`;
  - `To: <to>`;
  - `File: <outgoing name>`;
  - `sha256: <from the manifest>`;
  - `D4H numbers: <included, comma-separated>`.

  It refuses if `SENT.md` exists, if the manifest is missing, or if the outgoing file's sha256 no
  longer matches the manifest.

- CLI: `python3 backend/isrid_submission.py mark-sent <batch-dir> --to <address> [--on YYYY-MM-DD]`.

- [ ] **Step 1: Write the failing tests.**
  - `test_mark_sent_round_trip`: after `mark_sent`, a new `prepare` on the same export puts those
    numbers in `already_sent`.
  - `test_mark_sent_refuses_edited_file`.
  - `test_mark_sent_refuses_twice`.

- [ ] **Steps 2–4: Watch them fail, implement, then watch them pass.**

- [ ] **Step 5: Mutation-check each pin.**
  - Skip the sha256 check.
  - Write `D4H numbers:` empty.

- [ ] **Step 6: Commit.** `feat: isrid_submission mark-sent records what went to ISRID`.

---

### Task 6: The skill and the field map

**Files:**
- Create: `.claude/skills/isrid-quarterly/SKILL.md`
- Create: `.claude/skills/isrid-quarterly/field-map.md`
- Add: the spec and this plan, under `docs/superpowers/`.

`field-map.md` is the field map, rewritten from the draft at
`research/isrid-submissions/field-map-draft.md`. Rewriting it:
- strips every incident number and date;
- adds the attribution header: *ISRID Data Standards 2016*, R. J. Koester, dbS Productions, used
  under CC BY-NC-SA 4.0, adapted; this file carries the same licence.

`SKILL.md` front matter:
- `name: isrid-quarterly`;
- a description that triggers on "ISRID", "quarterly ISRID", "send to ISRID", "LPB export" and
  "Lost Behavior export".

Its body, in order:

1. **Scope and hard rules.**
   - It never sends, uploads or writes to D4H.
   - PII stays in `research/`.
   - Every filled cell has a source, and the dispatcher approves it.
2. **Download.** The D4H menu path, plus a reminder to approve drafts first.
3. **Run `prepare`.** Read the flag counts back to the dispatcher. They confirm or override each flag that
   isn't `in-county`, and records exclusions with a reason.
4. **Enrich each included row.**
   - Read the D4H incident and `incident-involved-persons?incident_id=` with the PAT recipe from
     the AAR skill.
   - Find the AAR folder with `grep -l <d4h> research/after-action/*/report.md`.
   - For each blank or conflicting column, show the D4H value, the proposed value and the source.
   - Write the approved cells into `decisions.json`.
   - Rows from before Turbo are enriched from D4H only.
5. **Comment.** Build it from the template in the spec. Show each one, and write it only once the
   dispatcher approves it.
6. **Run `finalize`.** On a refusal, read the named check back to the dispatcher. Never edit the raw file.
7. **Hand-off message.** It names:
   - the batch folder;
   - `leadership-summary.md`, for the team's leadership to review;
   - the `isrid-*.csv`, which the dispatcher emails to ISRID;
   - `fix-in-d4h.md`;
   - the `mark-sent` command to run after sending.

The whole body runs under 200 lines. It links the spec rather than repeating it.

- [ ] **Step 1: Write both files.**
- [ ] **Step 2: Check them.** `git diff --cached` on these files holds no 5-digit D4H number from a
  real incident, and no name, address or coordinate.
- [ ] **Step 3: Commit.** `docs: isrid-quarterly skill, field map, spec and plan`.

---

### Task 7: Full suite, review, PR

- [ ] **Step 1: Run the build's test step.** Run: `python3 -m pytest -q backend/`. Expected: the
  same pass count as main, plus the new tests.
- [ ] **Step 2: Review.** Run `/code-review` or the `feature-dev:code-reviewer` agent against the
  branch, then address what it finds.
- [ ] **Step 3: Push and open a PR against `main`.**
  - Put the mutation totals from each task in the body.
  - Use no closing keyword next to a `#N`.
  - Say plainly that validation against a real export happens after this.
- [ ] **Step 4: Hand-validate.** Run the skill against the team's current export (kept in
  gitignored `research/`) from the branch checkout.
  - The dispatcher makes the decisions.
  - The batch lands in `research/isrid-submissions/2026-Q4/`.
  - Do not run `mark-sent`, because nothing is sent.
  - Report the two percentages, and any check that refused.
