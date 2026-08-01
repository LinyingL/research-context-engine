"""Tests for rce.ingest.attempts (task A2): config-driven parsing of a
hand-maintained attempt timeline into `attempt` nodes."""

import ast
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


def test_sample_config_dead_variables_and_active_verdicts_actually_load(tmp_path):
    """B1 regression: SAMPLE_CONFIG is both the module's own documentation
    and the exact template printed to a user with no config file yet -- if
    its own `dead_variables`/`active_verdicts` keys land inside `[columns]`
    (a TOML ordering bug: any bare key written after a [table] header nests
    into that table), every user who copy-pastes it gets a silently-None
    config and the revived-dead-variables check always reports "skipped"
    with no indication anything is wrong."""
    (tmp_path / ".rce").mkdir()
    (tmp_path / ".rce" / "attempts.toml").write_text(attempts.SAMPLE_CONFIG)
    config = attempts.load_config(tmp_path)
    assert config.dead_variables is not None
    assert config.active_verdicts is not None
    assert "信息熵" in config.dead_variables
    assert "✅" in config.active_verdicts
    # Also confirm they did NOT get nested under columns (the actual bug shape).
    assert "dead_variables" not in config.columns
    assert "active_verdicts" not in config.columns


def test_config_reads_date_year(tmp_path):
    (tmp_path / ".rce").mkdir()
    (tmp_path / ".rce" / "attempts.toml").write_text(
        'file = "map.md"\nheading = "X"\ndate_year = 2026\n'
        '[columns]\nid = "#"\ndate = "d"\ndescription = "p"\n'
        'variables = "v"\nresult = "r"\nverdict = "j"\n'
    )
    config = attempts.load_config(tmp_path)
    assert config.date_year == 2026


def test_config_date_year_must_be_int(tmp_path):
    (tmp_path / ".rce").mkdir()
    (tmp_path / ".rce" / "attempts.toml").write_text(
        'file = "map.md"\nheading = "X"\ndate_year = "2026"\n'
        '[columns]\nid = "#"\ndate = "d"\ndescription = "p"\n'
        'variables = "v"\nresult = "r"\nverdict = "j"\n'
    )
    with pytest.raises(attempts.AttemptsConfigError, match="date_year"):
        attempts.load_config(tmp_path)


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


def test_extract_step_refs_ignores_year_range(caplog):
    """Regression: a 4-digit year range copied into the description (e.g.
    from the project map's own narrative text) must never be mistaken for a
    13-step phantom range."""
    with caplog.at_level("WARNING"):
        refs = attempts._extract_step_refs("background (2014-2026) and steps (16-18)")
    assert refs == ["16-18"]
    assert "2014" not in "".join(refs)


def test_extract_step_refs_drops_overly_wide_range(caplog):
    with caplog.at_level("WARNING"):
        refs = attempts._extract_step_refs("typo'd range (1-500)")
    assert refs == []
    assert "wider than" in caplog.text


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


def test_reingest_is_idempotent_when_source_is_unchanged(conn, tmp_path, monkeypatch):
    """No source-file change -> no human_fields write at all, not merely an
    unchanged value: re-ingest must not call set_human_fields a second time
    for a row whose verdict/result didn't move (no gratuitous write, no log
    noise -- see module docstring)."""
    config = _config(tmp_path, TABLE_MD)
    attempts.ingest_attempts_repo(conn, tmp_path, config)

    calls: list[str] = []
    original = db.set_human_fields

    def spy(conn_, node_id, human_fields):
        calls.append(node_id)
        return original(conn_, node_id, human_fields)

    monkeypatch.setattr(db, "set_human_fields", spy)

    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["attempts"] == 3
    assert counts["created"] == 0
    assert counts["updated"] == 3
    assert calls == []  # nothing in the source changed -- no write, no noise
    node = db.get_node(conn, "attempt:map.md#16")
    assert node["human_fields"] == {"verdict": "✅ current", "result": "t=2.91"}


