"""Tests for rce.consistency (task A3): the three deterministic checks over
an ingested attempt timeline, plus `rce attempts`'/`--check`'s CLI wiring.
"""

import os
import subprocess
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


def _config(steps_dir: str | None = None, dead_variables=None, active_verdicts=None) -> attempts_ingest.AttemptsConfig:
    return attempts_ingest.AttemptsConfig(
        file="map.md", heading=HEADING, columns=COLUMNS, steps_dir=steps_dir,
        dead_variables=dead_variables, active_verdicts=active_verdicts,
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


def _init_and_write_config(project: Path, steps_dir=None, dead_variables=None, active_verdicts=None) -> None:
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
