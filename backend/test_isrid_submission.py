"""Tests for backend/isrid_submission.py — the quarterly ISRID batch script.

The repo is PUBLIC: every fixture here is synthetic. D4H numbers start with 9, places and
streets are invented, and no value is copied from a real incident.
"""

import csv
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest

import isrid_submission as isr
from isrid_submission import (
    EXPORT_HEADER,
    SubmissionError,
    d4h_number,
    deidentify_row,
    forbidden_hits,
    read_export,
)

# The header D4H's Lost Behavior export carries (34 columns, measured 2026-11-09).
EXPECTED_HEADER = (
    "Mission #", "Incident Date", "Incident Type", "County", "City", "Subject Category",
    "# Subjects", "Involvement", "Age", "Sex", "Search Time", "Lost Time", "Distance From IPP",
    "Direction From IPP", "Find Feature", "Outcome", "Elevation Change", "Mobility Time",
    "Population Density", "IPP Coordinates", "IPP Lat", "IPP Lng", "Find Coordinates", "Find Lat",
    "Find Lng", "Eco Region", "Environment", "Weather", "Equipment", "Experience", "Fitness",
    "Scenario", "Terrain", "Comment",
)


def make_row(**over):
    """A synthetic, complete row. Keyword args use the header with spaces/#/symbols replaced."""
    row = {
        "Mission #": "90001 XXSO Example Ave",
        "Incident Date": "2026-01-15 21:05:00",
        "Incident Type": "Search",
        "County": "California",
        "City": "San Jose",
        "Subject Category": "Dementia",
        "# Subjects": "1",
        "Involvement": "Subject",
        "Age": "80",
        "Sex": "f",
        "Search Time": "5hrs 0m",
        "Lost Time": "8hrs 0m",
        "Distance From IPP": "1.2mi",
        "Direction From IPP": "90.0°",
        "Find Feature": "Road",
        "Outcome": "Person Assisted",
        "Elevation Change": "",
        "Mobility Time": "2hrs 0m",
        "Population Density": "Urban",
        "IPP Coordinates": "37.100000, -121.900000",
        "IPP Lat": "37.100000",
        "IPP Lng": "-121.900000",
        "Find Coordinates": "37.110000, -121.880000",
        "Find Lat": "37.110000",
        "Find Lng": "-121.880000",
        "Eco Region": "Temperate",
        "Environment": "Urban",
        "Weather": "Clear, 60F",
        "Equipment": "Fair",
        "Experience": "Fair",
        "Fitness": "Poor",
        "Scenario": "Lost",
        "Terrain": "Flat",
        "Comment": "Found well near Example Park by a passer-by.",
    }
    for k, v in over.items():
        row[k] = v
    return row


def write_export(path: Path, rows, header=EXPECTED_HEADER):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(h, "") for h in header])
    return path


# ── Task 1: header allow-list and row de-identification ─────────────────────


def test_module_header_is_the_d4h_header():
    assert EXPORT_HEADER == EXPECTED_HEADER


@pytest.mark.parametrize(
    "header,offender",
    [
        (EXPECTED_HEADER + ("New Column",), "New Column"),
        (("Incident Date", "Mission #") + EXPECTED_HEADER[2:], "Mission #"),
        (EXPECTED_HEADER[:-1] + ("Notes",), "Notes"),
    ],
    ids=["added", "swapped", "renamed"],
)
def test_header_must_match_exactly(tmp_path, header, offender):
    p = write_export(tmp_path / "x.csv", [make_row()], header=header)
    with pytest.raises(SubmissionError) as e:
        read_export(p)
    assert offender in str(e.value)


def test_read_export_returns_rows(tmp_path):
    p = write_export(tmp_path / "x.csv", [make_row(), make_row(**{"Mission #": "90002 XXSO Other"})])
    rows = read_export(p)
    assert [r["Mission #"][:5] for r in rows] == ["90001", "90002"]


def test_d4h_number():
    assert d4h_number("90001 XXSO Example Ave") == "90001"
    with pytest.raises(SubmissionError):
        d4h_number("SO-17 Example")
    with pytest.raises(SubmissionError):
        d4h_number("900012 XXSO")      # six digits is not a D4H number


def test_deidentify_row():
    row = make_row()
    out = deidentify_row(row, "Outcome: 1 well.")
    assert out["Mission #"] == "90001"
    assert out["Comment"] == "Outcome: 1 well."
    assert out["County"] == "Santa Clara"
    for h in EXPECTED_HEADER:
        if h not in ("Mission #", "Comment", "County"):
            assert out[h] == row[h], h
    assert row["Mission #"] == "90001 XXSO Example Ave"   # input not mutated


@pytest.mark.parametrize(
    "value",
    ["call 408-555-0142", "mail a@b.org", "near 456 Placid Court", "event 26-001-071",
     "by XXSO staff", "on EXAMPLE lane"],
    ids=["phone", "email", "address", "event-number", "mission-agency", "mission-street"],
)
def test_forbidden_hits(value):
    row = deidentify_row(make_row(), value)
    assert forbidden_hits(row, "90001 XXSO Example Ave") == ["Comment"]


