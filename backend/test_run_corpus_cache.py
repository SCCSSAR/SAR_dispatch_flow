"""
test_run_corpus_cache.py — smoke tests for backend/migration_validation/run_corpus.py
_cache_state() and _assert_sdk_available(). Confirms the four documented cache
states return the right tag, and the SDK guard fails loudly when the requested
SDK isn't installed — both without burning Vertex AI calls.
"""

import builtins
import json
import sys
from pathlib import Path

import pytest

from backend.migration_validation.run_corpus import (
    _assert_sdk_available,
    _cache_state,
)


def _block_import(*blocked_prefixes: str):
    """Return an __import__ replacement that raises ImportError for any module
    whose dotted name starts with any prefix in *blocked_prefixes*. Other
    imports fall through to the real builtin.

    Why this and not sys.modules manipulation: once a module has been imported
    in the test session, removing it from sys.modules doesn't necessarily
    unbind it from its parent package's attributes (e.g. google.genai stays
    as an attribute on the google package), so `from google import genai`
    succeeds anyway. Blocking at __import__ time is reliable.
    """
    real = builtins.__import__
    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        full = name
        if fromlist:
            for sub in fromlist:
                if any(f"{name}.{sub}".startswith(p) for p in blocked_prefixes):
                    raise ImportError(f"blocked by test: {name}.{sub}")
        if any(full.startswith(p) for p in blocked_prefixes):
            raise ImportError(f"blocked by test: {full}")
        return real(name, globals, locals, fromlist, level)
    return fake


def _make_pair(tmp_path: Path, name: str, txt: str | None, meta: dict | str | None) -> tuple[Path, Path]:
    """Helper: write run-N.txt and run-N.meta.json fixtures (or skip either).

    txt:  None → file not created. ""   → empty file. "abc" → text file with content.
    meta: None → file not created. ""   → empty file. dict  → JSON-serialized.
                                   str  → raw string (used to test malformed JSON).
    """
    run_txt = tmp_path / f"{name}.txt"
    meta_json = tmp_path / f"{name}.meta.json"
    if txt is not None:
        run_txt.write_text(txt)
    if meta is not None:
        if isinstance(meta, dict):
            meta_json.write_text(json.dumps(meta))
        else:
            meta_json.write_text(meta)
    return run_txt, meta_json


def test_cache_state_empty(tmp_path: Path):
    """Neither file present → state='empty'."""
    run_txt, meta_json = _make_pair(tmp_path, "run-1", txt=None, meta=None)
    state, meta = _cache_state(run_txt, meta_json)
    assert state == "empty"
    assert meta is None


def test_cache_state_ok(tmp_path: Path):
    """Both files present, meta status='ok' → state='ok'."""
    run_txt, meta_json = _make_pair(
        tmp_path, "run-1",
        txt="captured Gemini output goes here",
        meta={"status": "ok", "latency_ms": 1234, "sdk": "legacy"},
    )
    state, meta = _cache_state(run_txt, meta_json)
    assert state == "ok"
    assert meta is not None
    assert meta["status"] == "ok"


def test_cache_state_error(tmp_path: Path):
    """Both files present, meta status='error' → state='error'."""
    run_txt, meta_json = _make_pair(
        tmp_path, "run-1",
        txt="[ERROR] RuntimeError: Vertex AI timeout",
        meta={"status": "error", "latency_ms": 60000, "sdk": "legacy"},
    )
    state, meta = _cache_state(run_txt, meta_json)
    assert state == "error"
    assert meta is not None
    assert meta["status"] == "error"


def test_cache_state_partial_only_txt(tmp_path: Path):
    """Output present but no meta → state='partial' (treat as empty, redo)."""
    run_txt, meta_json = _make_pair(tmp_path, "run-1", txt="orphaned output", meta=None)
    state, meta = _cache_state(run_txt, meta_json)
    assert state == "partial"
    assert meta is None


def test_cache_state_partial_only_meta(tmp_path: Path):
    """Meta present but no output → state='partial' (treat as empty, redo)."""
    run_txt, meta_json = _make_pair(tmp_path, "run-1", txt=None, meta={"status": "ok"})
    state, meta = _cache_state(run_txt, meta_json)
    assert state == "partial"
    assert meta is None


def test_cache_state_partial_empty_txt(tmp_path: Path):
    """Empty output file → treated as missing → state='partial' when meta is also empty.

    Both empty: both treated as missing → state='empty'.
    Empty txt + present meta: txt missing, meta present → state='partial'.
    """
    run_txt, meta_json = _make_pair(tmp_path, "run-1", txt="", meta={"status": "ok"})
    state, _ = _cache_state(run_txt, meta_json)
    assert state == "partial"


def test_cache_state_partial_malformed_meta(tmp_path: Path):
    """Both files present but meta JSON is corrupt → state='partial' (don't trust)."""
    run_txt, meta_json = _make_pair(
        tmp_path, "run-1",
        txt="captured output",
        meta="{not valid json",  # raw string, malformed
    )
    state, meta = _cache_state(run_txt, meta_json)
    assert state == "partial"
    assert meta is None


def test_cache_state_meta_missing_status_field(tmp_path: Path):
    """Both files valid JSON but meta has no 'status' key → state='error' (default)."""
    run_txt, meta_json = _make_pair(
        tmp_path, "run-1",
        txt="captured output",
        meta={"latency_ms": 1000},  # no 'status' key
    )
    state, meta = _cache_state(run_txt, meta_json)
    assert state == "error"
    assert meta is not None


# ---------------------------------------------------------------------------
# _assert_sdk_available() — issue #334 runtime SDK guard.
# Without this guard, --sdk legacy AFTER the migration would silently shell
# out to the post-migration genai-based gemini.py and poison legacy outputs.
# ---------------------------------------------------------------------------

def test_assert_sdk_available_genai_when_installed():
    """Post-migration tree ships google-genai → --sdk genai must succeed."""
    pytest.importorskip("google.genai", reason="google-genai not installed in this environment")
    _assert_sdk_available("genai")  # no exception


def test_assert_sdk_available_legacy_fails_when_vertexai_absent(monkeypatch):
    """If vertexai isn't installed, --sdk legacy must SystemExit with a clear message
    BEFORE any Vertex call fires."""
    monkeypatch.setattr(builtins, "__import__", _block_import("vertexai"))
    with pytest.raises(SystemExit) as exc:
        _assert_sdk_available("legacy")
    assert "google-cloud-aiplatform" in str(exc.value)


def test_assert_sdk_available_genai_fails_when_genai_absent(monkeypatch):
    """Symmetric: blocked google-genai → --sdk genai must SystemExit with a clear message."""
    monkeypatch.setattr(builtins, "__import__", _block_import("google.genai"))
    with pytest.raises(SystemExit) as exc:
        _assert_sdk_available("genai")
    assert "google-genai" in str(exc.value)


def test_assert_sdk_available_unknown_sdk_value():
    """Unknown --sdk value should SystemExit, not silently fall through."""
    with pytest.raises(SystemExit) as exc:
        _assert_sdk_available("openai")
    assert "openai" in str(exc.value)
