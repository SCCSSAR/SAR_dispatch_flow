"""Off-call list in the intake waiting panel (Bill, 2026-11-08).

While the form is being read, the dispatcher sees who is off-call in D4H, so
they know which roles need a backup before anything is paged. Display only.

d4h.py and main.py are not importable under local pytest (httpx / GCP deps),
so these tests EXEC the production functions out of the source files and test
their output. There is no mirror to drift.
"""
import ast
import re
from pathlib import Path

HERE = Path(__file__).parent


def _exec(filename: str, wanted: set, ns: dict | None = None) -> dict:
    """Exec the named top-level defs/assignments, in source order."""
    src = (HERE / filename).read_text(encoding="utf-8")
    ns = {} if ns is None else ns
    found = set()
    for node in ast.parse(src).body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in wanted:
            exec(ast.get_source_segment(src, node), ns)
            found.add(name)
    assert found == wanted, f"missing from {filename}: {wanted - found}"
    return ns


def _parser():
    ns = _exec("d4h.py", {"D4H_SUPPORT_CUSTOM_STATUS_ID", "_parse_off_call_response"})
    return ns["_parse_off_call_response"]


def _entries():
    ns = _exec("main.py", {"_OCEAN_RE", "_OFF_CALL_PANEL_HIDDEN_TEAMS", "_off_call_panel_entries"},
               {"re": re})
    return ns["_off_call_panel_entries"]


def _duty(mid, ref="305", name="Alvarez, Troy", status="OPERATIONAL", custom=None):
    return {"type": "OFF", "notes": "MEDICAL - do not print",
            "member": {"id": mid, "ref": ref, "name": name, "status": status,
                       "notes": "private", "customStatus": {"id": custom, "resourceType": "CustomMemberStatus"}}}


class TestParserCarriesStatus:
    def test_support_sub_status_is_flagged(self):
        out = _parser()({"results": [_duty(1, status="NON_OPERATIONAL", custom=1654)]})
        assert out[0]["status"] == "NON_OPERATIONAL"
        assert out[0]["support"] is True

    def test_loa_is_not_support(self):
        # LOA (2020) has the same member count as Support (7): the id, not a count, decides.
        out = _parser()({"results": [_duty(1, status="NON_OPERATIONAL", custom=2020)]})
        assert out[0]["support"] is False

    def test_operational_member(self):
        out = _parser()({"results": [_duty(1)]})
        assert out[0]["status"] == "OPERATIONAL" and out[0]["support"] is False

    def test_missing_status_and_custom_status_are_safe(self):
        out = _parser()({"results": [{"member": {"id": 1, "ref": "305", "name": "X"}}]})
        assert out[0]["status"] == "" and out[0]["support"] is False

    def test_notes_never_carried(self):
        out = _parser()({"results": [_duty(1)]})
        assert set(out[0]) == {"member_id", "ref", "name", "status", "support"}

    def test_non_dict_custom_status_cannot_raise(self):
        # The send path reads this parser too: an odd payload must not throw there.
        for odd in (1654, "Support", ["x"]):
            row = _duty(1)
            row["member"]["customStatus"] = odd
            assert _parser()({"results": [row]})[0]["support"] is False

    def test_support_id_is_the_probed_value(self):
        # GET /team/1775/member-custom-statuses, 2026-11-08: Support = 1654,
        # and every sub-status count matched the D4H UI (Support 7, LOA 7, ...).
        ns = _exec("d4h.py", {"D4H_SUPPORT_CUSTOM_STATUS_ID"})
        assert ns["D4H_SUPPORT_CUSTOM_STATUS_ID"] == 1654


def _m(name, ref="305", status="OPERATIONAL", support=False, mid=1):
    return {"member_id": mid, "ref": ref, "name": name, "status": status, "support": support}


K9, UAS, OPS = "g1", "g2", "g3"
GROUPS = [{"id": K9, "name": "K9"}, {"id": UAS, "name": "UAS"}, {"id": OPS, "name": "Ops / IC"}]


