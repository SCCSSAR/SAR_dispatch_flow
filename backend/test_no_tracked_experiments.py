"""Guard: nothing under `experiments/` may be tracked in git.

`experiments/` is gitignored (.gitignore:134), but `git add -f` overrides that
silently and no review step catches it. Six spike scripts were force-added
between 2026-07-31 and 2026-08-18 that way, each time for a defensible-sounding
reason — the spike was the empirical evidence behind a Locked Decision, so
committing it looked like good provenance.

Bill's ruling (2026-08-18): spikes stay OUT of git. They exist to move fast
against live third-party APIs, and that speed is bought by inlining things a
committed file must never carry — API keys, bearer tokens, real addresses, real
member names and ids. Whether any given spike happens to read its key from the
environment is not the point; the class is untrusted by construction, and the
review that would catch a lapse is exactly the review a `-f` add skips.

The six removed were audited first and were clean (all read credentials from
env vars), so no history rewrite was needed. That is luck, not a control.

Referencing a spike by path from a docstring or a Locked Decision stays fine —
it is a local-only pointer, the same convention docs/design-decisions.md uses.
"""
import subprocess

import pytest


def _tracked_experiment_paths() -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "experiments/"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        pytest.skip(f"git unavailable: {exc}")
    if proc.returncode != 0:
        pytest.skip("not a git checkout (Docker build context, tarball, etc.)")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_no_experiment_files_are_tracked():
    tracked = _tracked_experiment_paths()
    assert tracked == [], (
        "These files under experiments/ are tracked in git:\n  "
        + "\n  ".join(tracked)
        + "\n\nSpikes stay out of git (Bill, 2026-08-18) — they inline keys and "
        "real strings to move fast against live APIs. `git add -f` is what "
        "bypasses the .gitignore; do not use it here. To remove one while "
        "keeping it on disk: git rm --cached <path>"
    )
