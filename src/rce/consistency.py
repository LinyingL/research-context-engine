"""Three deterministic consistency checks over an ingested attempt timeline
(DESIGN.md section 4, task A3): broken step references, stale verdicts, and
revived dead variables. No model is involved anywhere in this module --
same "code beats models wherever code suffices" principle as every
deterministic extractor (DESIGN.md section 0).

All three checks read `attempt` nodes already written by
`rce.ingest.attempts.ingest_attempts_repo` -- they do not re-parse the
source Markdown themselves. `rce attempts --check` always re-ingests first
(see rce.cli.cmd_attempts), so the `attrs` these checks read are current as
of this run.

Each check is independently config-gated (DESIGN.md section 0, "never
guess"): a check whose prerequisite is not declared in `.rce/attempts.toml`
returns `skipped=True` with a stated reason instead of quietly reporting
"no findings", which would look identical to "checked, found nothing".

The stale-verdict check is the one check with a side effect: resolving a
script's last-touch commit is itself a deterministic fact worth keeping, so
it upserts `attempt --uses--> commit` (migration 0002's edge type) along the
way. The other two checks are pure reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from re import compile as re_compile
from sqlite3 import Connection
from typing import Any

from rce import db
from rce.ingest import attempts as attempts_ingest
from rce.ingest import git as git_ingest

logger = logging.getLogger(__name__)

_ISO_DATE_RE = re_compile(r"^\d{4}-\d{2}-\d{2}$")

# extractor tag for the one edge this module writes (attempt --uses--> commit).
_EXTRACTOR = "attempts_consistency"


@dataclass
class CheckResult:
    """One check's outcome: either `skipped` with a reason, or a (possibly
    empty) list of finding dicts. An empty `findings` on a non-skipped check
    is a real "checked, nothing wrong" -- never conflated with `skipped`."""

    name: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None


def attempts_for_file(conn: Connection, source_file: str) -> list[dict[str, Any]]:
    """`attempt` nodes ingested from one source Markdown file, in the id
    convention `attempt:<source_file>#<number>` (DESIGN.md section 4).
    Shared by all three checks below and by `rce attempts`'s plain listing
    (rce.cli), so both read the same set of nodes."""
    prefix = f"attempt:{source_file}#"
    return [n for n in db.get_nodes_by_type(conn, "attempt") if n["id"].startswith(prefix)]


def _iso_date(raw: str) -> date | None:
    """Strict `YYYY-MM-DD` only -- `None` for anything else (a date range
    like "07-08~09", a bare "07-07" with no year, a "≤07-07" upper bound).

    Known limitation (mirrors DESIGN.md section 5's connector-7 callouts):
    a real hand-written attempt timeline's date column is rarely this
    clean. Inferring the missing year, or picking a bound of a range, would
    be exactly the fabrication DESIGN.md section 0 forbids -- so an
    attempt whose date does not parse this strictly is skipped from the
    stale-verdict check individually (logged, never counted as "not
    stale"), rather than guessed at.
    """
    raw = raw.strip()
    if not _ISO_DATE_RE.match(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _nearest_neighbors(missing: int, available: list[int]) -> dict[str, int | None]:
    lower = max((n for n in available if n < missing), default=None)
    upper = min((n for n in available if n > missing), default=None)
    return {"prev_available": lower, "next_available": upper}


def check_broken_references(
    conn: Connection, project_root: str | Path, config: attempts_ingest.AttemptsConfig
) -> CheckResult:
    """Attempts whose referenced step number has no matching file under
    `steps_dir` (already resolved into `attrs["step_files_broken"]` by
    ingest -- see rce.ingest.attempts._resolve_step_files). Each finding
    also reports the nearest step numbers that DO exist in the directory,
    so a human can tell whether a step was renamed or actually deleted.
    """
    if not config.steps_dir:
        return CheckResult(
            "broken_references", skipped=True,
            skip_reason="steps_dir not configured in .rce/attempts.toml -- nothing to check references against",
        )
    available = attempts_ingest.available_step_numbers(Path(project_root) / config.steps_dir)
    findings: list[dict[str, Any]] = []
    for node in attempts_for_file(conn, config.file):
        for missing in node["attrs"].get("step_files_broken") or []:
            findings.append({
                "attempt": node["id"],
                "missing_step": missing,
                "neighbors": _nearest_neighbors(missing, available),
            })
    return CheckResult("broken_references", findings=findings)


def _ensure_commit_node(conn: Connection, commit: git_ingest.GitCommit) -> str:
    """Idempotently upsert the minimal Commit node `rce.ingest.git.
    ingest_git_repo` would itself write for this commit, so `uses` edges
    below always have a valid destination even when `rce ingest` (which
    ingests the *whole* git history) has never been run -- this check only
    ever touches the one commit it just resolved, never the rest of the
    log."""
    commit_id = f"commit:{commit.sha}"
    subject = commit.message.splitlines()[0] if commit.message else ""
    db.upsert_node(
        conn, commit_id, "commit", title=subject,
        attrs={
            "message": commit.message, "authored_at": commit.authored_at,
            "author_name": commit.author_name, "author_email": commit.author_email,
            "files": list(commit.files),
        },
    )
    return commit_id


def check_stale_verdicts(
    conn: Connection, project_root: str | Path, config: attempts_ingest.AttemptsConfig
) -> CheckResult:
    """Attempts whose recorded date is earlier than the last time one of
    their resolved dependency scripts (`attrs["step_files"]`) was touched --
    "the verdict was written, then the code changed again".

    A script's last-touch time comes from `rce.ingest.git.read_commits`
    (reused, not reimplemented): the most recent commit whose changed-file
    list includes that script's repo-relative path. A script never seen in
    git history (untracked, or the project has no git repo at all) falls
    back to the file's own mtime -- logged and marked `basis="mtime"` in
    the finding, never silently treated the same as a git-backed answer.
    Every git-resolved script also gets an `attempt --uses--> commit` edge
    (migration 0002), evidence = {"script", "commit_time"} -- a mtime
    fallback has no commit to point at, so no edge is written for it.
    """
    if not config.steps_dir:
        return CheckResult(
            "stale_verdicts", skipped=True,
            skip_reason="steps_dir not configured in .rce/attempts.toml -- no dependency scripts to check",
        )
    project_root = Path(project_root)
    try:
        commits = git_ingest.read_commits(project_root)
    except git_ingest.GitIngestError as exc:
        logger.warning(
            "no usable git history at %s (%s) -- every script falls back to file mtime", project_root, exc,
        )
        commits = []

    # Oldest-first (read_commits' own order), so the last write per path
    # wins and ends up the most recent commit that touched it.
    last_touch: dict[str, git_ingest.GitCommit] = {}
    for commit in commits:
        for touched in commit.files:
            last_touch[touched] = commit

    findings: list[dict[str, Any]] = []
    for node in attempts_for_file(conn, config.file):
        raw_date = node["attrs"].get("date", "")
        attempt_date = _iso_date(raw_date)
        if attempt_date is None:
            logger.info(
                "%s: date %r is not a plain YYYY-MM-DD date -- skipping the stale-verdict check "
                "for this attempt rather than guessing at a range or a missing year", node["id"], raw_date,
            )
            continue
        for filename in node["attrs"].get("step_files") or []:
            rel_path = f"{config.steps_dir}/{filename}"
            commit = last_touch.get(rel_path)
            if commit is not None:
                script_date = _iso_date(commit.authored_at[:10])
                basis = "git"
                commit_id = _ensure_commit_node(conn, commit)
                db.upsert_edge(
                    conn, node["id"], commit_id, "uses", extractor=_EXTRACTOR,
                    evidence={"script": rel_path, "commit_time": commit.authored_at},
                    confidence=1.0, status="auto",
                )
            else:
                try:
                    mtime = (project_root / rel_path).stat().st_mtime
                except OSError as exc:
                    logger.warning("%s: cannot stat %s for staleness check -- skipped (%s)", node["id"], rel_path, exc)
                    continue
                script_date = datetime.fromtimestamp(mtime, tz=timezone.utc).date()
                basis = "mtime"
            if script_date is not None and attempt_date < script_date:
                findings.append({
                    "attempt": node["id"], "script": rel_path, "attempt_date": raw_date,
                    "script_last_touched": script_date.isoformat(), "basis": basis,
                })
    return CheckResult("stale_verdicts", findings=findings)


def check_revived_dead_variables(
    conn: Connection, config: attempts_ingest.AttemptsConfig
) -> CheckResult:
    """A *living* attempt (its verdict contains one of `active_verdicts`)
    whose description or variable list mentions a `dead_variables` entry.
    Matching is a case-insensitive substring test only -- deliberately
    conservative (DESIGN.md section 0) -- and every finding carries the
    actual matched field's text verbatim so a human judges it themselves;
    this check never acts on a hit, only reports it.
    """
    if config.dead_variables is None:
        return CheckResult(
            "revived_dead_variables", skipped=True,
            skip_reason="dead_variables not declared in .rce/attempts.toml",
        )
    if config.active_verdicts is None:
        return CheckResult(
            "revived_dead_variables", skipped=True,
            skip_reason="active_verdicts not declared in .rce/attempts.toml",
        )
    findings: list[dict[str, Any]] = []
    for node in attempts_for_file(conn, config.file):
        verdict = node["human_fields"].get("verdict") or ""
        if not any(marker in verdict for marker in config.active_verdicts):
            continue
        for field_name in ("description", "variables"):
            text = node["attrs"].get(field_name) or ""
            lowered = text.lower()
            for dead in config.dead_variables:
                if dead.lower() in lowered:
                    findings.append({
                        "attempt": node["id"], "dead_variable": dead,
                        "field": field_name, "excerpt": text,
                    })
    return CheckResult("revived_dead_variables", findings=findings)


def run_checks(
    conn: Connection, project_root: str | Path, config: attempts_ingest.AttemptsConfig
) -> list[CheckResult]:
    """All three checks, in a fixed order, for `rce attempts --check`."""
    return [
        check_broken_references(conn, project_root, config),
        check_stale_verdicts(conn, project_root, config),
        check_revived_dead_variables(conn, config),
    ]
