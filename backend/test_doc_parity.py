"""
test_doc_parity.py — Lightweight doc-vs-code parity assertions.

Pins specific factual claims in docs/architecture.md against the actual
source of truth (code, build scripts, file presence). When this test
fails, EITHER the doc OR the code drifted; the failure message names
both sides of the discrepancy so a human can decide which to update.

WHY THIS EXISTS
---------------
PR #513 (2026-05-25) surfaced that docs/architecture.md had drifted
silently across several Phase 1.8 facts: D4H described as a "stub" long
after Phase 2 was live, Overpass mirror list still showing 3 entries
including the removed maps.mail.ru, Cloud Run revision retention still
quoting 2 when it had been raised to 10, rate-limit values stale, etc.
The pattern: nothing forced doc-vs-code parity the way
test_pii_log_patterns.py forces the "No PII in logs" guarantee.

DESIGN PRINCIPLES
-----------------
- Only pin FACTS (counts, identifiers, literal values, paths). Not prose.
- Each test asserts ONE narrow claim; failure message names BOTH sides
  so the human can decide which side drifted.
- Adding a new pin should take <15 lines; growing the suite is the goal.
- Doc text-matching is tolerant of formatting variation (whitespace,
  bullet styles) but strict on the literal value being asserted.

ADDING A PIN
------------
1. Find a brittle claim in docs/architecture.md (count, literal, path).
2. Find the canonical source of truth in code.
3. Write `def test_<claim>():` asserting the two match.
4. Docstring should name the doc section + code location.
5. Failure message must show both observed values.

WHAT THIS TEST IS NOT
---------------------
- It's not a doc completeness check. It pins specific brittle claims,
  not "the doc mentions everything important."
- It's not a code style check. The PII test owns that surface.
- It's not the place to test prose accuracy or grammar.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

# When pytest is invoked from the repo root (typical: `python3 -m pytest -q backend/`),
# resolve paths relative to the repo root. `__file__` lives in backend/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ARCHITECTURE_MD = _REPO_ROOT / "docs" / "architecture.md"
_BACKEND_MAIN = _REPO_ROOT / "backend" / "main.py"
_BACKEND_RATE_LIMIT = _REPO_ROOT / "backend" / "rate_limit.py"
_BACKEND_D4H = _REPO_ROOT / "backend" / "d4h.py"
_BUILD_SCCSSAR = _REPO_ROOT / "build-sccssar-dev.sh"
_BUILD_DEV = _REPO_ROOT / "build-dev.sh"


@pytest.fixture(scope="module")
def architecture_md() -> str:
    """Full text of docs/architecture.md, read once per test module."""
    return _ARCHITECTURE_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pin 1 — Overpass mirror count
# ---------------------------------------------------------------------------
# Doc claim:  docs/architecture.md §5 "Staging POI Lookup (Overpass API)"
#             — "Mirror fallback chain" lists the actual mirrors as a
#             numbered list. The doc currently asserts a 2-mirror config
#             after maps.mail.ru was removed 2026-03-16.
# Code truth: backend/main.py `_OVERPASS_MIRRORS` list literal.
# ---------------------------------------------------------------------------

def test_overpass_mirror_count_matches_doc(architecture_md: str) -> None:
    """The number of mirrors in code must match what architecture.md says."""
    # Code side: count entries in the _OVERPASS_MIRRORS list literal.
    main_py = _BACKEND_MAIN.read_text(encoding="utf-8")
    match = re.search(
        r"_OVERPASS_MIRRORS\s*=\s*\[(.*?)\]",
        main_py,
        flags=re.DOTALL,
    )
    assert match is not None, "Could not find _OVERPASS_MIRRORS list in backend/main.py"
    mirror_entries = [
        line.strip().rstrip(",").strip('"').strip("'")
        for line in match.group(1).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code_count = len(mirror_entries)

    # Doc side: find the "Mirror fallback chain" section and count numbered entries.
    # The section is a markdown numbered list. Count lines matching `^N. \``
    doc_section = re.search(
        r"Mirror fallback chain.*?(?=^---|\Z)",
        architecture_md,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert doc_section is not None, (
        "Could not find 'Mirror fallback chain' section in docs/architecture.md §5"
    )
    doc_mirror_lines = re.findall(
        r"^\d+\.\s+`[^`]+`",
        doc_section.group(0),
        flags=re.MULTILINE,
    )
    doc_count = len(doc_mirror_lines)

    assert code_count == doc_count, (
        f"Overpass mirror count drift: backend/main.py _OVERPASS_MIRRORS has "
        f"{code_count} entries ({mirror_entries}), but docs/architecture.md "
        f"§5 'Mirror fallback chain' lists {doc_count} entries "
        f"({doc_mirror_lines}). Update whichever side drifted."
    )


# ---------------------------------------------------------------------------
# Pin 2 — Rate limit default values
# ---------------------------------------------------------------------------
# Doc claim:  docs/architecture.md §10 "Rate Limiting" table — 5/min,
#             20/hr, 50/day per-user, 200/day global.
# Code truth: backend/rate_limit.py defaults via _int_env().
# ---------------------------------------------------------------------------

def test_rate_limit_defaults_match_doc(architecture_md: str) -> None:
    """Per-user rate-limit defaults in code must match architecture.md §10."""
    rate_py = _BACKEND_RATE_LIMIT.read_text(encoding="utf-8")

    # Extract each default from the `_int_env("KEY", N)` calls.
    def _extract_default(env_key: str) -> int:
        m = re.search(rf'_int_env\(\s*"{env_key}"\s*,\s*(\d+)\s*\)', rate_py)
        assert m is not None, (
            f"Could not find _int_env(\"{env_key}\", ...) default in "
            f"backend/rate_limit.py"
        )
        return int(m.group(1))

    code_values = {
        "minute":   _extract_default("OCR_RATE_LIMIT_PER_MINUTE"),
        "hour":     _extract_default("OCR_RATE_LIMIT_PER_HOUR"),
        "day":      _extract_default("OCR_RATE_LIMIT_PER_DAY"),
        "global":   _extract_default("OCR_DAILY_GLOBAL_CAP"),
    }

    # Doc side: §10 has lines like "Per minute | 5 requests | ..." and
    # "Global daily cap: 200 requests/day"
    def _doc_says(label_re: str) -> int | None:
        m = re.search(label_re, architecture_md)
        return int(m.group(1)) if m else None

    doc_values = {
        "minute":   _doc_says(r"Per minute\s*\|\s*(\d+)\s+requests?"),
        "hour":     _doc_says(r"Per hour\s*\|\s*(\d+)\s+requests?"),
        "day":      _doc_says(r"Per day\s*\|\s*(\d+)\s+requests?"),
        "global":   _doc_says(r"Global daily cap:\*\*\s*(\d+)\s+requests"),
    }

    mismatches = []
    for window, code_val in code_values.items():
        doc_val = doc_values[window]
        if doc_val is None:
            mismatches.append(
                f"  {window}: code says {code_val}, docs/architecture.md §10 "
                f"has no matching entry"
            )
        elif doc_val != code_val:
            mismatches.append(
                f"  {window}: code says {code_val}, docs/architecture.md §10 "
                f"says {doc_val}"
            )

    assert not mismatches, (
        "Rate-limit default drift between code and docs/architecture.md §10:\n"
        + "\n".join(mismatches)
        + "\nUpdate whichever side drifted."
    )


# ---------------------------------------------------------------------------
# Pin 3 — backend/d4h.py exists AND is documented in §2 file list
# ---------------------------------------------------------------------------
# Doc claim:  docs/architecture.md §2 "Backend API" file list includes
#             `backend/d4h.py` since Phase 2 went live.
# Code truth: backend/d4h.py file existence.
# ---------------------------------------------------------------------------

def test_d4h_module_present_and_documented(architecture_md: str) -> None:
    """backend/d4h.py must exist AND be mentioned in §2 file list."""
    assert _BACKEND_D4H.exists(), (
        "backend/d4h.py does not exist on disk, but docs/architecture.md "
        "describes it as a live module. Either restore d4h.py or remove the "
        "doc references."
    )
    assert "`backend/d4h.py`" in architecture_md, (
        "backend/d4h.py exists on disk but is not referenced in "
        "docs/architecture.md §2 'Backend API' file list. Add a row for it."
    )


# ---------------------------------------------------------------------------
# Pin 4 — Cloud Run revision retention count
# ---------------------------------------------------------------------------
# Doc claim:  docs/architecture.md §Artifact Retention Policies row
#             "Cloud Run revisions | N most recent revisions retained"
# Code truth: build-sccssar-dev.sh + build-dev.sh comment
#             "keeping N most recent"
# ---------------------------------------------------------------------------

def test_cloud_run_revision_retention_matches_doc(architecture_md: str) -> None:
    """Build scripts and §Artifact Retention must agree on the retention N."""
    # Code side: parse both build scripts; they must match each other AND the doc.
    def _retention_in(path: Path) -> int:
        text = path.read_text(encoding="utf-8")
        m = re.search(r"keeping\s+(\d+)\s+most recent", text)
        assert m is not None, (
            f"Could not find 'keeping N most recent' in {path.name}"
        )
        return int(m.group(1))

    sccssar_n = _retention_in(_BUILD_SCCSSAR)
    dev_n = _retention_in(_BUILD_DEV)
    assert sccssar_n == dev_n, (
        f"Build scripts disagree on revision retention: "
        f"build-sccssar-dev.sh says {sccssar_n}, build-dev.sh says {dev_n}. "
        f"They should match."
    )
    code_n = sccssar_n

    # Doc side: the §Artifact Retention table row for Cloud Run revisions.
    m = re.search(
        r"Cloud Run revisions\*?\*?\s*\|\s*(\d+)\s+most recent",
        architecture_md,
    )
    assert m is not None, (
        "Could not find 'Cloud Run revisions | N most recent' row in "
        "docs/architecture.md §Artifact Retention Policies"
    )
    doc_n = int(m.group(1))

    assert code_n == doc_n, (
        f"Cloud Run revision retention drift: build scripts keep {code_n} "
        f"most recent, but docs/architecture.md §Artifact Retention claims "
        f"{doc_n}. Update whichever side drifted."
    )


# ---------------------------------------------------------------------------
# Pin 5 — D4H Selective mode literal `fullTeam` appears in both code and docs
# ---------------------------------------------------------------------------
# Doc claim:  docs/architecture.md §8 "D4H Integration (Phase 2)" subsection
#             references the Selective mode literal `fullTeam: False`.
#             This is a CLAUDE.md Locked Decision — silently dropping it
#             would re-introduce the async-init race + duplicate
#             ATTENDING+REQUESTED rows + blank-name bug.
# Code truth: backend/d4h.py must contain the `fullTeam` literal in the
#             incident create payload.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pin 6 — Overpass per-mirror timeout literal
# ---------------------------------------------------------------------------
# Doc claim:  docs/architecture.md §Performance Considerations row
#             "Overpass POI query | ... | Ns timeout per mirror ..."
# Code truth: backend/main.py `_query_overpass_staging` uses
#             `httpx.AsyncClient(timeout=N.0)` inside the mirror loop.
# History: cut from 18s → 12s in issue #551 (2026-05-31) after a 30-day
#          log analysis showed p99 successful response was 11.51s and
#          max observed success was 11.92s — 0 successes ever reached
#          the 12-18s band, so 12s is data-supported.
# ---------------------------------------------------------------------------

def test_overpass_timeout_matches_doc(architecture_md: str) -> None:
    """Overpass per-mirror timeout in main.py must match architecture.md row."""
    main_py = _BACKEND_MAIN.read_text(encoding="utf-8")
    # Code side: find the httpx.AsyncClient(timeout=N.0) inside the
    # `for _endpoint in _OVERPASS_MIRRORS:` loop.
    mirror_loop = re.search(
        r"for _endpoint in _OVERPASS_MIRRORS:(.*?)(?=^def |\Z)",
        main_py,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert mirror_loop is not None, (
        "Could not find 'for _endpoint in _OVERPASS_MIRRORS:' loop in backend/main.py"
    )
    m = re.search(
        r"httpx\.AsyncClient\(\s*timeout\s*=\s*(\d+(?:\.\d+)?)\s*\)",
        mirror_loop.group(1),
    )
    assert m is not None, (
        "Could not find httpx.AsyncClient(timeout=...) inside the Overpass "
        "mirror loop in backend/main.py"
    )
    code_timeout = float(m.group(1))

    # Doc side: row "| Overpass POI query | ... | Ns timeout per mirror ... |"
    doc_match = re.search(
        r"Overpass POI query.*?(\d+)\s*s\s+timeout per mirror",
        architecture_md,
        flags=re.DOTALL,
    )
    assert doc_match is not None, (
        "Could not find 'Overpass POI query | ... | Ns timeout per mirror' "
        "row in docs/architecture.md §Performance Considerations"
    )
    doc_timeout = int(doc_match.group(1))

    assert int(code_timeout) == doc_timeout, (
        f"Overpass timeout drift: backend/main.py uses "
        f"httpx.AsyncClient(timeout={code_timeout}), but docs/architecture.md "
        f"§Performance Considerations says '{doc_timeout}s timeout per mirror'. "
        f"Update whichever side drifted."
    )


def test_d4h_selective_mode_literal_present_in_both(architecture_md: str) -> None:
    """The 'fullTeam' literal must appear in both backend/d4h.py and §8."""
    d4h_code = _BACKEND_D4H.read_text(encoding="utf-8")
    assert '"fullTeam"' in d4h_code or "'fullTeam'" in d4h_code, (
        "backend/d4h.py does not contain the literal 'fullTeam' — the Selective "
        "attendance mode flag is missing or renamed. This is a CLAUDE.md Locked "
        "Decision ('D4H Selective attendance mode (fullTeam: false)'). "
        "Omitting the field defaults to true server-side and re-introduces the "
        "async-init race + duplicate ATTENDING+REQUESTED rows."
    )
    assert "fullTeam" in architecture_md, (
        "docs/architecture.md does not mention the 'fullTeam' literal in the "
        "D4H Integration subsection (§8). The Selective mode rule is a CLAUDE.md "
        "Locked Decision and should be surfaced in the architecture doc too."
    )


# ---------------------------------------------------------------------------
# Pin 7 — STAGING_SOURCE allowed values (Overpass→Geoapify migration)
# ---------------------------------------------------------------------------
# Doc claim:  docs/architecture.md §5 "Staging POI Lookup (Geoapify primary →
#             Overpass fallback)" — the "Source selection" subsection names
#             `STAGING_SOURCE` and its two named values `overpass` / `geoapify`.
# Code truth: backend/main.py fail-fast guard
#             `if _STAGING_SOURCE not in ("", "overpass", "geoapify")`.
# WHY brittle: adding a third staging source (or renaming a value) must not
#             silently leave §5 describing a 2-source world.
# ---------------------------------------------------------------------------

def test_staging_source_values_match_doc(architecture_md: str) -> None:
    """Every named STAGING_SOURCE value in main.py's guard must appear in §5."""
    main_py = _BACKEND_MAIN.read_text(encoding="utf-8")
    guard = re.search(
        r"_STAGING_SOURCE\s+not in\s*\(([^)]*)\)",
        main_py,
    )
    assert guard is not None, (
        "Could not find the `_STAGING_SOURCE not in (...)` fail-fast guard in "
        "backend/main.py — the STAGING_SOURCE allowed-value contract moved."
    )
    # Named (non-empty) allowed values, e.g. {"overpass", "geoapify"}.
    code_values = {
        v for v in re.findall(r'"([^"]*)"', guard.group(1)) if v
    }
    assert code_values, "No named STAGING_SOURCE values parsed from the guard tuple."

    assert "`STAGING_SOURCE`" in architecture_md or "STAGING_SOURCE" in architecture_md, (
        "docs/architecture.md §5 does not mention the STAGING_SOURCE flag, but "
        "backend/main.py gates the staging source on it. Add the 'Source "
        "selection' subsection."
    )
    missing = [v for v in sorted(code_values) if v not in architecture_md]
    assert not missing, (
        f"STAGING_SOURCE value drift: backend/main.py allows {sorted(code_values)}, "
        f"but docs/architecture.md §5 does not mention {missing}. A source was "
        f"added/renamed in code without updating the architecture doc."
    )