class TestPanelEntries:
    def test_operational_and_support_shown_others_hidden(self):
        off = [_m("Alvarez, Troy"),
               _m("Fletcher, Stephen", ref="282", status="NON_OPERATIONAL", support=True),
               _m("Leave, On", ref="111", status="NON_OPERATIONAL"),
               _m("Gone, Long", ref="112", status="RETIRED")]
        out = _entries()(off, GROUPS, {K9: [], UAS: [], OPS: []})
        assert [e["name"] for e in out] == ["Troy Alvarez", "Stephen Fletcher"]

    def test_support_flag_passes_through(self):
        out = _entries()([_m("Fletcher, Stephen", status="NON_OPERATIONAL", support=True)], GROUPS, {})
        assert out[0]["support"] is True

    def test_sorted_by_last_name(self):
        out = _entries()([_m("Zed, Amy", ref="301"), _m("adams, Zoe", ref="302")], GROUPS, {})
        assert [e["name"] for e in out] == ["Zoe adams", "Amy Zed"]

    def test_name_spacing_is_tidied(self):
        out = _entries()([_m("Mateos,  Miguel")], GROUPS, {})
        assert out[0]["name"] == "Miguel Mateos"

    def test_teams_joined_on_ocean(self):
        gm = {K9: [{"ocean": "305"}, {"ocean": "999"}], UAS: [{"ocean": "305"}], OPS: [{"ocean": "999"}]}
        out = _entries()([_m("Alvarez, Troy", ref="305")], GROUPS, gm)
        assert out[0]["teams"] == ["K9", "UAS"]

    def test_matched_member_in_no_group_has_empty_teams(self):
        out = _entries()([_m("Alvarez, Troy", ref="305")], GROUPS, {K9: [{"ocean": "999"}]})
        assert out[0]["teams"] == []

    def test_ref_that_is_not_an_ocean_has_unknown_teams(self):
        out = _entries()([_m("Dunn, Casey", ref="A12")], GROUPS, {K9: [{"ocean": "A12"}]})
        assert out[0]["teams"] is None

    def test_contact_without_ocean_never_matches_blank_ref(self):
        out = _entries()([_m("Dunn, Casey", ref="")], GROUPS, {K9: [{"ocean": None}, {"ocean": ""}]})
        assert out[0]["teams"] is None

    def test_everbridge_unavailable_means_no_teams_for_anyone(self):
        out = _entries()([_m("Alvarez, Troy")], None, None)
        assert out[0]["teams"] is None

    def test_all_members_label_is_hidden(self):
        # Every member is in it, so it says nothing about who covers what (Bill, 2026-11-08).
        groups = GROUPS + [{"id": "g9", "name": "All Members"}]
        gm = {"g9": [{"ocean": "305"}, {"ocean": "306"}], UAS: [{"ocean": "305"}]}
        out = _entries()([_m("Alvarez, Troy", ref="305"), _m("Bell, Ann", ref="306")], groups, gm)
        assert out[0]["teams"] == ["UAS"]
        assert out[1]["teams"] == [], "in All Members only: matched, but no team worth showing"

    def test_hidden_label_match_ignores_case(self):
        groups = [{"id": "g9", "name": "ALL MEMBERS"}]
        out = _entries()([_m("Alvarez, Troy", ref="305")], groups, {"g9": [{"ocean": "305"}]})
        assert out[0]["teams"] == []

    def test_only_display_fields_reach_the_browser(self):
        out = _entries()([_m("Alvarez, Troy")], GROUPS, {})
        assert set(out[0]) == {"name", "support", "teams"}


