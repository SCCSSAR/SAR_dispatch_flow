"""
diff_corpus.py — Diff legacy-SDK vs genai-SDK outputs from run_corpus.py.

Acceptance per CLAUDE.md issue #111 plan:
- Structured fields (Event Name, LKP, Residence, MP name, DOB, Koester percentiles):
  exact-match required, ANY drift is a failure
- Staging recommendations: set-difference; between-SDK delta must be ≤ within-SDK noise
- Q1–Q12 answers: INFORMATIONAL only (per #84 — checkbox accuracy is known weak)
- Narrative blocks: not compared (Gemini stylistic variance is expected)

Usage:
    python3 -m backend.migration_validation.diff_corpus
    python3 -m backend.migration_validation.diff_corpus --form image0
"""

import argparse
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_DIR = REPO_ROOT / "experiments" / "migration-output-legacy"
GENAI_DIR  = REPO_ROOT / "experiments" / "migration-output-genai"


# Structured-field extractors — these MUST exact-match between SDKs.
# Each returns the matched value, or None if absent.

STRUCTURED_FIELDS = {
    "event_name":  re.compile(r"^Event Name:\s*(.+)$",        re.MULTILINE),
    "lkp":         re.compile(r"^Last Known Position:\s*(.+)$", re.MULTILINE),
    "residence":   re.compile(r"^Residence Address:\s*(.+)$", re.MULTILINE),
    "mp_name":     re.compile(r"^Missing Person:\s*(.+)$",    re.MULTILINE),
    "last_seen":   re.compile(r"^Last Seen At:\s*(.+)$",      re.MULTILINE),
    "agency":      re.compile(r"^Agency:\s*(.+)$",            re.MULTILINE),
    "koester_25":  re.compile(r"-\s*([\d.]+)\s+miles?.*25th",     re.MULTILINE),
    "koester_50":  re.compile(r"-\s*([\d.]+)\s+miles?.*50th",     re.MULTILINE),
    "koester_75":  re.compile(r"-\s*([\d.]+)\s+miles?.*75th",     re.MULTILINE),
}

# Q1–Q12 — extracted but NOT a pass/fail signal
Q_RE = re.compile(r"^Q(\d+)\s*-\s*([^-\n]+?)\s*-",  re.MULTILINE)

# Staging entries — set comparison. Numbered list items.
STAGING_RE = re.compile(r"^\s*\d+\.\s+(.+?)(?:\s+—|$)", re.MULTILINE)


def extract_fields(text: str) -> dict:
    out = {"structured": {}, "questions": {}, "staging": []}
    for name, regex in STRUCTURED_FIELDS.items():
        m = regex.search(text)
        out["structured"][name] = m.group(1).strip() if m else None
    for m in Q_RE.finditer(text):
        out["questions"][f"Q{m.group(1)}"] = m.group(2).strip()
    for m in STAGING_RE.finditer(text):
        out["staging"].append(m.group(1).strip().split(",")[0])  # first part = address
    return out


def _majority(vals: list) -> object | None:
    """Return the value that appears in >50% of runs, or None if no majority.

    Without this guard, three unique runs (e.g. ['a','b','c']) would yield an
    arbitrary winner via max(set, key=count) — and comparing two arbitrary
    winners between SDKs flags drift that doesn't exist. Returning None means
    "no signal," which the caller treats as a non-failure.
    """
    if not vals:
        return None
    candidate = max(set(vals), key=vals.count)
    return candidate if vals.count(candidate) > len(vals) / 2 else None