# ---------------------------------------------------------------------------
# Pin 8 — STAGING_SHADOW default + allowed values (sequential fallback)
# ---------------------------------------------------------------------------
# Doc claim:  docs/architecture.md §5 "Source selection" — `STAGING_SHADOW`
#             defaults to `on` and accepts `on`/`off`; `off` is the sequential
#             fallback-only mode sccssar-dev runs post-flip.
# Code truth: backend/main.py `os.environ.get("STAGING_SHADOW", "on")` default
#             + the `not in ("on", "off")` fail-fast guard.
# WHY brittle: the sequential (STAGING_SHADOW=off) fallback path is what makes
#             Overpass a secondary rather than a parallel shadow — the doc must
#             not drift from the code default that ships to real dispatchers.
# ---------------------------------------------------------------------------

def test_staging_shadow_default_and_values_match_doc(architecture_md: str) -> None:
    """STAGING_SHADOW default + allowed values in main.py must match §5."""
    main_py = _BACKEND_MAIN.read_text(encoding="utf-8")

    default_m = re.search(
        r'os\.environ\.get\(\s*"STAGING_SHADOW"\s*,\s*"([^"]*)"\s*\)',
        main_py,
    )
    assert default_m is not None, (
        "Could not find the `os.environ.get(\"STAGING_SHADOW\", ...)` default in "
        "backend/main.py."
    )
    code_default = default_m.group(1)  # "on"

    guard = re.search(r"_STAGING_SHADOW_RAW\s+not in\s*\(([^)]*)\)", main_py)
    assert guard is not None, (
        "Could not find the `_STAGING_SHADOW_RAW not in (...)` fail-fast guard in "
        "backend/main.py."
    )
    code_values = {v for v in re.findall(r'"([^"]*)"', guard.group(1)) if v}

    assert "STAGING_SHADOW" in architecture_md, (
        "docs/architecture.md §5 does not mention the STAGING_SHADOW flag, but "
        "backend/main.py reads it to choose parallel-shadow vs sequential-"
        "fallback mode. Document it in the 'Source selection' subsection."
    )
    # Both allowed values present in the doc.
    missing = [v for v in sorted(code_values) if v not in architecture_md]
    assert not missing, (
        f"STAGING_SHADOW value drift: backend/main.py allows {sorted(code_values)}, "
        f"but docs/architecture.md §5 does not mention {missing}."
    )
    # The code default must be marked as the default in the doc, e.g. "`on` (default)".
    doc_marks_default = re.search(
        rf"`{re.escape(code_default)}`\s*\(default",
        architecture_md,
    )
    assert doc_marks_default is not None, (
        f"STAGING_SHADOW default drift: backend/main.py defaults to "
        f"'{code_default}', but docs/architecture.md §5 does not mark "
        f"`{code_default}` as the default (expected e.g. '`{code_default}` "
        f"(default)'). Update whichever side drifted."
    )