def _strip_comments(src: str) -> str:
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def _endpoint_src() -> str:
    src = (HERE / "main.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "off_call_now":
            return ast.get_source_segment(src, node)
    raise AssertionError("off_call_now endpoint not found in main.py")


class TestOffCallNowEndpointWiring:
    def test_route_and_auth(self):
        src = (HERE / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "off_call_now")
        route = [ast.get_source_segment(src, d) for d in fn.decorator_list]
        assert route == ['app.get("/off-call-now")']
        assert "Depends(require_authorized_dispatcher)" in ast.get_source_segment(src, fn.args.defaults[0])

    def test_d4h_read_fails_open_inside_try(self):
        fn = ast.parse(_endpoint_src()).body[0]
        guarded = [t for t in ast.walk(fn) if isinstance(t, ast.Try)
                   and any(isinstance(c, ast.Attribute) and c.attr == "get_off_call_now"
                           for s in t.body for c in ast.walk(s))]
        assert guarded, "d4h.get_off_call_now must be called inside a try"
        handler = guarded[0].handlers[0]
        assert isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
        assert any(isinstance(s, ast.Return) for s in handler.body), "the except must return the unavailable body"

    def test_entries_helper_is_called(self):
        assert "_off_call_panel_entries(off_call," in _strip_comments(_endpoint_src())

    def test_does_not_spend_the_per_user_rate_limit(self):
        # The per-user limit (5/min) is shared with /ocr. This request fires
        # with Extract, so counting it could push /ocr itself into a 429 and
        # break the rule that the panel never blocks intake. AST, not a
        # substring: the docstring names the function to explain its absence.
        calls = [c.func.id for c in ast.walk(ast.parse(_endpoint_src()))
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
        assert "JSONResponse" in calls, "sanity: the walk sees real calls"
        assert "check_rate_limits" not in calls


class TestWaitingPanelFrontend:
    def _html(self) -> str:
        return (HERE.parent / "frontend" / "index.html").read_text(encoding="utf-8")

    def test_extract_fires_the_off_call_fetch(self):
        html = self._html()
        start = html.index('submitBtn.addEventListener("click"')
        handler = html[start:html.index("const formData = new FormData();", start)]
        assert "loadOffCallPanel();" in handler

    def test_fetch_target(self):
        assert "${BACKEND_URL}/off-call-now" in self._html()

    def test_footer_wording_agreed_with_bill(self):
        assert ("Off-call members aren't paged through their groups. "
                "To page one anyway, add them by name in Step 3.") in self._html()

    def test_one_footer_for_every_mode(self):
        # The draft sentence only mattered in personal-dev testing (Bill, 2026-11-08).
        html = self._html()
        assert "a draft keeps them" not in html
        assert "_everbridgeMode" not in html, "the mode switch existed only for that sentence"

    @staticmethod
    def _js_fn(html: str, signature: str) -> str:
        start = html.index(signature)
        depth, i = 0, html.index("{", start)
        for j in range(i, len(html)):
            depth += html[j] == "{"
            depth -= html[j] == "}"
            if depth == 0:
                return html[start:j + 1]
        raise AssertionError(signature)

    def test_off_call_fetch_starts_when_the_form_is_picked(self):
        # /ocr's Gemini call holds the event loop (~20 s); fetched on Extract the
        # panel waited for it (live 1.11.82: 22.9 s). Picking the form comes first.
        html = self._html()
        assert "prefetchOffCall();" in self._js_fn(html, "function setFile(file)")

    def test_one_request_serves_pick_and_extract(self):
        html = self._html()
        assert html.count("${BACKEND_URL}/off-call-now") == 1
        assert "${BACKEND_URL}/off-call-now" in self._js_fn(html, "function prefetchOffCall()")
        loader = self._js_fn(html, "async function loadOffCallPanel()")
        assert "await _offCallPromise" in loader


def _cached():
    ns = _exec("main.py", {"_OFF_CALL_PANEL_TTL_S", "_OFF_CALL_PANEL_DEGRADED_TTL_S",
                           "_off_call_panel_cached"})
    return ns["_off_call_panel_cached"]


FULL = {"status": "ok", "teams_status": "ok", "members": []}
NO_TEAMS = {"status": "ok", "teams_status": "unavailable", "members": []}
NO_D4H = {"status": "unavailable", "teams_status": "unavailable", "members": []}


class TestPanelCache:
    """A degraded answer is cached too, for less time: otherwise an outage
    makes every request fan out to D4H + every Everbridge group on the thread
    pool /ocr shares (review finding, 2026-11-08)."""

    def test_empty_cache_misses(self):
        assert _cached()({"at": 0.0, "body": None}, 5.0) is None

    def test_full_answer_lasts_a_minute(self):
        assert _cached()({"at": 100.0, "body": FULL}, 159.0) is FULL
        assert _cached()({"at": 100.0, "body": FULL}, 161.0) is None

    def test_degraded_answers_are_cached_briefly(self):
        for body in (NO_TEAMS, NO_D4H):
            assert _cached()({"at": 100.0, "body": body}, 114.0) is body
            assert _cached()({"at": 100.0, "body": body}, 116.0) is None


class TestEndpointSingleFlight:
    def _fn(self):
        return ast.parse(_endpoint_src()).body[0]

    def test_fetch_runs_under_the_lock_after_a_cache_check(self):
        locked = [w for w in ast.walk(self._fn()) if isinstance(w, ast.AsyncWith)
                  and ast.unparse(w.items[0].context_expr) == "_off_call_panel_lock"]
        assert locked, "the fetch must sit inside `async with _off_call_panel_lock`"
        inner = [ast.unparse(n) for n in ast.walk(locked[0]) if isinstance(n, ast.Call)]
        assert any(c.startswith("_off_call_panel_cached(") for c in inner)
        assert any(c.startswith("d4h.get_off_call_now") or "d4h.get_off_call_now" in c for c in inner)

    def test_d4h_failure_is_cached_before_returning(self):
        handlers = [h for t in ast.walk(self._fn()) if isinstance(t, ast.Try) for h in t.handlers
                    if any(isinstance(s, ast.Return) for s in h.body)]
        assert handlers
        calls = [ast.unparse(n.func) for s in handlers[0].body for n in ast.walk(s) if isinstance(n, ast.Call)]
        assert "_off_call_panel_cache.update" in calls


class TestFrontendNeverUsesInnerHtml:
    def test_loader_builds_dom_with_text_only(self):
        html = (HERE.parent / "frontend" / "index.html").read_text(encoding="utf-8")
        start = html.index("async function loadOffCallPanel()")
        fn = html[start:html.index('submitBtn.addEventListener("click"', start)]
        assert "textContent" in fn, "sanity: slice covers the loader"
        assert "innerHTML" not in fn and "insertAdjacentHTML" not in fn


class TestEveryAuthedFetchSendsTheNonce:
    """auth.py Gate 1.5 rejects any request without X-OIDC-Nonce (401). The
    first live run of /off-call-now on personal-dev (1.11.81, 2026-11-08) 401'd
    because its fetch sent only the bearer token; the preview stub ignored
    headers, so nothing offline could see it. Pinned file-wide, not per-call."""

    @staticmethod
    def _header_objects(s: str):
        for m in re.finditer(r"""['"]?Authorization['"]?\s*:""", s):
            a = s.rindex("{", 0, m.start())
            while s[a - 1] == "$":          # skip a template ${...} brace
                a = s.rindex("{", 0, a - 1)
            depth = 0
            for j in range(a, len(s)):
                depth += s[j] == "{"
                depth -= s[j] == "}"
                if depth == 0:
                    yield s.count("\n", 0, m.start()) + 1, s[a:j + 1]
                    break

    def test_bearer_and_nonce_travel_together(self):
        html = (HERE.parent / "frontend" / "index.html").read_text(encoding="utf-8")
        objs = list(self._header_objects(html))
        assert len(objs) >= 8, "sanity: the scan found the app's authed fetches"
        missing = [line for line, obj in objs if "X-OIDC-Nonce" not in obj]
        assert missing == [], f"Authorization without X-OIDC-Nonce at index.html lines {missing}"
