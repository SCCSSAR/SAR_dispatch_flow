"""
Boundary-mocked unit tests for backend/gemini.py.

These tests pin the structural contract of extract_incident_summary
(image+text path) and extract_staging_and_koester (text-only path) by
mocking the google-genai SDK at the boundary.

Issue #111 — Vertex AI SDK migration. Both call sites are now on the new
google-genai SDK; the legacy vertexai SDK has been fully removed.

Test environment requirement: this file imports `gemini`, which imports
`from google import genai` at module load time. The `pytest.importorskip`
guard below skips the entire file when `google.genai` is not installed
(e.g. when `python3 -m pytest` runs from a shell where the project venv
is not activated). This preserves the project convention of letting
build-dev.sh Step 0 succeed even outside the venv, while still exercising
the tests when the SDK is present.

Mock surface notes (both paths — new google-genai SDK):
- gemini._get_client() returns a Client whose .models.generate_content()
  returns a response object.
- response.candidates[0].finish_reason — types.FinishReason enum;
  types.FinishReason.MAX_TOKENS triggers the truncation guard.
- response.text — string return value.
- gemini.types.Part.from_bytes(data=..., mime_type=...) — used only on
  the image+text path. NOT called on the text-only PDF path.

These tests use asyncio.run() to drive the async functions, matching the
pattern used by experiments/migration_validation/run_corpus.py. We
deliberately do NOT introduce pytest-asyncio as a new test dependency.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# Skip this file gracefully when google.genai is not installed — `import
# gemini` below would otherwise cascade to `from google import genai` and
# abort pytest collection entirely (regressing build-dev.sh Step 0 outside
# the venv).
pytest.importorskip("google.genai")

import gemini


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_response(text: str = "ok", finish_reason=1) -> MagicMock:
    """
    Build a MagicMock that mimics a Gemini SDK response:

      response.candidates[0].finish_reason  (FinishReason enum)
      response.text                          (str)

    Default finish_reason=1 (STOP — normal completion).

    To simulate MAX_TOKENS truncation, pass
    `gemini.types.FinishReason.MAX_TOKENS`.
    """
    response = MagicMock()
    response.text = text
    candidate = MagicMock()
    candidate.finish_reason = finish_reason
    response.candidates = [candidate]
    return response


# ---------------------------------------------------------------------------
# extract_incident_summary (image+text path)
# ---------------------------------------------------------------------------

def test_extract_incident_summary_calls_vertex_with_image_part_and_max_tokens_32768():
    """
    Verify the new-SDK (google-genai) call site for the JPEG path:
      - _get_client() returns a Client
      - types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg") called
      - client.models.generate_content invoked with model=MODEL_ID,
        contents=[prompt, image_part], config=GenerateContentConfig(...)
      - config: temperature=0.1, max_output_tokens=32768

    Pinned by the migration plan: max_output_tokens MUST be 32768 on the
    JPEG path (raised from 16384 after a confirmed MAX_TOKENS truncation
    in production on the Torres form, 2026-03-08).
    """
    image_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

    fake_image_part = MagicMock(name="image_part")
    fake_response = _make_response(text="Initial Incident Summary:\n...", finish_reason=1)
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response

    with patch.object(gemini, "_get_client", return_value=fake_client) as mock_get_client, \
         patch.object(gemini.types, "Part") as mock_part_cls:
        mock_part_cls.from_bytes.return_value = fake_image_part

        result = asyncio.run(gemini.extract_incident_summary(
            image_bytes=image_bytes,
            lkp_coords="37.4,-121.9",
            staging_candidates=[],
        ))

    # Return value passed through unchanged
    assert result == "Initial Incident Summary:\n..."

    # _get_client invoked once (singleton getter)
    mock_get_client.assert_called_once_with()

    # types.Part.from_bytes invoked with raw bytes + JPEG mime type
    mock_part_cls.from_bytes.assert_called_once()
    part_call_kwargs = mock_part_cls.from_bytes.call_args.kwargs
    assert part_call_kwargs["data"] == image_bytes
    assert part_call_kwargs["mime_type"] == "image/jpeg"

    # client.models.generate_content invoked with model=, contents=, config=
    fake_client.models.generate_content.assert_called_once()
    _, call_kwargs = fake_client.models.generate_content.call_args
    assert call_kwargs["model"] == gemini.MODEL_ID

    contents = call_kwargs["contents"]
    assert isinstance(contents, list)
    assert len(contents) == 2
    assert isinstance(contents[0], str)              # prompt is a string
    assert "Initial Incident Summary" in contents[0]  # canonical prompt rendered
    assert contents[1] is fake_image_part             # second element is the image part

    # GenerateContentConfig pins
    cfg = call_kwargs["config"]
    assert cfg.temperature == 0.1
    assert cfg.max_output_tokens == 32768


def test_extract_incident_summary_raises_on_max_tokens_finish_reason():
    """
    finish_reason=MAX_TOKENS MUST raise RuntimeError. Silent truncation
    is unacceptable — the dispatcher would receive a partial form that
    looks complete.

    The wrapped error message should mention either "OCR service error"
    (the outer wrapper) or "MAX_TOKENS" (the inner cause).
    """
    image_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

    truncated_response = _make_response(
        text="partial output",
        finish_reason=gemini.types.FinishReason.MAX_TOKENS,
    )
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = truncated_response

    with patch.object(gemini, "_get_client", return_value=fake_client), \
         patch.object(gemini.types, "Part") as mock_part_cls:
        mock_part_cls.from_bytes.return_value = MagicMock(name="image_part")

        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(gemini.extract_incident_summary(
                image_bytes=image_bytes,
                lkp_coords="",
                staging_candidates=[],
            ))

    msg = str(exc_info.value)
    assert ("OCR service error" in msg) or ("MAX_TOKENS" in msg)


def test_extract_incident_summary_wraps_unexpected_errors():
    """
    Any non-MAX_TOKENS exception from the SDK MUST be wrapped as
    RuntimeError("OCR service error"). The original exception message must
    NOT leak through — gemini.py logs only the exception TYPE, not the
    message, because input data could appear in the message.

    NOTE: chained exceptions still expose the cause via __cause__, but the
    surfaced message string itself is the controlled "OCR service error".
    """
    image_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

    secret_payload = "SENSITIVE-LKP-DATA-DO-NOT-LEAK-2026"
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = ValueError(secret_payload)

    with patch.object(gemini, "_get_client", return_value=fake_client), \
         patch.object(gemini.types, "Part") as mock_part_cls:
        mock_part_cls.from_bytes.return_value = MagicMock(name="image_part")

        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(gemini.extract_incident_summary(
                image_bytes=image_bytes,
                lkp_coords="",
                staging_candidates=[],
            ))

    # Surfaced message is the wrapper string, NOT the inner sensitive payload
    assert str(exc_info.value) == "OCR service error"
    assert secret_payload not in str(exc_info.value)


# ---------------------------------------------------------------------------
# extract_staging_and_koester (text-only path)
# ---------------------------------------------------------------------------

def test_extract_staging_and_koester_calls_vertex_text_only_max_tokens_16384():
    """
    Verify the new-SDK (google-genai) call site for the PDF (text-only) path:
      - _get_client() returns a Client
      - types.Part.from_bytes is NOT called (no image)
      - client.models.generate_content invoked with model=MODEL_ID,
        contents=[prompt] (single-element list — no image), config=GenerateContentConfig(...)
      - config: temperature=0.1, max_output_tokens=16384

    Pinned by the migration plan: max_output_tokens MUST be 16384 on the
    PDF path (raised from 8192 after MAX_TOKENS truncation, 2026-03-04).
    """
    structured_context = (
        "Initial Incident Summary:\nMissing Person: TestSubject\n"
        "---\nEvent Log:\n...\n"
        "---\nLPB Questionnaire:\nQ1 - Yes - Familiar with area\n"
    )

    fake_response = _make_response(
        text="Staging Area Recommendations:\n1. ...\n---\nLPB Range Ring Analysis:\n...",
        finish_reason=1,
    )
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response

    with patch.object(gemini, "_get_client", return_value=fake_client) as mock_get_client, \
         patch.object(gemini.types, "Part") as mock_part_cls:
        result = asyncio.run(gemini.extract_staging_and_koester(
            structured_context=structured_context,
            lkp_coords="37.4,-121.9",
            staging_candidates=[],
        ))

    # Return value passed through unchanged
    assert result.startswith("Staging Area Recommendations:")

    # _get_client invoked once (singleton getter)
    mock_get_client.assert_called_once_with()

    # types.Part.from_bytes is NEVER called on the text-only path
    mock_part_cls.from_bytes.assert_not_called()

    # client.models.generate_content invoked with model=, contents=, config=
    fake_client.models.generate_content.assert_called_once()
    _, call_kwargs = fake_client.models.generate_content.call_args
    assert call_kwargs["model"] == gemini.MODEL_ID

    contents = call_kwargs["contents"]
    assert isinstance(contents, list)
    assert len(contents) == 1
    assert isinstance(contents[0], str)
    # The structured context should be embedded in the rendered prompt
    assert "TestSubject" in contents[0]

    # GenerateContentConfig pins
    cfg = call_kwargs["config"]
    assert cfg.temperature == 0.1
    assert cfg.max_output_tokens == 16384


def test_extract_staging_and_koester_raises_on_max_tokens():
    """
    Same MAX_TOKENS guard for the PDF path: finish_reason=MAX_TOKENS MUST
    raise RuntimeError. Surfaced message must mention either "Staging
    service error" (the PDF path's wrapper string — DIFFERENT from the
    JPEG path's "OCR service error") or "MAX_TOKENS" (inner cause).

    The PDF path uses "Staging service error" as its wrapper string
    (gemini.py line ~833). The JPEG path uses "OCR service error". The
    distinction is intentional and pinned by both tests.
    """
    structured_context = "Initial Incident Summary:\n...\n---\nEvent Log:\n...\n"

    truncated_response = _make_response(
        text="partial staging",
        finish_reason=gemini.types.FinishReason.MAX_TOKENS,
    )
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = truncated_response

    with patch.object(gemini, "_get_client", return_value=fake_client), \
         patch.object(gemini.types, "Part") as mock_part_cls:
        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(gemini.extract_staging_and_koester(
                structured_context=structured_context,
                lkp_coords="",
                staging_candidates=[],
            ))

    msg = str(exc_info.value)
    assert ("Staging service error" in msg) or ("MAX_TOKENS" in msg)
    # types.Part.from_bytes is NEVER called on the text-only path
    mock_part_cls.from_bytes.assert_not_called()


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT structure — v2 form detection (Issue #84)
# ---------------------------------------------------------------------------

class TestSystemPromptV2FormDetection:
    """SYSTEM_PROMPT must contain v2 form version detection.

    Pinned per constraint-promotion rule: v2 form is in active field use
    (first real-world submission: SJSU incident 2026-05-07). These tests
    ensure future prompt edits don't accidentally delete the v2 detection
    step or the left-side checkbox description.

    Source of truth: backend/gemini.py SYSTEM_PROMPT.
    """

    def test_v2_detection_step_present(self):
        """Prompt must instruct Gemini to detect form version via 'v2' text."""
        assert "VERSION 2" in gemini.SYSTEM_PROMPT

    def test_v1_fallback_present(self):
        """Prompt must retain v1 right-margin description as fallback."""
        assert "VERSION 1" in gemini.SYSTEM_PROMPT

    def test_v2_left_side_description(self):
        """v2 layout: checkboxes on the LEFT side — must be described."""
        assert "left side" in gemini.SYSTEM_PROMPT.lower()

    def test_left_yes_rule_preserved(self):
        """LEFT=YES positional rule must survive in the updated prompt."""
        assert "LEFT box" in gemini.SYSTEM_PROMPT or "LEFT of the two" in gemini.SYSTEM_PROMPT

    def test_event_log_includes_version_prefix(self):
        """Event Log entry 2 ('Intake form processed') MUST be prefixed with the
        detected form version ('v1 Intake form processed' or 'v2 Intake form
        processed'). This is the troubleshooting signal that confirms which
        checkbox layout branch fired.
        """
        assert "v1 Intake form processed" in gemini.SYSTEM_PROMPT
        assert "v2 Intake form processed" in gemini.SYSTEM_PROMPT
