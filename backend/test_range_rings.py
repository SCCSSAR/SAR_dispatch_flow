"""Tests for the range-ring distance table: the generic public default, the
RANGE_RING_TABLE Secret Manager override, and the wiring that carries them into
the Gemini prompts and Terraform.

Fixture values are deliberately round, invented numbers. This repo is public and
an override may hold a licensed dataset, so no real table belongs in a test.
"""
import ast
import json
import logging
import re
from pathlib import Path

import pytest

import range_rings
from range_rings import (
    DEFAULT_RING_TABLE,
    load_ring_table,
    parse_ring_table,
    render_prompt_block,
)

BACKEND = Path(__file__).parent
REPO = BACKEND.parent
ENVS = ("dev", "sccssar-dev")


def _override(**changes):
    table = {
        "source": "Example Team table, 2026",
        "categories": {
            "Dementia": [
                {"label": "25th percentile", "miles": 1.5},
                {"label": "50th percentile (median)", "miles": 2.5},
                {"label": "95th percentile", "miles": 9.5, "draw": False},
            ],
            "Hiker": [{"label": "75th percentile", "miles": 4.5}],
        },
    }
    table.update(changes)
    return json.dumps(table)


def _with_row(row):
    return _override(categories={"Dementia": [row]})


def _parse_rings(summary):
    """Executes the PRODUCTION parser out of main.py, which is not importable
    here but whose ring block is pure text."""
    src = (BACKEND / "main.py").read_text(encoding="utf-8")
    start = src.index("_LPB_SECTION_RE = re.compile")
    end = src.index("def _insert_subject_last_seen_entry")
    ns = {"re": re}
    exec(src[start:end], ns)  # noqa: S102 - production source, not input
    return ns["_parse_lpb_range_rings"](summary)


def _summary_from_block(block, category):
    """What a faithful Gemini copy of one category's lines looks like."""
    lines = block.splitlines()
    heading = next(l.strip() for l in lines if l.strip().startswith("2. "))
    start = next(i for i, l in enumerate(lines) if l.strip() == f"{category}:")
    rows = []
    for line in lines[start + 1:]:
        if not line.strip().startswith("- "):
            break
        rows.append(line.strip())
    return "\n".join([
        "LPB Range Ring Analysis:",
        f"1. Subject Category: {category}. Key factors: Alone.",
        heading,
        *rows,
        "3. Local Modifiers: Urban grid constrains travel to street corridors.",
    ])


class TestDefaultTable:
    def test_is_the_static_rings_for_every_category(self):
        rows = [
            [(r.label, r.miles, r.draw) for r in cat]
            for cat in DEFAULT_RING_TABLE.categories.values()
        ]
        assert rows[0] == [
            ("generic planning ring", 0.25, True),
            ("generic planning ring", 0.5, True),
            ("generic planning ring", 1.0, True),
        ]
        assert all(r == rows[0] for r in rows), (
            "A default that varies by category reads as a statistic, and this "
            "repo is public: per-category values belong in the override"
        )

    def test_keeps_the_subject_categories_gemini_chooses_from(self):
        assert set(DEFAULT_RING_TABLE.categories) == {
            "Dementia", "Mentally Ill", "Despondent", "Substance Intoxication",
            "Child 1-3", "Child 4-6", "Child 7-9", "Child 10-12",
            "Hiker", "Climber",
        }

    def test_source_says_these_are_not_statistics(self):
        assert "not statistics" in DEFAULT_RING_TABLE.source


class TestParseRingTable:
    def test_valid_override(self):
        table = parse_ring_table(_override())
        assert table.source == "Example Team table, 2026"
        assert [r.miles for r in table.categories["Dementia"]] == [1.5, 2.5, 9.5]
        assert [r.draw for r in table.categories["Dementia"]] == [True, True, False]

    @pytest.mark.parametrize("raw", ["", "   \n", "{}", " {} \n"])
    def test_unset_values_mean_no_override(self, raw):
        """`{}` is the placeholder version Terraform creates, so an env with no
        table still deploys and is not reported as broken."""
        assert parse_ring_table(raw) is None

    @pytest.mark.parametrize("raw", [
        "not json",
        "[]",
        json.dumps({"categories": {"Dementia": [{"label": "x", "miles": 1.5}]}}),
        _override(source=""),
        _override(source="two\nlines"),
        _override(source=7),
        _override(categories={}),
        _override(categories=[]),
        _override(categories={"": [{"label": "x", "miles": 1.5}]}),
        _override(categories={"Two\nLines": [{"label": "x", "miles": 1.5}]}),
        _override(categories={"Dementia": []}),
        _override(categories={"Dementia": "1.5"}),
        _with_row("1.5"),
        _with_row({"miles": 1.5}),
        _with_row({"label": "", "miles": 1.5}),
        _with_row({"label": "two\nlines", "miles": 1.5}),
        _with_row({"label": "x"}),
        _with_row({"label": "x", "miles": "1.5"}),
        _with_row({"label": "x", "miles": True}),
        _with_row({"label": "x", "miles": 0}),
        _with_row({"label": "x", "miles": -1.5}),
        _with_row({"label": "x", "miles": range_rings.MAX_RING_RADIUS_MI + 1}),
        _with_row({"label": "x", "miles": 1.5, "draw": "no"}),
        _with_row({"label": "x", "miles": 1.5, "drawn": False}),
    ])
    def test_invalid_tables_are_refused(self, raw):
        with pytest.raises(ValueError):
            parse_ring_table(raw)

    def test_radius_cap_is_inclusive_and_applies_only_to_drawn_rows(self):
        cap = range_rings.MAX_RING_RADIUS_MI
        assert parse_ring_table(_with_row({"label": "x", "miles": cap}))
        table = parse_ring_table(_with_row({"label": "x", "miles": cap + 1, "draw": False}))
        assert table.categories["Dementia"][0].miles == cap + 1


