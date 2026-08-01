"""Tests for rce.ingest.attempts (task A2): config-driven parsing of a
hand-maintained attempt timeline into `attempt` nodes."""

import sqlite3
from pathlib import Path

import pytest

from rce import db
from rce.ingest import attempts

COLUMNS = {
    "id": "#", "date": "Date", "description": "Path",
    "variables": "Variables", "result": "Result", "verdict": "Verdict",
}
HEADING = "Attempt timeline"

# Mirrors the real project map's shape: bold + emoji-marked verdict in row
# 16, a plain arrow in row 1's variables cell, `14a`/`14b` split-row ids.
TABLE_MD = f"""# Project map

## {HEADING} (22 rows)

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-07 | original draft | entropy->rate (monthly) | "significant" | dead end |
| 14a | 07-10 | frozen variant | stance entropy (daily) | never run | dropped |
| 16 | 07-26 | **TopicShift->volatility (16-18)** | TopicShift->RV (monthly) | t=2.91 | ✅ **current** |
"""

TABLE_WITH_PROSE = f"""## {HEADING}

Some narrative notes about the table before it actually appears.

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-07 | x | y | z | ok |
"""

TABLE_RAGGED = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-07 | too few cells |
| 2 | 07-08 | fine row | y | z | ok |
"""


def _config(tmp_path: Path, table_md: str, steps_dir: str | None = None) -> attempts.AttemptsConfig:
    (tmp_path / "map.md").write_text(table_md)
    if steps_dir:
        (tmp_path / steps_dir).mkdir()
    return attempts.AttemptsConfig(file="map.md", heading=HEADING, columns=COLUMNS, steps_dir=steps_dir)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


# -- config loading -----------------------------------------------------------


def test_missing_config_raises_with_template(tmp_path):
    with pytest.raises(attempts.AttemptsConfigError) as excinfo:
        attempts.load_config(tmp_path)
    assert "attempts.toml" in str(excinfo.value)
    assert "[columns]" in str(excinfo.value)  # copy-pasteable template included


def test_config_missing_required_column_raises(tmp_path):
    (tmp_path / ".rce").mkdir()
    (tmp_path / ".rce" / "attempts.toml").write_text(
        'file = "map.md"\nheading = "X"\n[columns]\nid = "#"\n'
    )
    with pytest.raises(attempts.AttemptsConfigError, match="verdict"):
        attempts.load_config(tmp_path)


def test_config_loads_real_shaped_toml(tmp_path):
    (tmp_path / ".rce").mkdir()
    (tmp_path / ".rce" / "attempts.toml").write_text(attempts.SAMPLE_CONFIG)
    config = attempts.load_config(tmp_path)
    assert config.file == "00-项目地图_唯一真相.md"
    assert config.columns["verdict"] == "判决"


# -- table parsing: real-world cell shapes -----------------------------------


def test_parses_bold_arrow_and_emoji_cells(tmp_path):
    rows = attempts.parse_attempts_table(TABLE_MD, HEADING, COLUMNS)
    assert [r.number for r in rows] == ["1", "14a", "16"]
    row16 = rows[2]
    assert row16.description == "TopicShift->volatility (16-18)"  # bold stripped
    assert row16.verdict == "✅ current"  # emoji + bold both survive/stripped correctly
    assert rows[0].variables == "entropy->rate (monthly)"  # arrow untouched


def test_link_code_and_br_cells_are_cleaned(tmp_path):
    md = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-07 | see [report](http://x.test/r) | `raw_metric` value | line one<br>line two | ok |
"""
    row = attempts.parse_attempts_table(md, HEADING, COLUMNS)[0]
    assert row.description == "see report"
    assert row.variables == "raw_metric value"
    assert row.result == "line one\nline two"


def test_prose_between_heading_and_table_is_skipped(tmp_path):
    rows = attempts.parse_attempts_table(TABLE_WITH_PROSE, HEADING, COLUMNS)
    assert len(rows) == 1 and rows[0].number == "1"


