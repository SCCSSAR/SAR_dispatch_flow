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