def test_reingest_resyncs_human_fields_when_source_verdict_changes(conn, tmp_path):
    """Architecture fix (this task): verdict/result are the researcher's
    judgement, but they live in the source Markdown file, not in this
    graph -- so a re-ingest must resync them, mirroring a real audit where
    a row's verdict changed from a dead marker to a revived one in the
    source and the graph needed to pick that up on the next `rce attempts`
    run, not stay frozen on the first parse."""
    config = _config(tmp_path, TABLE_MD)
    attempts.ingest_attempts_repo(conn, tmp_path, config)
    node = db.get_node(conn, "attempt:map.md#16")
    assert node["human_fields"] == {"verdict": "✅ current", "result": "t=2.91"}

    revived_md = TABLE_MD.replace(
        "| 16 | 07-26 | **TopicShift->volatility (16-18)** | TopicShift->RV (monthly) | t=2.91 | ✅ **current** |",
        "| 16 | 07-26 | **TopicShift->volatility (16-18)** | TopicShift->RV (monthly) | t=2.91 | 🕒 decided to revive |",
    )
    (tmp_path / "map.md").write_text(revived_md)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["created"] == 0
    assert counts["updated"] == 3
    node = db.get_node(conn, "attempt:map.md#16")
    assert node["human_fields"] == {"verdict": "🕒 decided to revive", "result": "t=2.91"}


def test_edited_description_refreshes_attrs_and_resyncs_human_fields(conn, tmp_path):
    """B4 regression (attrs refresh) plus the architecture fix (human_fields
    resync): editing the description ("途径") cell is everyday table
    maintenance, not an id collision -- attrs must refresh to the new text
    on re-ingest, and this must never be counted as a collision. A verdict
    written through some other path than the source file (simulated here;
    not an actual write path this codebase offers -- see module docstring's
    "two authorities" caveat) does not survive a re-ingest: the source file
    is the sole authority for verdict/result, so it resyncs back to what
    the source currently says (here: unchanged, since only the description
    cell moved) rather than preserving the out-of-band edit."""
    config = _config(tmp_path, TABLE_MD)
    attempts.ingest_attempts_repo(conn, tmp_path, config)

    db.set_human_fields(conn, "attempt:map.md#1", {"verdict": "manually corrected", "result": "z"})

    reworded_md = TABLE_MD.replace(
        "| 1 | 07-07 | original draft | entropy->rate (monthly) | \"significant\" | dead end |",
        "| 1 | 07-07 | reworded description | entropy->rate (monthly) | \"significant\" | dead end |",
    )
    (tmp_path / "map.md").write_text(reworded_md)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["collisions_skipped"] == 0
    node = db.get_node(conn, "attempt:map.md#1")
    assert node["attrs"]["description"] == "reworded description"  # attrs refreshed
    assert node["title"] == "reworded description"
    # Resynced from the source file, not the out-of-band "manually
    # corrected" value -- the source, not the graph, is authoritative here.
    assert node["human_fields"] == {"verdict": "dead end", "result": "\"significant\""}


def test_duplicate_id_within_one_parse_is_a_collision(conn, tmp_path):
    """The real collision DESIGN.md section 4 means: the same `#` used by
    two different rows in the same table (a duplicate, unrenumbered id),
    not a merely-reworded description."""
    md = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-07 | first row with id 1 | x | y | ok |
| 1 | 07-08 | second, different row also claiming id 1 | x | y | ok |
"""
    config = _config(tmp_path, md)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["attempts"] == 1
    assert counts["collisions_skipped"] == 1
    node = db.get_node(conn, "attempt:map.md#1")
    assert node["attrs"]["description"] == "first row with id 1"  # first row wins


def test_row_removed_from_table_is_deleted_with_its_node(conn, tmp_path):
    """Architecture fix (this task, deletion direction): the source file is
    the sole authority for whether an attempt row exists at all, exactly as
    it already is for verdict/result (see module docstring). A row deleted
    from the table must not leave a stale node behind for `rce attempts`'
    listing or `--check` to keep reporting on -- the previous "preserve any
    node with human_fields" guard was always true in practice (every node
    gets human_fields on first parse, even an empty one) and so never
    actually protected anything; it just made a deleted row un-deletable."""
    config = _config(tmp_path, TABLE_MD)
    attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert db.get_node(conn, "attempt:map.md#16") is not None

    trimmed_md = "\n".join(line for line in TABLE_MD.splitlines() if "| 16 |" not in line)
    (tmp_path / "map.md").write_text(trimmed_md)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["orphans_removed"] == 1
    assert "orphans_preserved" not in counts
    assert db.get_node(conn, "attempt:map.md#16") is None


def test_row_with_empty_verdict_and_result_is_still_deleted_when_removed(conn, tmp_path):
    """The guard this replaced (`if node["human_fields"]:`) was truthy even
    for a row whose verdict/result cells are both empty strings -- a
    non-empty dict `{"verdict": "", "result": ""}` is still truthy in
    Python -- so an empty-judgement row was exactly as stuck as a
    judged one. Confirms the fix covers that case too, not just a row that
    happens to carry non-empty verdict/result."""
    md = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-07 | keep me | x | y | ok |
| 2 | 07-08 | never judged yet | x | | |
"""
    config = _config(tmp_path, md)
    attempts.ingest_attempts_repo(conn, tmp_path, config)
    node = db.get_node(conn, "attempt:map.md#2")
    assert node["human_fields"] == {"verdict": "", "result": ""}  # truthy dict, empty values

    trimmed_md = "\n".join(line for line in md.splitlines() if "| 2 |" not in line)
    (tmp_path / "map.md").write_text(trimmed_md)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["orphans_removed"] == 1
    assert db.get_node(conn, "attempt:map.md#2") is None
    assert db.get_node(conn, "attempt:map.md#1") is not None  # untouched


