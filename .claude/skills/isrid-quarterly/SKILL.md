---
name: isrid-quarterly
description: Build the team's quarterly ISRID submission from D4H's Lost Behavior export. Selects every approved, in-county, not-yet-sent incident, fills blank or wrong ISRID columns from D4H and the after-action reports (Bill approves each value), rebuilds each comment from structured facts, de-identifies the rows, scores them against the ISRID Minimum (13 fields) and Silver (25), and writes a dated batch to gitignored research/isrid-submissions/ with a Leadership summary and a fix-in-D4H list. It never sends anything. Bill emails the file to Koester himself. Trigger on "ISRID", "quarterly ISRID", "ISRID batch", "send to Koester", "LPB export", "Lost Behavior export", "publish our incidents to ISRID".
---

# Quarterly ISRID submission

**Objective:** turn D4H's Lost Behavior export into one de-identified CSV that is fit to send to ISRID,
and keep an exact archive of what was sent. The design and its reasons are in
[docs/superpowers/specs/2026-11-10-isrid-quarterly-submission-design.md](../../../docs/superpowers/specs/2026-11-10-isrid-quarterly-submission-design.md).
The element-by-element map is in [field-map.md](field-map.md).

The deterministic work (copying, selecting, de-identifying, checking, scoring, writing files) is done by
`backend/isrid_submission.py`. Claude's work is the interview between `prepare` and `finalize`: finding
values, proposing them with their source, and writing down what Bill decides.

## Hard rules

1. **Never send, upload or share anything.** Bill emails the file to Koester himself, from his SAR
   account, after Leadership has reviewed it.
2. **Never write to D4H.** Every value changed in the CSV goes on `fix-in-d4h.md` for a person to make
   in D4H, which stays the system of record.
3. **Every filled or corrected cell has a source, and Bill approves it.** Never infer a value with
   nothing behind it. When D4H and the after-action report disagree, show both and their sources, and
   let Bill choose.
4. **PII stays in `research/`.** The export, the D4H reads and the batch hold subject data. Don't
   quote a name, address, phone number or free-text comment in chat beyond what Bill needs in order to
   decide. Never put one in an issue, a PR, MEMORY.md or the public repo.
5. **Production only.** Every gcloud call uses the sccssar-dev configuration from `deploy.env`. Never
   sign in to personal-dev for this.
6. **Never edit `raw-d4h-export.csv` or `worklist.json`.** `finalize` refuses a raw file that has
   changed. If the export is wrong, fix D4H, download it again and start a new run.

## Step 1 — Download the export (Bill)

1. In D4H, **approve every incident** that should go out. Drafts are left out of the export.
2. Go to **Operations › Incidents › Lost Behavior** and export the CSV. There is no API for this
   export (checked 2026-11-10 against D4H API 7.6.1, with negative controls; D4H's help article 494
   says the same).
3. Tell Claude the file's path.

## Step 2 — Prepare

```bash
python3 backend/isrid_submission.py prepare <path-to-export.csv>
```

`prepare` does the following:
- refuses an export whose header is not D4H's known 34 columns;
- opens `research/isrid-submissions/<quarter>/<YYYYMMDD-HHMM>/`;
- copies the export byte for byte;
- writes `worklist.json`.

A batch holds every row **not named in an earlier batch's `SENT.md`**, so a late approval lands in the
next batch. **Incidents dated before 2026-10-01 are never selected**: that is the commitment made to
Leadership, and `prepare` enforces it (`ELIGIBLE_FROM` in the script). Each selected row carries a
flag:

| Flag | Meaning | What to ask Bill |
|---|---|---|
| `in-county` | City is a Santa Clara County place | Nothing, unless it is a test |
| `mutual-aid?` | City is outside the county | Confirm it is mutual aid. The requesting county holds that record |
| `check` | City or County is blank | Which it is |
| `bad-mission-number` | Mission # doesn't start with a 5-digit D4H number | Usually exclude it. It has no number to send |

Read the counts back to Bill and get a decision on every row that isn't plainly in-county. **Don't
classify a row as a test or as mutual aid yourself**: a mutual-aid callout can look like a typo.

## Step 3 — Enrich each included row

For each included D4H number, gather the values.

**From D4H.** The incident, by its 5-digit reference, and its involved person:

```bash
eval "$(ENV_NAME=sccssar-dev bash -c '. ./deploy.env; printf "C=%q; P=%q\n" "$GCLOUD_CONFIG" "$PROJECT"')"
export D4H_ACCESS_TOKEN="$(gcloud secrets versions access latest --secret d4h-access-token --project "$P" --configuration="$C")"
[ -n "$D4H_ACCESS_TOKEN" ] || { echo "ABORT: no D4H token (gcloud auth expired?)"; exit 1; }
python3 - <D4H_NUMBER> <BATCH_DIR> <<'EOF'
import ast, json, os, sys, urllib.request
ref, out = sys.argv[1], sys.argv[2]
c = {t.id: n.value.value for n in ast.parse(open("backend/d4h.py").read()).body
     if isinstance(n, ast.Assign) for t in n.targets
     if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
def get(path):
    req = urllib.request.Request(f"{c['BASE_URL']}/team/{c['TEAM_ID']}/{path}",
          headers={"Authorization": "Bearer " + os.environ["D4H_ACCESS_TOKEN"], "Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))
hits = get(f"incidents?reference={ref}")["results"]
if len(hits) != 1:
    sys.exit(f"ABORT: {len(hits)} incidents with reference {ref}")
inc = hits[0]
inv = get(f"incident-involved-persons?incident_id={inc['id']}")
json.dump({"incident": inc, "involved": inv}, open(os.path.join(out, f"d4h-{ref}.json"), "w"), indent=2)
print(ref, "incident", inc["id"], "involved", inv.get("totalSize"), "approved", inc.get("approved"))
EOF
```

