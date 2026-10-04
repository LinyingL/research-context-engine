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

from rce import consistency, db, lineage, paths, query
from rce import project as project_identity
from rce.ingest import attempts as attempts_ingest
# `git_ingest` stays importable as `rce.cli.git_ingest` (tests patch it);
# the scan itself is `rce.ingest.pipeline`.
from rce.ingest import git as git_ingest  # noqa: F401
from rce.ingest import mappings as mappings_ingest
from rce.ingest import pipeline as ingest_pipeline
from rce.records import lock as records_lock
from rce.records import situation as records_situation
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
        f"(project.toml, attempts.toml, mappings.toml, backups/) -- commit or .gitignore it as you "
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
        if args.pending:  # purely additive -- omitting it reproduces the prior output exactly
            _print_pending_queue(conn, args.limit)
    finally:
        conn.close()
    return 0


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
                _print_consistency_report(results)
                return 1 if any(r.findings for r in results) else 0
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
    """Thin wrapper over db.set_edge_status, mirroring
    rce.mcp_server.confirm_edge's contract. Identifies the edge by its 4
    identity columns, or by `--index` into a freshly re-queried queue.
    A human write: under the project lock with identity re-checked, and
    refused on a pre-V5 project until it is migrated (9.10)."""
    opened = _open(args.path)
    db_path = _require_db(opened.root)
    with _write_guard(opened, human=True):
        return _confirm(args, db.connect(db_path))


def _confirm(args: argparse.Namespace, conn: Connection) -> int:
    try:
        positional = (args.src, args.dst, args.type, args.extractor)
        if args.index is not None:
            if any(v is not None for v in positional):
                raise CliError("--index cannot be combined with the src/dst/type/extractor positional args")
            queue = _ordered_edges(db.query_edges(conn, status=args.from_status))
            if not 1 <= args.index <= len(queue):
                raise CliError(
                    f"--index {args.index} out of range: the {args.from_status!r} queue has "
                    f"{len(queue)} edge(s) right now -- indices are 1-based and re-sorted on "
                    f"every run, so re-check with 'rce status --pending' immediately before use"
                )
            edge = queue[args.index - 1]
            src, dst, edge_type, extractor = edge["src"], edge["dst"], edge["type"], edge["extractor"]
        else:
            if any(v is None for v in positional):
                raise CliError(
                    "confirm requires either all four positional args (src dst type extractor) "
                    "or --index (with --from-status)"
                )
            src, dst, edge_type, extractor = positional

        matches = [
            e for e in db.query_edges(conn, src=src, dst=dst, type=edge_type) if e["extractor"] == extractor
        ]
        if not matches:
            raise CliError(f"no such edge: {src} --{edge_type}--> {dst} (extractor={extractor})")
        old_status = matches[0]["status"]
        db.set_edge_status(conn, src, dst, edge_type, extractor, args.status)
        print(f"Edge {src} --{edge_type}--> {dst} (extractor={extractor}): {old_status} -> {args.status}")
    finally:
        conn.close()
    return 0


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
        f"confidence={edge['confidence']:.2f} status={edge['status']} evidence={evidence}{location}"
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
            f"status={hop['status']}"
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
    return f"{entry['script']}:{entry['line']} ({entry['callee']})"


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


def cmd_project_other(args: argparse.Namespace) -> int:
    """「这是另一个项目」 (9.4): a new id with no `forked_from`; copied record
    files are moved into `.rce/backups/`."""
    return _answer(args, project_identity.other)


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
    p.add_argument("path", nargs="?", default=".", help="project root (default: '.')")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ingest", help="Ingest git + LaTeX/.bib + MLflow sources into the graph")
    p.add_argument("path", nargs="?", default=".", help="project root (default: '.')")
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
    p.add_argument("--path", default=".", help="project root (default: '.')")
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
    p.add_argument("--path", default=".", help="project root (default: '.')")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser(
        "trace",
        help="Walk the multi-hop provenance chain from a node (see 'query' for single-hop)",
    )
    p.add_argument("node_id", help="node id, e.g. figure:overview.png")
    p.add_argument("--path", default=".", help="project root (default: '.')")
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
    p.add_argument("path", nargs="?", default=".", help="project root (default: '.')")
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
    p.add_argument(
        "path", nargs="?", default=None,
        help=(
            "project root; also registered in ~/.rce/projects.json as most recently "
            "served. Omit to reopen the most recently served project instead"
        ),
    )
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
    q.add_argument("path", help="the registered project path to drop (as 'rce projects list' prints it)")
    q.set_defaults(func=cmd_projects_remove)

    p = sub.add_parser(
        "project",
        help=(
            "Answer what RCE asks when a project folder is a copy, or its original cannot be "
            "checked (DESIGN.md 9.4): 'rce project fork|claim|other [path]'"
        ),
    )
    project_sub = p.add_subparsers(dest="project_command", required=True)
    for name, func, text in (
        ("fork", cmd_project_fork, "continue this copy as an independent branch: new id, forked_from the original"),
        ("claim", cmd_project_claim, "this folder is the original: it becomes the id's home and the index is rebuilt from it"),
        ("other", cmd_project_other, "this is another project that received a copy of .rce/: new id, copied records moved to .rce/backups/"),
    ):
        q = project_sub.add_parser(name, help=text)
        q.add_argument("path", nargs="?", default=".", help="project root (default: '.')")
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
    p.add_argument(
        "path", nargs="?", default=None,
        help="project root (default: '.'); consistent with 'init'/'ingest' -- --path below also accepted",
    )
    p.add_argument(
        "--path", dest="path_flag", default=None,
        help="project root, equivalent to the positional argument above (this subcommand's original form)",
    )
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
    p.add_argument("path", nargs="?", default=".", help="project root (default: '.')")
    p.set_defaults(func=cmd_mappings)

    p = sub.add_parser(
        "confirm",
        help="Human confirm/reject one edge (writes via db.set_edge_status) -- no mcp extra required",
    )
    p.add_argument("src", nargs="?", default=None, help="edge src node id, e.g. claim:paper.tex#abc123")
    p.add_argument("dst", nargs="?", default=None, help="edge dst node id, e.g. experiment:run_a")
    p.add_argument("type", nargs="?", default=None, help="edge type, e.g. backed_by")
    p.add_argument("extractor", nargs="?", default=None, help="edge extractor, e.g. claims")
    p.add_argument(
        "--status", required=True, choices=["confirmed", "rejected"], help="new human verdict",
    )
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
    p.add_argument("--path", default=".", help="project root (default: '.')")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser(
        "judge",
        help=(
            "optional semantic layer: annotate pending backed_by candidates via a local "
            "model (writes evidence.semantic_review only, status stays pending -- see "
            "rce.semantic.judge)"
        ),
    )
    p.add_argument("--path", default=".", help="project root (default: '.')")
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
        return args.func(args)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
