"""Tests for rce.consistency (task A3): the three deterministic checks over
an ingested attempt timeline, plus `rce attempts`'/`--check`'s CLI wiring.
"""

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from rce import cli, consistency, db
from rce.ingest import attempts as attempts_ingest

HEADING = "Attempt timeline"
COLUMNS = {
    "id": "#", "date": "Date", "description": "Path",
    "variables": "Variables", "result": "Result", "verdict": "Verdict",
}


def _git(repo: Path, *args: str, env: dict | None = None) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _commit_file(repo: Path, rel_path: str, content: str, authored_at: str) -> str:
    """Commit `rel_path` with a fixed author/committer date, so the
    stale-verdict check has a deterministic commit time to compare against."""
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", rel_path)
    env = {**os.environ, "GIT_AUTHOR_DATE": authored_at, "GIT_COMMITTER_DATE": authored_at}
    _git(repo, "-c", "user.name=A", "-c", "user.email=a@example.com", "commit", "-m", f"add {rel_path}", env=env)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _config(
    steps_dir: str | None = None, dead_variables=None, active_verdicts=None, date_year=None,
) -> attempts_ingest.AttemptsConfig:
    return attempts_ingest.AttemptsConfig(
        file="map.md", heading=HEADING, columns=COLUMNS, steps_dir=steps_dir,
        dead_variables=dead_variables, active_verdicts=active_verdicts, date_year=date_year,
    )


def _ingest(conn, repo: Path, table_md: str, config: attempts_ingest.AttemptsConfig) -> None:
    (repo / "map.md").write_text(table_md)
    attempts_ingest.ingest_attempts_repo(conn, repo, config)


# -- check 1: broken references ----------------------------------------------


def test_broken_reference_reports_missing_step_and_neighbors(conn, tmp_path):
    (tmp_path / "steps").mkdir()
    (tmp_path / "steps" / "16-a.py").write_text("")
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | volatility test (16-18) | x | y | ✅ current |
"""
    config = _config(steps_dir="steps")
    _ingest(conn, tmp_path, table, config)

    result = consistency.check_broken_references(conn, tmp_path, config)
    assert not result.skipped
    findings = {f["missing_step"]: f["neighbors"] for f in result.findings}
    assert findings == {
        17: {"prev_available": 16, "next_available": None},
        18: {"prev_available": 16, "next_available": None},
    }


def test_broken_reference_finds_nothing_when_all_steps_present(conn, tmp_path):
    (tmp_path / "steps").mkdir()
    for n in (16, 17, 18):
        (tmp_path / "steps" / f"{n}-a.py").write_text("")
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | volatility test (16-18) | x | y | ✅ current |
"""
    config = _config(steps_dir="steps")
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_broken_references(conn, tmp_path, config)
    assert result.findings == []
    assert not result.skipped


def test_broken_reference_skipped_without_steps_dir(conn, tmp_path):
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | volatility test (16-18) | x | y | ✅ current |
"""
    config = _config(steps_dir=None)
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_broken_references(conn, tmp_path, config)
    assert result.skipped
    assert "steps_dir" in result.skip_reason


# -- check 2: stale verdicts --------------------------------------------------


def test_stale_verdict_flagged_when_script_committed_after_attempt_date(conn, tmp_path):
    _git(tmp_path, "init", "-q")
    _commit_file(tmp_path, "steps/16-a.py", "print(1)\n", "2026-07-15T12:00:00+00:00")
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | volatility test (16) | x | y | ✅ current |
"""
    config = _config(steps_dir="steps")
    _ingest(conn, tmp_path, table, config)

    result = consistency.check_stale_verdicts(conn, tmp_path, config)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding["attempt"] == "attempt:map.md#1"
    assert finding["basis"] == "git"
    assert finding["script_last_touched"] == "2026-07-15"


def test_stale_verdict_not_flagged_when_attempt_date_after_script_commit(conn, tmp_path):
    _git(tmp_path, "init", "-q")
    _commit_file(tmp_path, "steps/16-a.py", "print(1)\n", "2026-06-01T12:00:00+00:00")
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | volatility test (16) | x | y | ✅ current |
"""
    config = _config(steps_dir="steps")
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_stale_verdicts(conn, tmp_path, config)
    assert result.findings == []


def test_stale_verdict_skips_unparseable_date(conn, tmp_path, caplog):
    _git(tmp_path, "init", "-q")
    _commit_file(tmp_path, "steps/16-a.py", "print(1)\n", "2026-07-15T12:00:00+00:00")
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-01 | volatility test (16) | x | y | ✅ current |
"""
    config = _config(steps_dir="steps")
    _ingest(conn, tmp_path, table, config)
    with caplog.at_level("INFO"):
        result = consistency.check_stale_verdicts(conn, tmp_path, config)
    assert result.findings == []  # never guessed at the missing year
    assert "not a plain YYYY-MM-DD date" in caplog.text


