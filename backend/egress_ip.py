"""Observed egress address, logged once per instance at startup (ops#875).

Why: on 2026-10-02 CalTopo and D4H blocked Cloud Run's shared egress address
and nothing on our side recorded which address vendors had seen. With a static
egress address (Terraform output `egress_ip`) the EXPECTED value is known by
construction; this records the OBSERVED one, so a revision that silently lost
its VPC setting shows up as a Google pool address in the startup log.

Must never raise and must never delay serving: it runs as a background task
started from the lifespan, retrying because Google documents 30 s+ before
outbound connections work on a cold instance with Direct VPC egress + Cloud
NAT. The logged `after_ms` is that delay, measured per instance. Any failure
degrades to an 'unavailable:<reason>' string that is still worth logging.

Stdlib urllib rather than httpx so the module imports under the build's Step 0
pytest, where httpx is not installed, and its tests actually run there.
"""
import asyncio
import ipaddress
import time
import urllib.error
import urllib.request

EGRESS_ECHO_URL = "https://api.ipify.org"

# An IPv4 address is at most 15 characters; read a little more so a trailing
# newline survives, and never buffer an unexpected large body.
_MAX_BODY_BYTES = 64


def observed_egress_ip(
    timeout_s: float = 2.0,
    url: str = EGRESS_ECHO_URL,
    opener=urllib.request.urlopen,
) -> str:
    try:
        with opener(url, timeout=timeout_s) as resp:
            status = resp.status
            raw = resp.read(_MAX_BODY_BYTES)
    except urllib.error.HTTPError as e:
        e.close()
        return f"unavailable:http_{e.code}"
    except Exception as e:  # noqa: BLE001 — startup must not fail on the echo
        return f"unavailable:{type(e).__name__}"
    if status != 200:
        return f"unavailable:http_{status}"
    body = raw.decode("ascii", errors="replace").strip()
    if not body:
        return "unavailable:empty"
    try:
        ipaddress.IPv4Address(body)
    except ValueError:
        return "unavailable:not_ipv4"
    return body


async def startup_egress_ip(total_s: float = 3.0, probe=observed_egress_ip) -> str:
    """Run the blocking probe in the executor under a wall-clock cap.

    `timeout_s` above bounds each socket operation but not DNS resolution, so
    without this cap a resolver stall would hold the lifespan, and with it the
    instance's first request, for as long as the resolver takes.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, probe), total_s)
    except asyncio.TimeoutError:
        return "unavailable:timeout"



def _got_http_answer(result: str) -> bool:
    """True when the probe reached the echo service and got any HTTP response.
    Only exception-type failures (timeout, URLError, resets) mean no path out."""
    return not result.startswith("unavailable:") or result in (
        "unavailable:empty", "unavailable:not_ipv4",
    ) or result.startswith("unavailable:http_")


class EgressGate:
    """Startup gate for /health, the Cloud Run startup probe (ops#875).

    With Direct VPC egress + Cloud NAT an instance can pass its startup probe
    30-44 s before outbound connections work (measured 2026-10-13), so it would
    take requests whose geocoding and vendor calls then fail. Closed until the
    first proof of connectivity, or until `cap_s` so an echo-service outage can
    only delay startup, never prevent it. Never closes again once open, so the
    liveness probe, which also reads /health, cannot be failed by it.
    """

    def __init__(self, cap_s: float = 60.0, clock=time.monotonic):
        self._cap_s = cap_s
        self._clock = clock
        self._start = clock()
        self._connected = False

    def mark_connected(self) -> None:
        self._connected = True

    def is_open(self) -> bool:
        return self._connected or self._clock() - self._start >= self._cap_s

async def log_egress_ip_when_ready(
    log,
    deadline_s: float = 120.0,
    interval_s: float = 3.0,
    probe=observed_egress_ip,
    clock=time.monotonic,
    sleep=asyncio.sleep,
    on_connected=None,
) -> str:
    """Retry the probe until it returns an address or the deadline passes, then
    log ONE line: the address (or the last failure), the elapsed time since
    this task started, and the attempt count.

    `on_connected` is called after every attempt that got an HTTP answer at all,
    which proves outbound connectivity even when the echo service misbehaves.
    """
    start = clock()
    attempts = 0
    while True:
        attempts += 1
        result = await startup_egress_ip(probe=probe)
        elapsed = clock() - start
        if on_connected is not None and _got_http_answer(result):
            try:
                on_connected()
            except Exception:  # noqa: BLE001 — the gate's cap still opens it
                pass
        if not result.startswith("unavailable:") or elapsed >= deadline_s:
            break
        await sleep(interval_s)
    try:
        log("startup egress_ip=%s after_ms=%d attempts=%d", result, int(elapsed * 1000), attempts)
    except Exception:  # noqa: BLE001 — a diagnostic must not raise out of a background task
        pass
    return result
