"""LPB environment classifier (LPB plan D6; spec 2026-09-10).

At dispatch, Turbo classifies the LKP as urban or rural/wilderness from the 2020
Census block's UR flag, and — for rural LKPs only — as flat or hilly/mountainous
from elevation relief within 2 km. The result is ONE line under the LPB header.
Rings are unchanged in this release.

main.py is not importable under local pytest (GCP clients at import time), so
these tests EXECUTE the production block lifted out of main.py between its
begin/end markers — never a hand-written mirror. Network calls go through a
fake `httpx` injected into the block's namespace.

Calibration fixtures are the spike's measured signals for places Bill labelled
on 2026-09-10 (numbers only; no coordinates of any real incident).
"""
import asyncio
import logging
import math
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

MAIN = Path(__file__).parent / "main.py"
_BEGIN = "# ── BEGIN LPB environment classifier"
_END = "# ── END LPB environment classifier"


def _load(fake_httpx=None):
    src = MAIN.read_text(encoding="utf-8")
    start, end = src.index(_BEGIN), src.index(_END)
    ns = {"re": re, "math": math, "asyncio": asyncio, "time": time,
          "logger": logging.getLogger("test_env"), "httpx": fake_httpx}
    exec(src[start:end], ns)  # noqa: S102 - production source, not input
    return ns


# ---------------------------------------------------------------------------
# Fake httpx
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class _FakeHttpx:
    """Scripted stand-in for httpx.AsyncClient.

    census(x, y) -> "U" | "R" | None (no block) | Exception to raise
    elevation(n) -> list of n elevations | Exception to raise
    """

    def __init__(self, census, elevation=None, delay_s=0.0):
        self.census, self.elevation, self.delay_s = census, elevation, delay_s
        self.calls = []  # (host, params)
        outer = self

        class AsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, params=None, **k):
                outer.calls.append((url, dict(params or {})))
                if outer.delay_s:
                    await asyncio.sleep(outer.delay_s)
                if "geocoding.geo.census.gov" in url:
                    v = outer.census(float(params["x"]), float(params["y"]))
                    if isinstance(v, Exception):
                        raise v
                    blocks = [] if v is None else [{"UR": v}]
                    return _Resp(200, {"result": {"geographies": {"2020 Census Blocks": blocks}}})
                if "open-meteo.com" in url:
                    n = len(params["latitude"].split(","))
                    v = outer.elevation(n)
                    if isinstance(v, Exception):
                        raise v
                    return _Resp(200, {"elevation": v})
                raise AssertionError(f"unexpected URL {url}")

        self.AsyncClient = AsyncClient

    def hosts(self):
        return [u for u, _ in self.calls]


def _run(ns, lat=37.33412, lng=-121.90987):
    return asyncio.run(ns["_classify_environment"](lat, lng))