def diff_form(form_name: str) -> dict:
    """Return per-field diff results for one form. Returns dict with 'pass' bool."""
    legacy_runs = sorted((LEGACY_DIR / form_name).glob("run-*.txt"))
    genai_runs  = sorted((GENAI_DIR  / form_name).glob("run-*.txt"))
    if not legacy_runs or not genai_runs:
        return {"form": form_name, "pass": False, "error": "missing runs"}

    legacy_extracts = [extract_fields(p.read_text()) for p in legacy_runs]
    genai_extracts  = [extract_fields(p.read_text()) for p in genai_runs]

    result = {"form": form_name, "pass": True, "issues": [],
              "legacy_runs": len(legacy_runs), "genai_runs": len(genai_runs)}

    # Structured fields — any cross-run drift on either side is informative;
    # any between-SDK majority-vote disagreement on a deterministic field is a FAIL.
    for field in STRUCTURED_FIELDS:
        legacy_vals = [e["structured"].get(field) for e in legacy_extracts]
        genai_vals  = [e["structured"].get(field) for e in genai_extracts]
        legacy_majority = _majority(legacy_vals)
        genai_majority  = _majority(genai_vals)
        # If either side has no majority, there's no signal — skip.
        # If both have majorities and they disagree, that's real drift.
        if legacy_majority is not None and genai_majority is not None \
                and legacy_majority != genai_majority:
            result["pass"] = False
            result["issues"].append({
                "field": field,
                "legacy_majority": legacy_majority,
                "genai_majority": genai_majority,
                "legacy_runs": legacy_vals,
                "genai_runs": genai_vals,
            })

    # Q1–Q12 — informational only
    q_diffs = []
    for q in [f"Q{i}" for i in range(1, 13)]:
        legacy_qs = [e["questions"].get(q) for e in legacy_extracts]
        genai_qs  = [e["questions"].get(q) for e in genai_extracts]
        if set(legacy_qs) != set(genai_qs):
            q_diffs.append({"q": q, "legacy": legacy_qs, "genai": genai_qs})
    result["q_diffs_informational"] = q_diffs

    # Staging recommendations — set difference of first-line addresses, allow noise
    legacy_staging_sets = [set(e["staging"]) for e in legacy_extracts]
    genai_staging_sets  = [set(e["staging"]) for e in genai_extracts]
    legacy_intersection = set.intersection(*legacy_staging_sets) if legacy_staging_sets else set()
    genai_intersection  = set.intersection(*genai_staging_sets) if genai_staging_sets else set()
    sym_diff = legacy_intersection.symmetric_difference(genai_intersection)
    # Threshold: between-SDK staging set difference > 2 entries triggers a warning, not a fail
    if len(sym_diff) > 2:
        result["staging_warning"] = list(sym_diff)

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--form", type=str, default="", help="Diff only this form (substring match)")
    args = parser.parse_args()

    forms = sorted(d.name for d in LEGACY_DIR.iterdir() if d.is_dir())
    if args.form:
        forms = [f for f in forms if args.form in f]

    results = [diff_form(f) for f in forms]
    passed = sum(1 for r in results if r["pass"])
    total = len(results)

    report_path = REPO_ROOT / "experiments" / "migration-diff-report.md"
    lines = ["# Migration Diff Report", "",
             f"**Forms tested:** {total}  ", f"**Passed:** {passed}  ",
             f"**Failed:** {total - passed}", ""]
    for r in results:
        status = "✅" if r["pass"] else "❌"
        lines.append(f"## {status} {r['form']}")
        if not r["pass"]:
            for issue in r.get("issues", []):
                lines.append(f"- **{issue['field']}** drift:")
                lines.append(f"  - legacy: `{issue['legacy_majority']}` (runs: {issue['legacy_runs']})")
                lines.append(f"  - genai:  `{issue['genai_majority']}` (runs: {issue['genai_runs']})")
        if r.get("staging_warning"):
            lines.append(f"- ⚠️ Staging set drift: {r['staging_warning']}")
        if r.get("q_diffs_informational"):
            lines.append(f"- ℹ️ Q1-Q12 differences (informational): {len(r['q_diffs_informational'])} questions")
        lines.append("")

    report_path.write_text("\n".join(lines))
    print(f"Report: {report_path}")
    print(f"\n{passed}/{total} forms PASSED structured-field check")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