def test_stale_verdict_falls_back_to_mtime_for_untracked_script(conn, tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "steps").mkdir()
    (tmp_path / "steps" / "16-a.py").write_text("print(1)\n")  # never `git add`ed
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2020-01-01 | volatility test (16) | x | y | ✅ current |
"""
    config = _config(steps_dir="steps")
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_stale_verdicts(conn, tmp_path, config)
    assert len(result.findings) == 1
    assert result.findings[0]["basis"] == "mtime"


def test_stale_verdict_skipped_without_steps_dir(conn, tmp_path):
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | volatility test | x | y | ✅ current |
"""
    config = _config(steps_dir=None)
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_stale_verdicts(conn, tmp_path, config)
    assert result.skipped
    assert "steps_dir" in result.skip_reason


def test_uses_edge_created_and_idempotent_on_rerun(conn, tmp_path):
    _git(tmp_path, "init", "-q")
    sha = _commit_file(tmp_path, "steps/16-a.py", "print(1)\n", "2026-07-15T12:00:00+00:00")
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | volatility test (16) | x | y | ✅ current |
"""
    config = _config(steps_dir="steps")
    _ingest(conn, tmp_path, table, config)

    consistency.check_stale_verdicts(conn, tmp_path, config)
    edges = db.query_edges(conn, src="attempt:map.md#1", type="uses")
    assert len(edges) == 1
    assert edges[0]["dst"] == f"commit:{sha}"
    assert edges[0]["evidence"]["occurrences"][0]["script"] == "steps/16-a.py"

    consistency.check_stale_verdicts(conn, tmp_path, config)  # re-run
    edges_again = db.query_edges(conn, src="attempt:map.md#1", type="uses")
    assert len(edges_again) == 1  # no duplicate edge row
    assert len(edges_again[0]["evidence"]["occurrences"]) == 1  # no duplicate occurrence


# -- stale verdicts: date_year-aware parsing (B3b) ---------------------------


@pytest.mark.parametrize(
    "raw_date,expected",
    [
        ("07-26", "2026-07-26"),
        ("≤07-07", "2026-07-07"),
        ("<=07-07", "2026-07-07"),
        ("07-08~09", "2026-07-09"),   # same-month range -- later day
        ("07-10 冻结", "2026-07-10"),  # trailing Chinese annotation ignored
        ("07-09~12", "2026-07-12"),   # later day, still same month
        ("07-28~08-02", "2026-08-02"),  # cross-month range -- later month+day
        ("2026-07-15", "2026-07-15"),  # full ISO still accepted with date_year set
    ],
)
def test_parse_attempt_date_accepted_forms_with_date_year(raw_date, expected):
    assert consistency._parse_attempt_date(raw_date, date_year=2026) == date.fromisoformat(expected)


@pytest.mark.parametrize("raw_date", ["07-26", "≤07-07", "07-08~09", "not a date", ""])
def test_parse_attempt_date_rejects_non_iso_without_date_year(raw_date):
    assert consistency._parse_attempt_date(raw_date, date_year=None) is None


def test_stale_verdict_uses_date_year_for_real_shaped_dates(conn, tmp_path):
    """The exact five real-world date shapes from a hand-maintained table
    (task B3b): none are plain YYYY-MM-DD, all must parse once date_year is
    declared, and the later end of any range must be the one compared
    against the script's last-touch date."""
    _git(tmp_path, "init", "-q")
    _commit_file(tmp_path, "steps/5-a.py", "print(1)\n", "2026-07-27T12:00:00+00:00")
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-26 | volatility test (5) | x | y | ✅ current |
"""
    config = _config(steps_dir="steps", date_year=2026)
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_stale_verdicts(conn, tmp_path, config)
    assert result.checked == 1
    assert result.total == 1
    assert len(result.findings) == 1
    assert result.findings[0]["attempt_date"] == "07-26"


# -- CheckResult coverage (B3a) -----------------------------------------------


def test_stale_verdict_reports_zero_coverage_without_date_year(conn, tmp_path):
    _git(tmp_path, "init", "-q")
    _commit_file(tmp_path, "steps/16-a.py", "print(1)\n", "2026-07-15T12:00:00+00:00")
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-01 | volatility test (16) | x | y | ✅ current |
| 2 | 07-02 | another attempt | x | y | ✅ current |
"""
    config = _config(steps_dir="steps")  # no date_year -- both dates unparseable
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_stale_verdicts(conn, tmp_path, config)
    assert result.total == 2
    assert result.checked == 0
    assert result.findings == []
    assert result.items_skipped_reason == "unparseable date"