def test_ragged_row_skipped_with_warning(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        rows = attempts.parse_attempts_table(TABLE_RAGGED, HEADING, COLUMNS)
    assert [r.number for r in rows] == ["2"]
    assert "expected 6" in caplog.text


def test_heading_not_found_returns_empty(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        rows = attempts.parse_attempts_table("# nothing here\n", HEADING, COLUMNS)
    assert rows == []
    assert "not found" in caplog.text


def test_unmapped_column_name_raises_config_error(tmp_path):
    bad_columns = {**COLUMNS, "verdict": "Judgement (does not exist)"}
    with pytest.raises(attempts.AttemptsConfigError, match="Judgement"):
        attempts.parse_attempts_table(TABLE_MD, HEADING, bad_columns)


# -- step-number reference resolution ----------------------------------------


@pytest.mark.parametrize(
    "description,expected_refs",
    [
        ("path with (5) only", ["5"]),
        ("path with (13-15) range", ["13-15"]),
        ("path with (3) and (7-8)", ["3", "7-8"]),
        ("path with no refs", []),
    ],
)
def test_extract_step_refs(description, expected_refs):
    assert attempts._extract_step_refs(description) == expected_refs


def test_resolve_step_files_matches_prefix_and_reports_broken(tmp_path):
    steps = tmp_path / "steps"
    steps.mkdir()
    (steps / "13-a.Rmd").write_text("")
    (steps / "13-b.pdf").write_text("")
    (steps / "15-c.Rmd").write_text("")
    (steps / "130-not-a-match.py").write_text("")  # must not match step 13

    files, broken = attempts._resolve_step_files(steps, ["13-15"])
    assert files == ["13-a.Rmd", "13-b.pdf", "15-c.Rmd"]
    assert broken == [14]  # 14 is in range but has no file


def test_resolve_step_files_missing_dir_reports_all_broken(tmp_path):
    files, broken = attempts._resolve_step_files(tmp_path / "nope", ["5"])
    assert files == []
    assert broken == [5]


# -- full ingest: idempotency, collisions, human_fields, orphans ------------


def test_ingest_creates_nodes_and_sets_human_fields(conn, tmp_path):
    config = _config(tmp_path, TABLE_MD, steps_dir="steps")
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["attempts"] == 3
    assert counts["created"] == 3

    node = db.get_node(conn, "attempt:map.md#16")
    assert node["attrs"]["step_refs"] == ["16-18"]
    assert node["attrs"]["step_files"] == []  # steps_dir exists but is empty
    assert node["attrs"]["step_files_broken"] == [16, 17, 18]
    assert node["human_fields"] == {"verdict": "✅ current", "result": "t=2.91"}


def test_reingest_is_idempotent_and_preserves_edited_human_fields(conn, tmp_path):
    config = _config(tmp_path, TABLE_MD)
    attempts.ingest_attempts_repo(conn, tmp_path, config)

    # A human corrects the stored verdict through some other write path.
    db.set_human_fields(conn, "attempt:map.md#16", {"verdict": "manually corrected", "result": "t=2.91"})

    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["attempts"] == 3
    assert counts["created"] == 0
    assert counts["updated"] == 3
    node = db.get_node(conn, "attempt:map.md#16")
    assert node["human_fields"]["verdict"] == "manually corrected"  # never clobbered


def test_id_collision_with_different_description_is_skipped(conn, tmp_path):
    config = _config(tmp_path, TABLE_MD)
    attempts.ingest_attempts_repo(conn, tmp_path, config)

    colliding_md = TABLE_MD.replace(
        "| 1 | 07-07 | original draft | entropy->rate (monthly) | \"significant\" | dead end |",
        "| 1 | 07-07 | a totally different attempt | z | z | z |",
    )
    (tmp_path / "map.md").write_text(colliding_md)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["collisions_skipped"] == 1
    node = db.get_node(conn, "attempt:map.md#1")
    assert node["attrs"]["description"] == "original draft"  # untouched by the collision


def test_row_removed_from_table_is_preserved_with_human_fields(conn, tmp_path):
    config = _config(tmp_path, TABLE_MD)
    attempts.ingest_attempts_repo(conn, tmp_path, config)

    trimmed_md = "\n".join(line for line in TABLE_MD.splitlines() if "| 16 |" not in line)
    (tmp_path / "map.md").write_text(trimmed_md)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["orphans_preserved"] == 1
    assert counts["orphans_removed"] == 0
    assert db.get_node(conn, "attempt:map.md#16") is not None  # never deleted


def test_orphan_without_human_fields_is_removed(conn, tmp_path):
    # A node of this type/file with no human_fields (e.g. from a bug or a
    # future extractor) is the one case cleanup actually deletes.
    db.upsert_node(conn, "attempt:map.md#99", "attempt", attrs={"description": "stray"})
    config = _config(tmp_path, TABLE_MD)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["orphans_removed"] == 1
    assert db.get_node(conn, "attempt:map.md#99") is None


def test_unreadable_source_file_returns_zero_counts(conn, tmp_path):
    config = attempts.AttemptsConfig(file="does-not-exist.md", heading=HEADING, columns=COLUMNS)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["attempts"] == 0
