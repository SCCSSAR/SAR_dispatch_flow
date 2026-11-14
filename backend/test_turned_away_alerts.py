"""Map and Incident Log alerts name the real reason the app turned a click away.

Before this, both buttons showed one canned alert for every failure. A 429
from the rate limiter, the team-wide 503 cap and an expired sign-in all told
the dispatcher to "Check CalTopo credentials" or to verify Drive consent,
neither of which a dispatcher can fix, and neither of which had failed.

The behaviour tests run the page's own helpers under node, extracted from
frontend/index.html, so there is no mirror to drift. They skip when node is
not installed; the wiring and literal pins below always run.
"""

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
HTML = (HERE.parent / "frontend" / "index.html").read_text(encoding="utf-8")
GLOBAL_CAP_DETAIL_SITE = HERE / "rate_limit.py"


def _js_fn(src: str, signature: str) -> str:
    start = src.index(signature)
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        depth += src[j] == "{"
        depth -= src[j] == "}"
        if depth == 0:
            return src[start:j + 1]
    raise AssertionError(signature)


HELPERS = "\n".join(_js_fn(HTML, sig) for sig in (
    "function _httpError(",
    "function _nextUtcMidnightPacific(",
    "function _turnedAwayMessage(",
))


def _run(cases: list) -> list:
    if shutil.which("node") is None:
        pytest.skip("node not installed: behaviour of the index.html helpers not exercised")
    script = HELPERS + """
const cases = JSON.parse(process.argv[1]);
const out = cases.map(([status, retryAfter, body, nowIso]) => {
  const resp = { status, headers: { get: (k) => (k === 'Retry-After' ? retryAfter : null) } };
  const err = _httpError(resp, body);
  return _turnedAwayMessage('Map not created', err, nowIso ? new Date(nowIso) : new Date());
});
process.stdout.write(JSON.stringify(out));
"""
    res = subprocess.run(["node", "-e", script, json.dumps(cases)],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _global_cap_detail() -> str:
    tree = ast.parse(GLOBAL_CAP_DETAIL_SITE.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_check_global_cap")
    for call in ast.walk(fn):
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "HTTPException":
            kw = {k.arg: k.value for k in call.keywords}
            if getattr(kw.get("status_code"), "value", None) == 503:
                return kw["detail"].value
    raise AssertionError("no 503 HTTPException in _check_global_cap")


RATE = {"detail": "Rate limit exceeded. Please try again later."}
JAN = "2027-01-15T20:00:00Z"   # PST: midnight UTC is 4:00 PM Pacific
JUL = "2027-07-15T20:00:00Z"   # PDT: midnight UTC is 5:00 PM Pacific


class TestTurnedAwayMessages:
    def test_each_rate_limit_window_is_named(self):
        minute, hour, day = _run([[429, "60", RATE, JAN],
                                  [429, "3600", RATE, JAN],
                                  [429, "86400", RATE, JAN]])
        assert minute.startswith("Map not created: ")
        assert "one minute" in minute
        assert "hourly limit" in hour and "start of the next hour" in hour
        assert "daily limit" in day and "4:00 PM Pacific" in day

    def test_daily_reset_follows_daylight_time(self):
        [day] = _run([[429, "86400", RATE, JUL]])
        assert "5:00 PM Pacific" in day

    def test_team_cap_is_not_blamed_on_the_dispatcher(self):
        [msg] = _run([[503, None, {"detail": _global_cap_detail()}, JAN]])
        assert "whole team" in msg and "4:00 PM Pacific" in msg

    def test_a_503_with_retry_after_says_how_long(self):
        # /create-map's CalTopo rate-limit branch: 503 + Retry-After 10.
        [msg] = _run([[503, "10", {"detail": "CalTopo is rate-limited"}, JAN]])
        assert "Wait 10 seconds" in msg
        assert "whole team" not in msg

    def test_a_bare_503_is_not_called_the_team_cap(self):
        # Cloud Run's own 503 has an HTML body: no detail, no Retry-After.
        [msg] = _run([[503, None, {}, JAN]])
        assert msg is None

    def test_sign_in_refusals(self):
        for status in (401, 403):
            [msg] = _run([[status, None, {"detail": "Unauthorized"}, JAN]])
            assert "sign in again" in msg

    def test_real_failures_keep_the_buttons_own_message(self):
        assert _run([[502, None, {"detail": "CalTopo map creation failed"}, JAN],
                     [422, None, {"detail": []}, JAN]]) == [None, None]

    def test_error_message_still_carries_the_detail(self):
        # The map catch block finds an orphan map by searching err.message.
        if shutil.which("node") is None:
            pytest.skip("node not installed")
        script = HELPERS + """
const e = _httpError({ status: 502, headers: { get: () => null } },
                     { detail: 'partially created - orphan map id: ABC' });
process.stdout.write(e.message);
"""
        res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        assert res.stdout == "partially created - orphan map id: ABC"


class TestWiring:
    def test_team_cap_literal_matches_rate_limit_py(self):
        # Cross-file literal: the helper recognises the cap by its detail text.
        fn = _js_fn(HTML, "function _turnedAwayMessage(")
        assert f"=== {json.dumps(_global_cap_detail())}" in fn

    @pytest.mark.parametrize("handler_start, action, old_throw", [
        ("const resp = await fetch(`${BACKEND_URL}/create-doc`", "Incident Log not created",
         "throw new Error(err.detail"),
        ("let resp = await fetch('/create-map'", "Map not created",
         "throw new Error(err.detail"),
        ("let resp = await fetch(`${BACKEND_URL}/send-notification`", "Notification not sent",
         "throw new Error(`Send failed (${resp.status}): ${errText}`)"),
    ])
    def test_each_button_routes_failures_through_the_helpers(self, handler_start, action, old_throw):
        start = HTML.index(handler_start)
        catch = HTML.index("} catch (err) {", start)
        body = HTML[start:catch]
        end = HTML.index("\n  });", catch)
        catch_block = "\n".join(l.split("//")[0] for l in HTML[catch:end].splitlines())
        not_ok = body.index("if (!resp.ok) {")
        block = body[not_ok:body.index("\n      }", not_ok)]
        assert "throw _httpError(resp, " in block
        assert old_throw not in body
        if "resp.text()" in block:
            # Send reads text; without the parse the team cap's detail is lost.
            assert "errBody = JSON.parse(errText)" in block
        call = f"_turnedAwayMessage('{action}', err)"
        assert call in catch_block
        # The helper's answer must be what the alert shows, under its own test.
        guard = re.search(r"if \(turnedAway\) \{\s*alert\(turnedAway\);", catch_block)
        assert guard, "alert(turnedAway) must sit directly under `if (turnedAway)`"
        assert catch_block.index(call) < guard.start()


class TestExtractStatus:
    """Extract showed "wait 3600 seconds" or "86400 seconds" on a 429, and
    called the all-day team cap "try again in a few minutes"."""

    BRANCH = "if (resp.status === 429 || resp.status === 503) {"

    def _branch(self) -> str:
        start = HTML.index(self.BRANCH)
        return HTML[start:HTML.index("return;", start)]

    def test_limits_go_through_the_helper(self):
        branch = self._branch()
        assert "_hideNotifyChecklist();" in branch
        assert ('showStatus("error", _turnedAwayMessage("Form not read", '
                '_httpError(resp, ') in branch

    def test_a_bare_503_keeps_the_old_message(self):
        assert ('|| "Service temporarily unavailable. Please try again in a few minutes.");'
                in self._branch())

    def test_old_messages_are_gone(self):
        assert "seconds before trying again" not in HTML
        assert HTML.count("resp.status === 429") == 1, "one 429 branch in the Extract handler"


class TestMapGenericAlert:
    """A dispatcher cannot check CalTopo credentials: they live in Secret
    Manager. The generic map alert now says what the dispatcher CAN do."""

    NEW = "Map creation failed. Try again once. If it fails again, create the map in CalTopo by hand."

    def _map_catch(self) -> str:
        start = HTML.index("let resp = await fetch('/create-map'")
        catch = HTML.index("} catch (err) {", start)
        return HTML[catch:HTML.index("\n  });", catch)]

    def test_generic_branch_shows_the_new_text(self):
        catch = self._map_catch()
        tail = catch[catch.index("} else if (turnedAway) {"):]
        assert re.search(r"\} else \{\s*(//[^\n]*\s*)*alert\(" + re.escape(f'"{self.NEW}"') + r"\);", tail)

    def test_credentials_advice_is_gone(self):
        assert "Check CalTopo credentials" not in HTML