def test_stale_verdict_partial_coverage_reported(conn, tmp_path):
    _git(tmp_path, "init", "-q")
    _commit_file(tmp_path, "steps/16-a.py", "print(1)\n", "2026-07-15T12:00:00+00:00")
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | fully parseable date (16) | x | y | ✅ current |
| 2 | 07-02 | needs date_year but none configured | x | y | ✅ current |
"""
    config = _config(steps_dir="steps")  # no date_year -- row 2 unparseable, row 1 fine
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_stale_verdicts(conn, tmp_path, config)
    assert result.total == 2
    assert result.checked == 1
    assert result.items_skipped_reason == "unparseable date"


def test_broken_references_and_revived_variables_report_full_coverage(conn, tmp_path):
    """The other two checks have no per-item skip mechanism -- checked
    always equals total (B3a: coverage fields must be honest across all
    three checks, not just the one with unparseable dates)."""
    (tmp_path / "steps").mkdir()
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | volatility test (16) | x | y | ✅ current |
"""
    config = _config(steps_dir="steps", dead_variables=["信息熵"], active_verdicts=["✅"])
    _ingest(conn, tmp_path, table, config)

    broken = consistency.check_broken_references(conn, tmp_path, config)
    assert broken.total == 1 and broken.checked == 1

    revived = consistency.check_revived_dead_variables(conn, config)
    assert revived.total == 1 and revived.checked == 1


