"""
apply_helpers.py — Re-run pure post-processing helpers against cached
Gemini outputs. Cost: $0 — no Gemini calls.

Use when shipping a new pure-Python helper that operates on the OCR summary
text (e.g. _rewrite_dob_age_hint added in PR #402). Validates the helper
against the real-world corpus before deploy, surfacing format variants the
synthetic test cases missed.

Usage:
    # Apply the age-from-DOB helper to every cached genai run
    python3 -m backend.migration_validation.apply_helpers \\
        --label genai --helper age_from_dob

    # Same, but only forms whose name contains "IMG"
    python3 -m backend.migration_validation.apply_helpers \\
        --label genai --helper age_from_dob --filter IMG

    # Pin the date the helper sees as "today" (default: actual today)
    python3 -m backend.migration_validation.apply_helpers \\
        --label genai --helper age_from_dob --today 2026-05-09

    # Point at a corpus root outside the default location (useful when
    # running from a worktree that doesn't have the cached outputs)
    python3 -m backend.migration_validation.apply_helpers \\
        --label genai --helper age_from_dob \\
        --corpus-root /Users/billburns/src/SAR_dispatch_flow/.claude/worktrees/distracted-goldstine-6c8da4/experiments

PRIVACY: cached outputs contain real OCR data. Diffs printed to stdout
include MP names, ages, addresses. Do not redirect stdout into a tracked
file or paste output into PRs verbatim.

HELPERS REGISTRY: extend the HELPERS dict to add more pure-text helpers
as they ship. Each helper takes (summary_text, today_date) and returns
(new_summary_text, change_description_or_None). Mirror the helper logic
from main.py — same pattern as test_main_regression.py — to avoid
importing main.py (which pulls heavyweight GCP deps).
"""

import argparse
import datetime
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS_ROOT = REPO_ROOT / "experiments"


# ---------------------------------------------------------------------------
# Mirrored helpers from main.py — must stay in sync.
# (Same mirror pattern as backend/test_main_regression.py.)
# ---------------------------------------------------------------------------

# Verbatim mirror of main.py::_DOB_AGE_HINT_RE — see the Locked Decision on the
# defensive age-from-DOB recompute. Pinned by TestDobAgeHintRegexParity.
_DOB_AGE_HINT_RE = re.compile(
    r"\((\d{1,3})(?:\s+(?:years?\s+old|yrs?\s+old|y\.?o\.?|y/o))?\)",
    re.IGNORECASE,
)
_DOB_LINE_RE = re.compile(r"^DOB:\s*(.+)$", re.MULTILINE)
_DOB_FORMATS = (
    "%m/%d/%Y", "%m/%d/%y",
    "%m-%d-%Y", "%m-%d-%y",  # hyphen separator — common in handwritten forms (corpus)
    "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y",
)


def _compute_age_from_dob(dob_text, today):
    if not dob_text:
        return None
    candidate = dob_text.split("(", 1)[0].strip()
    if not candidate:
        return None
    parsed = None
    used_2digit_year = False
    # Try-cascade: ValueError per-format is expected (most formats won't match
    # any given input). All-formats-failed is handled explicitly via parsed=None.
    for fmt in _DOB_FORMATS:
        try:
            parsed = datetime.datetime.strptime(candidate, fmt).date()
            used_2digit_year = fmt in ("%m/%d/%y", "%m-%d-%y")
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    if parsed.year > today.year:
        if not used_2digit_year:
            return None
        try:
            parsed = parsed.replace(year=parsed.year - 100)
        except ValueError:
            return None
    if parsed > today:
        return None
    age = today.year - parsed.year
    if (today.month, today.day) < (parsed.month, parsed.day):
        age -= 1
    return age if age >= 0 else None


def _rewrite_dob_age_hint(summary, today):
    dob_match = _DOB_LINE_RE.search(summary)
    if not dob_match:
        return summary, None
    dob_line = dob_match.group(1)
    hint_match = _DOB_AGE_HINT_RE.search(dob_line)
    if not hint_match:
        return summary, None
    try:
        old_age = int(hint_match.group(1))
    except ValueError:
        return summary, None
    new_age = _compute_age_from_dob(dob_line, today)
    if new_age is None or new_age == old_age:
        return summary, None
    new_hint = f"({new_age} years old)"
    abs_hint_start = dob_match.start(1) + hint_match.start()
    abs_hint_end = dob_match.start(1) + hint_match.end()
    new_summary = summary[:abs_hint_start] + new_hint + summary[abs_hint_end:]
    return new_summary, (old_age, new_age)


