---
name: monthly-recap
description: Draft one month of the team's incident recap slides for the General Team meeting. Reads the month's D4H incidents and CalTopo maps, drafts each incident's summary, highlight and lessons learned from the after-action reports and the incident's Slack channel into an editable drafts.md, then, once the presenter has edited it, checks the text for personal data and writes a new .pptx (a summary slide with a county map, and one slide per incident with its drawn map and a link to the live CalTopo map). It only ever creates new files; the presenter imports the slides into the year's deck themselves. Trigger on "monthly recap", "incident recap", "General Team slides", "team meeting slides", "recap October", "slides for the team meeting".
---

# Monthly incident recap

**Objective:** one new `.pptx` per month that the presenter corrects, adds photos to, and imports into
the year's deck. The design and its reasons are in
[docs/superpowers/specs/2026-11-10-monthly-incident-recap-design.md](../../../docs/superpowers/specs/2026-11-10-monthly-incident-recap-design.md).

The deterministic work (reading D4H and CalTopo, hours, maps, the text check, writing the file) is
`backend/monthly_recap.py` with `recap_map.py` and `recap_pptx.py`. Claude's work is Step 3: drafting
the words from the after-action reports and Slack, and Step 4: waiting for the presenter.

"The presenter" means whoever shows the recap at the team meeting. Every team-specific value
(county, its extent, in-county agency names, the deploy.env environment) lives in the team's config
file, not in this skill or the scripts.

## Hard rules

1. **Never overwrite.** `collect` writes `drafts.md`, or `drafts-2.md` when that exists; `render`
   refuses when the month's `.pptx` exists. Never delete or rewrite either to get round that: the
   presenter's edits live in them.
2. **Never touch the team's deck or Drive.** The presenter moves the file into the team's folder and
   imports its slides. Do not use a Google Drive connector: it may be signed in to a personal account,
   and incident material never crosses to one.
3. **Never change CalTopo sharing.** Maps are read with a signed GET and drawn locally.
4. **Read production only, write nothing upstream.** Every gcloud call uses the environment named by
   `d4h_env` in the team config.
5. **Slide text never carries** the subject's name, date of birth or phone, an email, the agency's
   Event #, a house-numbered address, or a responder's name. A lesson is about what happened, never
   about a person. The map pictures may show CalTopo marker titles, which carry addresses; that is the
   team's call for a closed, background-checked audience. `render` checks every text field and refuses
   on a hit.
6. **Never invent a fact.** A value with no source stays `[fill in]` for the presenter.
7. **PII stays in `research/`.** Don't quote incident detail in chat beyond what the presenter needs to
   decide something; never put it in an issue, a PR or the repo.

## Step 0 — Set up, once

1. Copy [config.template.json](config.template.json) to `research/monthly-recap/config.json`
   (gitignored) and fill in every `<…>` value. The script refuses to run until it is filled in.
2. Create the venv the `.pptx` writer needs (python-pptx is not on the system python):

   ```bash
   python3 -m venv research/monthly-recap/.venv && research/monthly-recap/.venv/bin/pip install -q -r .claude/skills/monthly-recap/requirements.txt
   ```

## Step 1 — Credentials

Read `d4h_env` from the config, then export the three read credentials from that environment:

```bash
ENV_NAME="$(python3 -c 'import json;print(json.load(open("research/monthly-recap/config.json"))["d4h_env"])')"
eval "$(ENV_NAME="$ENV_NAME" bash -c '. ./deploy.env; printf "C=%q; P=%q\n" "$GCLOUD_CONFIG" "$PROJECT"')"
sec() { gcloud secrets versions access latest --secret "$1" --project "$P" --configuration="$C"; }
export D4H_ACCESS_TOKEN="$(sec d4h-access-token)" CALTOPO_CREDENTIAL_ID="$(sec caltopo-credential-id)" \
       CALTOPO_CREDENTIAL_SECRET="$(sec caltopo-credential-secret)"
[ -n "$D4H_ACCESS_TOKEN" ] && [ -n "$CALTOPO_CREDENTIAL_SECRET" ] || echo "ABORT: credentials missing (gcloud auth expired?)"
```