def test_cli_reports_zero_coverage_honestly_not_as_ok(tmp_path, capsys):
    """B3: a check that examined zero attempts must never render as a plain
    'OK: no issues found' -- the exact failure mode a real 23-row table
    with no date_year hit (23/23 skipped, reported as a clean bill of
    health)."""
    project = tmp_path / "proj"
    _init_and_write_config(project, steps_dir="steps")
    (project / "steps").mkdir()
    (project / "steps" / "16-a.py").write_text("")
    (project / "map.md").write_text(f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-01 | volatility test (16) | x | y | ✅ current |
""")
    assert cli.main(["attempts", "--path", str(project), "--check"]) == 0
    out = capsys.readouterr().out
    assert "0/1 attempts checked" in out
    assert "coverage is zero" in out
    assert "OK" not in out.split("[stale_verdicts]")[1].split("\n")[0]


# -- attempts_for_file / findings ordering (item 8) ---------------------------


def test_attempts_for_file_and_findings_sorted_by_attempt_number(conn, tmp_path):
    """`--check`'s findings must come out in the timeline's own `#` order
    (natural sort: "14a" then "15", not string order), the same order the
    plain listing uses -- required for `--check` to be diffable in CI."""
    (tmp_path / "steps").mkdir()
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 14a | 2026-07-01 | x (99) | x | y | ✅ current |
| 2 | 2026-07-01 | y (98) | x | y | ✅ current |
| 15 | 2026-07-01 | z (97) | x | y | ✅ current |
"""
    config = _config(steps_dir="steps")
    _ingest(conn, tmp_path, table, config)
    nodes = consistency.attempts_for_file(conn, "map.md")
    assert [n["attrs"]["number"] for n in nodes] == ["2", "14a", "15"]

    result = consistency.check_broken_references(conn, tmp_path, config)
    assert [f["attempt"] for f in result.findings] == [
        "attempt:map.md#2", "attempt:map.md#14a", "attempt:map.md#15",
    ]


# -- check 3: revived dead variables -----------------------------------------


def test_revived_dead_variable_flagged_for_active_verdict(conn, tmp_path):
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | reuses 信息熵 again | x | y | ✅ current |
"""
    config = _config(dead_variables=["信息熵"], active_verdicts=["✅"])
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_revived_dead_variables(conn, config)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding["dead_variable"] == "信息熵"
    assert finding["field"] == "description"
    assert "信息熵" in finding["excerpt"]


def test_revived_dead_variable_not_flagged_for_inactive_verdict(conn, tmp_path):
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | reuses 信息熵 again | x | y | ☠️ dead |
"""
    config = _config(dead_variables=["信息熵"], active_verdicts=["✅"])
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_revived_dead_variables(conn, config)
    assert result.findings == []


def test_revived_dead_variable_case_insensitive_substring_match(conn, tmp_path):
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | x | LnRate configured ratio again | y | ✅ current |
"""
    config = _config(dead_variables=["lnrate configured ratio"], active_verdicts=["✅"])
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_revived_dead_variables(conn, config)
    assert len(result.findings) == 1
    assert result.findings[0]["field"] == "variables"


def test_revived_dead_variable_skipped_without_dead_variables_config(conn, tmp_path):
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | x | y | z | ✅ current |
"""
    config = _config(dead_variables=None, active_verdicts=["✅"])
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_revived_dead_variables(conn, config)
    assert result.skipped
    assert "dead_variables" in result.skip_reason


def test_revived_dead_variable_skipped_without_active_verdicts_config(conn, tmp_path):
    table = f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | x | y | z | ✅ current |
"""
    config = _config(dead_variables=["信息熵"], active_verdicts=None)
    _ingest(conn, tmp_path, table, config)
    result = consistency.check_revived_dead_variables(conn, config)
    assert result.skipped
    assert "active_verdicts" in result.skip_reason


# -- CLI: `rce attempts` / `--check` ------------------------------------------


def _init_and_write_config(
    project: Path, steps_dir=None, dead_variables=None, active_verdicts=None, date_year=None,
) -> None:
    project.mkdir(parents=True, exist_ok=True)
    assert cli.main(["init", str(project)]) == 0
    rce_dir = project / ".rce"
    lines = ['file = "map.md"', f'heading = "{HEADING}"']
    if steps_dir:
        lines.append(f'steps_dir = "{steps_dir}"')
    if dead_variables is not None:
        lines.append(f"dead_variables = {dead_variables!r}")
    if active_verdicts is not None:
        lines.append(f"active_verdicts = {active_verdicts!r}")
    if date_year is not None:
        lines.append(f"date_year = {date_year!r}")
    # All the keys above must precede [columns] (B1: TOML nests a bare key
    # written after a [table] header into that table).
    lines.append("[columns]")
    for key, header in COLUMNS.items():
        lines.append(f'{key} = "{header}"')
    (rce_dir / "attempts.toml").write_text("\n".join(lines) + "\n")


def test_cli_attempts_check_exits_nonzero_when_issues_found(tmp_path, capsys):
    project = tmp_path / "proj"
    _init_and_write_config(project, dead_variables=["信息熵"], active_verdicts=["✅"])
    (project / "map.md").write_text(f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | reuses 信息熵 | x | y | ✅ current |
""")
    assert cli.main(["attempts", "--path", str(project), "--check"]) == 1
    out = capsys.readouterr().out
    assert "revived_dead_variables" in out.replace(" ", "_")


def test_cli_attempts_check_exits_zero_when_clean(tmp_path, capsys):
    project = tmp_path / "proj"
    _init_and_write_config(project, dead_variables=["信息熵"], active_verdicts=["✅"])
    (project / "map.md").write_text(f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | clean attempt | x | y | ✅ current |
""")
    assert cli.main(["attempts", "--path", str(project), "--check"]) == 0


def test_cli_attempts_default_lists_registered_attempts(tmp_path, capsys):
    project = tmp_path / "proj"
    _init_and_write_config(project)
    (project / "map.md").write_text(f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | some attempt | x | y | ✅ current |
""")
    assert cli.main(["attempts", "--path", str(project)]) == 0
    out = capsys.readouterr().out
    assert "#1" in out
    assert "2026-07-01" in out
    assert "current" in out


