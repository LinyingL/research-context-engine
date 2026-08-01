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
# "MM-DD", optionally followed by "~DD" (same-month range) or "~MM-DD"
# (cross-month range); trailing free text (a Chinese annotation like
# " 冻结") is allowed and ignored, hence the lookahead instead of a full
# end-of-string anchor. Any "<=" / "≤" upper-bound prefix is stripped by
# the caller before this pattern is tried -- it is not part of this regex.
_MONTH_DAY_RANGE_RE = re_compile(
    r"^(?P<m1>\d{1,2})-(?P<d1>\d{1,2})(?:~(?:(?P<m2>\d{1,2})-)?(?P<d2>\d{1,2}))?(?=\s|$)"
)

# extractor tag for the one edge this module writes (attempt --uses--> commit).
_EXTRACTOR = "attempts_consistency"


@dataclass
class CheckResult:
    """One check's outcome: either `skipped` with a reason, or a (possibly
    empty) list of finding dicts. An empty `findings` on a non-skipped check
    is a real "checked, nothing wrong" -- never conflated with `skipped`.

    `total`/`checked` are the check's own coverage: how many `attempt`
    nodes it considered, and how many of those it actually evaluated (the
    rest skipped for a per-item reason -- currently only
    `check_stale_verdicts`, whose date column may not parse; the other two
    checks always have `checked == total`). Coverage below 100% -- and
    especially `checked == 0` -- must never be allowed to look like "OK, no
    issues found": zero attempts actually examined is not a clean bill of
    health, it is an unmeasured one, and the CLI report (`rce.cli.
    _print_consistency_report`) is required to say so explicitly rather
    than only printing `findings`."""

    name: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None
    total: int = 0
    checked: int = 0
    items_skipped_reason: str | None = None


def attempts_for_file(conn: Connection, source_file: str) -> list[dict[str, Any]]:
    """`attempt` nodes ingested from one source Markdown file, in the id
    convention `attempt:<source_file>#<number>` (DESIGN.md section 4),
    sorted by `attempts_ingest.attempt_sort_key` (the same natural "#"
    order `rce attempts`'s plain listing uses). Shared by all three checks
    below and by that listing, so both a project's findings and its listing
    always walk attempts in the same stable order -- required for `--check`
    to be diffable in CI/pre-commit rather than following raw database read
    order."""
    prefix = f"attempt:{source_file}#"
    nodes = [n for n in db.get_nodes_by_type(conn, "attempt") if n["id"].startswith(prefix)]
    return sorted(nodes, key=lambda n: attempts_ingest.attempt_sort_key(n["attrs"].get("number", "")))


def _iso_date(raw: str) -> date | None:
    """Strict `YYYY-MM-DD` only -- `None` for anything else. Used for
    machine-recorded dates (a git commit's `authored_at`, a file's mtime)
    that are always already in this exact form; see `_parse_attempt_date`
    for the attempt timeline's own, much messier date column."""
    raw = raw.strip()
    if not _ISO_DATE_RE.match(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_attempt_date(raw: str, date_year: int | None) -> date | None:
    """Parse one attempt's own date cell into a definite `date`, or `None`
    if it cannot be parsed without guessing (DESIGN.md section 0).

    Accepted forms, in order:

    1. A full `YYYY-MM-DD` (`date.fromisoformat`) -- always accepted,
       regardless of `date_year`.
    2. Only when `.rce/attempts.toml` declares `date_year` (a human stating
       "this table's dates are all in year Y" -- rce never infers this):
         - `MM-DD`, e.g. `"07-26"` -- combined with `date_year`.
         - `<=MM-DD` / `≤MM-DD`, e.g. `"≤07-07"` -- an upper-bound
           prefix. The prefix is stripped and the date after it parsed
           as-is; it does not change *which* date is taken, only that the
           true date might be earlier than what's written. The
           stale-verdict check this feeds wants the latest date that is
           still guaranteed truthful, and an upper bound's own written
           value already is that.
         - `MM-DD~DD`, e.g. `"07-08~09"` -- a same-month day range. The
           LATER day is taken: this check asks "was the verdict made stale
           by a later edit", and a verdict is only safely dated once the
           attempt is actually finished, i.e. the end of the range, never
           its start.
         - `MM-DD~MM-DD`, e.g. a range spanning a month boundary -- same
           rule, the later end (both month and day) is taken.
         - Trailing free text after the date, e.g. `"07-10 冻结"`
           (a Chinese annotation), is ignored -- it is the researcher's own
           note, not part of the date.
    3. Anything else -- a bare `MM-DD` with `date_year` unset, unparseable
       junk, an unrecognized separator -- returns `None`. The caller
       (`check_stale_verdicts`) skips that one attempt for this check and
       counts it in the check's coverage figures (`CheckResult.total` vs.
       `.checked`); it is never guessed at, and a fully-skipped check must
       never be allowed to look like "OK, no issues found" (see
       `CheckResult`'s own docstring and `rce.cli._print_consistency_report`).
    """
    raw = raw.strip()
    if _ISO_DATE_RE.match(raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    if date_year is None:
        return None
    body = raw
    if body.startswith("≤"):
        body = body[1:].strip()
    elif body.startswith("<="):
        body = body[2:].strip()
    m = _MONTH_DAY_RANGE_RE.match(body)
    if not m:
        return None
    month, day = int(m.group("m1")), int(m.group("d1"))
    if m.group("d2"):
        if m.group("m2"):
            month = int(m.group("m2"))
        day = int(m.group("d2"))
    try:
        return date(date_year, month, day)
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
    nodes = attempts_for_file(conn, config.file)
    for node in nodes:
        for missing in node["attrs"].get("step_files_broken") or []:
            findings.append({
                "attempt": node["id"],
                "missing_step": missing,
                "neighbors": _nearest_neighbors(missing, available),
            })
    # No per-item skip in this check -- every attempt's step_files_broken is
    # examined, so checked always equals total.
    return CheckResult("broken_references", findings=findings, total=len(nodes), checked=len(nodes))


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
    nodes = attempts_for_file(conn, config.file)
    checked = 0
    for node in nodes:
        raw_date = node["attrs"].get("date", "")
        attempt_date = _parse_attempt_date(raw_date, config.date_year)
        if attempt_date is None:
            reason = (
                "not a plain YYYY-MM-DD date and no date_year configured to try looser forms"
                if config.date_year is None
                else "unparseable even with date_year configured"
            )
            logger.info(
                "%s: date %r is not parseable (%s) -- skipping the stale-verdict check for this "
                "attempt rather than guessing", node["id"], raw_date, reason,
            )
            continue
        checked += 1
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
    skipped_count = len(nodes) - checked
    return CheckResult(
        "stale_verdicts", findings=findings, total=len(nodes), checked=checked,
        items_skipped_reason="unparseable date" if skipped_count else None,
    )


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
    nodes = attempts_for_file(conn, config.file)
    for node in nodes:
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
    # No per-item skip in this check -- every attempt is evaluated (an
    # inactive verdict is a non-match, not a skip), so checked == total.
    return CheckResult(
        "revived_dead_variables", findings=findings, total=len(nodes), checked=len(nodes),
    )


def run_checks(
    conn: Connection, project_root: str | Path, config: attempts_ingest.AttemptsConfig
) -> list[CheckResult]:
    """All three checks, in a fixed order, for `rce attempts --check`."""
    return [
        check_broken_references(conn, project_root, config),
        check_stale_verdicts(conn, project_root, config),
        check_revived_dead_variables(conn, config),
    ]
