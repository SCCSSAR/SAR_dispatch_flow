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
        for outcome, want in [("Deceased", "Found deceased"), ("Not Located", "Not found"),
                              ("Person Assisted", "Found")]:
            f = mr.build_facts(_incident(), [], {"Outcome": outcome}, TAG_TITLES, CFG)
            assert f.found_status == want

    def test_date_is_pacific(self):
        f = mr.build_facts(_incident(startsAt="2026-10-30T04:11:00.000Z"), [], None, TAG_TITLES, CFG)
        assert f.date_pt == "10-29"