# ---------------------------------------------------------------------------
# Dispatcher-facing docs (issues #644, #677)
#
# These docs had NO parity pins at all, which is why a shipped feature removal
# left them wrong for months and CI stayed green: #614 deleted the WhatsApp
# button and the two-section textarea in v1.11.18, and the guide went on telling
# dispatchers to click the button and to look in the section that no longer
# existed. test_doc_parity.py covered only docs/architecture.md.
# ---------------------------------------------------------------------------

# Scope note (#2.0, publication manifest). These pins used to hard-require
# docs/dispatcher-guide.md and docs/dispatcher-training.md by bare read_text().
# Both are team-specific and are withheld from the public repo, so the suite
# raised FileNotFoundError on a checkout of it -- and this suite is Step 0 of
# both build scripts. The fix is NOT skip-if-absent, which would go vacuous
# silently. Each pin now asserts at the scope it actually holds at, over the
# docs that are present, with an explicit non-vacuity floor.

# Every dispatcher-facing doc, whichever ship here.
_DISPATCHER_DOC_NAMES = ("dispatcher-guide.md", "dispatcher-training.md", "DISPATCHING.md")

# The full walkthrough a dispatcher is expected to work from, most specific
# first. The completeness pins below apply to this doc only: dispatcher-training.md
# is training material, not the reference, and has never been held to them.
_PRIMARY_REFERENCE_NAMES = ("dispatcher-guide.md", "DISPATCHING.md")


