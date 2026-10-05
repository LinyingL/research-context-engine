"""RCE command-line interface (T4): `rce init` / `ingest` / `status` / `query` /
`trace` / `confirm` (F3) / `judge` (S2, optional semantic layer) / `lineage`
(W4, read-only four-block report over task W2's dataflow graph) / `serve`
(V1, local read-only web view over the graph -- see rce.webapp.server).

stdlib argparse only (DESIGN.md section 0, Occam rule 1). Orchestrates
the existing extractors (rce.ingest.git/latex/dataflow/pyfig/mlflow/wandb/
mdpaper/claims) and rce.db (section 7 Phase A order: git -> latex/.bib ->
dataflow (task W2, data lineage) -> pyfig -> mlflow -> wandb -> mdpaper
(task W3, Markdown paper support) -> claims); writes only via
db.upsert_node/upsert_edge, no new graph mutation logic here. mdpaper runs
after mlflow/wandb (same experiment-nodes-must-exist-first requirement the
tex claims step has) and before the tex claims step, since both need
section/experiment state already in place before generating their own
backed_by candidates.

W1: `cmd_ingest` catches `git_ingest.NotAGitRepositoryError` specifically
(a project root that is not a git repository at all -- the common case for
a researcher's working directory that was never `git init`ed) and degrades
rather than aborting: it prints one explanatory line, skips commit/
contributor ingestion (nothing to read), and gets the file inventory from
`rce.ingest.files.list_source_files` (a plain filesystem walk) instead of
`git_ingest.list_source_files`. Every other extractor still runs. A git
repository's behavior is unchanged -- this only branches on the specific
"not a repository" failure, not `GitIngestError` in general, which still
aborts the whole ingest as before (missing git binary, permission error,
a corrupt repo, ...).
`trace` reuses rce.query.trace() directly -- multi-hop traversal logic lives
in exactly one place. `lineage` (W4) reuses rce.lineage.build_lineage_report()
the same way -- it is a pure read over edges `rce ingest` already wrote (task
W2's dataflow extractor), never a re-parse of source files and never a write
path; see rce.lineage's own module docstring for the four blocks it reports.

A project is "initialized" once its graph exists (`rce init`); every other
command requires that file and errors clearly if absent (no guessing, per
the constitution). Since DESIGN.md section 8.10 rule 1 the graph is NOT in
the project -- it lives at `~/.rce/graphs/<id>/graph.db`, out of reach of
iCloud/Dropbox file providers, and `rce.paths` is the one module that says
where. `_require_db` below resolves through `rce.paths.resolve_graph_db`,
which also performs the one-time migration of a legacy in-project
`.rce/graph.db` on first touch by any subcommand.

Positioning ruling 2026-07-22 (Owner): RCE is a local-first standalone tool;
MCP is one optional exit among several, not a requirement. Concretely: (1)
`rce.mcp_server` is imported lazily, only inside the `mcp` subcommand branch
of main() below, so every other subcommand works with the optional 'mcp'
extra uninstalled (see pyproject.toml); (2) `trace` exists here so multi-hop
provenance is a full CLI feature, not something only reachable through an AI
client's MCP tool calls.

V5 phase 5 (DESIGN.md 9.5, 9.8): `rce rebuild` (`rce.rebuild`: a fresh
index beside the current one, compared per link before the swap), `rce
records [--verify|--clean [--yes]]` (`rce.inventory`) and `rce migrate
[--list|--yes|--not-mine|--from DIR]` (`rce.migration`: pre-V5 judgments
moved into the record, an explicit act). `rce status` reports pre-V5
indexes waiting for the folder.

V5 phase 6 (DESIGN.md 9.12): every subcommand that addresses a project
takes it as a positional path or as `--path` (`add_project_path`; both at
once is refused); `rce project adopt|restore` answer a lost identity file;
`rce records --answer ... --missing IDS` binds the shrink answer to the
entries the question showed.

F3 (Blocker C): `status --pending`/`confirm` give the zero-dependency
baseline its own human-confirmation path -- previously the sole writer of
`edges.status` was the optional `mcp` extra's `rce_confirm_edge`,
contradicting DESIGN.md section 2 now that ingest writes real `pending`
edges. stdlib argparse only.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from rce import consistency, db, inventory, lineage, migration, paths, query
from rce import project as project_identity
from rce import rebuild as rebuild_mod
from rce.ingest import attempts as attempts_ingest
# `git_ingest` stays importable as `rce.cli.git_ingest` (tests patch it);
# the scan itself is `rce.ingest.pipeline`.
from rce.ingest import git as git_ingest  # noqa: F401
from rce.ingest import mappings as mappings_ingest
from rce.ingest import pipeline as ingest_pipeline
from rce.records import cards as variable_cards
from rce.records import implementation as card_implementation
from rce.records import files as record_files
from rce.records import judgements
from rce.records import ledger as ledger_mod
from rce.records import lock as records_lock
from rce.records import situation as records_situation
from rce.records import variables as variables_mod
# S2: `rce judge`, the optional semantic layer. Unlike rce.mcp_server
# (behind a lazy import because it needs the third-party 'mcp' extra),
# rce.semantic.{backend,judge} use only stdlib urllib -- importing them
# here eagerly costs zero-dependency installs nothing, so `judge` gets a
# normal top-level subparser like every other subcommand, not the `mcp`
# subcommand's pass-through special case.
from rce.semantic import backend as semantic_backend
from rce.semantic import judge as semantic_judge
# V1: `rce serve`, the local read-only web view (DESIGN.md section 7,
# "Later"). stdlib http.server only (see rce.webapp.server's module
# docstring) -- like rce.semantic above, and unlike rce.mcp_server, this
# needs no optional extra, so it gets a normal top-level subparser.
# V3 phase 1: rce.webapp.registry is the machine-managed project registry
# (~/.rce/projects.json) that lets `rce serve` resume the most recently
# served project when no path is given.
# V3 phase 4: rce.webapp.macapp generates the double-clickable RCE.app
# launcher bundle (`rce app`). Generation is pure stdlib file writing,
# platform-independent -- only cmd_app's *default* install location is
# macOS-gated -- so this too is a plain eager import.
from rce.webapp import canvas as canvas_mod
from rce.webapp import macapp
from rce.webapp import registry as project_registry
from rce.webapp import server as webapp_server

# Kept as module attributes (error messages and tests quote them), but the
# definitions live in rce.paths now -- no module computes the graph's
# location itself anymore (DESIGN.md section 8.10 rule 1).
RCE_DIRNAME = paths.RCE_DIRNAME
DB_FILENAME = paths.DB_FILENAME


class CliError(Exception):
    """User-facing error; caught once in main() -> "Error: <msg>" on stderr, exit 1."""


# -- one path convention (DESIGN.md 9.12, "The command line") --------------------
#
# Every subcommand that addresses a project takes it either as a positional
# path or as `--path` -- both spellings everywhere, and the help says so.
# Giving both is refused (it was already, for `attempts`); nothing that
# worked before stops working.

POSITIONAL_PATH_HELP = "project root (default: {default}); or give it as --path"
FLAG_PATH_HELP = "project root, the same as the positional path (give one or the other, not both)"


def add_project_path(parser: argparse.ArgumentParser, *, default: str | None = ".", help: str | None = None) -> None:
    """Add the positional `path` and `--path` to a subcommand. `default`
    is used when neither is given (None: the subcommand decides)."""
    shown = f"'{default}'" if default is not None else "see below"
    parser.add_argument("path", nargs="?", default=None, help=help or POSITIONAL_PATH_HELP.format(default=shown))
    parser.add_argument("--path", dest="path_flag", default=None, metavar="PATH", help=FLAG_PATH_HELP)
    parser.set_defaults(path_default=default)


def settle_project_path(positional: str | None, flag: str | None, default: str | None) -> str | None:
    """The one project path a command line gave (`add_project_path`)."""
    if positional is not None and flag is not None:
        raise CliError("give the project root either positionally or via --path, not both")
    if flag is not None:
        return flag
    return positional if positional is not None else default


def _settle_path(args: argparse.Namespace) -> None:
    """After parsing: `args.path` is the project path whichever way it was
    given; `args.path_flag` is consumed. `rce confirm --index N <path>`:
    with --index there is no src/dst/type/extractor, so the one positional
    given is the project path."""
    if not hasattr(args, "path_flag"):
        return
    positional = args.path
    if getattr(args, "func", None) is cmd_confirm and args.index is not None and positional is None:
        given = [v for v in (args.src, args.dst, args.type, args.extractor) if v is not None]
        if len(given) == 1 and args.src is not None:
            positional, args.src = args.src, None
    args.path = settle_project_path(positional, args.path_flag, args.path_default)
    args.path_flag = None


def _open(path_str: str, *, register: bool = False) -> project_identity.Opened:
    """THE identity check (DESIGN.md 9.4), first thing in every subcommand
    that names a project: before anything is written. A moved project is
    adopted, a missing index is built empty; a copy, a home that cannot be
    checked, a lost or unreadable identity stops here with the reason and
    the exact commands, and nothing is written (not even the registry)."""
    root = Path(path_str).resolve()
    if not root.is_dir():
        raise CliError(f"{root} is not a directory")
    try:
        return project_identity.open_project(root, register=register)
    except project_identity.ProjectBlocked as exc:
        raise CliError(str(exc)) from exc
    except project_identity.AnswerRefused as exc:
        raise CliError(str(exc)) from exc


def _resolve_project_root(path_str: str) -> Path:
    return _open(path_str).root


@contextmanager
def _write_guard(opened: project_identity.Opened, *, human: bool):
    """Hold the project lock and re-check identity for a write (9.4, 9.7):
    an index write (a scan) or, with `human=True`, a human record -- which
    a pre-V5 project refuses until it is migrated."""
    try:
        with records_situation.write_guard(opened.root, opened.project_id, human=human):
            yield
    except records_situation.WriteRefused as exc:
        raise CliError(str(exc)) from exc
    except records_lock.ProjectLockError as exc:
        raise CliError(f"could not take the project lock: {exc}") from exc


def _require_db(project_root: Path) -> Path:
    """The graph for `project_root`, or a clear CliError. Resolved through
    `rce.paths.resolve_graph_db` -- which is `graph_db_path` plus the
    one-time migration of a legacy in-project `.rce/graph.db` -- so this
    is one of the "first touch by any subcommand" sites section 8.10
    rule 1 names. A migration that could not be verified is fatal for the
    run: the legacy graph is still there and untouched, and continuing
    would mean silently building a second, empty one."""
    try:
        path = paths.resolve_graph_db(project_root)
    except paths.GraphMigrationError as exc:
        raise CliError(str(exc)) from exc
    if not path.exists():
        raise CliError(
            f"no RCE project at {project_root} (missing its graph at {path}); "
            f"run 'rce init {project_root}' first"
        )
    return path


def _format_counts(counts: dict[str, int]) -> str:
    return " ".join(f"{k}={v}" for k, v in counts.items())


def _print_graph_counts(conn: Connection) -> None:
    """Whole-graph counts by type, shared by `status` and `ingest`'s closing
    summary -- via db.get_nodes_by_type/query_edges/pending_edges only, no
    raw SQL here (db.py's module contract)."""
    node_counts = {t: len(db.get_nodes_by_type(conn, t)) for t in sorted(db.NODE_TYPES)}
    edge_counts = {t: 0 for t in sorted(db.EDGE_TYPES)}
    for edge in db.query_edges(conn):
        edge_counts[edge["type"]] += 1
    print(f"  Nodes: {_format_counts(node_counts)}")
    print(f"  Edges: {_format_counts(edge_counts)}")
    print(f"  Pending confirmation queue: {len(db.pending_edges(conn))}")
    waiting = judgements.review_count(conn)
    if waiting:
        # 9.6: old judgments waiting for the researcher, never in the queue above.
        print(f"  Judgments under review: {waiting} (see 'rce review')")


def _ordered_edges(edges: list[dict]) -> list[dict]:
    """Deterministic order shared by `status --pending` and `confirm
    --index`, so the Nth edge one prints is the Nth edge the other resolves."""
    return sorted(edges, key=lambda e: (e["src"], e["dst"], e["type"], e["extractor"]))


def _format_semantic_review_suffix(evidence: dict[str, Any]) -> str:
    """Second line for a pending edge that already carries a `semantic_review`
    annotation (written by `rce judge`, S2) -- lets a human skim the queue
    and see which candidates a model already flagged as likely coincidental,
    without opening `rce query` on each one. `[FLAGGED]` on `related=False`
    is the "see this one first" signal the task asked for; it is purely
    display -- the edge's `status` is untouched by judge and stays whatever
    it already was (pending, per the constitution).

    Includes `metric=` (bug fix), omitted entirely for an older edge whose
    `semantic_review` predates this attribution field rather than printing
    a misleading `metric=None`: a `backed_by` edge can carry several
    (experiment, metric) candidate pairs (see `candidate_count`), but
    `rce.semantic.judge` only ever reviews one occurrence per edge and
    records which one in `semantic_review["metric"]`/`["metric_value"]`
    (see rce.semantic.judge.review_pending_backed_by). Without printing it
    here, a human reading this line had no way to tell which of possibly
    several matched metrics the model's `related`/`reason` verdict was
    actually about.
    """
    review = evidence.get("semantic_review") if isinstance(evidence, dict) else None
    if not isinstance(review, dict):
        return ""
    flag = "[FLAGGED: model says likely unrelated] " if review.get("related") is False else ""
    metric_note = f"metric={review['metric']!r} " if "metric" in review else ""
    better = review.get("better_match")
    better_note = f" better_match={better!r}" if better else ""
    return (
        f"\n      semantic_review: {flag}{metric_note}"
        f"related={review.get('related')!r} reason={review.get('reason')!r}"
        f"{better_note} (model={review.get('model')!r})"
    )


def _display_line(location: dict[str, Any] | None) -> int | None:
    """Pull the plain `line` int out of `query.claim_source_location`'s
    `{"file", "line"}` result, for `_format_occurrence`'s `display_line`
    parameter (which only ever wants the int, not the file)."""
    return location["line"] if location else None


def _format_pending_line(index: int, edge: dict, conn: Connection) -> str:
    location = query.claim_source_location(conn, edge["src"]) if edge["type"] == "backed_by" else None
    return (
        f"  [{index}] {edge['src']} --{edge['type']}--> {edge['dst']} "
        f"extractor={edge['extractor']} confidence={edge['confidence']:.2f} "
        f"evidence={_format_evidence_summary(edge['evidence'], display_line=_display_line(location))}"
        f"{_format_semantic_review_suffix(edge['evidence'])}"
    )


def _print_pending_queue(conn: Connection, limit: int | None) -> None:
    """Every pending edge, detailed enough to act on via `confirm`. `limit`
    (no invented default -- unset prints all) truncates display only, and
    only ever with an explicit notice, never silently."""
    queue = _ordered_edges(db.pending_edges(conn))
    print(f"Pending confirmation queue ({len(queue)}):")
    if not queue:
        print("  (empty)")
        return
    shown = queue if limit is None else queue[:limit]
    for i, edge in enumerate(shown, start=1):
        print(_format_pending_line(i, edge, conn))
    if limit is not None and len(queue) > limit:
        print(
            f"  ... truncated: showing {limit} of {len(queue)} pending edge(s) -- pass a larger "
            f"--limit to see more. Indices are only valid for this run's own listing."
        )


def cmd_init(args: argparse.Namespace) -> int:
    """Create the project's identity, `.rce/project.toml` (DESIGN.md 9.4,
    created exclusively, never overwritten), and its index under the id
    at `~/.rce/graphs/<id>/` -- outside the project (8.10 rule 1) -- plus
    the one-line `.rce/README` signpost, and print where the index is.

    Idempotent on a project that already has an id. A folder in a
    situation that must be answered first (a copy, ...) is refused with
    the commands that answer it; a pre-V5 project is refused too: its
    judgments sit in the old index, and giving it an id is what `rce
    migrate` does once they have been moved into the record (9.5)."""
    project_root = Path(args.path).resolve()
    if not project_root.is_dir():
        raise CliError(f"{project_root} is not a directory")
    try:
        result = project_identity.init_project(project_root)
    except project_identity.ProjectBlocked as exc:
        raise CliError(str(exc)) from exc
    except (project_identity.AnswerRefused, records_situation.WriteRefused) as exc:
        raise CliError(str(exc)) from exc
    node = project_identity.project_node_id(result.identity.id)
    print(f"Initialized RCE project at {project_root} (project node: {node})")
    if result.created_identity:
        print(f"Project id: {result.identity.id} (written to {RCE_DIRNAME}/project.toml)")
    print(f"Graph: {result.db_path}")
    if result.applied:
        print(f"Applied migrations: {result.applied}")
    # Still a nudge only -- RCE never edits the user's own files (DESIGN.md
    # section 2, "零习惯改变"): `.rce/` holds only the project's identity
    # and files the researcher owns, so whether it goes into git is theirs.
    print(
        f"Note: '{RCE_DIRNAME}/' in your project now holds only your own files "
        f"(project.toml, judgements.toml, canvas.json, attempts.toml, mappings.toml, backups/) -- "
        f"commit or .gitignore it as you "
        f"prefer. The derived graph is outside the project; see {result.readme}."
    )
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Scan every source into the index (`rce.ingest.pipeline`), under the
    project lock with the identity re-checked (9.7, 9.4): a scan is an
    index write like any other."""
    opened = _open(args.path)
    project_root = opened.root
    db_path = _require_db(project_root)
    with _write_guard(opened, human=False):
        conn = db.connect(db_path)
        try:
            print(f"Ingesting {project_root}")
            try:
                skipped = ingest_pipeline.ingest_sources(
                    conn, project_root, mlruns=args.mlruns, wandb=args.wandb, echo=print,
                )
            except ingest_pipeline.IngestFailed as exc:
                raise CliError(str(exc)) from exc
            print("Ingest summary (whole graph):")
            _print_graph_counts(conn)
            print(f"  Skipped/unresolved during this run (see logs): {skipped}")
        finally:
            conn.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project_root = _resolve_project_root(args.path)
    db_path = _require_db(project_root)
    conn = db.connect(db_path)
    try:
        print(f"Project: {project_root}")
        # Section 8.10 rule 1: "`rce status` and `/api/summary` report the
        # graph's actual location so nothing is hidden" -- the graph is no
        # longer where a user would think to look for it.
        print(f"Graph: {db_path}")
        _print_graph_counts(conn)
        _print_migration_waiting(project_root)
        if args.pending:  # purely additive -- omitting it reproduces the prior output exactly
            _print_pending_queue(conn, args.limit)
    finally:
        conn.close()
    return 0


def _print_migration_waiting(project_root: Path) -> None:
    """9.5 "What is looked for": checked on every open, with or without an
    id -- pre-V5 indexes that may hold this folder's judgments."""
    waiting = migration.waiting_payload(project_root)
    if waiting["migrating_from"]:
        print(f"  Migration from {waiting['migrating_from']} not finished: human records are read-only until 'rce migrate' resumes it")
    elif waiting["waiting"]:
        where = ", ".join(w["db_path"] for w in waiting["waiting"])
        print(f"  Legacy records waiting: a pre-V5 index may hold this project's judgments ({where}); see 'rce migrate'")


def _print_attempts_listing(conn: Connection, config: attempts_ingest.AttemptsConfig) -> None:
    """`rce attempts` with no --check: what's registered, not a judgement --
    editor/date/verdict/link count, in the attempt's own `#` order
    (`rce.consistency.attempts_for_file` already sorts by
    `rce.ingest.attempts.attempt_sort_key`, the same order `--check`'s
    findings now use, so both walk a project's attempts identically)."""
    nodes = consistency.attempts_for_file(conn, config.file)
    print(f"Registered attempts in {config.file} ({len(nodes)}):")
    for node in nodes:
        number = node["attrs"].get("number", "?")
        raw_date = node["attrs"].get("date", "")
        verdict = node["human_fields"].get("verdict", "")
        step_files = len(node["attrs"].get("step_files") or [])
        print(f"  #{number:<5} {raw_date:<14} {verdict:<24} linked_step_files={step_files}")


def _format_finding(check_name: str, finding: dict[str, Any]) -> str:
    """One finding line per check type (task A3) -- each check's dict shape
    is its own, so this is a dispatch, not a generic key=value dump."""
    if check_name == "broken_references":
        neighbors = finding["neighbors"]
        return (
            f"{finding['attempt']}: references step {finding['missing_step']}, no matching file -- "
            f"nearest existing steps: prev={neighbors['prev_available']} next={neighbors['next_available']}"
        )
    if check_name == "stale_verdicts":
        return (
            f"{finding['attempt']}: verdict dated {finding['attempt_date']!r} but {finding['script']} "
            f"last touched {finding['script_last_touched']} (basis={finding['basis']})"
        )
    if check_name == "revived_dead_variables":
        return (
            f"{finding['attempt']}: dead variable {finding['dead_variable']!r} found in "
            f"{finding['field']}: {finding['excerpt']!r}"
        )
    return str(finding)  # defensive only -- every real check name is handled above


def _format_coverage(result: consistency.CheckResult) -> str:
    """`N/M attempts checked` plus, when some were skipped, why -- shared by
    every non-skipped branch of `_print_consistency_report` so the coverage
    figure is never left out of a clean-looking report (B3: a check that
    examined zero attempts must never read the same as a check that
    examined all of them and found nothing)."""
    note = f"{result.checked}/{result.total} attempts checked"
    skipped_count = result.total - result.checked
    if skipped_count:
        note += f" ({skipped_count} skipped: {result.items_skipped_reason})"
    return note


def _print_consistency_report(results: list[consistency.CheckResult]) -> None:
    print("Consistency checks:")
    for result in results:
        if result.skipped:
            print(f"  [{result.name}] skipped: {result.skip_reason}")
            continue
        if result.total == 0:
            # Nothing to check at all (e.g. an empty attempt timeline) --
            # a genuinely different case from "checked some/none of N and
            # found nothing", so this is the one case still allowed to say
            # a plain OK.
            print(f"  [{result.name}] OK: no attempts to check")
            continue
        coverage = _format_coverage(result)
        if not result.findings:
            if result.checked == 0:
                print(f"  [{result.name}] {coverage} -- coverage is zero, this is NOT a clean bill of health")
            elif result.checked < result.total:
                print(f"  [{result.name}] {coverage}, no issues found among those checked")
            else:
                print(f"  [{result.name}] OK: no issues found ({coverage})")
            continue
        print(f"  [{result.name}] {coverage}, {len(result.findings)} issue(s):")
        for finding in result.findings:
            print(f"    - {_format_finding(result.name, finding)}")


def _resolve_attempts_path(args: argparse.Namespace) -> str:
    """`rce attempts` accepts the project root either positionally (like
    `init`/`ingest`) or via `--path` (its own original convention, kept for
    compatibility) -- but not both at once."""
    if args.path is not None and args.path_flag is not None:
        raise CliError("give the project root either positionally or via --path, not both")
    if args.path_flag is not None:
        return args.path_flag
    if args.path is not None:
        return args.path
    return "."


def cmd_attempts(args: argparse.Namespace) -> int:
    """`rce attempts` (task A2 ingest, task A3 listing/`--check`):
    config-driven ingest of a hand-maintained attempt timeline (see
    rce.ingest.attempts), always run first so what follows reflects the
    current source file. Config-gated on purpose (DESIGN.md section 0,
    "never guess") -- with no .rce/attempts.toml this prints a
    copy-pasteable template and exits 1 instead of guessing which table in
    the project is the attempt timeline. The same clean-message-plus-exit-1
    handling also covers every other way `.rce/attempts.toml` can drift out
    of sync with the project: a configured `[columns]` name no longer
    present in the table's actual header (e.g. a renamed column), or the
    configured heading/table no longer locatable at all
    (`attempts_ingest.AttemptsTableNotFoundError`, DESIGN.md's "Attempt
    orphans" section) -- both are raised as `AttemptsConfigError` (the
    latter via that subclass) from inside `ingest_attempts_repo`, not just
    from `load_config`, so both must be caught here too; letting either
    propagate out of this function would print a raw Python traceback
    instead of the clean message this docstring promises.

    Without `--check`: a plain listing of what is registered (rce.consistency
    .attempts_for_file), no judgement involved. With `--check`: the three
    deterministic consistency checks (rce.consistency.run_checks), grouped
    in a report; exits 1 if any check reports a finding (skipped checks
    never affect the exit code -- a missing config declaration is not
    itself "a problem found"), so this composes into a caller's own CI/
    pre-commit pipeline.
    """
    opened = _open(_resolve_attempts_path(args))
    project_root = opened.root
    db_path = _require_db(project_root)
    with _write_guard(opened, human=False):
        conn = db.connect(db_path)
        try:
            try:
                config = attempts_ingest.load_config(project_root)
                counts = attempts_ingest.ingest_attempts_repo(conn, project_root, config)
            except attempts_ingest.AttemptsConfigError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            print(f"Attempts ({config.file}): {_format_counts(counts)}")

            if args.check:
                results = consistency.run_checks(conn, project_root, config)
                judgements.apply_after_scan(conn, project_root, print)  # 9.1: the end of a scan
                _print_consistency_report(results)
                return 1 if any(r.findings for r in results) else 0
            judgements.apply_after_scan(conn, project_root, print)
            _print_attempts_listing(conn, config)
        finally:
            conn.close()
    return 0


def cmd_mappings(args: argparse.Namespace) -> int:
    """`rce mappings` (DESIGN.md section 8.5): ingest the human mappings
    file `.rce/mappings.toml` -- the same `ingest_mappings` the web app's
    watcher runs when the file changes -- and print the counts plus every
    refused entry with its line (or entry index). Refused entries do not
    fail the run (the good ones were ingested; the file is the researcher's
    to fix), so the exit code is 0; a file that cannot be read or parsed at
    all exits 1 with the graph untouched (failing to read the source is not
    evidence its entries were deleted). A missing file is reported, not an
    error: there is simply nothing to ingest, and existing mapping edges
    are left as they are."""
    opened = _open(args.path)
    project_root = opened.root
    db_path = _require_db(project_root)
    with _write_guard(opened, human=False):
        conn = db.connect(db_path)
        try:
            try:
                report = mappings_ingest.ingest_mappings(conn, project_root)
            except mappings_ingest.MappingsFileError as exc:
                print(f"Error: {exc} -- graph left untouched", file=sys.stderr)
                return 1
            judgements.apply_after_scan(conn, project_root, print)  # 9.1: the end of a scan
        finally:
            conn.close()
    if not report.file_present:
        print(
            f"Mappings: no {mappings_ingest.MAPPINGS_RELATIVE_PATH} -- nothing to ingest "
            "(existing mapping edges left untouched)"
        )
        return 0
    print(f"Mappings ({mappings_ingest.MAPPINGS_RELATIVE_PATH}): {_format_counts(report.counts)}")
    for problem in report.problems:
        print(f"  refused {problem.location()}: {problem.message}")
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    """A human act on one machine link, written to the judgment ledger
    first and only then reflected in the index (DESIGN.md 9.1, 9.8):
    `rce.records.judgements.judge`, the one write path every surface uses.
    Verdicts: confirmed, rejected, withdrawn (the machine's status again),
    undone (takes back the last act). Identifies the link by its 4
    identity columns, or by `--index` into a freshly re-queried queue. A
    hand-drawn link is refused (its truth is .rce/mappings.toml), and so
    is a pre-V5 project until it is migrated (9.10)."""
    opened = _open(args.path)
    db_path = _require_db(opened.root)
    conn = db.connect(db_path)
    try:
        src, dst, edge_type, extractor = _confirm_target(args, conn)
        matches = [
            e for e in db.query_edges(conn, src=src, dst=dst, type=edge_type) if e["extractor"] == extractor
        ]
        old_status = matches[0]["status"] if matches else None
    finally:
        conn.close()
    try:
        judged = judgements.judge(
            opened.root, (src, dst, edge_type, extractor), args.status,
            via="cli", note=args.note, expected_id=opened.project_id,
        )
    except judgements.JudgementRefused as exc:
        raise CliError(f"not written: {exc}") from exc
    except records_situation.WriteRefused as exc:
        raise CliError(str(exc)) from exc
    except records_lock.ProjectLockError as exc:
        raise CliError(f"could not take the project lock: {exc}") from exc
    entry = judged.entry
    print(
        f"Edge {src} --{edge_type}--> {dst} (extractor={extractor}): {old_status} -> {judged.status} "
        f"[recorded {entry.get('verdict')} as {entry.id}, seq {entry.seq}, in .rce/judgements.toml]"
    )
    state = judged.state
    if state is not None and state["outcome"] != "applied":
        reason = state.get("reason") or state["outcome"]
        print(f"  not applied: {reason} ({judgements.REASON_LABELS.get(reason, '')}) -- see 'rce review'")
    return 0


def _confirm_target(args: argparse.Namespace, conn: Connection) -> tuple[str, str, str, str]:
    positional = (args.src, args.dst, args.type, args.extractor)
    if args.index is not None:
        if any(v is not None for v in positional):
            raise CliError("--index cannot be combined with the src/dst/type/extractor positional args")
        # The same queue `status --pending` prints: a link whose old
        # judgment is under review is not in 待确认 (9.6).
        candidates = db.pending_edges(conn) if args.from_status == "pending" else db.query_edges(conn, status=args.from_status)
        queue = _ordered_edges(candidates)
        if not 1 <= args.index <= len(queue):
            raise CliError(
                f"--index {args.index} out of range: the {args.from_status!r} queue has "
                f"{len(queue)} edge(s) right now -- indices are 1-based and re-sorted on "
                f"every run, so re-check with 'rce status --pending' immediately before use"
            )
        edge = queue[args.index - 1]
        return edge["src"], edge["dst"], edge["type"], edge["extractor"]
    if any(v is None for v in positional):
        raise CliError(
            "confirm requires either all four positional args (src dst type extractor) "
            "or --index (with --from-status)"
        )
    if args.extractor == "mapping":
        raise CliError(
            "this is a hand-drawn link: its one authority is .rce/mappings.toml -- edit or delete "
            "the mapping there (or in the app) instead"
        )
    return positional  # type: ignore[return-value]


def _print_card_reviews(card_items: dict[str, Any]) -> None:
    """9.11 stage (b) in `rce review`: one item per changed script, naming
    every card on it; RCE cannot tell whether a definition changed."""
    groups = card_items["groups"]
    if not groups:
        return
    print(f"Variable cards whose implementation moved: {card_items['count']} "
          "(RCE cannot tell whether a definition changed; answer in the app's 变量 view, "
          "or open the next draft with 'rce variable revise <id>')")
    for group in groups:
        head = f"{group['script']}: {len(group['cards'])} card(s)" if group["script"] else "input data"
        print(f"  {head}")
        for member in group["cards"]:
            reasons = ", ".join(r["code"] for r in member["reasons"])
            waiting = f" -- draft v{member['draft']} is open" if member.get("draft") is not None else ""
            print(f"    {member['card']} v{member['version']}: {reasons}{waiting}")


def cmd_review(args: argparse.Namespace) -> int:
    """`rce review` (DESIGN.md 9.6, 9.8): the judgments not applied --
    under review (with the reason, the old verdict, its date and note, the
    basis then and now, and candidate links), in conflict, held because
    their source could not be read, or whose link the index does not hold.
    Settle one with `rce confirm ...`: the same verdict again (仍然成立,
    recorded on the basis as it is now), the opposite one, or withdrawn."""
    opened = _open(args.path)
    db_path = _require_db(opened.root)
    # The ledger may have changed since anything last applied it (a hand
    # edit, a sync, no engine running): apply it first -- an index write,
    # so under the project lock like a scan.
    with _write_guard(opened, human=False):
        conn = db.connect(db_path)
        try:
            judgements.apply_ledger(conn, opened.root)
            items = judgements.review_items(conn)
            card_items = card_implementation.review_groups(conn)
        finally:
            conn.close()
    if args.json:
        print(json.dumps({**items, "cards": card_items}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    ledger_state = items["ledger"]
    if ledger_state and ledger_state.get("state") != "ok":
        print(
            f"Judgment ledger: {ledger_state['state']} -- {ledger_state.get('reason')} "
            f"({ledger_state.get('detail') or ''}); the index keeps what it had and nothing is written to the ledger"
        )
        if ledger_state.get("state") == "shrunk":
            print("  see the question with: rce records (it prints the answers bound to the missing entries)")
    print(f"Under review: {items['count']}")
    for item in items["review"]:
        _print_review_item(item)
    if items["source_unreadable"]:
        print(f"Source not readable (judgment kept as it was): {len(items['source_unreadable'])}")
        for item in items["source_unreadable"]:
            print(f"  {_item_label(item)} [{item['source_status']}]")
    if items["not_in_index"]:
        print(f"Judged links the index does not hold: {len(items['not_in_index'])}")
        for item in items["not_in_index"]:
            print(f"  {_item_label(item)} ({item['verdict']} {_when(item)})")
    _print_card_reviews(card_items)
    return 0


MIGRATED_WHEN = "migrated from the old index; original judgment time unknown"


def _when(item: dict[str, Any]) -> str:
    """When a judgment was made, as far as the record knows: a migrated
    entry's `at` is the migration's (9.12), never shown as the judgment's."""
    if item.get("migrated"):
        return f"({MIGRATED_WHEN})"
    return f"at {item.get('at')}"


def _item_label(item: dict[str, Any]) -> str:
    return f"{item['src']} --{item['type']}--> {item['dst']} (extractor={item['extractor']})"


def _print_review_item(item: dict[str, Any]) -> None:
    print(f"  {_item_label(item)}")
    print(f"    reason: {item['reason']} ({item['label']})")
    if item["outcome"] == "conflict":
        for n, branch in enumerate(item["detail"].get("branches", []), start=1):
            acts = ", ".join(f"{e.get('verdict')} seq {e.get('seq')} {_when(e)}" for e in branch) or "-"
            print(f"    history {n}: {acts}")
        print("    settle it with a new judgment: rce confirm ... --status confirmed|rejected|withdrawn")
        return
    note = f", note: {item['note']}" if item.get("note") else ""
    print(f"    was: {item['verdict']} {_when(item)}{note}")
    print(f"    basis then: {json.dumps(item['basis'], ensure_ascii=False, sort_keys=True)}")
    print(f"    basis now:  {json.dumps(item['basis_now'], ensure_ascii=False, sort_keys=True)}")
    for cand in item["candidates"]:
        print(f"    candidate ({judgements.CANDIDATE_HINT}): {_item_label(cand)}")
    print(
        f"    settle: rce confirm {item['src']} {item['dst']} {item['type']} {item['extractor']} "
        f"--status {item['verdict']}|{'confirmed' if item['verdict'] == 'rejected' else 'rejected'}|withdrawn"
    )


def cmd_records(args: argparse.Namespace) -> int:
    """`rce records` (DESIGN.md 9.2, 9.8): where each kind of human labor
    lives for this project, how many, the newest snapshot, and anything
    that stands between RCE and trusting it (`rce.inventory`). `--verify`
    checks, per link, that the index's human state is what the record
    implies -- judgments, hand-drawn links, attempt verdicts -- and exits 1
    with the differences if not. `--clean` lists copies nothing refers to
    and, with `--yes`, removes them. `--answer file|restore` answers 9.3's
    question when the judgment ledger has fewer entries than the index
    applied."""
    opened = _open(args.path)
    root = opened.root
    if args.missing is not None and not args.answer:
        raise CliError("--missing goes with --answer")
    if args.answer:
        shown = None if args.missing is None else [i.strip() for i in args.missing.split(",") if i.strip()]
        try:
            answered = judgements.answer_shrunk(root, args.answer, expected_missing=shown, expected_id=opened.project_id)
        except judgements.JudgementRefused as exc:
            raise CliError(f"not answered: {exc}") from exc
        except records_situation.WriteRefused as exc:
            raise CliError(str(exc)) from exc
        if args.answer == judgements.ANSWER_FILE:
            print(f"Took the file as it is: {len(answered.missing)} entr(y/ies) dropped from the index's copy")
        else:
            print(f"Appended {len(answered.appended)} missing entr(y/ies) to .rce/judgements.toml (via = recovered)")
    if args.clean:
        return _records_clean(opened, apply=args.yes)
    db_path = paths.graph_db_path(root)
    conn = db.connect(db_path) if db_path.exists() else None
    try:
        for line in records_inventory_lines(conn, root):
            print(line)
        if not args.verify:
            return 0
        if conn is None:
            raise CliError(f"there is no index to verify ({db_path}); open the project to build it")
        problems = inventory.verify(conn, root)
    finally:
        if conn is not None:
            conn.close()
    if problems:
        print(f"Verify: {len(problems)} mismatch(es)")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("Verify: the index's human state is what the record implies")
    return 0


def _records_clean(opened: project_identity.Opened, *, apply: bool) -> int:
    try:
        report = inventory.clean(opened.root, apply=apply, expected_id=opened.project_id)
    except records_situation.WriteRefused as exc:
        raise CliError(str(exc)) from exc
    except records_lock.ProjectLockError as exc:
        raise CliError(f"could not take the project lock: {exc}") from exc
    for directory in report.undecidable:
        print(f"  left alone: {directory} (its references cannot all be read)")
    if apply:
        print(f"Removed {len(report.removed)} copy/copies nothing refers to")
        for path in report.removed:
            print(f"  removed {path}")
        return 0
    print(f"Copies nothing refers to: {len(report.removable)}" + (" (dry run; --yes removes them)" if report.removable else ""))
    for path in report.removable:
        print(f"  {path}")
    return 0


def records_inventory_lines(conn: Connection | None, root: Path) -> list[str]:
    """The 9.2 inventory (`rce.inventory.inventory`), one line per kind of
    human labor -- each kind exactly once -- plus its problems, and, when
    the ledger has shrunk, the exact command that answers the question
    shown (with the ids of the missing entries, 9.12)."""
    lines = [f"Records of {root}:"]
    seen: set[str] = set()
    for row in inventory.inventory(conn, root):
        if row.kind in seen:
            continue
        seen.add(row.kind)
        snap = f"; newest snapshot {row.snapshot}" if row.snapshot else ""
        lines.append(f"  {row.kind}: {row.path} -- {row.count}{snap}")
        for problem in dict.fromkeys(row.problems):
            lines.append(f"    ! {problem}")
    if conn is not None:
        lines += _shrunk_question_lines(conn, root)
    return lines


def _shrunk_question_lines(conn: Connection, root: Path) -> list[str]:
    """9.3's question, as the CLI shows it: the missing entries, and the
    answers bound to them (`--missing`)."""
    got = _identity_or_none(root)
    loaded, decision = judgements.assess(conn, root, got, for_migration=True)
    if decision.reason != "shrunk" or not decision.missing:
        return []
    ids = ",".join(str(m["id"]) for m in decision.missing)
    out = [f"  The judgment ledger has {len(decision.missing)} fewer judgment(s) than the index applied:"]
    for m in decision.missing:
        out.append(f"    {m.get('id')}: {m.get('verdict')} {m.get('src')} --{m.get('type')}--> {m.get('dst')}")
    out.append(f"  answer: rce records --answer file --missing {ids} {root}     (take the file as it is)")
    out.append(f"      or: rce records --answer restore --missing {ids} {root}  (append the missing ones back)")
    return out


def _identity_or_none(root: Path):
    from rce.records.identity import IdentityState, read_identity  # noqa: PLC0415

    got = read_identity(root)
    return got.identity if got.state is IdentityState.PRESENT else None


def cmd_rebuild(args: argparse.Namespace) -> int:
    """`rce rebuild` (DESIGN.md 9.8): a fresh index beside the current one,
    the record applied, human state compared per link; swapped only when
    the comparison is clean and every source could be read. The previous
    index is kept one generation (`graph.db.prev`)."""
    opened = _open(args.path)
    print(f"Rebuilding the index of {opened.root}")
    try:
        result = rebuild_mod.rebuild(opened.root, expected_id=opened.project_id, echo=print)
    except rebuild_mod.RebuildRefused as exc:
        raise CliError(str(exc)) from exc
    except rebuild_mod.SwapRefused as exc:
        raise CliError(str(exc)) from exc
    except records_situation.WriteRefused as exc:
        raise CliError(str(exc)) from exc
    except records_lock.ProjectLockError as exc:
        raise CliError(f"could not take the project lock: {exc}") from exc
    tally = result.tally
    if tally:
        print(
            f"Per-link comparison: {tally.get('applied_before', 0)} judgment(s) applied before, "
            f"{tally.get('applied_after', 0)} still applied after, {tally.get('changed_source', 0)} on a changed source"
        )
    for item in result.blocked:
        print(f"  blocked: {item}")
    for item in result.failures:
        print(f"  failure: {item}")
    if not result.swapped:
        print("Not swapped: the current index is unchanged and keeps serving")
        return 1
    print(f"Swapped in the new index at {result.db_path}" + (f"; the previous one is kept at {result.previous}" if result.previous else ""))
    return 0


def _print_preview(view: migration.Preview) -> None:
    index = view.index
    print(f"  {index.db_path}")
    if index.error:
        print(f"    cannot be read: {index.error}")
        return
    came = index.recorded_path or "(not recorded)"
    exists = "" if index.path_exists is None else (" (exists)" if index.path_exists else " (no longer exists)")
    print(f"    came from: {came}{exists}")
    print(
        f"    holds: confirmed {index.confirmed}, rejected {index.rejected} "
        f"({index.remembered} remembering a prior status), arranged views {index.arranged_views}, "
        f"hand-drawn links {index.mapping_links} (skipped: their truth is mappings.toml)"
    )
    if index.judged:
        print(f"    match: a scan of {view.root} produces both ends of {view.produced} of {index.judged} judged link(s)")
    if view.scan_error:
        print(f"    (the scan of this folder failed: {view.scan_error})")


def cmd_migrate(args: argparse.Namespace) -> int:
    """`rce migrate` (DESIGN.md 9.5): an explicit act, never a side effect
    of opening. `--list`: every un-retired pre-V5 index on this machine.
    Without `--yes`: what waits for this folder and how well it matches,
    nothing written. `--yes`: migrate (export, basis, identity, rebuild
    and verify against the old index's own count, retire). `--not-mine`:
    「这不是这个项目的」 -- the old index is left alone and not offered to
    this folder again. `--from DIR`: a stranded index. An unfinished
    migration resumes without being asked again."""
    if args.list:
        found = migration.list_old_indexes()
        print(f"Un-retired pre-V5 indexes under {paths.rce_home() / paths.GRAPHS_DIRNAME}: {len(found)}")
        for index in found:
            exists = "?" if index.path_exists is None else ("exists" if index.path_exists else "missing")
            print(
                f"  {index.db_path.parent} -- {index.recorded_path or '(no project path recorded)'} [{exists}]; "
                f"confirmed {index.confirmed}, rejected {index.rejected}, remembered prior status {index.remembered}, "
                f"arranged views {index.arranged_views}" + (f"; cannot be read: {index.error}" if index.error else "")
            )
        return 0
    root = Path(args.path).resolve()
    if not root.is_dir():
        raise CliError(f"{root} is not a directory")
    try:
        project_identity.open_project(root)
    except (project_identity.ProjectBlocked, project_identity.AnswerRefused) as exc:
        raise CliError(str(exc)) from exc
    try:
        if args.not_mine:
            declined = migration.decline(root, from_dir=args.from_dir)
            print(f"Not this project's: {', '.join(declined) or 'nothing waiting'} -- left untouched, and not offered to {root} again")
            return 0
        results = migration.migrate(root, yes=args.yes, from_dir=args.from_dir, echo=print if args.verbose else (lambda _l: None))
    except migration.MigrationRefused as exc:
        raise CliError(str(exc)) from exc
    except records_lock.ProjectLockError as exc:
        raise CliError(f"could not take the project lock: {exc}") from exc
    if results and results[0].previews:
        print(f"Pre-V5 index(es) that may hold the judgments of {root}:")
        for view in results[0].previews:
            _print_preview(view)
        print("Nothing was written. To move these judgments into .rce/judgements.toml: rce migrate --yes; "
              "if this is not this project's index: rce migrate --not-mine")
        return 1
    status = 0
    for result in results:
        print(f"Migration of {result.key}" + (" (resumed)" if result.resumed else "") + ":")
        if result.tally is not None:
            for line in result.tally.lines():
                print(f"  {line}")
        if result.exported is not None:
            print(f"  ledger entries appended: {result.exported.appended}"
                  + ("; arrangement copied to .rce/canvas.json" if result.exported.copied_arrangement else ""))
        for note in result.notes:
            print(f"  note: {note}")
        if result.ok:
            if result.retired_to is not None:
                print(f"  the old index was retired to {result.retired_to}")
            print(f"  project id: {result.identity.id if result.identity else '?'}")
        else:
            print(f"  stopped: {result.stopped}")
            status = 1
    return status


def cmd_judge(args: argparse.Namespace) -> int:
    """`rce judge` (S2): the optional semantic layer. Reviews every pending
    `backed_by` candidate and annotates it with a model's opinion --
    written to `evidence.semantic_review` via `db.set_edge_semantic_review`
    only (see rce.semantic.judge's module docstring for why: the machine
    write path may only ever produce `status` in {auto, pending}, so this
    command never calls `db.set_edge_status`/`db.upsert_edge` and cannot
    move an edge to confirmed/rejected no matter what the model says).

    Zero-dependency baseline (task requirement 6): a backend that is not
    reachable fails this one command clearly and exits non-zero -- it never
    touches ingest/status/query/trace/confirm, none of which import
    anything from rce.semantic to begin with.
    """
    opened = _open(args.path)
    db_path = _require_db(opened.root)
    with _write_guard(opened, human=False):
        return _judge(args, db.connect(db_path))


def _judge(args: argparse.Namespace, conn: Connection) -> int:
    try:
        llm = semantic_backend.LlmBackend()
        try:
            llm.probe()
        except semantic_backend.LlmError as exc:
            raise CliError(
                f"semantic backend unavailable: {exc} -- 'rce judge' is the only affected "
                "command; ingest/status/query/trace/confirm work with no model running at all"
            ) from exc

        result = semantic_judge.review_pending_backed_by(
            conn, llm, limit=args.limit, dry_run=args.dry_run,
        )
        mode = "dry run -- no writes" if args.dry_run else "writing semantic_review annotations"
        print(f"Judging pending backed_by candidates ({mode}), backend model={llm.model!r}:")
        print(f"  pending backed_by edges total: {result.total_pending}")
        for outcome in result.reviewed:
            if outcome.error:
                print(f"  [error] {outcome.src} --backed_by--> {outcome.dst}: {outcome.error}")
                continue
            flag = " [hallucinated better_match dropped]" if outcome.hallucination_dropped else ""
            print(
                f"  {outcome.src} --backed_by--> {outcome.dst}: related={outcome.related} "
                f"better_match={outcome.better_match!r}{flag} reason={outcome.reason!r}"
            )
        errors = sum(1 for o in result.reviewed if o.error)
        written = sum(1 for o in result.reviewed if o.written)
        print(f"  reviewed={len(result.reviewed)} written={written} errors={errors}")
    finally:
        conn.close()
    return 0


def _print_edge(edge: dict, other_side: str, direction: str, conn=None) -> None:
    evidence = json.dumps(edge["evidence"], sort_keys=True)
    marker = judgements.review_marker(judgements.link_flags(conn).for_key(judgements.key_of(edge))) if conn is not None else ""
    # A claim's line lives on the claim node, not in the edge evidence, so resolve
    # it here the same way `status --pending` and `trace` do -- otherwise this is
    # the one consumer that cannot tell the reader where in the paper to look.
    location = ""
    if conn is not None and edge["type"] == "backed_by":
        loc = query.claim_source_location(conn, edge["src"]) or query.claim_source_location(
            conn, edge["dst"]
        )
        if loc and loc.get("line") is not None:
            location = f" source_location={loc.get('file')}:{loc['line']}"
    print(
        f"  {direction} {edge[other_side]} [{edge['type']}] extractor={edge['extractor']} "
        f"confidence={edge['confidence']:.2f} status={edge['status']}{marker} evidence={evidence}{location}"
    )


def cmd_query(args: argparse.Namespace) -> int:
    project_root = _resolve_project_root(args.path)
    conn = db.connect(_require_db(project_root))
    try:
        node = db.get_node(conn, args.node_id)
        if node is None:
            print(f"No such node: {args.node_id}", file=sys.stderr)
            return 1

        print(f"Node: {node['id']} ({node['type']})")
        if node["title"]:
            print(f"  title: {node['title']}")
        print(f"  attrs: {json.dumps(node['attrs'], sort_keys=True)}")
        if node["human_fields"]:
            print(f"  human_fields: {json.dumps(node['human_fields'], sort_keys=True)}")

        outgoing = db.query_edges(conn, src=args.node_id)
        incoming = db.query_edges(conn, dst=args.node_id)
        print(f"Outgoing edges ({len(outgoing)}):")
        for edge in outgoing:
            _print_edge(edge, "dst", "->", conn)
        if not outgoing:
            print("  (none)")
        print(f"Incoming edges ({len(incoming)}):")
        for edge in incoming:
            _print_edge(edge, "src", "<-", conn)
        if not incoming:
            print("  (none)")
    finally:
        conn.close()
    return 0


def _format_occurrence(occurrence: dict[str, Any], display_line: int | None = None) -> str:
    """Render one evidence occurrence dict as a readable string.

    Special-cases the common {"file": ..., "line": ...} shape (latex/pyfig
    extractors) as "file:line"; every other key (sha, run_id, artifact_path,
    callee, ...) falls back to sorted "key=value" pairs. This only reformats
    keys that are actually present -- it never invents fields an extractor
    didn't record.

    `display_line` fills in a missing "line" for display only (never
    written back to the occurrence dict in the database) -- `backed_by`
    occurrences from rce.ingest.claims no longer carry one (see
    rce.query.claim_source_location), so the caller passes the claim
    node's current line here to keep the "file:line" rendering instead of
    falling back to a bare "file=...".
    """
    remaining = dict(occurrence)
    if display_line is not None and "file" in remaining and "line" not in remaining:
        remaining["line"] = display_line
    parts: list[str] = []
    if "file" in remaining and "line" in remaining:
        parts.append(f"{remaining.pop('file')}:{remaining.pop('line')}")
    parts.extend(f"{key}={remaining[key]}" for key in sorted(remaining))
    return ", ".join(parts) if parts else "(no detail)"


def _format_evidence_summary(evidence: dict[str, Any], display_line: int | None = None) -> str:
    """Expand an edge's evidence into a readable summary.

    db.upsert_edge stores evidence as {"occurrences": [dict, ...]} (T10); a
    pre-T10 bare-evidence dict (see db._merge_edge_evidence's docstring) is
    treated as its own single occurrence rather than requiring a migration.

    `display_line` (see `_format_occurrence`) is passed through to every
    occurrence -- harmless for occurrence shapes that already carry their
    own "line" (it is only ever used to fill a *missing* one).
    """
    occurrences = evidence.get("occurrences")
    if not isinstance(occurrences, list):
        occurrences = [evidence]
    return "; ".join(_format_occurrence(occ, display_line) for occ in occurrences)


def _format_trace_human(node_id: str, max_hops: int, result: dict[str, Any]) -> str:
    """Indented, evidence-expanded text for `rce trace` (no --json).

    `hop["source_location"]` (query.trace's uniform, query-time-resolved
    claim line -- see rce.query.claim_source_location) is unwrapped to its
    plain `line` int and threaded through as `_format_evidence_summary`'s
    `display_line`, exactly like `_format_pending_line` does for `status
    --pending` -- the same value, read through the same function, so the
    two display paths can never drift the way they did when only one of
    them was patched to backfill it.
    """
    if not result["hops"]:
        return f"Node {node_id} exists but has no provenance edges recorded."
    lines = [f"Provenance trace for {node_id} (max_hops={max_hops}):"]
    for hop in result["hops"]:
        indent = "  " * hop["depth"]
        lines.append(f"{indent}[depth {hop['depth']}] {hop['src']} --{hop['type']}--> {hop['dst']}")
        lines.append(
            f"{indent}    extractor={hop['extractor']} confidence={hop['confidence']:.2f} "
            f"status={hop['status']}{judgements.review_marker(hop)}"
        )
        display_line = _display_line(hop.get("source_location"))
        lines.append(f"{indent}    evidence: {_format_evidence_summary(hop['evidence'], display_line)}")
    return "\n".join(lines)


def cmd_trace(args: argparse.Namespace) -> int:
    project_root = _resolve_project_root(args.path)
    conn = db.connect(_require_db(project_root))
    try:
        result = query.trace(conn, args.node_id, max_hops=args.hops)
    finally:
        conn.close()

    if not result["found"]:
        print(f"No such node: {args.node_id}", file=sys.stderr)
        return 1
    if args.json:
        # max_hops is echoed back explicitly (T-blocker fix, 2026-07-26) so a
        # scripted consumer can tell how far this trace was allowed to walk,
        # rather than inferring it from an argv it may not have access to.
        print(json.dumps({**result, "max_hops": args.hops}, sort_keys=True))
    else:
        print(_format_trace_human(args.node_id, args.hops, result))
    return 0


def _format_lineage_entry(entry: dict[str, Any]) -> str:
    if entry.get("human"):
        # A human mapping (rce.lineage): no call site to cite, and the
        # assertion's source is the mappings file the researcher wrote.
        return f"{entry['script']} (human mapping, .rce/mappings.toml)"
    return f"{entry['script']}:{entry['line']} ({entry['callee']}){judgements.review_marker(entry)}"


def _format_lineage_human(report: dict[str, Any], orphans_only: bool) -> str:
    """Human-readable rendering of `rce.lineage.build_lineage_report`'s four
    blocks. Per-block rule: a block is only headed and listed when it
    actually has content -- an empty block is not padded out with a "none
    found" line of its own, matching the task's own spec ("每块有内容才打").
    The one exception is when there is truly nothing to say at all (every
    block empty, or -- in --orphans mode -- the one block being shown is
    empty): that case is never left silent or rendered as a bare "OK". It
    states what was actually scanned and why the result is empty instead
    (DESIGN.md section 0: a missing finding is a normal outcome, but it must
    still be stated, never indistinguishable from "nothing was checked").
    """
    scanned = report["scanned"]
    scan_note = (
        f"scanned {scanned['scripts']} script node(s) -> {scanned['reads_edges']} reads "
        f"edge(s), {scanned['writes_edges']} writes edge(s) across {scanned['targets']} "
        f"dataset/figure target(s)"
    )
    lines = [f"Lineage report ({scan_note}):"]

    orphans = report["orphans"]
    if orphans:
        lines.append(f"\nOrphan inputs -- read but never written ({len(orphans)}):")
        for o in orphans:
            lines.append(f"  {o['path']}")
            for r in o["readers"]:
                lines.append(f"    <- read by {_format_lineage_entry(r)}")
    elif orphans_only:
        lines.append(
            f"\nNo orphan inputs found ({scan_note}): every dataset read here has at "
            "least one writer, or nothing here reads a dataset at all."
        )

    if orphans_only:
        return "\n".join(lines)

    chains = report["chains"]
    if chains:
        lines.append(f"\nLineage chains ({len(chains)}):")
        for c in chains:
            lines.append(f"  {c['path']}")
            for w in c["writers"]:
                lines.append(f"    <- written by {_format_lineage_entry(w)}")
            for r in c["readers"]:
                lines.append(f"    -> read by {_format_lineage_entry(r)}")

    broken = report["broken_links"]
    if broken:
        lines.append(f"\nBroken links -- evidence.missing=true ({len(broken)}):")
        for b in broken:
            lines.append(f"  {b['script']}:{b['line']} {b['kind']} {b['target']} (not found on disk)")

    duplicates = report["duplicates"]
    if duplicates:
        lines.append(f"\nDuplicate copies ({len(duplicates)}):")
        for d in duplicates:
            lines.append(f"  {d['path']} (this is the copy actually read)")
            for other in d["other_copies"]:
                lines.append(f"    also exists at: {other}")

    if not (orphans or chains or broken or duplicates):
        lines.append(
            f"\nNothing to report: {scan_note}, and none of it matched an orphan-input, "
            "lineage-chain, broken-link, or duplicate-copy pattern."
        )
    return "\n".join(lines)


def cmd_lineage(args: argparse.Namespace) -> int:
    """`rce lineage` (task W4): the one new user-facing exit this round --
    a read-only, four-block report over task W2's dataflow graph
    (`script --reads/writes--> dataset|figure`). Requires `rce ingest` to
    have already run, exactly like `query`/`trace`/`status` -- this command
    writes nothing and re-parses no source file (see rce.lineage's module
    docstring for the four blocks and their scoping rules).

    `--orphans` narrows the human/`--json` output to block 1 alone. The
    exit code rule is the same either way: 1 if orphan inputs or broken
    links were found (the two blocks that represent an actual gap, not
    merely a fact worth knowing), 0 otherwise -- computed from the full
    report regardless of which blocks `--orphans` chooses to print, so the
    exit code never depends on which flag was used to ask.
    """
    project_root = _resolve_project_root(args.path)
    conn = db.connect(_require_db(project_root))
    try:
        report = lineage.build_lineage_report(conn, project_root)
    finally:
        conn.close()

    if args.json:
        payload = report if not args.orphans else {
            "scanned": report["scanned"], "orphans": report["orphans"],
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        print(_format_lineage_human(report, args.orphans))

    return 1 if (report["orphans"] or report["broken_links"]) else 0


def cmd_serve(args: argparse.Namespace) -> int:
    """`rce serve` (task V1): starts `rce.webapp.server`'s local read-only
    web view over the graph, bound to 127.0.0.1 only. Requires `rce init`
    (and normally `rce ingest`/`rce attempts`) to have already run, exactly
    like every other read-only subcommand -- `rce.webapp.server.serve`
    raises `ProjectNotInitializedError` with the same message shape
    `_require_db` above gives, re-raised here as `CliError` so it prints and
    exits the same way any other missing-project error does rather than a
    raw traceback.

    V3 phase 1: the path is now optional. Given one, the project is
    recorded in the machine-managed registry (~/.rce/projects.json,
    rce.webapp.registry) as most recently served -- but only when it is
    actually an initialized project, so a typo'd or never-`rce init`ed path
    fails with the usual clean error without leaving a junk registry entry
    behind. Given no path, the most recently served registry entry is
    served instead -- NOT the current directory: an implicit "." would be
    exactly the kind of guess DESIGN.md section 0 rules out, since a bare
    `rce serve` is most naturally "reopen what I had open", not "serve
    wherever my shell happens to be". An empty registry fails with an
    actionable error rather than guessing either meaning.
    """
    if args.path is not None:
        project_root = Path(args.path).resolve()
        if not project_root.is_dir():
            raise CliError(f"{project_root} is not a directory")
        # The identity check first (DESIGN.md 9.4). A project that opens is
        # registered as most recently served; one in a situation to answer
        # is served in its blocked state, and nothing is written.
        served = webapp_server.served_for(project_root, register=True)
    else:
        entries = project_registry.load()
        if not entries:
            raise CliError(
                "no project path given and the project registry "
                f"(~/{project_registry.RCE_DIRNAME}/{project_registry.REGISTRY_FILENAME}) is empty -- "
                "run 'rce serve <path>' once with an explicit project path to register it; "
                "after that, a bare 'rce serve' reopens the most recently served project"
            )
        entry = entries[0]
        # A bare `rce serve` (how RCE.app starts the engine) whose most
        # recent entry is gone or no longer this project STARTS ANYWAY and
        # serves that entry's "missing" state, so the app can offer
        # 「选择新位置…」 instead of failing to start (9.4).
        served = webapp_server.served_for(
            Path(entry["path"]), expected_id=entry.get("id"), label=entry["label"], register=True,
        )
        project_root = served.root
    try:
        webapp_server.serve(project_root, args.port, open_browser=not args.no_browser, served=served)
    except webapp_server.ApiError as exc:
        raise CliError(str(exc)) from exc
    return 0


def _project_state_note(entry: dict[str, str]) -> str:
    """The one thing a listing must say about an entry beyond its path:
    whether it is usable, and if not, which of the two ways it is not.
    Same two questions `GET /api/projects` answers with its `available`/
    `initialized` pair (DESIGN.md section 8.10 rule 3) -- a directory that
    is gone is a dead entry worth removing; one that was merely never
    `rce init`ed is fine and just needs initializing."""
    state = webapp_server.entry_state(entry)
    if state["missing"]:
        if entry.get("id") and Path(entry["path"]).is_dir():
            return (
                "  (this folder no longer carries the project -- it was moved; open it at its new "
                "path, or 'rce projects remove' to drop this entry)"
            )
        return "  (directory missing -- 'rce projects remove' to drop this entry)"
    if not state["initialized"]:
        return f"  (not initialized -- run 'rce init {entry['path']}')"
    return ""


def cmd_projects_list(args: argparse.Namespace) -> int:
    """`rce projects list`: the registry the app's project switcher shows,
    on the command line (DESIGN.md section 8.10 rule 3 asks for CLI
    parity, so the researcher never has to hand-edit
    ~/.rce/projects.json). Most-recently-served first, exactly the order
    `GET /api/projects` returns."""
    entries = project_registry.load()
    if not entries:
        print(
            f"No registered projects yet ({project_registry.registry_path()} is empty or absent). "
            "'rce serve <path>' registers one."
        )
        return 0
    print(f"Registered projects ({len(entries)}, most recently served first):")
    for entry in entries:
        project_id = f"  [{entry['id']}]" if entry.get("id") else "  [pre-V5]"
        print(f"  {entry['label']}  {entry['path']}{project_id}{_project_state_note(entry)}")
    return 0


def _print_answered(result: project_identity.Answered) -> int:
    if result.answer == "fork":
        print(f"{result.root} is now project {result.identity.id}, forked from {result.previous_id}.")
        if result.git_tracked_identity:
            print(
                f"Note: {RCE_DIRNAME}/project.toml is tracked by git -- committing it carries the new "
                f"identity into whatever branch it is merged to."
            )
    elif result.answer == "claim":
        print(f"{result.root} is now the home of project {result.identity.id}; its index was rebuilt from this folder.")
        if result.replaced_index is not None:
            print(f"The previous index was kept at {result.replaced_index}.")
        print("The other folder carrying this id will be asked how to continue when it is next opened.")
    elif result.answer == "adopt":
        print(f"{result.root} keeps its records under a new identity: project {result.identity.id}.")
        print("A fresh index was built from its sources and records.")
    elif result.answer == "restore":
        print(f"Restored {RCE_DIRNAME}/project.toml from {result.restored_from}: project {result.identity.id}.")
        if result.blocked_after:
            print(f"The folder now has a question of its own:\n{result.blocked_after}")
            return 1
        print(f"Opened: {result.situation_after}.")
    else:
        print(f"{result.root} is now an independent project {result.identity.id}.")
        if result.moved_aside:
            print(f"Copied records moved into {RCE_DIRNAME}/backups/: {', '.join(result.moved_aside)}")
    print(f"Graph: {paths.index_dir(result.identity.id) / paths.DB_FILENAME}")
    if result.build_error:
        print(f"Warning: the scan of this folder did not finish ({result.build_error}); run 'rce ingest'.",
              file=sys.stderr)
        return 1
    return 0


def _answer(args: argparse.Namespace, fn) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        raise CliError(f"{root} is not a directory")
    try:
        result = fn(root, echo=print)
    except (project_identity.AnswerRefused, project_identity.ProjectBlocked) as exc:
        raise CliError(str(exc)) from exc
    except (records_situation.WriteRefused, records_lock.ProjectLockError) as exc:
        raise CliError(str(exc)) from exc
    return _print_answered(result)


def cmd_project_fork(args: argparse.Namespace) -> int:
    """「作为独立分支继续」 (DESIGN.md 9.4): this copy becomes its own project
    (new id, `forked_from` the original), with its own index."""
    return _answer(args, project_identity.fork)


def cmd_project_claim(args: argparse.Namespace) -> int:
    """「这里才是原项目」 (9.4): this folder becomes the home of the id, and
    the index is rebuilt from it."""
    return _answer(args, project_identity.claim)


def cmd_project_adopt(args: argparse.Namespace) -> int:
    """「沿用这些记录，建立新身份」 (DESIGN.md 9.12): the identity file was
    lost; a new id, every record kept where it is, a fresh index built."""
    return _answer(args, project_identity.adopt)


def cmd_project_restore(args: argparse.Namespace) -> int:
    """Restore `.rce/project.toml` from its newest snapshot (9.12), then the
    identity check as on any open."""
    return _answer(args, project_identity.restore)


def cmd_project_other(args: argparse.Namespace) -> int:
    """「这是另一个项目」 (9.4): a new id with no `forked_from`; copied record
    files are moved into `.rce/backups/`."""
    return _answer(args, project_identity.other)


# -- variable definition cards (DESIGN.md 9.11) ---------------------------------------


@contextmanager
def _card_errors():
    try:
        yield
    except variables_mod.VariableError as exc:
        raise CliError(f"not written: {exc}") from exc
    except records_situation.WriteRefused as exc:
        raise CliError(str(exc)) from exc
    except records_lock.ProjectLockError as exc:
        raise CliError(f"could not take the project lock: {exc}") from exc


def _card_overview(root: Path, card_id: str | None = None) -> list[dict[str, Any]]:
    db_path = paths.graph_db_path(root)
    conn = db.connect(db_path) if db_path.exists() else None
    try:
        found = variable_cards.overview(conn, root)
    finally:
        if conn is not None:
            conn.close()
    if card_id is None:
        return found
    key = variables_mod.card_key(card_id)
    match = [c for c in found if variables_mod.card_key(c["id"]) == key]
    if not match:
        raise CliError(f"there is no variable card {card_id!r} in {variables_mod.variables_dir(root)}")
    return match[:1]


def _card_status_word(card: dict[str, Any]) -> str:
    trust = card.get("trust") or {}
    if card["state"] == "unreadable":
        return f"UNREADABLE ({card['reason']}: {card['detail']})"
    if trust.get("state") not in (None, "ok"):
        missing = len(trust.get("missing") or [])
        what = f"{missing} entr(y/ies) fewer than the index applied" if trust["state"] == "shrunk" else (trust.get("detail") or "")
        return f"FROZEN -- {trust.get('reason')}: {what}; writes to this card are refused"
    if card["abandoned"]:
        return f"abandoned at {card['abandoned'].get('at')} ({card['abandoned'].get('note')})"
    if card["in_use"] is not None:
        return f"v{card['in_use']} in use"
    return "no confirmed version"


def _print_card_line(card: dict[str, Any]) -> None:
    draft = f", draft v{card['draft']} open" if card["draft"] is not None else ""
    print(f"  {card['id']}: {_card_status_word(card)}{draft}")
    for q in card["questions"]:
        print(f"    ! v{q['version']} was changed after it was confirmed ({q['message']}): "
              f"rce variable answer {card['id']} new|correct --version {q['version']}")
    for problem in card["problems"]:
        print(f"    ! {problem}")
    for flag in card["dead_flags"]:
        print(f"    ! dead-variable list disagrees: {flag['direction']} ({flag['message']})")


def cmd_variable_list(args: argparse.Namespace) -> int:
    """`rce variable list`: every card, the version in use, its draft, and
    what stands between RCE and trusting it."""
    root = _open(args.path).root
    found = _card_overview(root)
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"Variable cards of {root}: {len(found)}")
    for card in found:
        _print_card_line(card)
    return 0


def _print_check(name: str, item: dict[str, Any]) -> None:
    extra = ", ".join(f"{k}={v}" for k, v in item.items() if k not in ("result", "dataset"))
    print(f"      {name}: {item.get('result', '')} {extra}".rstrip())


def cmd_variable_show(args: argparse.Namespace) -> int:
    """`rce variable show <id>`: one card -- each version with its status,
    checks and observations, the history from the log, references."""
    root = _open(args.path).root
    card = _card_overview(root, args.id)[0]
    if args.json:
        print(json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    _print_card_line(card)
    for v in card["versions"]:
        label = f" [{v['label']}]" if v["label"] else ""
        print(f"  v{v['version']} ({v['file']}): {v['status']}{label}, file {v['file_state']}")
        if v["error"]:
            print(f"    ! {v['error']}")
        if v.get("entry"):
            print(f"    confirmed at {v['confirmed_at']}; reference {v['reference']['label']}")
            print(f"    attested (the output was built with this definition, in your words): {v['attested']}")
            if v["superseded_by"]:
                print(f"    superseded by v{v['superseded_by']['version']} from {v['superseded_by']['at']}")
            checked = v.get("checked") or {}
            for name in ("script", "writes", "field"):
                if name in checked:
                    _print_check(name, checked[name])
            for item in checked.get("reads") or []:
                _print_check(f"reads {item.get('dataset')}", item)
            observed = v.get("observed") or {}
            if observed.get("output"):
                print(f"      observed output (on disk at confirmation; not a claim it was built by this version): "
                      f"{json.dumps(observed['output'], ensure_ascii=False, sort_keys=True)}")
            if v["copy_missing"]:
                print(f"    ! {v['copy_missing']}: the frozen copy is not there; this version cannot be restored from it")
        for note in v["upstream_notes"]:
            print(f"    note: {note}")
    print("  history:")
    for e in card["history"]:
        what = f" v{e['version']}" if "version" in e else ""
        note = f" -- {e['note']}" if e.get("note") else ""
        print(f"    seq {e.get('seq')} {e.get('act')}{what} at {e.get('at')} via {e.get('via')}{note}")
    trust = card.get("trust") or {}
    if trust.get("state") == "shrunk":
        ids = ",".join(str(m["id"]) for m in trust["missing"])
        print(f"  log.toml has {len(trust['missing'])} fewer entr(y/ies) than the index applied:")
        for m in trust["missing"]:
            print(f"    {m.get('id')}: {m.get('act')}{' v' + str(m['version']) if 'version' in m else ''}")
        print(f"  answer: rce variable answer {card['id']} file --missing {ids} {root}     (take the file as it is)")
        print(f"      or: rce variable answer {card['id']} restore --missing {ids} {root}  (append the missing ones back)")
    return 0


def cmd_variable_new(args: argparse.Namespace) -> int:
    """`rce variable new <id>`: a card directory, created exclusively, with
    v1.toml from the commented template. RCE never writes the definition."""
    opened = _open(args.path)
    with _card_errors():
        path = variable_cards.new_card(opened.root, args.id, expected_id=opened.project_id)
    print(f"Created {path.relative_to(opened.root)} from the template. Write the definition in it, then: "
          f"rce variable confirm {args.id}")
    return 0


def cmd_variable_revise(args: argparse.Namespace) -> int:
    """`rce variable revise <id>`: the current confirmed version copied byte
    for byte to the next number, as the draft."""
    opened = _open(args.path)
    with _card_errors():
        path = variable_cards.revise(opened.root, args.id, expected_id=opened.project_id)
    print(f"Opened draft {path.relative_to(opened.root)} (a copy of the version in use). Edit it, then confirm it.")
    return 0


def cmd_variable_confirm(args: argparse.Namespace) -> int:
    """`rce variable confirm <id> [--attest yes|no|unknown]`: the draft
    becomes a definition results may rely on (snapshot first, entry last)."""
    opened = _open(args.path)
    with _card_errors():
        done = variable_cards.confirm(opened.root, args.id, attested=args.attest, expected_id=opened.project_id)
    entry = done.entry
    print(f"Confirmed {done.reference.variable} v{entry.get('version')} as {entry.id} (seq {entry.seq}); "
          f"reference {done.reference.label}")
    print(f"  attested: {entry.get('attested')} (your answer to: was the output as it stands built with this definition?)")
    checked = entry.get("checked") or {}
    for name in ("script", "writes", "field"):
        if name in checked:
            _print_check(name, dict(checked[name]))
    for item in checked.get("reads") or []:
        _print_check(f"reads {item.get('dataset')}", dict(item))
    return 0


def cmd_variable_answer(args: argparse.Namespace) -> int:
    """`rce variable answer <id> new|correct [--version N]`: the question
    「v<n> 的定义在确认后被改动了」. `file|restore [--missing ids]`: the 9.3
    question when the card's log has fewer entries than the index applied."""
    opened = _open(args.path)
    with _card_errors():
        if args.answer in (variables_mod.ANSWER_NEW, variables_mod.ANSWER_CORRECT):
            if args.missing is not None:
                raise CliError("--missing goes with the answers file|restore")
            done = variable_cards.answer_edited(opened.root, args.id, args.answer, version=args.version,
                                                expected_id=opened.project_id)
            if done.answer == variables_mod.ANSWER_NEW:
                print(f"Saved the edited text as draft v{done.new_version}; v{done.version}.toml was put back from its "
                      f"frozen copy, byte for byte.")
            else:
                print(f"Recorded a correction of v{done.version} as {done.entry.id}: both wordings stay readable.")
            return 0
        shown = None if args.missing is None else [i.strip() for i in args.missing.split(",") if i.strip()]
        done = variable_cards.answer_shrunk(opened.root, args.id, args.answer, expected_missing=shown,
                                            expected_id=opened.project_id)
    if done.answer == "file":
        print(f"Took log.toml as it is: {len(done.missing)} entr(y/ies) dropped from the index's copy"
              + (f"; {len(done.appended)} version number(s) marked removed" if done.appended else ""))
    else:
        print(f"Appended {len(done.appended)} missing entr(y/ies) to log.toml (via = recovered)"
              + (f"; put back {len(done.restored_copies)} frozen cop(y/ies)" if done.restored_copies else ""))
    return 0


def cmd_variable_abandon(args: argparse.Namespace) -> int:
    """`rce variable abandon <id> --note "..."`: why the variable died."""
    opened = _open(args.path)
    with _card_errors():
        entry = variable_cards.abandon(opened.root, args.id, note=args.note, expected_id=opened.project_id)
    print(f"Recorded: {args.id} abandoned at {entry.at} ({entry.id}).")
    return 0


def cmd_variable_revive(args: argparse.Namespace) -> int:
    """`rce variable revive <id> --note "..."`."""
    opened = _open(args.path)
    with _card_errors():
        entry = variable_cards.revive(opened.root, args.id, note=args.note, expected_id=opened.project_id)
    print(f"Recorded: {args.id} revived at {entry.at} ({entry.id}).")
    return 0


def cmd_projects_remove(args: argparse.Namespace) -> int:
    """`rce projects remove <path>`: drop one registry entry. Removes a
    bookmark, never a project -- nothing on disk is touched and nothing is
    ingested again, which is why this needs no confirmation prompt.

    The stored paths are absolute and resolved (`registry.register`), so a
    literal match is tried first and the resolved spelling second: a user
    typing `rce projects remove .` means the directory they are standing
    in. That convenience is deliberately NOT extended to
    `POST /api/projects/remove`, whose caller may be a web page and which
    therefore matches by string equality alone."""
    if args.path is None:
        raise CliError("name the registered project path to drop (as 'rce projects list' prints it)")
    if not project_registry.remove(args.path):
        resolved = str(Path(args.path).expanduser().resolve())
        if resolved == args.path or not project_registry.remove(resolved):
            raise CliError(
                f"{args.path!r} is not in the project registry "
                f"({project_registry.registry_path()}); 'rce projects list' shows what is"
            )
        print(f"Removed {resolved} from the project registry (nothing on disk was deleted).")
        return 0
    print(f"Removed {args.path} from the project registry (nothing on disk was deleted).")
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    """`rce app` (task V4 phase 3, DESIGN.md 8.9; first shipped in V3
    phase 4): build RCE.app with rce.webapp.macapp.build_app -- the native
    shell compiled by the system swiftc, or, when that toolchain is
    missing, the V3 launcher-script bundle plus a one-line notice saying
    so. Either replaces an existing RCE.app in place. See that module's
    docstring for the bundle's exact shape and why the rce path is a
    runtime sidecar rather than baked into source.

    `--dir` builds into any directory on any platform (the tests use it,
    and it is how a user targets /Applications instead). Only the
    *default* location, ~/Applications, is macOS-gated: on another
    platform there is no `open`, no .app double-click, and no
    ~/Applications convention, so defaulting there would generate a
    bundle nothing can launch -- the error says to pass --dir instead of
    guessing at a per-platform equivalent. The hidden `--port` (default
    7357) exists for tests and trial builds only, so a trial app never
    probes or stops the researcher's own engine."""
    if args.dir is not None:
        target_dir = Path(args.dir).expanduser().resolve()
    else:
        if not macapp.is_macos():
            raise CliError(
                "the default install location (~/Applications) is only meaningful on "
                "macOS; on this platform pass an explicit --dir to choose where the "
                "bundle is generated"
            )
        target_dir = Path.home() / "Applications"
    try:
        result = macapp.build_app(target_dir, port=args.port)
    except macapp.MacAppError as exc:
        raise CliError(str(exc)) from exc
    for notice in result.notices:
        print(notice)
    print(f"RCE.app written to {result.bundle}")
    if result.native:
        print("双击 RCE.app 即可打开研究地图；引擎没在运行时会自动启动。")
    else:
        print("双击 RCE.app 即可打开研究地图；服务已在运行时会直接打开页面。")
    return 0


def _tcp_port(value: str) -> int:
    """argparse `type=` for `rce app --port`: an integer in 1..65535."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {value!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be between 1 and 65535, got {port}")
    return port


def _import_mcp_server():
    """Lazy import for the optional 'mcp' extra (positioning ruling
    2026-07-22): rce.mcp_server does `from mcp.server.fastmcp import
    FastMCP` at module scope, so importing it eagerly at this module's top
    would make every `rce` subcommand require the mcp package. Only the
    `mcp` subcommand needs it. Raises ImportError if the extra isn't
    installed; the caller turns that into a clear, actionable message.
    """
    from rce import mcp_server

    return mcp_server


def _positive_hops(value: str) -> int:
    """argparse `type=` for `--hops`: must be an integer >= 1.

    T-blocker fix (2026-07-26): query.trace()'s BFS loop is `range(1,
    max_hops + 1)`, so max_hops <= 0 makes it not execute at all -- the
    traversal never even looks at the start node's own directly-incident
    edges. The result is hops=[], which _format_trace_human then reports as
    "Node X exists but has no provenance edges recorded", a false statement
    for any node that actually has edges (confirmed via `rce query` showing
    incoming/outgoing edges the same run). Rejecting <= 0 here, before the
    traversal ever runs, is preferred over rewording the empty-result
    message: with --hops >= 1 guaranteed, depth 1 always inspects every edge
    touching the start node regardless of the hop budget (db.EDGE_TYPES ==
    query.TRACE_EDGE_TYPES, so no edge type is untraceable), so an empty
    result is then always a truthful "zero edges touch this node".
    """
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"--hops must be >= 1, got {parsed}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rce", description="Research Context Engine CLI.")
    # Global, precedes the subcommand (e.g. `rce -v attempts --check`): every
    # extractor logs its skip/orphan/fallback reasons at INFO via the
    # standard `logging` module, but main() never called `basicConfig`, so
    # none of that was ever visible in normal use -- only this flag turns it
    # on, and only for this one invocation.
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="enable INFO-level diagnostic logging (skip reasons, orphan preservation, etc.)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "init",
        help=(
            "Initialize an RCE project at a path: creates its identity file .rce/project.toml, "
            "its graph under ~/.rce/graphs/<id>/ (outside the project) and a .rce/README saying so"
        ),
    )
    add_project_path(p)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ingest", help="Ingest git + LaTeX/.bib + MLflow sources into the graph")
    add_project_path(p)
    p.add_argument(
        "--mlruns", default=None,
        help="MLflow local FileStore dir (default: <path>/mlruns if present)",
    )
    p.add_argument(
        "--wandb", default=None, metavar="ENTITY/PROJECT",
        help=(
            "W&B entity/project to ingest, e.g. 'acme/my-project' "
            "(requires the WANDB_API_KEY env var; see rce.ingest.wandb)"
        ),
    )
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("status", help="Show node/edge counts and the pending confirmation queue")
    add_project_path(p)
    p.add_argument(
        "--pending", action="store_true",
        help="also list each pending edge (src/dst/type/extractor/confidence/evidence) for 'rce confirm'",
    )
    p.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="cap how many pending edges --pending prints (default: no cap); truncation is always stated, never silent",
    )
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "query",
        help=(
            "Show a node and its immediate (single-hop) incoming/outgoing edges with "
            "evidence; use 'trace' for multi-hop provenance"
        ),
    )
    p.add_argument("node_id", help="node id, e.g. figure:overview.png")
    add_project_path(p)
    p.set_defaults(func=cmd_query)

    p = sub.add_parser(
        "trace",
        help="Walk the multi-hop provenance chain from a node (see 'query' for single-hop)",
    )
    p.add_argument("node_id", help="node id, e.g. figure:overview.png")
    add_project_path(p)
    p.add_argument(
        "--hops", type=_positive_hops, default=4, help="max traversal depth (default: 4, must be >= 1)"
    )
    p.add_argument(
        "--json", action="store_true", help="output structured JSON instead of human-readable text"
    )
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser(
        "lineage",
        help=(
            "Read-only four-block report over the W2 dataflow graph: orphan inputs, "
            "lineage chains, broken (missing-on-disk) links, and duplicate-named copies"
        ),
    )
    add_project_path(p)
    p.add_argument(
        "--orphans", action="store_true",
        help="print only block 1 (data files read by a script but written by none)",
    )
    p.add_argument(
        "--json", action="store_true", help="output structured JSON instead of human-readable text"
    )
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser(
        "serve",
        help=(
            "Start a local read-only web view over the graph, bound to 127.0.0.1 only "
            "(task V1); prints the URL and opens a browser tab unless --no-browser is given"
        ),
    )
    add_project_path(p, default=None, help=(
        "project root, or give it as --path; also registered in ~/.rce/projects.json as most "
        "recently served. Omit both to reopen the most recently served project instead"
    ))
    p.add_argument(
        "--port", type=int, default=8317, help="TCP port to bind on 127.0.0.1 (default: 8317)"
    )
    p.add_argument(
        "--no-browser", action="store_true", help="do not automatically open a browser tab"
    )
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "projects",
        help=(
            "Inspect and prune the project registry the app's switcher reads "
            "(~/.rce/projects.json): 'rce projects list' / 'rce projects remove <path>'"
        ),
    )
    projects_sub = p.add_subparsers(dest="projects_command", required=True)
    q = projects_sub.add_parser(
        "list", help="List registered projects, most recently served first, flagging dead entries",
    )
    q.set_defaults(func=cmd_projects_list)
    q = projects_sub.add_parser(
        "remove", help="Remove one entry from the registry (a bookmark only -- deletes nothing)",
    )
    add_project_path(q, default=None, help=(
        "the registered project path to drop (as 'rce projects list' prints it); or give it as --path"
    ))
    q.set_defaults(func=cmd_projects_remove)

    p = sub.add_parser(
        "project",
        help=(
            "Answer what RCE asks when a project folder is a copy, its original cannot be "
            "checked, or its identity file was lost (DESIGN.md 9.4, 9.12): "
            "'rce project fork|claim|other|adopt|restore [path]'"
        ),
    )
    project_sub = p.add_subparsers(dest="project_command", required=True)
    for name, func, text in (
        ("fork", cmd_project_fork, "continue this copy as an independent branch: new id, forked_from the original"),
        ("claim", cmd_project_claim, "this folder is the original: it becomes the id's home and the index is rebuilt from it"),
        ("other", cmd_project_other, "this is another project that received a copy of .rce/: new id, copied records moved to .rce/backups/"),
        ("adopt", cmd_project_adopt, "the identity file was lost: keep every record here under a new id and build a fresh index from them"),
        ("restore", cmd_project_restore, "the identity file was lost: put .rce/project.toml back from its newest snapshot in .rce/backups/"),
    ):
        q = project_sub.add_parser(name, help=text)
        add_project_path(q)
        q.set_defaults(func=func)

    p = sub.add_parser(
        "app",
        help=(
            "Build RCE.app, the native macOS window around the web app (compiled with "
            "the system swiftc; falls back to the browser launcher bundle without it). "
            "Starts 'rce serve' itself when no engine is running (default: "
            "~/Applications, macOS only; --dir works anywhere)"
        ),
    )
    p.add_argument(
        "--dir", default=None, metavar="DIR",
        help=(
            "directory to generate RCE.app into instead of ~/Applications "
            "(e.g. /Applications); works on any platform"
        ),
    )
    # Hidden: for tests and trial builds, never for the researcher's own
    # install (one fixed port is how every app finds the one engine).
    p.add_argument("--port", type=_tcp_port, default=macapp.DEFAULT_PORT, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_app)

    p = sub.add_parser(
        "attempts",
        help=(
            "Ingest a hand-maintained attempt timeline via .rce/attempts.toml (config-gated, "
            "never guessed); lists registered attempts, or runs consistency checks with --check"
        ),
    )
    add_project_path(p)
    p.add_argument(
        "--check", action="store_true",
        help=(
            "run the three deterministic consistency checks (broken step references, stale "
            "verdicts, revived dead variables) instead of listing -- exits 1 if any check "
            "reports a finding"
        ),
    )
    p.set_defaults(func=cmd_attempts)

    p = sub.add_parser(
        "mappings",
        help=(
            "Ingest hand-drawn links from .rce/mappings.toml (reads | writes | generates) as "
            "confirmed edges; prints counts and any refused entries"
        ),
    )
    add_project_path(p)
    p.set_defaults(func=cmd_mappings)

    p = sub.add_parser(
        "confirm",
        help="Record a human verdict on one machine link in .rce/judgements.toml, then apply it",
    )
    p.add_argument("src", nargs="?", default=None, help="edge src node id, e.g. claim:paper.tex#abc123")
    p.add_argument("dst", nargs="?", default=None, help="edge dst node id, e.g. experiment:run_a")
    p.add_argument("type", nargs="?", default=None, help="edge type, e.g. backed_by")
    p.add_argument("extractor", nargs="?", default=None, help="edge extractor, e.g. claims")
    p.add_argument(
        "--status", required=True, choices=["confirmed", "rejected", "withdrawn", "undone"],
        help="the act: confirmed / rejected; withdrawn (the machine's status again); undone (takes back the last act)",
    )
    p.add_argument("--note", default=None, help="optional note stored with the judgment")
    p.add_argument(
        "--index", type=int, default=None, metavar="N",
        help=(
            "alternative to the 4 positional args: 1-based position in the --from-status queue, "
            "re-queried/re-sorted by THIS invocation in the same order as 'status --pending'. Not "
            "a stable id -- another confirm or ingest run can shift what index N means"
        ),
    )
    p.add_argument(
        "--from-status", default="pending", choices=sorted(db.EDGE_STATUSES),
        help="status to select --index from (default: pending)",
    )
    add_project_path(p, help=(
        "project root (default: '.'), after the four link args -- or, with --index, the one "
        "positional; or give it as --path"
    ))
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser(
        "review",
        help="List judgments not applied: under review (with reason, old verdict, basis then/now, candidates), "
             "in conflict, or held because their source could not be read",
    )
    p.add_argument("--json", action="store_true", help="print the list as JSON")
    add_project_path(p)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser(
        "records",
        help="Inventory of this project's human records (.rce/); --verify checks the index against them",
    )
    p.add_argument("--verify", action="store_true", help="check per link that the index's human state is what the record implies; exit 1 if not")
    p.add_argument(
        "--answer", choices=list(judgements.ANSWERS), default=None,
        help="when the judgment ledger has fewer entries than the index applied: 'file' takes the file as "
             "truth (以文件为准), 'restore' appends the missing entries back (把缺少的补回文件)",
    )
    p.add_argument("--clean", action="store_true", help="list kept copies nothing refers to (dry run)")
    p.add_argument("--yes", action="store_true", help="with --clean: remove them")
    p.add_argument(
        "--missing", default=None, metavar="ID[,ID...]",
        help="with --answer: the ids of the missing entries the question showed ('rce records' prints "
             "them); if the file changed since, nothing is done and the question is shown again",
    )
    add_project_path(p)
    p.set_defaults(func=cmd_records)

    p = sub.add_parser(
        "variable",
        help="Variable definition cards in .rce/variables/ (DESIGN.md 9.11): "
             "'rce variable new|list|show|revise|confirm|answer|abandon|revive'",
    )
    variable_sub = p.add_subparsers(dest="variable_command", required=True)
    q = variable_sub.add_parser("list", help="every card, the version in use, its draft and its problems")
    q.add_argument("--json", action="store_true", help="print the cards as JSON")
    add_project_path(q)
    q.set_defaults(func=cmd_variable_list)
    for name, func, text in (
        ("new", cmd_variable_new, "create .rce/variables/<id>/v1.toml from the commented template"),
        ("show", cmd_variable_show, "one card: versions, checks, observations, history, references"),
        ("revise", cmd_variable_revise, "copy the version in use, byte for byte, to the next number as the draft"),
        ("confirm", cmd_variable_confirm, "confirm the draft: checks, copies, then the log entry"),
        ("abandon", cmd_variable_abandon, "record why the variable died (--note is required)"),
        ("revive", cmd_variable_revive, "record why an abandoned variable is back (--note is required)"),
        ("answer", cmd_variable_answer, "answer a card's question: new|correct (a confirmed version was edited), "
                                        "file|restore (its log has fewer entries than the index applied)"),
    ):
        q = variable_sub.add_parser(name, help=text)
        q.add_argument("id", help="the variable's id (its directory name; compared case-folded)")
        if name == "answer":
            q.add_argument("answer", choices=["new", "correct", "file", "restore"])
            q.add_argument("--version", type=int, default=None, help="with new|correct: which edited version")
            q.add_argument("--missing", default=None, metavar="ID[,ID...]",
                           help="with file|restore: the ids of the missing entries 'rce variable show' printed")
        if name == "confirm":
            q.add_argument("--attest", choices=list(variables_mod.ATTESTED), default="unknown",
                           help="your answer: was the output file as it stands built with THIS definition? (default unknown)")
        if name in ("abandon", "revive"):
            q.add_argument("--note", required=True, help="the reason, in your words")
        if name == "show":
            q.add_argument("--json", action="store_true", help="print the card as JSON")
        add_project_path(q)
        q.set_defaults(func=func)

    p = sub.add_parser(
        "rebuild",
        help="Build a fresh index beside the current one, apply the record, compare per link, then swap "
             "(the previous index is kept one generation)",
    )
    add_project_path(p)
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser(
        "migrate",
        help="Move the judgments of a pre-V5 index into .rce/judgements.toml (explicit; --list shows every old index)",
    )
    p.add_argument("--list", action="store_true", help="list every un-retired pre-V5 index on this machine")
    p.add_argument("--yes", action="store_true", help="migrate (without it, only show what waits and how well it matches)")
    p.add_argument("--not-mine", action="store_true", help="this is not this project's index: leave it, and do not offer it again")
    p.add_argument("--from", dest="from_dir", default=None, metavar="DIR", help="migrate from this index directory (a stranded index)")
    p.add_argument("-v", "--verbose", action="store_true", help="print the scan's progress")
    add_project_path(p)
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser(
        "judge",
        help=(
            "optional semantic layer: annotate pending backed_by candidates via a local "
            "model (writes evidence.semantic_review only, status stays pending -- see "
            "rce.semantic.judge)"
        ),
    )
    add_project_path(p)
    p.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="review at most N pending backed_by edges, in 'status --pending' order (default: all)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="call the model and print what would be annotated, but write nothing to the database",
    )
    p.set_defaults(func=cmd_judge)

    # `mcp`'s own args (--path etc.) are parsed by rce.mcp_server.main itself,
    # not by this parser -- see the argv[0] == "mcp" interception in main()
    # below, which also lazy-imports rce.mcp_server (via _import_mcp_server)
    # so every other subcommand keeps working with the optional 'mcp' extra
    # uninstalled. Registered here only so it shows up in `rce --help`'s
    # command list; its own --help is served by mcp_server's parser instead
    # (prog "rce mcp"), which is why it takes no arguments here.
    sub.add_parser(
        "mcp",
        help=(
            "optional: expose the graph to MCP-capable clients (requires "
            "pip install \"rce[mcp]\"); args pass through, e.g. 'rce mcp --path .'"
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "mcp":
        try:
            mcp_server = _import_mcp_server()
        except ImportError as exc:
            print(
                "Error: the 'mcp' subcommand requires the optional 'mcp' extra, "
                "which is not installed (MCP is one of several optional exits, "
                "not a requirement to use rce). Install it with: "
                'pip install "rce[mcp]"\n'
                f"(underlying error: {exc})",
                file=sys.stderr,
            )
            return 1
        # Pass-through per T5's architecture: rce.mcp_server.main does its own
        # argument parsing (e.g. --path), so forward the remainder untouched
        # rather than re-declaring the same options in this parser.
        return mcp_server.main(argv[1:])
    args = build_parser().parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    try:
        _settle_path(args)
        return args.func(args)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
