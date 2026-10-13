"""Pins for egress_ip (ops#875).

The helper runs inside the Cloud Run lifespan on every cold start, so the
properties that matter are (1) it reports the echoed address on success and
(2) it can NEVER raise or hang the instance: an outage at the echo service must
degrade to a logged 'unavailable' string.

egress_ip uses stdlib urllib, not httpx, so these tests run under the build's
Step 0 pytest, where httpx is not installed.
"""
import asyncio
import inspect
import io
import threading
import urllib.error

import pytest

from egress_ip import (
    EGRESS_ECHO_URL,
    log_egress_ip_when_ready,
    observed_egress_ip,
    startup_egress_ip,
)


class _Resp(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status


def _opener(body=b"", status=200, exc=None, seen=None):
    def fake(url, timeout):
        if seen is not None:
            seen.append((url, timeout))
        if exc is not None:
            raise exc
        return _Resp(body, status)
    return fake


def test_returns_trimmed_body_on_200():
    seen = []
    assert observed_egress_ip(opener=_opener(b"34.0.0.1\n", seen=seen)) == "34.0.0.1"
    assert seen == [(EGRESS_ECHO_URL, 2.0)]


def test_http_error_is_unavailable_with_status():
    err = urllib.error.HTTPError(EGRESS_ECHO_URL, 503, "busy", {}, None)
    assert observed_egress_ip(opener=_opener(exc=err)) == "unavailable:http_503"


def test_non_200_success_status_is_unavailable_with_status():
    assert observed_egress_ip(opener=_opener(b"34.0.0.1", status=204)) == "unavailable:http_204"


def test_transport_error_is_unavailable_with_exception_name():
    assert (
        observed_egress_ip(opener=_opener(exc=TimeoutError("slow")))
        == "unavailable:TimeoutError"
    )


def test_unexpected_exception_is_unavailable_not_raised():
    assert (
        observed_egress_ip(opener=_opener(exc=RuntimeError("boom")))
        == "unavailable:RuntimeError"
    )


def test_empty_body_is_unavailable():
    assert observed_egress_ip(opener=_opener(b"   ")) == "unavailable:empty"


@pytest.mark.parametrize("bad", [b"not an ip", b"999.1.1.1", b"2001:db8::1"])
def test_non_ipv4_body_is_unavailable(bad):
    assert observed_egress_ip(opener=_opener(bad)) == "unavailable:not_ipv4"


def test_default_per_request_timeout_is_two_seconds():
    sig = inspect.signature(observed_egress_ip)
    assert sig.parameters["timeout_s"].default == 2.0


# --- startup_egress_ip: the TOTAL bound -----------------------------------
# The per-request timeout covers connect and read but not DNS resolution, so
# the lifespan wraps the call in a wall-clock cap. These tests prove the cap
# holds against a call that never returns on its own.

def test_startup_returns_value_from_helper():
    result = asyncio.run(startup_egress_ip(probe=lambda: "34.0.0.1"))
    assert result == "34.0.0.1"


def test_startup_total_bound_returns_timeout_instead_of_hanging():
    release = threading.Event()

    def stuck():
        release.wait(5)
        return "34.0.0.1"

    # Not asyncio.run: it joins the default executor on exit, which would wait
    # on the stuck thread. loop.close() shuts the executor down without waiting,
    # so the thread is released only AFTER the result is in hand and the test
    # has no timing race.
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(startup_egress_ip(total_s=0.05, probe=stuck))
    finally:
        release.set()
        loop.close()
    assert result == "unavailable:timeout"


def test_startup_default_total_bound_is_three_seconds():
    sig = inspect.signature(startup_egress_ip)
    assert sig.parameters["total_s"].default == 3.0


# --- log_egress_ip_when_ready: background retry until egress works ---------
# Google documents 30 s+ before outbound connections work on a cold instance
# with Direct VPC egress + Cloud NAT. The startup line must therefore retry
# rather than report the first timeout, and must record how long it took.

def _run_ready(results, deadline_s=120.0, interval_s=3.0):
    """Drive the retry loop with a fake clock: each sleep advances time."""
    now = [0.0]
    sleeps = []
    logged = []
    seq = iter(results)

    async def fake_sleep(s):
        sleeps.append(s)
        now[0] += s

    out = asyncio.run(log_egress_ip_when_ready(
        lambda *a: logged.append(a),
        deadline_s=deadline_s,
        interval_s=interval_s,
        probe=lambda: next(seq),
        clock=lambda: now[0],
        sleep=fake_sleep,
    ))
    return out, logged, sleeps


def test_ready_first_attempt_logs_once_with_zero_wait():
    out, logged, sleeps = _run_ready(["34.0.0.1"])
    assert out == "34.0.0.1"
    assert logged == [("startup egress_ip=%s after_ms=%d attempts=%d", "34.0.0.1", 0, 1)]
    assert sleeps == []


def test_ready_retries_through_failures_and_reports_elapsed():
    out, logged, sleeps = _run_ready(
        ["unavailable:timeout", "unavailable:URLError", "203.0.113.10"]
    )
    assert out == "203.0.113.10"
    assert logged == [
        ("startup egress_ip=%s after_ms=%d attempts=%d", "203.0.113.10", 6000, 3)
    ]
    assert sleeps == [3.0, 3.0]


def test_ready_gives_up_at_deadline_and_logs_last_reason_once():
    out, logged, sleeps = _run_ready(
        ["unavailable:timeout"] * 10, deadline_s=10.0, interval_s=3.0
    )
    assert out == "unavailable:timeout"
    # Attempts at t = 0, 3, 6, 9, 12; the deadline is checked after each one.
    assert logged == [
        ("startup egress_ip=%s after_ms=%d attempts=%d", "unavailable:timeout", 12000, 5)
    ]
    assert len(sleeps) == 4


def test_ready_survives_a_failing_log_callback():
    def bad_log(*a):
        raise RuntimeError("handler down")

    out = asyncio.run(log_egress_ip_when_ready(
        bad_log, probe=lambda: "34.0.0.1", clock=lambda: 0.0,
    ))
    assert out == "34.0.0.1"


def test_ready_defaults_cover_the_documented_delay():
    sig = inspect.signature(log_egress_ip_when_ready)
    assert sig.parameters["deadline_s"].default == 120.0
    assert sig.parameters["interval_s"].default == 3.0