class TestLoadRingTable:
    def test_unset_uses_the_default_quietly(self, monkeypatch, caplog):
        monkeypatch.delenv("RANGE_RING_TABLE", raising=False)
        with caplog.at_level(logging.INFO, logger="range_rings"):
            assert load_ring_table() is DEFAULT_RING_TABLE
        assert "mode=default" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_valid_env_value_overrides_the_default(self, monkeypatch, caplog):
        monkeypatch.setenv("RANGE_RING_TABLE", _override())
        with caplog.at_level(logging.INFO, logger="range_rings"):
            table = load_ring_table()
        assert table.source == "Example Team table, 2026"
        assert "mode=override categories=2" in caplog.text

    @pytest.mark.parametrize("raw", [
        '{"source": "PRIVATE-SOURCE", "categories": {"PRIVATE-CATEGORY": [{"label": "PRIVATE-LABEL", "miles": "77.7"}]}}',
        'PRIVATE-SOURCE PRIVATE-CATEGORY PRIVATE-LABEL 77.7',
    ])
    def test_invalid_value_falls_back_without_logging_its_contents(self, monkeypatch, caplog, raw):
        """A bad secret must cost the override, not the dispatch. It logs at
        ERROR because the fallback is a real loss, and never echoes the value:
        the table may be licensed and logs are not the place for it."""
        monkeypatch.setenv("RANGE_RING_TABLE", raw)
        with caplog.at_level(logging.INFO, logger="range_rings"):
            assert load_ring_table() is DEFAULT_RING_TABLE
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert re.fullmatch(
            r"Range ring table \| mode=invalid error=(ValueError|JSONDecodeError)",
            errors[0].getMessage(),
        )
        for private in ("PRIVATE", "77.7"):
            assert private not in caplog.text

    def test_absurdly_nested_value_still_falls_back(self, monkeypatch):
        monkeypatch.setenv("RANGE_RING_TABLE", "[" * 200_000)
        assert load_ring_table() is DEFAULT_RING_TABLE

    def test_one_bad_row_loses_the_whole_override(self, monkeypatch):
        """Half a table would mix sourced rows with generic ones under one
        heading, so the override is all or nothing."""
        table = json.loads(_override())
        table["categories"]["Hiker"][0]["miles"] = "bad"
        monkeypatch.setenv("RANGE_RING_TABLE", json.dumps(table))
        assert load_ring_table() is DEFAULT_RING_TABLE


class TestRenderPromptBlock:
    def test_heading_names_the_source(self):
        block = render_prompt_block(parse_ring_table(_override()))
        assert "2. Range Ring Distances (Example Team table, 2026):" in block

    def test_lines_are_miles_first_with_computed_km(self):
        block = render_prompt_block(parse_ring_table(_override()))
        assert "- 1.5 miles (2.4 km) — 25th percentile" in block

    def test_default_keeps_the_quarter_mile_precision(self):
        block = render_prompt_block(DEFAULT_RING_TABLE)
        for line in (
            "- 0.25 miles (0.4 km) — generic planning ring",
            "- 0.5 miles (0.8 km) — generic planning ring",
            "- 1.0 miles (1.6 km) — generic planning ring",
        ):
            assert line in block

    def test_undrawn_row_is_marked_text_only(self):
        block = render_prompt_block(parse_ring_table(_override()))
        assert "- 9.5 miles (15.3 km) — 95th percentile (text only)" in block
        assert "- 2.5 miles (4.0 km) — 50th percentile (median)\n" in block

    def test_default_round_trips_through_the_production_ring_parser(self):
        summary = _summary_from_block(render_prompt_block(DEFAULT_RING_TABLE), "Dementia")
        assert _parse_rings(summary) == [
            {"radius_miles": 0.25, "label": "0.25mi — generic planning ring"},
            {"radius_miles": 0.5, "label": "0.5mi — generic planning ring"},
            {"radius_miles": 1.0, "label": "1.0mi — generic planning ring"},
        ]

    def test_override_draws_its_rows_and_not_the_text_only_one(self):
        block = render_prompt_block(parse_ring_table(_override()))
        rings = _parse_rings(_summary_from_block(block, "Dementia"))
        assert [r["radius_miles"] for r in rings] == [1.5, 2.5]
        assert "- 9.5 miles" in _summary_from_block(block, "Dementia")