def _docs_dir():
    return Path(__file__).resolve().parents[1] / "docs"


def _dispatcher_docs():
    """Every dispatcher-facing doc present in THIS repo, by name.

    Non-vacuity floor: a repo with none of them is a packaging mistake, not a
    reason for these pins to pass quietly.
    """
    root = _docs_dir()
    found = {n: (root / n).read_text(encoding="utf-8")
             for n in _DISPATCHER_DOC_NAMES if (root / n).exists()}
    assert found, (
        "No dispatcher-facing doc found in docs/ "
        f"(looked for {list(_DISPATCHER_DOC_NAMES)}). Every build of this repo "
        "ships at least one, so this is a manifest error, not an empty pass."
    )
    return found


def _primary_reference():
    """(name, text) of the dispatcher reference this repo ships."""
    docs = _dispatcher_docs()
    for name in _PRIMARY_REFERENCE_NAMES:
        if name in docs:
            return name, docs[name]
    raise AssertionError(
        f"No primary dispatcher reference present (looked for "
        f"{list(_PRIMARY_REFERENCE_NAMES)}). The completeness pins below cannot "
        f"be satisfied by training material alone."
    )


def test_dispatcher_docs_do_not_teach_the_removed_whatsapp_button():
    """The button is gone (pinned by TestWhatsAppSurfaceRemoved). The docs must
    not instruct a dispatcher to click it.

    Retirement NOTES are allowed and wanted -- dispatchers who trained on the
    WhatsApp workflow benefit from seeing when it ended -- so this pins the
    instruction, not the word.
    """
    banned = [
        "WHATSAPP DISPATCH",          # the removed textarea section marker
        "Opens WhatsApp Web",
        "WhatsApp group",
        "Slack/WhatsApp",
        "until WhatsApp is officially deprecated",
        "until officially deprecated",
    ]
    for name, text in _dispatcher_docs().items():
        hits = [b for b in banned if b in text]
        assert not hits, (
            f"docs/{name} still teaches the removed WhatsApp surface: {hits}. "
            f"The button was deleted in #614/#631 (v1.11.18) and WhatsApp was "
            f"retired 2026-08-01."
        )


