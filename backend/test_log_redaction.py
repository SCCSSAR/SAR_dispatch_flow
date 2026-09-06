"""Issue #608 — API keys must never reach Cloud Run logs.

httpx logs every outbound request at INFO with the full URL including query
string, and both geocoding providers pass their API key as a query parameter.
On 2026-07-24 that wrote live Google Maps keys to sccssar-dev (production) logs
in plaintext, readable by anyone with log-read IAM on the project.

The fix redacts secret-bearing query parameters inside _StructuredJsonHandler —
the single handler every log record passes through — so it covers third-party
libraries we do not control, not just the ones we knew about.

Companion to test_pii_log_patterns.py: that guard walks OUR logger call sites
for PII. This one covers text we never wrote, formatted by somebody else's
library, on its way to the sink.

Mirrors main.py::_redact_secrets — the suite does not import main (heavyweight
GCP deps), so the mirror is pinned by AST against production below.
"""

import ast
import inspect
import re
import textwrap
from pathlib import Path


_SECRET_QS_RE = re.compile(
    r"(?i)([?&](?:key|api_?key|access_?token|token|secret|signature|sig|password|pwd)=)"
    r"[^&\s\"'\\]+"
)


def _redact_secrets(text):
    """Mirror of main.py::_redact_secrets()."""
    try:
        return _SECRET_QS_RE.sub(r"\1REDACTED", text)
    except Exception:  # pragma: no cover
        return text


class TestRedactSecrets:
    """Real leaked line SHAPES, with synthetic values, must come out clean."""

    def test_the_2026_07_24_google_maps_line(self):
        raw = (
            'HTTP Request: GET https://maps.googleapis.com/maps/api/geocode/json'
            '?address=447+Great+Mall+Dr%2C+Santa+Clara%2C+CA'
            '&key=AIzaSyEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE "HTTP/1.1 200 OK"'
        )
        out = _redact_secrets(raw)
        assert "AIzaSyEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE" not in out
        assert "key=REDACTED" in out

    def test_geoapify_apikey_param(self):
        raw = ("HTTP Request: GET https://api.geoapify.com/v2/places"
               "?categories=leisure.park&apiKey=abcdef0123456789 \"200 OK\"")
        out = _redact_secrets(raw)
        assert "abcdef0123456789" not in out
        assert "apiKey=REDACTED" in out

    def test_caltopo_signature_param(self):
        raw = "POST https://caltopo.com/api/v1/map/X?signature=aGVsbG8rd29ybGQ%3D&id=7"
        out = _redact_secrets(raw)
        assert "aGVsbG8rd29ybGQ" not in out
        assert "signature=REDACTED" in out

    def test_the_useful_part_of_the_line_survives(self):
        """Redaction must not destroy the diagnostic value of the log line.

        The request lines are how the 2026-07-25 wrong-city investigation was
        resolved — host, path, and the address being geocoded all have to
        remain readable, or the next investigation loses its evidence.
        """
        raw = ('HTTP Request: GET https://maps.googleapis.com/maps/api/geocode/json'
               '?address=447+Great+Mall+Dr&key=SECRETVALUE "HTTP/1.1 200 OK"')
        out = _redact_secrets(raw)
        assert "maps.googleapis.com/maps/api/geocode/json" in out
        assert "address=447+Great+Mall+Dr" in out
        assert "HTTP/1.1 200 OK" in out

    def test_parameter_name_is_kept(self):
        """Keeping the name tells a reader the call WAS authenticated."""
        assert "key=REDACTED" in _redact_secrets("https://x/y?key=abc")

    def test_multiple_secrets_in_one_line(self):
        out = _redact_secrets("https://x/y?key=aaa&token=bbb&safe=keepme")
        assert "aaa" not in out and "bbb" not in out
        assert "safe=keepme" in out

    def test_first_param_uses_question_mark(self):
        assert "key=REDACTED" in _redact_secrets("https://x/y?key=zzz")

    def test_no_secret_is_left_untouched(self):
        raw = "map_data built | lkp=yes residence=geocoded staging_count=7"
        assert _redact_secrets(raw) == raw

    def test_case_insensitive_param_names(self):
        for name in ("KEY", "ApiKey", "API_KEY", "Access_Token"):
            out = _redact_secrets(f"https://x/y?{name}=supersecret")
            assert "supersecret" not in out, f"{name} not redacted"

    def test_substring_param_names_are_not_over_matched(self):
        """A param merely CONTAINING 'key' must not be blanked.

        Over-redaction is its own failure: blanking monkey=1 or
        keyword=hospital would quietly destroy diagnostic context and nobody
        would notice, because nobody reads logs that look fine.
        """
        out = _redact_secrets("https://x/y?monkey=1&keyword=hospital")
        assert "monkey=1" in out
        assert "keyword=hospital" in out

    def test_never_raises_on_odd_input(self):
        for bad in ("", "?key=", "no-url-at-all", "?key=%%%"):
            _redact_secrets(bad)  # must not raise