# ---------------------------------------------------------------------------
# The rule (pure) — calibrated against Bill's labels
# ---------------------------------------------------------------------------
class TestEnvRule:
    # (label, ur_point, ring_urs, relief_2km_m, expected_population, expected_terrain)
    CASES = [
        ("urban core",               "U", ["U"] * 8,              17,  "urban", None),
        ("03766 Urban/Flat",         "U", ["U"] * 8,              87,  "urban", None),
        ("Alviso Rural/Flat",        "R", ["U", "U"] + ["R"] * 6, 10,  "rural", "flat"),
        ("Stanford Dish Rural/Hilly", "R", ["U"] + ["R"] * 7,     118, "rural", "mountainous"),
        ("Coyote Valley Wild/Hilly", "R", ["U", "U"] + ["R"] * 6, 153, "rural", "mountainous"),
        ("Rancho Wild/Mountainous",  "R", ["U"] * 5 + ["R"] * 3,  211, "rural", "mountainous"),
        ("Bonny Doon Rural/Hilly",   "R", ["R"] * 8,              440, "rural", "mountainous"),
    ]

    @pytest.mark.parametrize("label,ur,ring,relief,pop,terr", CASES)
    def test_labelled_places(self, label, ur, ring, relief, pop, terr):
        r = _load()["_env_from_signals"](ur, ring, relief if ur == "R" else None)
        assert (r["population"], r["terrain"]) == (pop, terr), label

    def test_relief_cutoff_boundary(self):
        f = _load()["_env_from_signals"]
        assert f("R", ["R"] * 8, 99)["terrain"] == "flat"
        assert f("R", ["R"] * 8, 100)["terrain"] == "mountainous"

    def test_urban_never_evaluates_terrain(self):
        """ISRID's Urban tables ignore terrain — a relief value must not leak in."""
        assert _load()["_env_from_signals"]("U", ["U"] * 8, 400)["terrain"] is None

    def test_failed_lookup_is_not_determined(self):
        r = _load()["_env_from_signals"](None, [None] * 8, None)
        assert r["population"] is None and r["status"] == "not_determined"

    def test_no_census_block_is_its_own_status(self):
        """A lookup that WORKED and found no block means the LKP is outside
        the US — almost always a bad geocode (2026-07-31 Nova Scotia). That is
        a different message from "the lookup failed"."""
        r = _load()["_env_from_signals"]("", [""] * 8, None)
        assert r["population"] is None and r["status"] == "no_census_block"

    def test_interface_rural_point_mostly_urban_ring(self):
        r = _load()["_env_from_signals"]("R", ["U"] * 5 + ["R"] * 3, 211)
        assert r["interface"] is True and r["urban_frac"] == pytest.approx(5 / 8)

    def test_interface_urban_point_mostly_rural_ring(self):
        assert _load()["_env_from_signals"]("U", ["U"] * 3 + ["R"] * 5, None)["interface"] is True

    def test_interface_threshold_boundary(self):
        f = _load()["_env_from_signals"]
        assert f("R", ["U"] * 4 + ["R"] * 4, 50)["interface"] is True    # 0.50 -> edge
        assert f("U", ["U"] * 4 + ["R"] * 4, None)["interface"] is False  # 0.50 is not < 0.5

    def test_partial_ring_never_claims_interface(self):
        """Two-leg symmetry: 7 of 8 answers is not enough to assert an edge."""
        r = _load()["_env_from_signals"]("R", ["U"] * 7 + [None], 50)
        assert r["interface"] is False and r["urban_frac"] is None


# ---------------------------------------------------------------------------
# The line, and where it goes
# ---------------------------------------------------------------------------
SUMMARY = (
    "Event Name: 2026-09-10 XXSO MAIN\n"
    "Event Log:\n2026-09-10 10:00 - Request received\n"
    "---\n\n"
    "LPB Range Ring Analysis (Robert Koester — \"Lost Person Behavior\"):\n"
    "1. Subject Category: Dementia. Key factors: Dementia, Alone.\n"
    "2. Koester Statistics for this category:\n"
    "- 0.2 miles (0.3 km) — 25th percentile distance\n"
    "- 0.3 miles (0.5 km) — 50th percentile distance (median)\n"
    "- 0.6 miles (1.0 km) — 75th percentile distance\n"
    "3. Local Modifiers: Urban grid constrains travel to street corridors.\n"
)