def test_forbidden_hits_clean_row():
    row = deidentify_row(make_row(), "Outcome: 1 well. Last seen 2026-01-15 13:00; found 2026-01-16 02:15.")
    assert forbidden_hits(row, "90001 XXSO Example Ave") == []


def test_forbidden_hits_skips_city_for_mission_words():
    # A street word can equal a city word; City is reviewed by Bill instead.
    row = deidentify_row(make_row(City="Saratoga"), "Outcome: 1 well.")
    assert forbidden_hits(row, "90001 XXSO Saratoga Sunnyvale") == []


def test_forbidden_hits_checks_weather():
    row = deidentify_row(make_row(Weather="call 408 555 0142"), "Outcome: 1 well.")
    assert forbidden_hits(row, "90001 XXSO Example Ave") == ["Weather"]


# ── Task 2: Minimum (13) and Silver (25) scoring ────────────────────────────

FOUR_COUNTS = ["# well", "# injured", "# DOA", "# no trace"]


@pytest.mark.parametrize("text,expected", [
    ("Outcome: 1 well.", True), ("Found WELL by family", True), ("1 injured", True),
    ("minor injury", True), ("DOA", True), ("subject deceased", True), ("found dead", True),
    ("Outcome: 1 no trace.", True), ("", False), ("Wellington Park", False), ("Person Assisted", False),
])
def test_states_condition(text, expected):
    assert isr.states_condition(text) is expected


def test_complete_row_scores_13():
    assert isr.score_minimum(make_row(), "Outcome: 1 well.") == (13, [])


def test_person_assisted_without_condition_loses_four():
    n, missing = isr.score_minimum(make_row(), "")
    assert n == 8
    assert sorted(missing) == sorted(FOUR_COUNTS + ["Comment"])


def test_each_minimum_field_is_scored():
    blanks = {"Incident Type": "Incident Type", "Incident Date": "SAR notified",
              "Search Time": "Found date/time", "Lost Time": "Last seen date/time",
              "Subject Category": "Subject Category", "IPP Lat": "IPP coordinates",
              "IPP Lng": "IPP coordinates", "Find Lat": "Find coordinates",
              "Find Lng": "Find coordinates", "Scenario": "Scenario"}
    for col, field in blanks.items():
        n, missing = isr.score_minimum(make_row(**{col: ""}), "Outcome: 1 well.")
        assert (n, missing) == (12, [field]), col


def test_no_trace_excuses_find_fields():
    row = make_row(**{"Search Time": "", "Find Lat": "", "Find Lng": ""})
    assert isr.score_minimum(row, "Outcome: 1 no trace.") == (13, [])
    n, missing = isr.score_minimum(row, "Outcome: 1 well.")
    assert n == 11 and sorted(missing) == ["Find coordinates", "Found date/time"]


def test_dash_is_blank():
    n, missing = isr.score_minimum(make_row(Scenario="-"), "Outcome: 1 well.")
    assert (n, missing) == (12, ["Scenario"])
    n, _ = isr.score_minimum(make_row(Scenario="  "), "Outcome: 1 well.")
    assert n == 12


def test_silver_sex_placeholder():
    full = "Outcome: 1 well. Destination: home. Direction of travel: north."
    n, missing = isr.score_silver(make_row(Sex="o"), full, set())
    assert n == 23 and "Sex" in missing
    assert isr.score_silver(make_row(Sex="o"), full, {"Sex"})[0] == 24
    assert isr.score_silver(make_row(Sex=",o"), full, set())[0] == 23
    assert isr.score_silver(make_row(Sex=","), full, {"Sex"})[0] == 23    # blank, not a confirmation target
    assert isr.score_silver(make_row(Sex="f"), full, set())[0] == 24


def test_silver_track_offset_never_held():
    full = "Outcome: 1 well. Destination: home. Direction of travel: north."
    n, missing = isr.score_silver(make_row(), full, set())
    assert (n, missing) == (24, ["Track offset (ISRID derives)"])


def test_silver_matches_deck_count():
    # Shaped like the 2026-11-09 deck's complete records: Mobility blank, no destination or DOT.
    n, missing = isr.score_silver(make_row(**{"Mobility Time": ""}), "Outcome: 1 well.", set())
    assert n == 21
    assert sorted(missing) == sorted(["Mobility", "Destination", "Direction of Travel",
                                      "Track offset (ISRID derives)"])


def test_silver_includes_minimum_gaps():
    n, missing = isr.score_silver(make_row(Scenario=""), "Outcome: 1 well.", set())
    assert n == 21 and "Scenario" in missing     # 12 Minimum + 9 Silver additions


# ── Task 3: prepare ─────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 10, 14, 32)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def batch_with(tmp_path, rows, now=NOW):
    export = write_export(tmp_path / "export.csv", rows)
    root = tmp_path / "root"
    return isr.prepare(export, root, now), export, root