def test_dispatcher_guide_states_the_whatsapp_retirement_date():
    """Deleting the history is the wrong fix -- replace the open-ended
    'until officially deprecated' with the date it actually ended.

    Scoped to the team guide: the retirement date is this deployment's history,
    and a generic public guide has no reason to carry it.
    """
    guide = _dispatcher_docs().get("dispatcher-guide.md")
    if guide is None:
        pytest.skip("docs/dispatcher-guide.md is not shipped in this repo")
    assert "Retired 2026-08-01" in guide, (
        "docs/dispatcher-guide.md no longer records when WhatsApp was retired."
    )


def test_dispatcher_docs_do_not_promise_event_name_auto_update():
    """A staging override MUST NOT rewrite the Event Name (Locked Decision,
    Bill 2026-07-27; pinned in code by TestStagingOverrideDoesNotRewriteEventName).

    The guide taught the opposite behaviour for over a week after it was
    removed. That is worse than a gap: the dispatcher is told to review a field
    that never changes, so a genuinely wrong Event Name reads as expected.

    Negative assertion, so it costs nothing to hold every doc to it.
    """
    for name, text in _dispatcher_docs().items():
        assert "auto-updates to reflect the new street name" not in text, (
            f"docs/{name} still promises the Event Name auto-updates on a "
            f"staging override. That behaviour was deliberately removed 2026-07-27."
        )


