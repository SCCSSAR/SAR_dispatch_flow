"""
test_rate_limit_txn_retry.py — the Firestore transaction retry in rate_limit.py,
executed straight out of the production source.

test_rate_limit.py covers the same helper but skips at module level when the
google SDKs are missing, which is the case for system python3 and therefore for
the build scripts' Step 0. This file needs no SDK: it parses rate_limit.py,
exec's the retry constants and `_run_txn_with_retry` with a stand-in
`InvalidArgument`, and tests what the real function does.

Why more than one retry (2026-11-05): on personal-dev revision 00320 the commit
failed with "The referenced transaction has expired or is no longer valid" less
than a second after the transaction began, so not the 60 s cold-start lifetime
issue #522 was written for. Once the immediate retry succeeded (11-04, /ocr);
once it failed too and /everbridge-contacts returned 500 (11-05).
"""

import ast
import typing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_SRC = Path(__file__).with_name("rate_limit.py").read_text()
_WANTED_ASSIGNS = {"_EXPIRED_TXN_MARKER", "_TXN_MAX_ATTEMPTS", "_TXN_RETRY_BACKOFF_S"}


class FakeInvalidArgument(Exception):
    pass


def _load(uniform=lambda a, b: 1.0):
    """Exec the production constants + helper into a namespace with stubs.

    Returns (namespace, sleeps) where `sleeps` records every time.sleep arg.
    """
    tree = ast.parse(_SRC)
    keep = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in _WANTED_ASSIGNS for t in node.targets
        ):
            keep.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_run_txn_with_retry":
            keep.append(node)
    names = {getattr(n, "name", None) or n.targets[0].id for n in keep}
    assert names == _WANTED_ASSIGNS | {"_run_txn_with_retry"}, (
        f"rate_limit.py is missing part of the retry contract: found {sorted(names)}"
    )
    sleeps: list[float] = []
    ns = {
        "InvalidArgument": FakeInvalidArgument,
        "logger": MagicMock(),
        "time": SimpleNamespace(sleep=sleeps.append),
        "random": SimpleNamespace(uniform=uniform),
        "firestore": SimpleNamespace(
            Client=object, Transaction=object, DocumentReference=object
        ),
        "Callable": typing.Callable,
        "T": typing.TypeVar("T"),
    }
    exec(compile(ast.Module(body=keep, type_ignores=[]), "rate_limit.py", "exec"), ns)
    return ns, sleeps


def _expired(n):
    return FakeInvalidArgument(
        f"400 The referenced transaction has expired or is no longer valid. attempt {n}"
    )


class TestTxnRetryFromProductionSource:

    def test_three_attempts_with_two_backoffs(self):
        ns, _ = _load()
        assert ns["_TXN_MAX_ATTEMPTS"] == 3
        # Indexed by attempt: a short tuple IndexErrors on the path the retry
        # exists to serve.
        assert len(ns["_TXN_RETRY_BACKOFF_S"]) == ns["_TXN_MAX_ATTEMPTS"] - 1
        assert list(ns["_TXN_RETRY_BACKOFF_S"]) == sorted(ns["_TXN_RETRY_BACKOFF_S"])

    def test_succeeds_on_third_attempt(self):
        ns, sleeps = _load()
        db, ref = MagicMock(), MagicMock()
        txn_fn = MagicMock(side_effect=[_expired(1), _expired(2), "ok"])
        assert ns["_run_txn_with_retry"](db, txn_fn, ref, label="t") == "ok"
        assert txn_fn.call_count == 3
        assert sleeps == list(ns["_TXN_RETRY_BACKOFF_S"])

    def test_gives_up_after_last_attempt_without_a_final_sleep(self):
        ns, sleeps = _load()
        txn_fn = MagicMock(side_effect=[_expired(1), _expired(2), _expired(3)])
        with pytest.raises(FakeInvalidArgument, match="attempt 3"):
            ns["_run_txn_with_retry"](MagicMock(), txn_fn, MagicMock(), label="t")
        assert txn_fn.call_count == 3
        assert len(sleeps) == 2, "no sleep after the last attempt"

    def test_unrelated_invalid_argument_is_not_retried(self):
        ns, sleeps = _load()
        txn_fn = MagicMock(side_effect=FakeInvalidArgument("invalid field path 'a.b'"))
        with pytest.raises(FakeInvalidArgument, match="field path"):
            ns["_run_txn_with_retry"](MagicMock(), txn_fn, MagicMock(), label="t")
        assert txn_fn.call_count == 1
        assert sleeps == []

    def test_each_attempt_gets_a_fresh_transaction(self):
        ns, _ = _load()
        db = MagicMock()
        txn_fn = MagicMock(side_effect=[_expired(1), _expired(2), "ok"])
        ns["_run_txn_with_retry"](db, txn_fn, MagicMock(), label="t")
        assert db.transaction.call_count == 3

    @pytest.mark.parametrize("pick", ["low", "high"])
    def test_backoff_is_jittered_between_half_and_one_and_a_half(self, pick):
        seen = []

        def uniform(a, b):
            seen.append((a, b))
            return a if pick == "low" else b

        ns, sleeps = _load(uniform=uniform)
        txn_fn = MagicMock(side_effect=[_expired(1), _expired(2), "ok"])
        ns["_run_txn_with_retry"](MagicMock(), txn_fn, MagicMock(), label="t")
        assert seen == [(0.5, 1.5)] * 2
        factor = 0.5 if pick == "low" else 1.5
        assert sleeps == pytest.approx([b * factor for b in ns["_TXN_RETRY_BACKOFF_S"]])