# ---------------------------------------------------------------------------
# Helper registry. Each entry:
#   key  → CLI name
#   fn   → function (summary_text, today_date) → (new_text, change|None)
#          where `change` is a small tuple/dict the harness can format
#   desc → one-line description for --list-helpers / errors
# ---------------------------------------------------------------------------

def _apply_age_from_dob(text, today):
    """Wraps _rewrite_dob_age_hint; returns (new_text, (old, new)|None)."""
    return _rewrite_dob_age_hint(text, today)


# --- issue #675 -------------------------------------------------------------
# Mirror of main.py::_LPB_UNANSWERED_RE / _unanswered_lpb_note. Pinned by
# TestUnansweredLpbNote in test_main_regression.py, which reads BOTH this file
# and main.py so the two cannot drift.
#
# This helper is a DETECTOR, not a rewriter: it returns the text unchanged and
# reports the Event Log line it would emit as the `change` payload. That fits
# the registry contract as-is and makes the corpus run answer the question that
# actually matters — how often does this fire, and on which questions.

_LPB_UNANSWERED_RE = re.compile(
    r"^(Q\d+) - NOT ANSWERED \(flag for follow-up\) - (.+)$",
    re.MULTILINE,
)


def _unanswered_lpb_note(summary):
    """Mirror of main.py::_unanswered_lpb_note."""
    if not summary:
        return None
    rows = _LPB_UNANSWERED_RE.findall(summary)
    if not rows:
        return None
    items = []
    for qnum, label in rows:
        clean = label.split(" — ")[0].split("?")[0].strip().rstrip(".")
        items.append(f"{qnum} ({clean})" if clean else qnum)
    noun = "item" if len(items) == 1 else "items"
    return (
        f"WARNING: {len(items)} questionnaire {noun} left blank on the form: "
        f"{', '.join(items)} — cannot be answered from the form; "
        f"verify with officer if it affects the search."
    )


def _apply_unanswered_lpb(text, today):
    """Detector: returns (text, event_log_line|None). Never rewrites."""
    return text, _unanswered_lpb_note(text)


_OFFICER_OVERRIDE_LABEL = "Officer-designated staging location"
_DISPATCHER_OVERRIDE_LABEL = "Dispatcher-specified staging location"


def _staging_line_dedup_key(body):
    """Mirror of main.py::_staging_line_dedup_key."""
    loc_part = body.split(" — ")[0].strip()
    return re.sub(r"\s+", " ", loc_part.split(",")[0].lower()).strip()


def _apply_staging_dedup(text, today):
    """Mirror of the PASS 2 rendered-staging dedup in main.py.

    ⚠️ THIS CORPUS IS NOT A FALSE-POSITIVE CHECK. run_corpus.py calls Gemini
    with `staging_candidates=[]` (lines 121 and 151), so all 120 cached runs
    ARE the zero-candidate condition the filter exists for. There are no
    candidate-backed entries here to falsely drop, and a low drop count would
    have meant nothing. Read how an artefact was generated before reading what
    it says.

    What it does measure is the bug, at a scale the live incidents could not:
    24 of the 69 runs carrying a staging list — 34% — contain at least one
    same-address duplicate (17 runs drop 1, six drop 2, one drops 3). Median
    list length is unchanged at 6 and exactly one run falls below three
    entries (3 → 2).

    The false-positive argument is structural, not empirical: a real list has
    already passed _rank_dedupe_cap_staging, which collapses same-name and
    same-house+street candidates BEFORE they reach Gemini. So no two entries in
    a candidate-backed list can share this key unless Gemini invented one. The
    one legitimate collision is an officer entry sharing an address with a
    candidate — which the prompt already asks Gemini to render as a single
    merged entry, so collapsing it is the intended outcome.
    """
    start = text.find("\nStaging Area Recommendations:\n")
    end = text.find("\n---\n", start + 1) if start != -1 else -1
    if start == -1 or end == -1:
        return text, None
    section = text[start + 1:end]
    seen, dropped, out, next_num = set(), [], [], 1
    for line in section.splitlines():
        m = re.match(r"^(\d+)\.\s+(.+)$", line)
        if not m:
            out.append(line)
            continue
        body = m.group(2)
        key = _staging_line_dedup_key(body)
        is_override = (
            _OFFICER_OVERRIDE_LABEL in body or _DISPATCHER_OVERRIDE_LABEL in body
        )
        if key and not is_override and key in seen:
            dropped.append(f"{key!r} :: {body[:90]}")
            continue
        if key:
            seen.add(key)
        out.append(f"{next_num}. {body}")
        next_num += 1
    if not dropped:
        return text, None
    # Tuple, not list: the cross-run consistency report does `set(changes)` over
    # every change payload, so a mutable payload raises TypeError the moment the
    # helper fires — after the "Changes:" block has already printed, which makes
    # the report look complete. Caught by Aikido on PR #699; the corpus run I
    # took the numbers from had crashed on exactly this line.
    return text[:start + 1] + "\n".join(out) + text[end:], tuple(dropped)