class TestLine:
    def test_urban_line(self):
        ns = _load()
        line = ns["_format_environment_line"](ns["_env_from_signals"]("U", ["U"] * 8, None))
        assert line.startswith("Environment: Urban or Suburban (Census 2020)")
        assert "Terrain: not used for urban areas" in line and "Temperate" in line

    def test_rural_line_names_both_d4h_options_and_relief(self):
        ns = _load()
        line = ns["_format_environment_line"](ns["_env_from_signals"]("R", ["R"] * 8, 118))
        assert "Rural or Wilderness" in line and "Hilly or Mountainous (118 m relief within 2 km)" in line

    def test_interface_adds_warning_line(self):
        ns = _load()
        line = ns["_format_environment_line"](ns["_env_from_signals"]("R", ["U"] * 5 + ["R"] * 3, 211))
        assert "62% of the area within 1 km is urban" in line

    def test_not_determined_is_explicit_never_silent(self):
        ns = _load()
        for env in (None, ns["_env_from_signals"](None, [None] * 8, None)):
            assert "Environment: not determined" in ns["_format_environment_line"](env)

    def test_no_census_block_points_at_the_geocode(self):
        ns = _load()
        line = ns["_format_environment_line"](ns["_env_from_signals"]("", [""] * 8, None))
        assert "no US Census block at the LKP" in line and "check the LKP geocode" in line
        failed = ns["_format_environment_line"](ns["_env_from_signals"](None, [None] * 8, None))
        assert "check the LKP geocode" not in failed and "lookup unavailable" in failed

    def test_residence_fallback_is_named(self):
        ns = _load()
        line = ns["_format_environment_line"](ns["_env_from_signals"]("U", ["U"] * 8, None), from_residence=True)
        assert "from Residence" in line

    def test_rural_with_failed_elevation_says_so(self):
        ns = _load()
        line = ns["_format_environment_line"](ns["_env_from_signals"]("R", ["R"] * 8, None))
        assert "Terrain: not determined" in line

    def test_no_line_starts_with_a_parser_trigger(self):
        """Three parsers key on a leading digit, `-` or `Q#` inside this section."""
        ns = _load()
        f, sig = ns["_format_environment_line"], ns["_env_from_signals"]
        for env in (sig("U", ["U"] * 8, None), sig("R", ["U"] * 5 + ["R"] * 3, 211), None):
            for ln in f(env).splitlines():
                assert not re.match(r"^(\d|-|Q\d)", ln), ln


class TestInsertion:
    def test_inserted_directly_under_the_header(self):
        ns = _load()
        out = ns["_insert_environment_line"](SUMMARY, "Environment: X")
        hdr = out.index("LPB Range Ring Analysis")
        assert out[out.index("\n", hdr) + 1:].startswith("Environment: X\n")

    def test_event_log_untouched(self):
        ns = _load()
        out = ns["_insert_environment_line"](SUMMARY, "Environment: X")
        assert out.split("---")[0] == SUMMARY.split("---")[0]

    def test_idempotent(self):
        ns = _load()
        once = ns["_insert_environment_line"](SUMMARY, "Environment: X")
        assert ns["_insert_environment_line"](once, "Environment: X") == once

    def test_no_lpb_section_is_a_no_op(self):
        ns = _load()
        assert ns["_insert_environment_line"]("Event Name: x\n", "Environment: X") == "Event Name: x\n"

    def test_rings_parse_identically_with_the_line(self):
        """The production ring parser must not see the new line."""
        src = MAIN.read_text(encoding="utf-8")
        rns = {"re": re}
        exec(src[src.index("_LPB_SECTION_RE = re.compile"):src.index("def _insert_subject_last_seen_entry")], rns)  # noqa: S102
        ns = _load()
        sig = ns["_env_from_signals"]
        for env in (sig("U", ["U"] * 8, None), sig("R", ["U"] * 5 + ["R"] * 3, 211), None):
            out = ns["_insert_environment_line"](SUMMARY, ns["_format_environment_line"](env))
            assert rns["_parse_lpb_range_rings"](out) == rns["_parse_lpb_range_rings"](SUMMARY)