def test_dispatcher_reference_documents_coordinate_staging_on_both_surfaces():
    """#668 made the intake form's staging field parse coordinates, so the
    reference must not tell dispatchers the panel is the only way.

    Coordinates-only intake is normal for wilderness and remote mutual aid, not
    a degraded input -- the reference has to say so, or a dispatcher reads a
    correct coordinate as a form-filling error.
    """
    name, text = _primary_reference()
    for phrase, why in [
        ("only coordinates", "the coordinates-only subsection is missing"),
        ("Staging Area for Resources", "the form surface is not mentioned"),
        ("UTM", "UTM support is not stated"),
    ]:
        assert phrase in text, f"docs/{name}: {why}"


def test_pdf_build_recipe_is_recorded():
    """The PDF was previously rebuilt by hand with an unrecorded command, which
    both #644 and #677 name as the reason it drifts from the .md."""
    root = _docs_dir()
    # Tied to the artifact it builds. If the PDF is not shipped here there is
    # nothing to drift from the .md, which is the only failure this pin exists
    # to catch -- so requiring the script unconditionally would fail a repo that
    # withholds both, for no reader's benefit.
    if not (root / "dispatcher-guide.pdf").exists():
        pytest.skip("docs/dispatcher-guide.pdf is not shipped in this repo")
    script = root / "build-docs-pdf.sh"
    assert script.exists(), "docs/build-docs-pdf.sh is gone — the PDF build recipe is unrecorded again."
    body = script.read_text(encoding="utf-8")
    for flag in ("--pdf-engine=xelatex", "Helvetica Neue", "--toc-depth=2", "pdf-symbols.tex"):
        assert flag in body, f"docs/build-docs-pdf.sh no longer pins {flag!r}"
