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


async def log_egress_ip_when_ready(
    log,
    deadline_s: float = 120.0,
    interval_s: float = 3.0,
    probe=observed_egress_ip,
    clock=time.monotonic,
    sleep=asyncio.sleep,
) -> str:
    """Retry the probe until it returns an address or the deadline passes, then
    log ONE line: the address (or the last failure), the elapsed time since
    this task started, and the attempt count.
    """
    start = clock()
    attempts = 0
    while True:
        attempts += 1
        result = await startup_egress_ip(probe=probe)
        elapsed = clock() - start
        if not result.startswith("unavailable:") or elapsed >= deadline_s:
            break
        await sleep(interval_s)
    try:
        log("startup egress_ip=%s after_ms=%d attempts=%d", result, int(elapsed * 1000), attempts)
    except Exception:  # noqa: BLE001 — a diagnostic must not raise out of a background task
        pass
    return result