def test_quarter_label():
    assert isr.quarter_label(datetime(2026, 11, 10)) == "2026-Q4"
    assert isr.quarter_label(datetime(2027, 1, 1)) == "2027-Q1"
    assert isr.quarter_label(datetime(2026, 6, 30)) == "2026-Q2"
    assert isr.quarter_label(datetime(2026, 7, 1)) == "2026-Q3"


def test_prepare_copies_raw_bytes(tmp_path):
    batch, export, root = batch_with(tmp_path, [make_row()])
    assert batch == root / "2026-Q4" / "20261110-1432"
    raw = batch / "raw-d4h-export.csv"
    assert raw.read_bytes() == export.read_bytes()
    wl = json.loads((batch / "worklist.json").read_text())
    assert wl["raw_sha256"] == sha(export)
    assert (wl["quarter"], wl["run"], wl["rules_version"]) == ("2026-Q4", "20261110-1432", "1")


def test_prepare_refuses_existing_folder(tmp_path):
    batch, export, root = batch_with(tmp_path, [make_row()])
    with pytest.raises(SubmissionError):
        isr.prepare(export, root, NOW)


def test_prepare_refuses_bad_header(tmp_path):
    export = write_export(tmp_path / "export.csv", [make_row()], header=EXPECTED_HEADER + ("X",))
    with pytest.raises(SubmissionError):
        isr.prepare(export, tmp_path / "root", NOW)
    assert not (tmp_path / "root").exists()


def test_prepare_skips_sent(tmp_path):
    root = tmp_path / "root"
    earlier = root / "2026-Q3" / "20260930-0900"
    earlier.mkdir(parents=True)
    (earlier / "SENT.md").write_text("Sent on: 2026-09-30\nD4H numbers: 90001, 90003\n")
    export = write_export(tmp_path / "export.csv",
                          [make_row(), make_row(**{"Mission #": "90002 XXSO Other"})])
    batch = isr.prepare(export, root, NOW)
    wl = json.loads((batch / "worklist.json").read_text())
    assert wl["already_sent"] == ["90001"]
    assert [r["d4h"] for r in wl["rows"]] == ["90002"]
    assert isr.sent_numbers(root) == {"90001", "90003"}


def test_unsent_batch_does_not_count_as_sent(tmp_path):
    batch, export, root = batch_with(tmp_path, [make_row()])
    batch2 = isr.prepare(export, root, datetime(2026, 11, 10, 15, 0))
    assert [r["d4h"] for r in json.loads((batch2 / "worklist.json").read_text())["rows"]] == ["90001"]


@pytest.mark.parametrize("city,county,flag", [
    ("San Jose", "California", "in-county"), ("los gatos", "California", "in-county"),
    ("Mount Hamilton", "California", "in-county"), ("Arroyo Grande", "California", "mutual-aid?"),
    ("", "California", "check"), ("San Jose", "", "check"),
])
def test_flags(city, county, flag):
    assert isr.flag_row(make_row(City=city, County=county)) == flag


def test_bad_mission_number_is_flagged(tmp_path):
    batch, _, _ = batch_with(tmp_path, [make_row(), make_row(**{"Mission #": "SO-17 Example"})])
    rows = json.loads((batch / "worklist.json").read_text())["rows"]
    assert rows[1]["d4h"] is None and rows[1]["flag"] == "bad-mission-number" and rows[1]["line"] == 3


def test_worklist_minimum_as_exported(tmp_path):
    batch, _, _ = batch_with(tmp_path, [make_row(), make_row(**{"Mission #": "90002 XXSO Other",
                                                               "Comment": "", "Scenario": ""})])
    rows = json.loads((batch / "worklist.json").read_text())["rows"]
    assert [r["minimum_as_exported"] for r in rows] == [13, 7]


def test_worklist_holds_no_free_text(tmp_path):
    batch, _, _ = batch_with(tmp_path, [make_row(City="Gilroy", Weather="Foggy morning")])
    text = (batch / "worklist.json").read_text()
    for value in ("Gilroy", "Foggy", "Example", "XXSO", "passer-by", "Dementia"):
        assert value not in text, value


def test_cli_prepare(tmp_path, capsys):
    export = write_export(tmp_path / "export.csv", [make_row(), make_row(**{"Mission #": "90002 X", "City": "Elsewhere"})])
    assert isr.main(["prepare", str(export), "--root", str(tmp_path / "root")]) == 0
    out = capsys.readouterr().out
    assert "rows to decide: 2" in out and "in-county: 1" in out and "mutual-aid?: 1" in out
    assert "San Jose" not in out and "Elsewhere" not in out


def test_cli_refusal_exits_1(tmp_path, capsys):
    export = write_export(tmp_path / "export.csv", [make_row()], header=EXPECTED_HEADER[1:])
    assert isr.main(["prepare", str(export), "--root", str(tmp_path / "root")]) == 1
    assert "REFUSED" in capsys.readouterr().err