class TestHandlerAppliesRedaction:
    """Pin the WIRING, not just the helper.

    A correct _redact_secrets that nothing calls is the same as no fix at all —
    and it would pass every test above.
    """

    @staticmethod
    def _main_source():
        return (Path(__file__).parent / "main.py").read_text(encoding="utf-8")

    @staticmethod
    def _emit_body(src):
        start = src.find("class _StructuredJsonHandler")
        assert start != -1, "_StructuredJsonHandler not found in main.py"
        end = src.find("\n_root_logger = ", start)
        assert end != -1 and end > start, "could not bound _StructuredJsonHandler"
        return src[start:end]

    def test_message_is_redacted(self):
        body = self._emit_body(self._main_source())
        assert "_redact_secrets(self.format(record))" in body, (
            "the log message is no longer passed through _redact_secrets — "
            "API keys in third-party log lines reach Cloud Run again"
        )

    def test_exception_text_is_redacted(self):
        """An httpx exception repr carries the full failing URL."""
        body = self._emit_body(self._main_source())
        assert "_redact_secrets(self.formatException(record.exc_info))" in body, (
            "exception text is no longer redacted — a failed geocode logs its "
            "key in the traceback"
        )


class TestMirrorParity:
    """The mirror above must still match production, compared by AST.

    Without this, a change to the production redactor cannot fail this file —
    the tests would keep exercising a stale copy and reporting green. Same
    contract as the mirror pin in test_geocoding_guard.py.
    """

    def test_redact_secrets_matches_production(self):
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        m = re.search(
            r"^def _redact_secrets\(.*?(?=\n\n(?:def |class |async def |# -{10,}))",
            src, re.DOTALL | re.MULTILINE,
        )
        assert m, "_redact_secrets not found in main.py"

        def shape(fn_src):
            tree = ast.parse(textwrap.dedent(fn_src))
            for node in ast.walk(tree):
                if isinstance(node, ast.arg):
                    node.annotation = None
                if isinstance(node, ast.FunctionDef):
                    node.returns = None
                    if (node.body and isinstance(node.body[0], ast.Expr)
                            and isinstance(node.body[0].value, ast.Constant)
                            and isinstance(node.body[0].value.value, str)):
                        node.body = node.body[1:]
            return ast.dump(tree)

        assert shape(m.group(0)) == shape(inspect.getsource(_redact_secrets)), (
            "_redact_secrets in test_log_redaction.py has drifted from "
            "backend/main.py (source of truth) — the tests above are "
            "exercising the stale copy"
        )

    def test_pattern_matches_production(self):
        """The regex is the whole fix; pin it separately from the function."""
        src = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        start = src.find("_SECRET_QS_RE = re.compile(")
        assert start != -1, "_SECRET_QS_RE not found in main.py"
        # Bound on the closing paren AT COLUMN 0, not the first ")" — the first
        # one lives inside the regex itself ("(?i)"), which truncated the slice
        # to before every token this test checks and failed for the wrong
        # reason. A window that ends early is the same hazard as one that never
        # ends: the assertions stop describing the thing they name.
        end = src.find("\n)\n", start)
        assert end != -1 and end > start, "could not bound the _SECRET_QS_RE assignment"
        prod = src[start:end]
        assert "re.compile" in prod and len(prod) > 80, "pattern window looks truncated"
        for token in ("api_?key", "access_?token", "signature", "password"):
            assert token in prod, f"production pattern lost the {token!r} alternative"