def test_cli_attempts_accepts_positional_path_like_init_and_ingest(tmp_path, capsys):
    """Item 10: `rce attempts <path>` (no --path) must work the same as
    `rce init <path>`/`rce ingest <path>` -- previously only --path was
    accepted, inconsistent with every other subcommand that takes a path."""
    project = tmp_path / "proj"
    _init_and_write_config(project)
    (project / "map.md").write_text(f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-07-01 | some attempt | x | y | ✅ current |
""")
    assert cli.main(["attempts", str(project)]) == 0
    out = capsys.readouterr().out
    assert "#1" in out


def test_cli_attempts_rejects_both_positional_and_flag_path(tmp_path, capsys):
    project = tmp_path / "proj"
    _init_and_write_config(project)
    (project / "map.md").write_text(f"## {HEADING}\n\n| # | Date | Path | Variables | Result | Verdict |\n|---|---|---|---|---|---|\n")
    # cli.main() catches CliError itself (see main()'s try/except) and turns
    # it into "Error: ..." on stderr with exit code 1 -- it never propagates.
    assert cli.main(["attempts", str(project), "--path", str(project)]) == 1
    assert "either positionally or via --path" in capsys.readouterr().err


# -- B2: full path through TOML for all three checks' config items -----------


def test_full_toml_path_wires_all_three_checks_configs(tmp_path):
    """B2 regression: every other test in this suite constructs
    `AttemptsConfig` directly in Python, so a config-shape bug (like B1's
    SAMPLE_CONFIG key placement) never showed up in any test. This one goes
    through a real `.rce/attempts.toml` file end to end -- `load_config`
    reads it, and all three checks' gating config items (steps_dir,
    dead_variables, active_verdicts, date_year) are exercised via that
    loaded config, not a hand-built dataclass."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "steps").mkdir()
    (project / "steps" / "16-a.py").write_text("")
    rce_dir = project / ".rce"
    rce_dir.mkdir()
    (rce_dir / "attempts.toml").write_text(
        'file = "map.md"\n'
        f'heading = "{HEADING}"\n'
        'steps_dir = "steps"\n'
        'dead_variables = ["信息熵"]\n'
        'active_verdicts = ["✅"]\n'
        "date_year = 2026\n"
        "[columns]\n"
        + "\n".join(f'{k} = "{v}"' for k, v in COLUMNS.items())
        + "\n"
    )
    (project / "map.md").write_text(f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-01 | reuses 信息熵, references (16-18) | x | y | ✅ current |
""")
    config = attempts_ingest.load_config(project)
    assert config.steps_dir == "steps"
    assert config.dead_variables == ["信息熵"]
    assert config.active_verdicts == ["✅"]
    assert config.date_year == 2026

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        attempts_ingest.ingest_attempts_repo(conn, project, config)
        results = {r.name: r for r in consistency.run_checks(conn, project, config)}

        # check 1 (steps_dir via TOML): step 17/18 genuinely missing.
        assert not results["broken_references"].skipped
        assert {f["missing_step"] for f in results["broken_references"].findings} == {17, 18}

        # check 2 (steps_dir + date_year via TOML): "07-01" only parses
        # because date_year was read from the TOML file.
        assert not results["stale_verdicts"].skipped
        assert results["stale_verdicts"].checked == 1

        # check 3 (dead_variables + active_verdicts via TOML).
        assert not results["revived_dead_variables"].skipped
        assert len(results["revived_dead_variables"].findings) == 1
        assert results["revived_dead_variables"].findings[0]["dead_variable"] == "信息熵"
    finally:
        conn.close()


# -- item 9: -v/--verbose global flag -----------------------------------------


def test_verbose_flag_makes_info_diagnostics_visible(tmp_path):
    """Without -v, INFO-level skip/orphan diagnostics are invisible in
    normal use (main() never called logging.basicConfig); -v turns them on.
    Run as a real subprocess so this observes actual process behavior
    rather than pytest's own logging capture machinery."""
    project = tmp_path / "proj"
    _init_and_write_config(project, steps_dir="steps")
    (project / "steps").mkdir()
    (project / "map.md").write_text(f"""## {HEADING}

| # | Date | Path | Variables | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 07-01 | volatility test | x | y | ✅ current |
""")

    quiet = subprocess.run(
        [sys.executable, "-m", "rce.cli", "attempts", "--path", str(project), "--check"],
        capture_output=True, text=True,
    )
    assert "not parseable" not in quiet.stderr

    verbose = subprocess.run(
        [sys.executable, "-m", "rce.cli", "-v", "attempts", "--path", str(project), "--check"],
        capture_output=True, text=True,
    )
    assert "not parseable" in verbose.stderr