class TestRingParserCompatibility:
    def test_old_heading_still_parses(self):
        """Gemini can keep the old wording, and a heading miss silently loses
        every ring (seen 2026-10-29), so the parser accepts both."""
        summary = (
            "LPB Range Ring Analysis (Robert Koester):\n"
            "2. Koester Statistics for this category:\n"
            "- 1.5 miles (2.4 km) — 25th percentile distance\n"
            "3. Local Modifiers: none.\n"
        )
        assert [r["radius_miles"] for r in _parse_rings(summary)] == [1.5]

    def test_radius_cap_matches_the_table_validator(self):
        src = (BACKEND / "main.py").read_text(encoding="utf-8")
        assert f"_MAX_RING_RADIUS_MI = {range_rings.MAX_RING_RADIUS_MI}" in src


class TestPromptWiring:
    @staticmethod
    def _gemini_src():
        return (BACKEND / "gemini.py").read_text(encoding="utf-8")

    @classmethod
    def _function(cls, name):
        tree = ast.parse(cls._gemini_src())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return ast.unparse(node)
        raise AssertionError(f"{name} not found in gemini.py")

    @classmethod
    def _prompt(cls, name):
        """gemini.py needs google-genai, absent under local pytest, so the
        prompt literals are read out of the source."""
        for node in ast.parse(cls._gemini_src()).body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == name:
                return ast.literal_eval(node.value)
        raise AssertionError(f"{name} not found in gemini.py")

    @pytest.mark.parametrize("prompt", ["SYSTEM_PROMPT", "STAGING_KOESTER_PROMPT"])
    def test_both_prompts_carry_the_placeholder_and_no_table(self, prompt):
        text = self._prompt(prompt)
        assert text.count("__RANGE_RING_TABLE__") == 1
        assert not re.search(r"\d\s*(?:mi|miles)\s*\(\s*\d", text), (
            "Distance values are back in the prompt source, where an override "
            "cannot replace them"
        )
        assert "Koester Statistics" not in text
        assert "published Koester values" not in text

    @pytest.mark.parametrize("function", ["extract_incident_summary", "extract_staging_and_koester"])
    def test_both_call_paths_substitute_it(self, function):
        assert ".replace('__RANGE_RING_TABLE__', range_ring_prompt_block())" in self._function(function)

    def test_block_comes_from_the_loaded_table(self):
        assert "return render_prompt_block(load_ring_table())" in self._function("range_ring_prompt_block")

    def test_startup_loads_the_table_so_a_bad_secret_logs_before_a_callout(self):
        src = (BACKEND / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        lifespan = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"
        )
        calls = [
            n.value.func.id for n in lifespan.body
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
        ]
        assert "range_ring_prompt_block" in calls


class TestTerraformWiring:
    """The secret must exist with a version before Cloud Run can mount it, and
    the licensed table must never pass through Terraform state."""

    @staticmethod
    def _tf(env):
        return (REPO / "terraform" / "environments" / env / "main.tf").read_text(encoding="utf-8")

    @pytest.mark.parametrize("env", ENVS)
    def test_env_var_mounts_the_secret_as_the_last_env_block(self, env):
        tf = self._tf(env)
        names = re.findall(r'^\s*name\s*=\s*"([A-Z][A-Z0-9_]*)"', tf, re.MULTILINE)
        assert names[-1] == range_rings.ENV_VAR, (
            "Rule #7: append new env blocks at the END of the list"
        )
        block = tf[tf.index(f'name = "{range_rings.ENV_VAR}"'):]
        assert re.search(
            r'secret_key_ref\s*\{\s*secret\s*=\s*"range-ring-table"\s*version\s*=\s*"latest"', block
        )

    @pytest.mark.parametrize("env", ENVS)
    def test_runtime_account_can_read_it(self, env):
        tf = self._tf(env)
        start = tf.index("dispatch_runner_accessor_secrets = toset([")
        assert '"range-ring-table"' in tf[start:tf.index("])", start)]

    @pytest.mark.parametrize("env", ENVS)
    def test_terraform_only_ever_writes_the_empty_placeholder(self, env):
        tf = self._tf(env)
        start = tf.index('resource "google_secret_manager_secret_version" "range_ring_table"')
        block = tf[start:tf.index("\n}\n", start)]
        assert re.search(r'secret_data\s*=\s*"\{\}"', block)
        assert re.search(r"ignore_changes\s*=\s*\[secret_data\]", block)

    @pytest.mark.parametrize("env", ENVS)
    def test_service_waits_for_the_placeholder_version(self, env):
        tf = self._tf(env)
        assert "google_secret_manager_secret_version.range_ring_table" in tf