The `reference` filter was checked on 2026-11-10: an invented reference returns 0 results, and the
unfiltered list returns every incident. The involved-person record holds `cause` and `injuries`, which
the export drops.

**From the after-action report**, when there is one:

```bash
grep -l "<D4H_NUMBER>" research/after-action/*/report.md
```

Use the Step 2.5 entry sheet, the CalTopo find read and the Slack find time. Incidents from before
Turbo have no report, so enrich them from D4H alone and say so.

**Propose, then record.** For each column of [field-map.md](field-map.md) section A that is blank, or
where the report contradicts D4H, show Bill three things: D4H's value, the proposed value and its
source. Record each value Bill approves in `<batch>/decisions.json`:

```json
{"rows": {
  "<D4H number>": {"include": true,
                   "cells": {"Incident Type": {"value": "Search", "source": "AAR 2026-xx-xx report.md"}},
                   "confirmed": ["Sex"],
                   "comment": "<approved in Step 4>"},
  "<D4H number>": {"include": false, "reason": "mutual aid"},
  "line-<n>":     {"include": false, "reason": "no D4H number"}}}
```

- A row without a D4H number is keyed `line-<n>`, using the `line` from `worklist.json`. Such a row
  can only be excluded.
- `Mission #`, `Comment` and `County` can't be filled. The script rewrites them itself.
- Put `"confirmed": ["Sex"]` on a row only when Bill confirms that a Sex of `o` is really Other. Turbo
  writes `o` when the form gives no sex, so the Silver score counts it as missing until it is
  confirmed.
- Check `Eco Region`: `Dry` is the pick-list default, and Santa Clara County is `Temperate`.

## Step 4 — Rebuild each comment

The comment that goes out is built from facts, never edited from D4H's free text, which stays only in
the private enriched file. Each clause comes in this order and is left out when we don't hold it (see
field-map.md section B):

```
Outcome: 1 well. Last seen <YYYY-MM-DD HH:MM>; SAR notified <…>; found <…>.
IPP: <type> (<reason when revised>). Cause: <ISRID cause>. Found by: <resource>.
Incident outcome: <closed by …>. Resources: <n> tasks, <n> dogs, <n> UAS, <n> personnel. Subject: <height>, <weight>, <build>.
```

- **Always start with the outcome clause.** The four outcome counts score as met only when the
  comment states the condition (well, injured, DOA, no trace).
- Times are local, with no zone label.
- Use ISRID's option words, numbers and times only. Leave out names, places, streets and clothing.
- Show each comment to Bill, and write it into `decisions.json` only once he approves it.

## Step 5 — Finalize

```bash
python3 backend/isrid_submission.py finalize <batch-dir>
```

`finalize` builds everything in memory and writes nothing if any check fails:
- a row with no decision;
- an exclusion with no reason;
- an included row with no comment;
- a filled cell with no source;
- a change to the raw file;
- **a phone number, email, street address, agency Event # or event-name word in any outgoing cell.**

On a refusal, read Bill the check it names; the message never shows the value. Fix
`decisions.json`, never the raw file, then run it again.

**A comment can trip on a harmless word.** The event name (Mission #) sometimes carries a status
word that an admin added, such as `Cancelled`. The check treats every word of 4 or more letters in the
event name as identifying, so a comment saying "cancelled" is refused (first seen in the 2026-11-10
validation run). Reword the comment, for example "called off". Don't weaken the check.

## Step 6 — Hand off to Bill

Tell Bill, in this order:

1. **The batch is ready for your review:** `<batch-dir>`.
2. **Review this with Leadership:** `leadership-summary.md`. It has the counts, the percentage meeting
   all 13 Minimum fields as exported and as sent, and a row per record with its Minimum and Silver
   scores, City, Weather, and the comment that will go out.
3. **After Leadership approves, email this file to Koester from your SAR account:**
   `isrid-sccssar-<quarter>-<run>.csv`. Send only that file. `enriched-PRIVATE.csv` holds subject
   data.
4. **Make the same fixes in D4H:** `fix-in-d4h.md`.
5. **Once it has gone, record the send.** Without this, the next batch selects these records again:

   ```bash
   python3 backend/isrid_submission.py mark-sent <batch-dir> --to <address> --on <YYYY-MM-DD>
   ```

A batch that is never sent needs no clean-up. Its records stay selectable, and the next run opens a
new folder.