HELPERS = {
    "staging_dedup": {
        "fn": _apply_staging_dedup,
        "desc": "Drop duplicate rendered staging entries sharing a house+street "
                "(or park name), keeping officer/dispatcher entries. NOTE: this "
                "corpus is generated with staging_candidates=[], so it measures "
                "the bug (34% of runs), not false positives — see the docstring.",
    },
    "age_from_dob": {
        "fn": _apply_age_from_dob,
        "desc": "Recompute Gemini's parenthesized age hint on the DOB line "
                "(PR #402, _rewrite_dob_age_hint).",
    },
    "unanswered_lpb": {
        "fn": _apply_unanswered_lpb,
        "desc": "Report the Event Log line naming questionnaire items the form "
                "left blank (issue #675, _unanswered_lpb_note). Detector — "
                "never rewrites the summary.",
    },
}


# ---------------------------------------------------------------------------
# Corpus walk + per-output apply + reporting.
# ---------------------------------------------------------------------------

def _has_dob_line(text):
    return bool(_DOB_LINE_RE.search(text))


def _has_age_hint(text):
    m = _DOB_LINE_RE.search(text)
    return bool(m and _DOB_AGE_HINT_RE.search(m.group(1)))


def _format_change(helper_name, change):
    if helper_name == "age_from_dob" and isinstance(change, tuple):
        old, new = change
        return f"({old} years old) → ({new} years old)"
    return repr(change)


def _walk_corpus(corpus_root, label, name_filter):
    """Yield (form_dir_name, run_path) for every cached run-*.txt under the
    label's output directory."""
    label_dir = corpus_root / f"migration-output-{label}"
    if not label_dir.exists():
        raise SystemExit(
            f"ERROR: corpus directory does not exist: {label_dir}\n"
            f"       Run `python3 -m backend.migration_validation.run_corpus "
            f"--sdk {label}` to capture outputs first, or pass --corpus-root."
        )
    for form_dir in sorted(label_dir.iterdir()):
        if not form_dir.is_dir():
            continue
        if name_filter and name_filter not in form_dir.name:
            continue
        for run_path in sorted(form_dir.glob("run-*.txt")):
            yield form_dir.name, run_path