# ---------------------------------------------------------------------------
# Network behaviour (fake httpx)
# ---------------------------------------------------------------------------
class TestClassifyNetwork:
    def test_urban_point_never_calls_elevation(self):
        fake = _FakeHttpx(census=lambda x, y: "U", elevation=lambda n: [0] * n)
        r = _run(_load(fake))
        assert r["population"] == "urban"
        assert not any("open-meteo" in h for h in fake.hosts())

    def test_rural_point_calls_elevation_once_with_25_points(self):
        fake = _FakeHttpx(census=lambda x, y: "R", elevation=lambda n: [100] * (n - 1) + [260])
        r = _run(_load(fake))
        meteo = [p for u, p in fake.calls if "open-meteo" in u]
        assert len(meteo) == 1 and len(meteo[0]["latitude"].split(",")) == 25
        assert r["terrain"] == "mountainous" and r["relief_m"] == 160

    def test_census_requests_the_blocks_layer_by_name(self):
        """Layer id "10" is block GROUPS (no UR flag) — verified 2026-09-10."""
        fake = _FakeHttpx(census=lambda x, y: "U")
        _run(_load(fake))
        census = [p for u, p in fake.calls if "census" in u]
        assert len(census) == 9 and all(p["layers"] == "2020 Census Blocks" for p in census)

    def test_coordinates_are_rounded_to_3dp_before_leaving(self):
        fake = _FakeHttpx(census=lambda x, y: "R", elevation=lambda n: [0] * n)
        _run(_load(fake), lat=37.3341234, lng=-121.9098765)
        for u, p in fake.calls:
            vals = [p["x"], p["y"]] if "census" in u else p["latitude"].split(",") + p["longitude"].split(",")
            for v in vals:
                assert len(v.split(".")[1]) <= 3, (u, v)
        centre = [p for u, p in fake.calls if "census" in u][0]
        assert (centre["y"], centre["x"]) == ("37.334", "-121.910")

    def test_no_block_response_is_distinguished_from_failure(self):
        fake = _FakeHttpx(census=lambda x, y: None)   # 200 OK, empty block list
        assert _run(_load(fake))["status"] == "no_census_block"

    def test_census_failure_is_not_determined_and_never_raises(self):
        fake = _FakeHttpx(census=lambda x, y: RuntimeError("boom"))
        r = _run(_load(fake))
        assert r["population"] is None and r["status"] == "not_determined"

    def test_elevation_failure_keeps_population(self):
        fake = _FakeHttpx(census=lambda x, y: "R", elevation=lambda n: RuntimeError("down"))
        r = _run(_load(fake))
        assert r["population"] == "rural" and r["terrain"] is None

    def test_budget_exceeded_falls_back(self):
        fake = _FakeHttpx(census=lambda x, y: "U", delay_s=0.3)
        ns = _load(fake)
        ns["_ENV_BUDGET_S"] = 0.05
        t = time.monotonic()
        r = _run(ns)
        assert r["status"] == "unavailable" and time.monotonic() - t < 0.25


# ---------------------------------------------------------------------------
# Wiring into /ocr (structure pins; each proven by mutation before merge)
# ---------------------------------------------------------------------------
def _ocr_code():
    src = MAIN.read_text(encoding="utf-8")
    start = src.index('@app.post("/ocr")')
    end = src.index("\n@app.", start + 10)
    return "\n".join(l.split("#")[0] for l in src[start:end].splitlines())


class TestOcrWiring:
    def test_runs_alongside_the_staging_lookup(self):
        code = _ocr_code()
        g = code.index("await asyncio.gather(\n", code.index("if geo:"))
        block = code[g:code.index(")\n", code.index("_classify_environment(", g)) + 1]
        assert "_query_staging_pois(lat, lng, radius_m=1200)" in block
        assert "_classify_environment(lat, lng)" in block

    def test_result_initialised_before_geo_branch(self):
        code = _ocr_code()
        assert code.index("_env_result = None") < code.index("_classify_environment(lat, lng)")

    def test_line_inserted_before_rings_are_parsed(self):
        code = _ocr_code()
        ins = code.index("summary = _insert_environment_line(")
        assert ins < code.index('map_data["rings"] = _parse_lpb_range_rings(summary)')
        call = code[ins:code.index("\n", code.index("_format_environment_line(", ins))]
        assert "_env_result" in call and "_lkp_from_residence" in call