If gcloud asks to reauthenticate, stop and ask the presenter to sign in; never fall back to another
environment.

## Step 2 — Collect

Ask which month (`YYYY-MM`) and whether there is a fresh D4H Lost Behavior export (Operations ›
Incidents › Lost Behavior › export). The export fills found status, distance and environment; without
it those come from the after-action reports in Step 3.

```bash
research/monthly-recap/.venv/bin/python backend/monthly_recap.py collect --month <YYYY-MM> [--lpb-export <path>]
```

It writes `research/monthly-recap/<YYYY-MM>/`: one `d4h-<number>.json` per incident, the CalTopo map
data, `names.json` (used only by the text check), and the drafts file it prints. Records named as
tests or carrying the sandbox suffix are listed under `## Excluded`, with the reason. Read that list
to the presenter: they can move a record back.

## Step 3 — Draft the words

For each incident section in the drafts file:

1. **Find its after-action report**: `research/after-action/*/report.md` whose header names the D4H
   number (`incident #<number>`) or whose folder matches the date and event name. Read the
   `## On-scene debrief notes`, the `For dispatchers and Plans:` lines in `## D4H summary`, and
   `## Find data (LPB)`.
2. **Read its Slack channel.** Search private channels, archived included, for the incident date
   (`YYYY-MM-DD`); confirm the event name matches; read the whole channel. Archived channels stay
   readable while the presenter is a member. Mutual-aid channels may have been made by hand, with a
   different name pattern.
3. **Write, replacing every `[draft]`:**
   - **Summary:** one or two plain sentences: who was missing in general terms (category, not a
     name), what the team did, how it ended.
   - **Highlight:** one sentence the team will remember: the find, a clever call, an unusual moment.
   - **Lessons learned:** up to three, each something the team can do differently next time, from the
     debrief or the channel. None to draw on → delete the bullet and write "No lessons drafted: add
     or delete" in Sources and conflicts.
   - **The disruption** (cancelled callouts only): how many had answered YES, how far people got
     (still at home, en route, at staging, briefed), and when the stand-down came.
4. **Fill facts** left as `[fill in]` only from a source: found status and distance from the
   report's Find data; environment from Turbo's Environment line. Settle a `[confirm]` mutual-aid
   guess with the presenter. Hours stay `[fill in]` until D4H is closed out; say so.
5. **Under `### Sources and conflicts`**, list where each drafted line came from (report section,
   Slack message date and time) and every disagreement between sources, with the value chosen and why.
   The precedence table is in the design document. This section is never rendered.

Keep the `## ` lines and the ```facts fences exactly as they are; the renderer reads them.

## Step 4 — The presenter reviews

Give the drafts path and stop. The presenter edits it in their own editor. Do not render until they
say they have finished. Never write to the drafts file after that point; if more drafting is needed,
run `collect` again (it writes `drafts-2.md`) or edit only what they ask for.

## Step 5 — Render

```bash
research/monthly-recap/.venv/bin/python backend/monthly_recap.py render --drafts research/monthly-recap/<YYYY-MM>/drafts.md
```

- `REFUSED: slide text must not carry these` lists incident numbers, fields and kinds (phone,
  address, subject_name…), never the values. Ask the presenter to edit those fields, then render again.
- `REFUSED: … already exists`: a deck for that month was generated before. The presenter decides
  whether to move or rename it; never delete it yourself.

OpenStreetMap tiles are cached in `research/monthly-recap/tiles/`; every picture carries the OSM
credit.

## Step 6 — Hand off

Tell the presenter:

- the `.pptx` path;
- to move it into the team's monthly-review folder in Drive and import its slides into the year's
  deck (Google Slides: File › Import slides), then add photos and corrections there;
- every `[fill in]` still on a slide, by incident;
- anything that went wrong (a map not read, an excluded record they might want back).

## Growing this skill

When a month's run needed a hand fix that a rule could have made, add the rule here and a test beside
the script, the same way the after-action skill grows.
