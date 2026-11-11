"""Tests for backend/monthly_recap.py — the monthly General Team incident recap.

The repo is PUBLIC: every fixture here is synthetic. D4H numbers start with 9; places, streets,
agencies and names are invented, and no value is copied from a real incident.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

import monthly_recap as mr

CFG = {
    "county": "Example County",
    "county_extent": [[37.50, -122.20], [36.90, -121.20]],
    "in_county_agencies": ["EXSO", "EXPD", "OAKVALE"],
    "d4h_env": "prod-env",
}


def _write_cfg(tmp_path, **over):
    cfg = {**CFG, **over}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return p


class TestLoadConfig:
    def test_valid(self, tmp_path):
        assert mr.load_config(_write_cfg(tmp_path))["county"] == "Example County"

    def test_missing_file_names_template(self, tmp_path):
        with pytest.raises(mr.RecapError, match="config.template.json"):
            mr.load_config(tmp_path / "nope.json")

    def test_unfilled_placeholder_refused(self, tmp_path):
        with pytest.raises(mr.RecapError, match="county"):
            mr.load_config(_write_cfg(tmp_path, county="<Your county>"))

    def test_bad_extent_refused(self, tmp_path):
        with pytest.raises(mr.RecapError, match="county_extent"):
            mr.load_config(_write_cfg(tmp_path, county_extent=[[37.5, -122.2]]))

    def test_template_placeholder_extent_refused(self, tmp_path):
        with pytest.raises(mr.RecapError, match="county_extent"):
            mr.load_config(_write_cfg(tmp_path, county_extent=[[0.0, 0.0], [0.0, 0.0]]))

    def test_shipped_template_is_refused(self, tmp_path):
        tpl = Path(__file__).resolve().parents[1] / ".claude/skills/monthly-recap/config.template.json"
        with pytest.raises(mr.RecapError):
            mr.load_config(tpl)

    def test_notes_ignored(self, tmp_path):
        p = _write_cfg(tmp_path, _about="a note")
        assert "_about" not in mr.load_config(p)


class TestMonthWindow:
    def test_october_pacific(self):
        start, end = mr.month_window("2026-10")
        assert (start.isoformat(), end.isoformat()) == ("2026-10-01T00:00:00-07:00", "2026-11-01T00:00:00-07:00")

    def test_december_rolls_year(self):
        assert mr.month_window("2026-12")[1].isoformat() == "2027-01-01T00:00:00-08:00"

    def test_bad_month(self):
        with pytest.raises(mr.RecapError):
            mr.month_window("2026-13")


class TestIncidentHours:
    def test_closed_out(self):
        assert mr.incident_hours("2026-10-02T22:49:00.000Z", "2026-10-03T05:00:00.000Z") == pytest.approx(6.1833, abs=1e-3)

    def test_not_closed_out_is_none(self):
        assert mr.incident_hours("2026-10-02T22:49:00.000Z", "2026-10-02T22:49:00.000Z") is None

    def test_missing_end_is_none(self):
        assert mr.incident_hours("2026-10-02T22:49:00.000Z", None) is None


class TestVolunteerHoursLabel:
    def test_multiplication(self):
        assert mr.volunteer_hours_label(10, 6.1833) == "10 × 6.2 h = 62"

    def test_rounds_total_from_unrounded_hours(self):
        assert mr.volunteer_hours_label(8, 12.65) == "8 × 12.7 h = 101"

    def test_not_closed_out(self):
        assert mr.volunteer_hours_label(7, None) == "7 × not closed out"


class TestTestReason:
    def test_personal_dev_suffix(self):
        assert mr.test_reason("EXPD Elmwood [a1f9]") == "[abcd] personal-dev suffix"

    def test_named_test(self):
        assert mr.test_reason("EXPD Off-call Test 1") == "named as a test"

    def test_real_record(self):
        assert mr.test_reason("EXPD Testarossa Way") is None


class TestIsCancelled:
    def test_cancelled(self):
        assert mr.is_cancelled("EXPD Elmwood CANCELLED")

    def test_canceled_us_spelling(self):
        assert mr.is_cancelled("EXPD Elmwood canceled")

    def test_word_inside_street_is_not(self):
        assert not mr.is_cancelled("EXPD Cancelledo Ct")


class TestGuessMutualAid:
    def test_ma_in_name(self):
        assert mr.guess_mutual_aid("Farshire M/A Type 1", False, None, CFG) == (True, True)

    def test_in_county_agency(self):
        assert mr.guess_mutual_aid("EXPD Elmwood", True, (37.3, -121.9), CFG) == (False, True)

    def test_two_word_agency(self):
        assert mr.guess_mutual_aid("OAKVALE Elmwood", True, None, CFG) == (False, True)

    def test_pls_outside_county(self):
        assert mr.guess_mutual_aid("FARSO Ridge OP2", True, (35.3, -120.6), CFG) == (True, True)

    def test_unknown_inside_county_is_unconfident(self):
        assert mr.guess_mutual_aid("NEWPD Elmwood", True, (37.3, -121.9), CFG) == (True, False)


TURBO_HTML = (
    "<p>Initial Incident Summary:<br />Event Name: 2026-10-02 EXPD Elmwood<br />"
    "Event #: 99-123-4567<br />Agency: EXPD<br />"
    "Missing Person: Zorblat QUINTAVERE; at-risk: Alone<br />"
    "CalTopo Map ID: https://caltopo.com/m/ZZ12AB3<br />---<br />"
    "Environment: Urban or Suburban (Census 2020) · Eco-region: Temperate</p>"
)


class TestParseTurboDescription:
    def test_all_lines(self):
        t = mr.parse_turbo_description(TURBO_HTML)
        assert t.map_id == "ZZ12AB3"
        assert t.environment == "Urban or Suburban (Census 2020)"
        assert t.subject_names == ["Zorblat", "QUINTAVERE"]
        assert t.agency == "EXPD"
        assert t.turbo_record

    def test_hand_entered_record(self):
        t = mr.parse_turbo_description("<p>Mutual aid request from Farshire.</p>")
        assert (t.map_id, t.environment, t.subject_names, t.agency, t.turbo_record) == (None, None, [], None, False)

    def test_none_description(self):
        assert mr.parse_turbo_description(None).turbo_record is False


TAG_TITLES = {1: "Canine", 2: "UAS", 3: "Search Management", 4: "Transport", 5: "e-bike", 6: "Training"}


class TestSpecialtyTeams:
    def test_display_and_exclusions(self):
        assert mr.specialty_teams([3, 1, 4, 5, 2, 6], TAG_TITLES) == ["K9", "UAS", "e-bike"]

    def test_unknown_id(self):
        assert mr.specialty_teams([99], TAG_TITLES) == ["tag 99"]


class TestEnvironmentLabel:
    @pytest.mark.parametrize("lpb,turbo,want", [
        ("Suburban", None, "Urban"),
        ("Wilderness", "Urban or Suburban", "Wilderness"),
        (None, "Urban or Suburban (Census 2020)", "Urban"),
        ("", "Rural or Wilderness", "Wilderness"),
        (None, None, "[fill in]"),
    ])
    def test_label(self, lpb, turbo, want):
        assert mr.environment_label(lpb, turbo) == want


def _incident(**over):
    inc = {
        "id": 9001, "reference": "98765", "referenceDescription": "EXPD Elmwood",
        "startsAt": "2026-10-02T22:49:00.000Z", "endsAt": "2026-10-03T05:00:00.000Z",
        "countAttendance": 10, "tags": [{"id": 1}, {"id": 3}],
        "location": {"type": "Point", "coordinates": [-121.9, 37.3]},
        "description": TURBO_HTML,
    }
    inc.update(over)
    return inc


class TestBuildFacts:
    def test_turbo_record_with_lpb(self):
        lpb = {"Outcome": "Life Saved", "Distance From IPP": "1.2mi", "Direction From IPP": "NE",
               "Population Density": "Urban"}
        f = mr.build_facts(_incident(), [{"name": "Zorblat Quintavere"}], lpb, TAG_TITLES, CFG)
        assert (f.number, f.date_pt, f.event_name, f.attendees) == ("98765", "10-02", "EXPD Elmwood", 10)
        assert f.hours == pytest.approx(6.1833, abs=1e-3)
        assert (f.found_status, f.distance, f.environment) == ("Found", "1.2mi NE", "Urban")
        assert f.specialty == ["K9"]
        assert f.map_id == "ZZ12AB3"
        assert f.pls == (37.3, -121.9)
        assert (f.mutual_aid, f.ma_confident, f.cancelled) == (False, True, False)
        assert "Quintavere" in f.subject_names and "QUINTAVERE" in f.subject_names

    def test_null_island_pls_is_none(self):
        f = mr.build_facts(_incident(location={"coordinates": [0, 0]}), [], None, TAG_TITLES, CFG)
        assert f.pls is None

    def test_no_lpb_row_fills_in(self):
        f = mr.build_facts(_incident(), [], None, TAG_TITLES, CFG)
        assert (f.found_status, f.distance) == ("[fill in]", "[fill in]")
        assert f.environment == "Urban"  # from Turbo's Environment line

    def test_outcome_mapping(self):
        # Turbo writes Person Assisted as a placeholder at dispatch, so it is not evidence of a find.
        for outcome, want in [("Deceased", "Found deceased"), ("Not Located", "Not found"),
                              ("Person Assisted", "[confirm] Found"), ("Life Saved", "Found")]:
            f = mr.build_facts(_incident(), [], {"Outcome": outcome}, TAG_TITLES, CFG)
            assert f.found_status == want

    def test_person_assisted_with_find_evidence_is_found(self):
        lpb = {"Outcome": "Person Assisted", "Find Coordinates": "37.31, -121.91", "Search Time": "3"}
        assert mr.build_facts(_incident(), [], lpb, TAG_TITLES, CFG).found_status == "Found"

    def test_person_assisted_with_dash_evidence_stays_unconfirmed(self):
        lpb = {"Outcome": "Person Assisted", "Find Coordinates": "-", "Search Time": ""}
        assert mr.build_facts(_incident(), [], lpb, TAG_TITLES, CFG).found_status == "[confirm] Found"

    def test_export_dash_is_blank(self):
        # D4H's Lost Behavior export writes "-" for an empty distance (seen on the first real run).
        f = mr.build_facts(_incident(), [], {"Distance From IPP": "-", "Direction From IPP": "-"}, TAG_TITLES, CFG)
        assert f.distance == "[fill in]"

    def test_date_is_pacific(self):
        f = mr.build_facts(_incident(startsAt="2026-10-30T04:11:00.000Z"), [], None, TAG_TITLES, CFG)
        assert f.date_pt == "10-29"


# ── Task 3: drafts.md ────────────────────────────────────────────────────────

def _facts(**over):
    f = mr.build_facts(_incident(), [], {"Outcome": "Life Saved", "Distance From IPP": "1.2mi",
                                         "Direction From IPP": "NE"}, TAG_TITLES, CFG)
    for k, v in over.items():
        setattr(f, k, v)
    return f


class TestDraftsRoundTrip:
    def test_render_then_parse(self):
        cancelled = _facts(number="98766", event_name="EXPD Birch CANCELLED", cancelled=True, hours=None)
        text = mr.render_drafts("2026-10", [_facts(), cancelled], [("98000", "[abcd] personal-dev suffix")])
        d = mr.parse_drafts(text)
        assert d.month == "2026-10"
        assert [i.number for i in d.incidents] == ["98765", "98766"]
        first = d.incidents[0]
        assert first.title == "EXPD Elmwood"
        assert first.facts["volunteer_hours"] == "10 × 6.2 h = 62"
        assert first.facts["specialty"] == "K9"
        assert first.facts["pls"] == "37.30000, -121.90000"
        assert first.facts["map"] == "ZZ12AB3"
        assert (first.summary, first.highlight, first.lessons, first.disruption) == ("[draft]", "[draft]", ["[draft]"], [])
        second = d.incidents[1]
        assert second.facts["hours"] == "[fill in]"
        assert second.facts["volunteer_hours"] == "10 × not closed out"
        assert second.facts["cancelled"] == "yes"
        assert second.disruption == ["[draft]"]
        assert d.excluded == ["98000 · [abcd] personal-dev suffix"]

    def test_facts_order(self):
        text = mr.render_drafts("2026-10", [_facts()], [])
        assert list(mr.parse_drafts(text).incidents[0].facts) == list(mr.FACT_KEYS)


EDITED = (
    "# 2026-10 incident recap — drafts\r\n\r\nEdit anything.\r\n\r\n"
    "## 98765 · 10-02 · EXPD Elmwood\r\n```facts\r\ndate: 10-02\r\nevent: EXPD Elmwood\r\n"
    "found: Found well: walked home\r\n```\r\n"
    "### Summary\r\nA man left on foot — found 1.2 mi away.\r\n\r\nSecond line kept.\r\n"
    "### Highlight\r\nK9 picked up the trail at the bus stop.\r\n"
    "### Lessons learned\r\n- Ask for transit early.\r\n- Brief UAS on the no-fly zone: call it out.\r\n"
    "- A fourth thought.\r\n- And a fifth.\r\n"
    "### Sources and conflicts\r\nnot rendered\r\n"
)


class TestParseHandEdits:
    def test_crlf_and_text_kept_exactly(self):
        i = mr.parse_drafts(EDITED).incidents[0]
        assert i.facts["found"] == "Found well: walked home"
        assert i.summary == "A man left on foot — found 1.2 mi away.\n\nSecond line kept."
        assert i.highlight == "K9 picked up the trail at the bus stop."
        assert i.lessons == ["Ask for transit early.", "Brief UAS on the no-fly zone: call it out.",
                             "A fourth thought.", "And a fifth."]

    def test_missing_disruption_section_is_empty(self):
        assert mr.parse_drafts(EDITED).incidents[0].disruption == []

    def test_deleted_lessons_section(self):
        text = EDITED.replace("### Lessons learned\r\n- Ask for transit early.\r\n", "### Lessons learned\r\n")
        assert "Ask for transit early." not in mr.parse_drafts(text).incidents[0].lessons


class TestParseRejects:
    def test_missing_facts_block(self):
        text = "# 2026-10 incident recap — drafts\n\n## 98765 · 10-02 · EXPD Elmwood\n### Summary\nx\n"
        with pytest.raises(mr.DraftError, match="98765 · 10-02 · EXPD Elmwood"):
            mr.parse_drafts(text)

    def test_missing_month_header(self):
        with pytest.raises(mr.DraftError, match="month"):
            mr.parse_drafts("## 98765 · 10-02 · EXPD Elmwood\n```facts\ndate: 10-02\n```\n")


class TestNextDraftsPath:
    def test_sequence(self, tmp_path):
        assert mr.next_drafts_path(tmp_path).name == "drafts.md"
        (tmp_path / "drafts.md").write_text("x")
        assert mr.next_drafts_path(tmp_path).name == "drafts-2.md"
        (tmp_path / "drafts-2.md").write_text("x")
        assert mr.next_drafts_path(tmp_path).name == "drafts-3.md"


class TestConfirmMarker:
    def test_unconfident_mutual_aid(self):
        text = mr.render_drafts("2026-10", [_facts(mutual_aid=True, ma_confident=False)], [])
        assert "mutual_aid: [confirm] yes" in text
        assert mr.parse_drafts(text).incidents[0].facts["mutual_aid"] == "[confirm] yes"


# ── Task 5: slide text check ─────────────────────────────────────────────────

class TestSlideTextHits:
    @pytest.mark.parametrize("text,kind", [
        ("Call the RP at 555-867-5309 first.", "phone"),
        ("Reach them at zz@example.invalid", "email"),
        ("Staged at 4410 Quillback Ave.", "address"),
        ("Agency case 99-123-4567 opened.", "event_number"),
        ("Born 7/26/41, wandered off.", "dob"),
    ])
    def test_each_kind(self, text, kind):
        # An Event # also matches the ISRID local-phone pattern; either kind refuses the render.
        assert kind in mr.slide_text_hits(text, [])

    @pytest.mark.parametrize("text", ["10 × 6.2 h = 62", "1.2mi NE", "K9, UAS, e-bike",
                                      "Found well 0.8 mi from the PLS after 3 hours."])
    def test_clean(self, text):
        assert mr.slide_text_hits(text, ["Zorblat", "Quintavere"]) == []

    def test_subject_name_any_case(self):
        assert mr.slide_text_hits("QUINTAVERE was found.", ["Quintavere"]) == ["subject_name"]

    def test_surname_equal_to_street_still_hits(self):
        # Review Focus 4: a surname that is also the event's street is flagged anyway.
        assert mr.slide_text_hits("Search started on Elmwood.", ["Elmwood"]) == ["subject_name"]

    def test_short_name_token_ignored(self):
        assert mr.slide_text_hits("Al went to the park.", ["Al"]) == []

    def test_name_is_whole_word(self):
        assert mr.slide_text_hits("Teams were annoyed.", ["Noy"]) == []


class TestCheckMonth:
    def test_hit_names_place_not_value(self):
        text = mr.render_drafts("2026-10", [_facts()], [])
        d = mr.parse_drafts(text)
        d.incidents[0].lessons = ["Call 555-867-5309 sooner."]
        hits = mr.check_month(d, {"98765": []})
        assert hits == ["98765 lessons: phone"]
        assert "5309" not in hits[0]

    def test_title_and_facts_checked(self):
        d = mr.parse_drafts(mr.render_drafts("2026-10", [_facts()], []))
        d.incidents[0].facts["distance"] = "near 12 Quillback St"
        assert mr.check_month(d, {"98765": []}) == ["98765 distance: address"]

    def test_clean_month(self):
        d = mr.parse_drafts(mr.render_drafts("2026-10", [_facts()], []))
        assert mr.check_month(d, {"98765": ["Zorblat", "Quintavere"]}) == []


# ── Task 7: collect and render ───────────────────────────────────────────────

def _inc(ref, name, starts, ends, map_id=None, coords=(-121.9, 37.3), att=6):
    desc = f"<p>Agency: EXPD<br />CalTopo Map ID: https://caltopo.com/m/{map_id}</p>" if map_id else "<p>hand entered</p>"
    return {"id": int(ref), "reference": ref, "referenceDescription": name, "startsAt": starts, "endsAt": ends,
            "countAttendance": att, "tags": [{"id": 1}], "location": {"coordinates": list(coords)},
            "description": desc}


MONTH_INCIDENTS = [
    _inc("98701", "EXPD Elmwood", "2026-10-02T22:49:00.000Z", "2026-10-03T05:00:00.000Z", map_id="ZZ12AB3"),
    _inc("98702", "EXPD Birch [a1f9]", "2026-10-05T18:00:00.000Z", "2026-10-05T19:00:00.000Z"),
    _inc("98703", "EXPD Cedar CANCELLED", "2026-10-28T23:23:00.000Z", "2026-10-28T23:23:00.000Z"),
    _inc("98704", "FARSO Ridge OP2", "2026-10-27T13:30:00.000Z", "2026-10-28T02:09:00.000Z", coords=(-120.6, 35.3)),
    _inc("98699", "EXPD Late September", "2026-10-01T05:00:00.000Z", "2026-10-01T06:00:00.000Z"),  # 09-30 PT
]
CALTOPO_FEATURES = {"result": {"state": {"features": [
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-121.9, 37.3]},
     "properties": {"class": "Marker", "marker-symbol": "placemark2", "title": "LKP — 12 Quillback St"}}]}}}


class FakeD4H:
    def __init__(self, page_size=2):
        self.page_size, self.paths = page_size, []

    def __call__(self, path):
        self.paths.append(path)
        if path.startswith("tags"):
            return {"results": [{"id": 1, "title": "Canine"}], "totalSize": 1}
        if path.startswith("incident-involved-persons"):
            return {"results": [{"name": "Zorblat Quintavere"}], "totalSize": 1}
        assert path.startswith("incidents?after=") and "&before=" in path
        page = int(re.search(r"page=(\d+)", path).group(1))
        chunk = MONTH_INCIDENTS[page * self.page_size:(page + 1) * self.page_size]
        return {"results": chunk, "totalSize": len(MONTH_INCIDENTS), "page": page}


import re  # noqa: E402


def _collect(tmp_path, **kw):
    return mr.collect("2026-10", tmp_path, FakeD4H(), lambda map_id: CALTOPO_FEATURES, None, CFG, **kw)


class TestCollect:
    def test_drafts_and_sources(self, tmp_path):
        drafts = _collect(tmp_path)
        assert drafts == tmp_path / "2026-10" / "drafts.md"
        d = mr.parse_drafts(drafts.read_text())
        assert [i.number for i in d.incidents] == ["98701", "98704", "98703"]  # date order, PT window
        assert d.excluded == ["98702 · [abcd] personal-dev suffix"]
        assert (tmp_path / "2026-10" / "caltopo-ZZ12AB3.json").exists()
        assert (tmp_path / "2026-10" / "d4h-98701.json").exists()
        names = json.loads((tmp_path / "2026-10" / "names.json").read_text())
        assert "Quintavere" in names["98701"]

    def test_pages_through_the_list(self, tmp_path):
        d4h = FakeD4H(page_size=2)
        mr.collect("2026-10", tmp_path, d4h, lambda m: CALTOPO_FEATURES, None, CFG)
        assert sum(p.startswith("incidents?") for p in d4h.paths) == 3

    def test_not_closed_out(self, tmp_path):
        d = mr.parse_drafts(_collect(tmp_path).read_text())
        cancelled = next(i for i in d.incidents if i.number == "98703")
        assert cancelled.facts["hours"] == "[fill in]"
        assert cancelled.facts["volunteer_hours"] == "6 × not closed out"

    def test_mutual_aid_distance_from_county(self, tmp_path):
        d = mr.parse_drafts(_collect(tmp_path).read_text())
        ma = next(i for i in d.incidents if i.number == "98704")
        assert ma.facts["mutual_aid"] == "yes"
        assert re.fullmatch(r"≈ \d+ mi from the county centre", ma.facts["distance"])

    def test_second_collect_never_overwrites(self, tmp_path):
        first = _collect(tmp_path)
        first.write_text(first.read_text() + "\nthe presenter's edit\n")
        before = first.read_bytes()
        second = _collect(tmp_path)
        assert second.name == "drafts-2.md" and first.read_bytes() == before

    def test_no_county_distance_for_unconfident_mutual_aid(self, tmp_path):
        # A hand-entered M/A record whose D4H location sits inside the county: the distance means nothing.
        inside = _inc("98705", "NEWSO Hilltop OP1", "2026-10-15T15:00:00.000Z", "2026-10-15T20:00:00.000Z",
                      coords=(-121.9, 37.3))
        orig = list(MONTH_INCIDENTS)
        MONTH_INCIDENTS.append(inside)
        try:
            d = mr.parse_drafts(_collect(tmp_path).read_text())
        finally:
            MONTH_INCIDENTS[:] = orig
        inc = next(i for i in d.incidents if i.number == "98705")
        assert inc.facts["mutual_aid"] == "[confirm] yes"
        assert inc.facts["distance"] == "[fill in]"

    def test_caltopo_failure_is_not_fatal(self, tmp_path):
        def boom(map_id):
            raise OSError("down")
        drafts = mr.collect("2026-10", tmp_path, FakeD4H(), boom, None, CFG)
        assert drafts.exists() and not (tmp_path / "2026-10" / "caltopo-ZZ12AB3.json").exists()
        d = mr.parse_drafts(drafts.read_text())
        assert [i.number for i in d.incidents][0] == "98701"  # still on a slide
        assert all("98701" not in e for e in d.excluded)
        section = drafts.read_text().split("\n## 98701")[1].split("\n## ")[0]
        assert "CalTopo map not read (OSError)" in section.split("### Sources and conflicts")[1]


class TestRenderRefusesOnHit:
    def test_phone_in_lesson(self, tmp_path):
        drafts = _collect(tmp_path)
        drafts.write_text(drafts.read_text().replace("- [draft]", "- Call 555-867-5309 sooner.", 1))
        built = []
        with pytest.raises(mr.RecapError, match="98701 lessons: phone"):
            mr.render(drafts, lambda z, x, y: b"", CFG, build=lambda *a: built.append(a))
        assert built == [] and not list((tmp_path / "2026-10").glob("*.pptx"))

    def test_subject_name_from_names_json(self, tmp_path):
        drafts = _collect(tmp_path)
        drafts.write_text(drafts.read_text().replace("### Summary\n[draft]", "### Summary\nQuintavere walked off.", 1))
        with pytest.raises(mr.RecapError, match="subject_name"):
            mr.render(drafts, lambda z, x, y: b"", CFG, build=lambda *a: None)

    def test_existing_pptx_refused_before_drawing(self, tmp_path):
        drafts = _collect(tmp_path)
        (tmp_path / "2026-10" / "2026-10 Incident Recap.pptx").write_bytes(b"edited")
        calls = []
        with pytest.raises(mr.RecapError, match="already exists"):
            mr.render(drafts, lambda z, x, y: calls.append(1) or b"", CFG, build=lambda *a: None)
        assert calls == []


def _tile(z, x, y):
    import io as _io
    from PIL import Image as _Image
    buf = _io.BytesIO()
    _Image.new("RGB", (256, 256), (200, 230, 200)).save(buf, "PNG")
    return buf.getvalue()


class TestRenderWiring:
    def test_maps_links_and_overview(self, tmp_path):
        drafts = _collect(tmp_path)
        got = {}

        def build(draft, overview, maps, links, out):
            got.update(draft=draft, overview=overview, maps=maps, links=links, out=out)
            return out

        out = mr.render(drafts, _tile, CFG, build=build)
        assert out.name == "2026-10 Incident Recap.pptx"
        assert got["overview"].exists()
        assert got["links"] == {"98701": "https://caltopo.com/m/ZZ12AB3"}
        assert got["maps"]["98701"].exists()
        assert got["maps"]["98703"].exists()  # cancelled: no map id, drawn around its PLS
        assert got["maps"]["98704"].exists()  # M/A: drawn with the county in frame

    def test_no_map_and_no_pls_gets_placeholder(self, tmp_path):
        # Review Focus 3: nothing to draw → None (a photo placeholder on the slide), not a crash.
        drafts = _collect(tmp_path)
        text = drafts.read_text().replace("pls: 37.30000, -121.90000\n", "pls: \n")
        text = text.replace("map: ZZ12AB3\n", "map: \n")
        drafts.write_text(text)
        got = {}
        mr.render(drafts, _tile, CFG, build=lambda d, o, m, l, out: got.update(maps=m) or out)
        assert got["maps"]["98701"] is None


class TestCaltopoGetter:
    SECRET = __import__("base64").b64encode(b"k" * 32).decode()

    def test_signed_url(self):
        seen = []
        get = mr.caltopo_getter("CRED", self.SECRET, opener=lambda url: seen.append(url) or {"ok": 1}, now_ms=lambda: 1000)
        assert get("ZZ12AB3") == {"ok": 1}
        url = seen[0]
        assert url.startswith("https://caltopo.com/api/v1/map/ZZ12AB3/since/0?")
        q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        assert q["id"] == "CRED" and q["expires"] == str(1000 + 120_000)
        import base64, hashlib, hmac, urllib.parse
        want = base64.b64encode(hmac.new(base64.b64decode(self.SECRET),
                                         b"GET /api/v1/map/ZZ12AB3/since/0\n121000\n", hashlib.sha256).digest()).decode()
        assert urllib.parse.unquote(q["signature"]) == want

    def test_retries_5xx_only(self):
        calls = []

        def flaky(url):
            calls.append(url)
            if len(calls) < 3:
                raise mr.HTTPStatusError(502)
            return {"ok": 1}

        assert mr.caltopo_getter("C", self.SECRET, opener=flaky, sleep=lambda s: None)("ZZ12AB3") == {"ok": 1}
        assert len(calls) == 3

    def test_4xx_not_retried(self):
        calls = []

        def denied(url):
            calls.append(url)
            raise mr.HTTPStatusError(401)

        with pytest.raises(mr.HTTPStatusError):
            mr.caltopo_getter("C", self.SECRET, opener=denied, sleep=lambda s: None)("ZZ12AB3")
        assert len(calls) == 1


class TestD4HGetter:
    def test_bearer_and_url(self):
        seen = []
        get = mr.d4h_getter("TOKEN", opener=lambda url, headers: seen.append((url, headers)) or {"results": []})
        get("tags")
        url, headers = seen[0]
        assert url == "https://api.team-manager.us.d4h.com/v3/team/1775/tags"
        assert headers["Authorization"] == "Bearer TOKEN"


class TestMain:
    def test_collect_needs_tokens(self, tmp_path, monkeypatch):
        monkeypatch.delenv("D4H_ACCESS_TOKEN", raising=False)
        cfg = _write_cfg(tmp_path)
        assert mr.main(["collect", "--month", "2026-10", "--root", str(tmp_path), "--config", str(cfg)]) == 2

    def test_bad_config_is_exit_2(self, tmp_path):
        assert mr.main(["render", "--drafts", str(tmp_path / "d.md"), "--config", str(tmp_path / "none.json")]) == 2


class TestRenderMapRules:
    def _spy(self, tmp_path, monkeypatch):
        import recap_map as rm
        maps, overview = {}, {}
        real_map, real_overview = rm.render_map, rm.render_overview

        def spy_map(markers, w, h, fetch, out, **kw):
            maps[out.name] = kw
            return real_map(markers, w, h, fetch, out, **kw)

        def spy_overview(pins, extent, fetch, out):
            overview["pins"] = pins
            return real_overview(pins, extent, fetch, out)

        monkeypatch.setattr(rm, "render_map", spy_map)
        monkeypatch.setattr(rm, "render_overview", spy_overview)
        mr.render(_collect(tmp_path), _tile, CFG, build=lambda d, o, m, l, out: out)
        return maps, overview

    def test_mutual_aid_map_frames_the_county(self, tmp_path, monkeypatch):
        maps, _ = self._spy(tmp_path, monkeypatch)
        assert maps["map-98704.png"]["extent"] == [tuple(p) for p in CFG["county_extent"]]
        assert "extent" not in maps["map-98701.png"]

    def test_cancelled_map_frames_its_pls(self, tmp_path, monkeypatch):
        maps, _ = self._spy(tmp_path, monkeypatch)
        (lo_lat, lo_lng), (hi_lat, hi_lng) = maps["map-98703.png"]["extent"]
        assert lo_lat < 37.3 < hi_lat and lo_lng < -121.9 < hi_lng

    def test_overview_pins_in_county_only(self, tmp_path, monkeypatch):
        _, overview = self._spy(tmp_path, monkeypatch)
        assert [(n, c) for n, _, _, c in overview["pins"]] == [(1, False), (2, True)]


# ── Final review fix pass ────────────────────────────────────────────────────

TWO = ("# 2026-10 incident recap — drafts\n\n"
       "## 98701 · 10-02 · EXPD Elmwood\n```facts\ndate: 10-02\n```\n### Summary\nFirst.\n### Lessons learned\n- One.\n"
       "## 98702 · 10-05 · EXPD Birch\n```facts\ndate: 10-05\n```\n### Summary\nSecond.\n### Lessons learned\n- Two.\n")


class TestParseFailsLoudly:
    def test_deleted_header_does_not_merge(self):
        text = TWO.replace("## 98702 · 10-05 · EXPD Birch\n", "")
        with pytest.raises(mr.DraftError, match="98701"):
            mr.parse_drafts(text)

    def test_deleted_header_before_a_facts_only_incident(self):
        text = TWO.replace("## 98702 · 10-05 · EXPD Birch\n", "").replace("### Summary\nSecond.\n### Lessons learned\n- Two.\n", "")
        with pytest.raises(mr.DraftError, match="header line is missing"):
            mr.parse_drafts(text)

    def test_unknown_section_heading_refused(self):
        with pytest.raises(mr.DraftError, match="Lesons"):
            mr.parse_drafts(TWO.replace("### Lessons learned\n- One.", "### Lesons learned\n- One."))


class TestParseKeepsHandText:
    def test_section_name_any_case(self):
        d = mr.parse_drafts(TWO.replace("### Lessons learned\n- One.", "### Lessons Learned\n- One."))
        assert d.incidents[0].lessons == ["One."]

    def test_wrapped_bullet_joined(self):
        d = mr.parse_drafts(TWO.replace("- One.", "- One long lesson\n  that wraps."))
        assert d.incidents[0].lessons == ["One long lesson that wraps."]

    def test_star_and_numbered_bullets(self):
        d = mr.parse_drafts(TWO.replace("- One.", "* Star.\n1. Numbered."))
        assert d.incidents[0].lessons == ["Star.", "Numbered."]


class TestNameTokens:
    def test_placeholder_is_not_a_name(self):
        t = mr.parse_turbo_description("<p>Missing Person: [not recorded]<br />Agency: EXPD</p>")
        assert t.subject_names == []

    def test_any_bracketed_placeholder(self):
        assert mr.name_tokens("[name withheld]") == []

    def test_stoplist(self):
        assert mr.name_tokens("Unknown none NOT") == []

    def test_accented_name(self):
        assert mr.name_tokens("José Núñez") == ["José", "Núñez"]

    def test_accent_folded_match(self):
        assert mr.slide_text_hits("We found Jose Nunez at the park.", mr.name_tokens("José Núñez")) == ["subject_name"]
        assert mr.slide_text_hits("We found José at the park.", ["Jose"]) == ["subject_name"]


class TestSlideProseIsNotAnAddress:
    @pytest.mark.parametrize("text", ["6 members were a 30 minute drive out", "2 teams walked the creek trail",
                                      "3 K9 teams cleared the path", "4 members on the road"])
    def test_clean(self, text):
        assert mr.slide_text_hits(text, []) == []

    def test_real_address_still_hits(self):
        assert mr.slide_text_hits("Staged at 4410 Quillback Ave.", []) == ["address"]


class TestRenderNamesRequired:
    def test_missing_names_file_refused(self, tmp_path):
        drafts = _collect(tmp_path)
        (tmp_path / "2026-10" / "names.json").unlink()
        with pytest.raises(mr.RecapError, match="names.json is missing"):
            mr.render(drafts, _tile, CFG, build=lambda *a: None)

    def test_incident_missing_from_names_refused(self, tmp_path):
        drafts = _collect(tmp_path)
        p = tmp_path / "2026-10" / "names.json"
        names = json.loads(p.read_text())
        del names["98704"]
        p.write_text(json.dumps(names))
        with pytest.raises(mr.RecapError, match="98704"):
            mr.render(drafts, _tile, CFG, build=lambda *a: None)


class TestFilledHoursReachTheSlide:
    def test_hours_filled_after_close_out(self, tmp_path):
        drafts = _collect(tmp_path)
        text = drafts.read_text()
        sec = text.split("\n## 98703")[1].split("\n## ")[0]
        drafts.write_text(text.replace(sec, sec.replace("hours: [fill in]", "hours: 0.5", 1)))
        got = {}
        mr.render(drafts, _tile, CFG, build=lambda d, o, m, l, out: got.update(d=d) or out)
        inc = next(i for i in got["d"].incidents if i.number == "98703")
        assert inc.facts["volunteer_hours"] == "6 × 0.5 h = 3"


class TestRenderFetchesMissingMap:
    def test_map_id_changed_in_drafts_is_fetched(self, tmp_path):
        drafts = _collect(tmp_path)
        drafts.write_text(drafts.read_text().replace("map: ZZ12AB3", "map: ZZCOPY9"))
        fetched = []
        mr.render(drafts, _tile, CFG, build=lambda d, o, m, l, out: out,
                  get_caltopo=lambda mid: fetched.append(mid) or CALTOPO_FEATURES)
        assert fetched == ["ZZCOPY9"]
        assert (tmp_path / "2026-10" / "caltopo-ZZCOPY9.json").exists()


class TestOpenItems:
    def test_lists_confirm_and_fill_in(self):
        d = mr.parse_drafts(mr.render_drafts("2026-10", [_facts(mutual_aid=True, ma_confident=False, distance="[fill in]")], []))
        items = mr.open_items(d)
        assert "98765 mutual_aid: [confirm] yes" in items
        assert "98765 distance: [fill in]" in items


class TestClosedOutHoursKeepTheirPrecision:
    def test_collect_total_is_not_recomputed_from_rounded_hours(self, tmp_path):
        # 8 x 12.65 h = 101; the drafts show hours 12.7, and 8 x 12.7 = 102 would overstate it.
        drafts = _collect(tmp_path)
        text = drafts.read_text()
        sec = text.split("\n## 98704")[1].split("\n## ")[0]
        new = sec.replace("hours: 12.7\n", "hours: 12.7\n").replace(
            next(l for l in sec.splitlines() if l.startswith("volunteer_hours:")), "volunteer_hours: 8 × 12.7 h = 101")
        drafts.write_text(text.replace(sec, new))
        got = {}
        mr.render(drafts, _tile, CFG, build=lambda d, o, m, l, out: got.update(d=d) or out)
        inc = next(i for i in got["d"].incidents if i.number == "98704")
        assert inc.facts["volunteer_hours"] == "8 × 12.7 h = 101"


class TestMapIdIsValidated:
    """A map id becomes a file name and a CalTopo URL path, so drafts.md cannot steer either (Aikido, PR #108)."""

    @pytest.mark.parametrize("bad", ["../../etc/x", "ZZ12AB3/../x", "ZZ12AB3?id=1", "a b"])
    def test_render_refuses_a_bad_map_id(self, tmp_path, bad):
        drafts = _collect(tmp_path)
        drafts.write_text(drafts.read_text().replace("map: ZZ12AB3", f"map: {bad}"))
        fetched = []
        with pytest.raises(mr.RecapError, match="98701 map"):
            mr.render(drafts, _tile, CFG, build=lambda *a: None, get_caltopo=lambda m: fetched.append(m) or {})
        assert fetched == []

    def test_getter_refuses_a_bad_map_id(self):
        get = mr.caltopo_getter("C", TestCaltopoGetter.SECRET, opener=lambda url: pytest.fail("no request"))
        with pytest.raises(ValueError):
            get("../x")

    def test_good_ids_pass(self):
        assert all(mr.MAP_ID_RE.fullmatch(m) for m in ("ZZ12AB3", "K0J66M5", "7CLG3JH"))