def test_deleted_row_orphan_cleanup_also_removes_edges_from_other_extractors(conn, tmp_path):
    """rce.consistency writes `attempt --uses--> commit` edges tagged
    extractor="attempts_consistency", not "attempts". Unlike
    rce.ingest.claims's orphan cleanup (extractor-scoped, on purpose --
    see _cleanup_orphans docstring), this one must not scope the edge
    delete to its own extractor: a leftover `uses` edge from an earlier
    `--check` run still references the node, and nodes.id is a foreign key
    with no cascade, so leaving it in place would make delete_node raise
    sqlite3.IntegrityError -- exactly the real-world case (steps_dir
    configured, `--check` already run once) this test reproduces."""
    node_id = "attempt:map.md#1"
    config = _config(tmp_path, TABLE_MD)
    attempts.ingest_attempts_repo(conn, tmp_path, config)
    commit_node_id = "commit:deadbeef"
    db.upsert_node(conn, commit_node_id, "commit", attrs={})
    db.upsert_edge(
        conn, node_id, commit_node_id, "uses", extractor="attempts_consistency",
        confidence=1.0, status="auto", evidence={"occurrences": [{"script": "steps/1-a.py"}]},
    )
    assert db.query_edges(conn, src=node_id, type="uses")

    trimmed_md = "\n".join(line for line in TABLE_MD.splitlines() if "| 1 |" not in line)
    (tmp_path / "map.md").write_text(trimmed_md)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)  # must not raise
    assert counts["orphans_removed"] == 1
    assert db.get_node(conn, node_id) is None
    assert db.query_edges(conn, src=node_id, type="uses") == []


def test_unreadable_source_file_returns_zero_counts(conn, tmp_path):
    config = attempts.AttemptsConfig(file="does-not-exist.md", heading=HEADING, columns=COLUMNS)
    counts = attempts.ingest_attempts_repo(conn, tmp_path, config)
    assert counts["attempts"] == 0


# -- guardrail: the resync architecture's single-caller precondition --------


def _set_human_fields_call_sites(src_root: Path) -> list[tuple[Path, int]]:
    """Every syntactic call to `set_human_fields`/`db.set_human_fields` in
    `src_root`, found via `ast` (not a text grep) so a docstring or comment
    merely *mentioning* the name -- there are several, including in this
    very module's own docstring -- is never mistaken for a call site, and
    `db.py`'s own `def set_human_fields` is never mistaken for one either."""
    sites: list[tuple[Path, int]] = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if name == "set_human_fields":
                sites.append((path, node.lineno))
    return sites


def test_set_human_fields_has_exactly_one_caller_in_src():
    """Tripwire for the precondition the resync-from-source architecture
    rests on (module docstring's "This assumes the source Markdown file is
    currently the *only* place an attempt's verdict is ever recorded"):
    today `db.set_human_fields` has exactly one caller anywhere in `src/`,
    this extractor's own resync call. Was previously only a claim in a
    comment; now goes red the moment a second caller appears, instead of
    the two authorities silently disagreeing."""
    src_root = Path(__file__).resolve().parent.parent / "src"
    sites = _set_human_fields_call_sites(src_root)
    assert len(sites) == 1, (
        f"expected exactly one src/ call site for db.set_human_fields, found {len(sites)}: "
        f"{sites} -- if you just added a second caller, read rce.ingest.attempts's module "
        f"docstring (the 'two authorities that can disagree' paragraph) before proceeding, "
        f"this precondition no longer holds and must be resolved explicitly"
    )
    assert sites[0][0].name == "attempts.py"
