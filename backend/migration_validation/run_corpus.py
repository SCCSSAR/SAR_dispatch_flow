"""
run_corpus.py — Run all forms in experiments/test_forms/ through gemini.py
N times each, capture full text output + finish_reason + latency_ms.

Outputs are DURABLE: every successful Gemini response is written to
experiments/migration-output-{sdk}/<form-name>/run-<N>.txt with a sidecar
run-<N>.meta.json. Re-running the script SKIPS any (form, run) triple whose
output already exists with status=ok — re-invocations spend $0 by default.

Cache states (per run-N.txt + run-N.meta.json pair):
  - missing or empty + no meta        → FRESH call (counts toward bill)
  - non-empty + meta status="ok"      → SKIP (already captured durably)
  - non-empty + meta status="error"   → SKIP unless --retry-errors set
  - any state                         → FRESH call if --force set

Usage:
    python3 -m backend.migration_validation.run_corpus --sdk legacy
    python3 -m backend.migration_validation.run_corpus --sdk genai
    python3 -m backend.migration_validation.run_corpus --sdk genai --retry-errors
    python3 -m backend.migration_validation.run_corpus --sdk genai --filter "image0" --force

PRIVACY: outputs contain real OCR data — research/ and experiments/ are both
gitignored. Do not check in any output files.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "experiments" / "test_forms"
RUNS_PER_FORM = 3


def _sdk_version(sdk_name: str) -> str:
    """Best-effort SDK version string for the meta record. Diagnostic only — does NOT trigger cache invalidation."""
    try:
        if sdk_name == "legacy":
            import vertexai
            return f"google-cloud-aiplatform/{getattr(vertexai, '__version__', 'unknown')}"
        else:
            from google import genai
            return f"google-genai/{getattr(genai, '__version__', 'unknown')}"
    except Exception as exc:
        return f"unknown ({type(exc).__name__})"


def _assert_sdk_available(requested: str) -> None:
    """Verify the requested SDK is actually importable before any network call.

    Without this guard, --sdk legacy AFTER the migration silently shells out to
    the post-migration genai-based gemini.py and writes its outputs into
    migration-output-legacy/ — a corpus poisoning that's hard to spot.

    Raises SystemExit with a clear message if the SDK isn't installed.
    """
    if requested == "legacy":
        try:
            import vertexai  # noqa: F401
        except ImportError:
            raise SystemExit(
                "ERROR: --sdk legacy requested but google-cloud-aiplatform "
                "is not installed. Post-migration tree only ships google-genai. "
                "If you need a legacy capture, install pinned legacy version "
                "in a separate venv and re-run."
            )
    elif requested == "genai":
        try:
            from google import genai  # noqa: F401
        except ImportError:
            raise SystemExit(
                "ERROR: --sdk genai requested but google-genai is not installed. "
                "Run: pip install google-genai"
            )
    else:
        raise SystemExit(f"ERROR: unknown --sdk value {requested!r}")


def _cache_state(run_txt: Path, meta_json: Path) -> tuple[str, dict | None]:
    """Inspect the on-disk pair. Returns (state, meta_dict_or_None).

    state ∈ {"empty", "ok", "error", "partial"}.
      - "empty"  → no captured run yet; do a fresh call
      - "ok"     → durable success; skip
      - "error"  → durable error; skip unless --retry-errors
      - "partial" → output exists but no meta, OR meta but no output → treat as empty (fresh call)
    """
    txt_present = run_txt.exists() and run_txt.stat().st_size > 0
    meta_present = meta_json.exists() and meta_json.stat().st_size > 0
    if not txt_present and not meta_present:
        return "empty", None
    if txt_present != meta_present:
        return "partial", None
    try:
        meta = json.loads(meta_json.read_text())
    except Exception:
        return "partial", None
    return meta.get("status", "error"), meta


def _run_jpeg(form_path: Path, image_bytes: bytes):
    """Image+text path. Returns (output_text, status, latency_ms).

    NOTE: gemini.extract_incident_summary is `async`, so we wrap with asyncio.run.
    """
    import gemini

    start = time.monotonic()
    try:
        text = asyncio.run(gemini.extract_incident_summary(
            image_bytes=image_bytes,
            lkp_coords="37.4000,-121.8800 (San Jose, CA — synthetic)",
            staging_candidates=[],
        ))
        return text, "ok", int((time.monotonic() - start) * 1000)
    except Exception as exc:
        return f"[ERROR] {type(exc).__name__}: {exc}", "error", int((time.monotonic() - start) * 1000)


def _run_pdf(form_path: Path, pdf_bytes: bytes):
    """Text-only path: PDF → AcroForm extract → synthetic summary → Gemini staging+Koester.

    NOTE: gemini.extract_staging_and_koester is `async` and takes `structured_context`
    (not `synthetic_summary`). pdf_extract.build_synthetic_summary requires
    `dispatcher_last_name` and `intake_timestamp` args — we pass synthetic placeholders
    since this is offline corpus capture, not a real dispatch.
    """
    import gemini
    import pdf_extract

    fields = pdf_extract.extract_acroform_fields(pdf_bytes)
    summary_input = pdf_extract.build_synthetic_summary(
        fields,
        dispatcher_last_name="MigrationCheck",
        intake_timestamp="2026-04-30 12:00",
    )

    start = time.monotonic()
    try:
        text = asyncio.run(gemini.extract_staging_and_koester(
            structured_context=summary_input,
            lkp_coords="37.4000,-121.8800 (San Jose, CA — synthetic)",
            staging_candidates=[],
        ))
        return text, "ok", int((time.monotonic() - start) * 1000)
    except Exception as exc:
        return f"[ERROR] {type(exc).__name__}: {exc}", "error", int((time.monotonic() - start) * 1000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk", choices=["legacy", "genai"], required=True)
    parser.add_argument("--runs", type=int, default=RUNS_PER_FORM)
    parser.add_argument("--filter", type=str, default="",
                        help="Only run forms whose filename contains this substring")
    parser.add_argument("--retry-errors", action="store_true",
                        help="Re-run captures whose meta status='error' (default: skip)")
    parser.add_argument("--force", action="store_true",
                        help="DANGER: re-bill all (form, run) triples — overwrites durable captures")
    args = parser.parse_args()

    _assert_sdk_available(args.sdk)
    out_root = REPO_ROOT / "experiments" / f"migration-output-{args.sdk}"
    out_root.mkdir(parents=True, exist_ok=True)
    sdk_version = _sdk_version(args.sdk)

    if not CORPUS_DIR.exists():
        print(f"ERROR: corpus directory does not exist: {CORPUS_DIR}", file=sys.stderr)
        print("Add JPEG/PDF forms to experiments/test_forms/ before running.", file=sys.stderr)
        return

    forms = sorted(CORPUS_DIR.iterdir())
    if args.filter:
        forms = [f for f in forms if args.filter in f.name]

    fresh_calls = skipped_ok = skipped_err = retried = forced = 0

    for form_path in forms:
        if not form_path.is_file():
            continue
        suffix = form_path.suffix.lower()
        is_jpeg = suffix in (".jpeg", ".jpg")
        is_pdf = suffix == ".pdf"
        if not (is_jpeg or is_pdf):
            continue

        form_dir = out_root / form_path.stem
        form_dir.mkdir(exist_ok=True)
        runner = _run_jpeg if is_jpeg else _run_pdf

        # Read the bytes ONCE per form, hash for the meta record
        raw_bytes = form_path.read_bytes()
        input_sha = hashlib.sha256(raw_bytes).hexdigest()[:16]

        for run_idx in range(1, args.runs + 1):
            run_txt = form_dir / f"run-{run_idx}.txt"
            meta_json = form_dir / f"run-{run_idx}.meta.json"
            state, _ = _cache_state(run_txt, meta_json)

            # Skip-paths first — these are the cache-hit branches that don't
            # spend Vertex calls. --force overrides all skips.
            if not args.force and state == "ok":
                skipped_ok += 1
                print(f"[{args.sdk}] {form_path.name} run {run_idx}/{args.runs} SKIP-OK", flush=True)
                continue
            if not args.force and state == "error" and not args.retry_errors:
                skipped_err += 1
                print(f"[{args.sdk}] {form_path.name} run {run_idx}/{args.runs} SKIP-ERR", flush=True)
                continue

            # Call paths — pick the label for the line below, then fall through.
            if args.force:
                forced += 1
                label = "FORCE"
            elif state == "error":     # implicit: --retry-errors set
                retried += 1
                label = "RETRY-ERR"
            else:                       # state in ("empty", "partial")
                label = "FRESH"
            print(f"[{args.sdk}] {form_path.name} run {run_idx}/{args.runs} {label}", flush=True)

            text, status, latency_ms = runner(form_path, raw_bytes)
            run_txt.write_text(text, encoding="utf-8")
            meta_json.write_text(json.dumps({
                "status": status,
                "latency_ms": latency_ms,
                "sdk": args.sdk,
                "sdk_version": sdk_version,
                "input_sha256": input_sha,
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, indent=2), encoding="utf-8")
            fresh_calls += 1

    summary = {
        "sdk": args.sdk,
        "sdk_version": sdk_version,
        "runs_per_form": args.runs,
        "fresh_calls": fresh_calls,
        "skipped_ok": skipped_ok,
        "skipped_errors": skipped_err,
        "retried_errors": retried,
        "forced": forced,
    }
    (out_root / "manifest.json").write_text(json.dumps(summary, indent=2))
    print(f"\n{summary}")
    print(f"Outputs: {out_root}")


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    main()