def main():
    parser = argparse.ArgumentParser(
        description="Re-run pure post-processing helpers against cached "
                    "Gemini outputs ($0, no Gemini calls).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--label", required=True,
                        help="Cache label, e.g. 'genai' or 'legacy' (selects "
                             "experiments/migration-output-{label}/).")
    parser.add_argument("--helper", action="append", default=[],
                        help="Helper name (repeatable). See --list-helpers.")
    parser.add_argument("--filter", type=str, default="",
                        help="Only run forms whose directory name contains this "
                             "substring.")
    parser.add_argument("--today", type=str, default="",
                        help="Pin 'today' for date-sensitive helpers, format "
                             "YYYY-MM-DD. Defaults to actual today.")
    parser.add_argument("--corpus-root", type=str, default="",
                        help=f"Override corpus root (default: {DEFAULT_CORPUS_ROOT}). "
                             "Useful when running from a worktree that doesn't "
                             "have the cached outputs.")
    parser.add_argument("--list-helpers", action="store_true",
                        help="List registered helpers and exit.")
    args = parser.parse_args()

    if args.list_helpers:
        for name, meta in HELPERS.items():
            print(f"  {name:20s}  {meta['desc']}")
        return 0

    if not args.helper:
        parser.error("at least one --helper is required (use --list-helpers).")
    unknown = [h for h in args.helper if h not in HELPERS]
    if unknown:
        parser.error(f"unknown helper(s): {unknown}. Use --list-helpers.")

    today = datetime.date.today()
    if args.today:
        try:
            today = datetime.datetime.strptime(args.today, "%Y-%m-%d").date()
        except ValueError:
            parser.error(f"--today must be YYYY-MM-DD, got {args.today!r}")

    corpus_root = Path(args.corpus_root) if args.corpus_root else DEFAULT_CORPUS_ROOT

    # Stats
    total_outputs = 0
    outputs_with_dob = 0
    outputs_with_hint = 0
    outputs_changed = 0
    per_form_changes = []
    forms_seen = set()

    for form_name, run_path in _walk_corpus(corpus_root, args.label, args.filter):
        total_outputs += 1
        forms_seen.add(form_name)
        text = run_path.read_text(encoding="utf-8")
        # Stats are computed on the ORIGINAL text — the helper's job is to
        # change a fraction of the hint-bearing outputs.
        if _has_dob_line(text):
            outputs_with_dob += 1
        if _has_age_hint(text):
            outputs_with_hint += 1
        # Apply helpers in order
        current = text
        any_change = False
        for h in args.helper:
            new_text, change = HELPERS[h]["fn"](current, today)
            if change is not None:
                per_form_changes.append({
                    "form": form_name,
                    "run": run_path.name,
                    "helper": h,
                    "change": change,
                })
                any_change = True
                current = new_text
        if any_change:
            outputs_changed += 1

    # Reporting
    print(f"=== apply_helpers ===")
    print(f"Label:       {args.label}")
    print(f"Corpus root: {corpus_root}")
    print(f"Helpers:     {', '.join(args.helper)}")
    print(f"Today:       {today.isoformat()}")
    if args.filter:
        print(f"Filter:      {args.filter!r}")
    print()
    print(f"Forms scanned:           {len(forms_seen)}")
    print(f"Cached outputs scanned:  {total_outputs}")
    print(f"Outputs with DOB line:   {outputs_with_dob} ({_pct(outputs_with_dob, total_outputs)})")
    print(f"Outputs with age hint:   {outputs_with_hint} ({_pct(outputs_with_hint, total_outputs)})")
    print(f"Outputs changed:         {outputs_changed} ({_pct(outputs_changed, total_outputs)})")
    print()

    if not per_form_changes:
        print("No changes (helper did not fire on any cached output).")
        return 0

    # Group by (form, helper) for compact output
    print("Changes:")
    for entry in per_form_changes:
        line = f"  {entry['form']}/{entry['run']:12s}  {entry['helper']}: {_format_change(entry['helper'], entry['change'])}"
        print(line)

    # Cross-run consistency: same form + same helper + same change across all
    # runs is a strong signal Gemini is consistently wrong on that form.
    # Mixed changes within one form mean Gemini sometimes gets it right.
    # Key by (form, helper) so multi-helper invocations don't pool changes
    # from different helpers into one bucket (Aikido PR #402 review).
    print()
    print("Cross-run consistency (forms where the helper fired on every run "
          "with the SAME correction → Gemini is consistently wrong on that form):")
    by_form_helper = {}
    for entry in per_form_changes:
        key = (entry["form"], entry["helper"])
        by_form_helper.setdefault(key, []).append(entry["change"])
    consistent = []
    for (form, helper), changes in sorted(by_form_helper.items()):
        if len(set(changes)) == 1:
            consistent.append((form, helper, changes[0], len(changes)))
    if not consistent:
        print("  (none — every fired form had mixed corrections across runs)")
    else:
        for form, helper, change, n in consistent:
            print(f"  {form:50s}  {helper:20s}  {n}× {_format_change(helper, change)}")

    return 0


def _pct(n, total):
    if not total:
        return "0%"
    return f"{100 * n // total}%"


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    raise SystemExit(main())
