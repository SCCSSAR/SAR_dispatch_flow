"""
test_rate_limit.py — retry guard for cold-start transaction expiry (issue #522).

Firestore transactions have a 60s lifetime from db.transaction(). On a cold
container the first request was racing that window — client construction +
ADC + gRPC handshake + commit exceeded 60s and InvalidArgument fired.

The primary fix is the lifespan warmup in main.py; this test pins the
defense-in-depth retry layer in rate_limit.py._run_txn_with_retry.

Test environment requirement: this file imports `rate_limit`, which in turn
imports `from google.cloud import firestore` and
`from google.api_core.exceptions import InvalidArgument` at module load
time. The `pytest.importorskip` guards below skip the entire file when
the google SDKs are not installed (e.g. when `python3 -m pytest` runs
from a shell where the project venv is not activated). Same convention
as test_gemini.py — preserves build-dev.sh Step 0 outside the venv while
still exercising the tests when the SDK is present.
"""

from unittest.mock import MagicMock

import pytest

# Skip gracefully when google SDKs are not installed (matches test_gemini.py
# convention). rate_limit.py imports from both google.api_core and
# google.cloud.firestore, so either missing aborts collection entirely.
pytest.importorskip("google.api_core")
pytest.importorskip("google.cloud.firestore")

from google.api_core.exceptions import InvalidArgument  # noqa: E402

from rate_limit import _EXPIRED_TXN_MARKER, _run_txn_with_retry  # noqa: E402


class TestRunTxnWithRetry:
    """Unit-level coverage of the cold-start retry helper."""

    def test_happy_path_calls_txn_once(self):
        db = MagicMock()
        ref = MagicMock()
        txn_fn = MagicMock(return_value="ok")

        result = _run_txn_with_retry(db, txn_fn, ref, label="t")

        assert result == "ok"
        assert txn_fn.call_count == 1

    def test_retries_once_on_transaction_expired(self):
        db = MagicMock()
        ref = MagicMock()
        txn_fn = MagicMock(
            side_effect=[
                InvalidArgument(
                    "400 The referenced transaction has expired or is no longer valid."
                ),
                "ok",
            ],
        )

        result = _run_txn_with_retry(db, txn_fn, ref, label="t")

        assert result == "ok"
        assert txn_fn.call_count == 2

    def test_does_not_retry_on_unrelated_invalid_argument(self):
        # Narrow string match — generic InvalidArgument (malformed field
        # path, invalid filter op, etc.) must propagate so real bugs surface.
        db = MagicMock()
        ref = MagicMock()
        txn_fn = MagicMock(
            side_effect=InvalidArgument("invalid field path 'foo.bar'"),
        )

        with pytest.raises(InvalidArgument):
            _run_txn_with_retry(db, txn_fn, ref, label="t")

        assert txn_fn.call_count == 1

    def test_second_failure_propagates(self):
        # Policy: one retry, then give up. Never retry-loop forever.
        db = MagicMock()
        ref = MagicMock()
        txn_fn = MagicMock(
            side_effect=[
                InvalidArgument("transaction has expired attempt 1"),
                InvalidArgument("transaction has expired attempt 2"),
            ],
        )

        with pytest.raises(InvalidArgument, match="attempt 2"):
            _run_txn_with_retry(db, txn_fn, ref, label="t")

        assert txn_fn.call_count == 2

    def test_marker_substring_is_stable(self):
        # If the GCP error format ever changes, this is the single point
        # of repair. Pin the literal so a silent string drift is loud.
        assert _EXPIRED_TXN_MARKER == "transaction has expired"

    def test_each_attempt_constructs_a_fresh_transaction(self):
        # The whole point of retry is that the OLD transaction is dead.
        # Calling db.transaction() once and reusing it would defeat the fix.
        db = MagicMock()
        ref = MagicMock()
        txn_fn = MagicMock(
            side_effect=[
                InvalidArgument("transaction has expired"),
                "ok",
            ],
        )

        _run_txn_with_retry(db, txn_fn, ref, label="t")

        assert db.transaction.call_count == 2
