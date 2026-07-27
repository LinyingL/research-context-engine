"""RCE command-line interface (T4): `rce init` / `ingest` / `status` / `query` /
`trace` / `confirm` (F3) / `judge` (S2, optional semantic layer).

stdlib argparse only (DESIGN.md section 0, Occam rule 1). Orchestrates
the existing extractors (rce.ingest.git/latex/pyfig/mlflow/wandb) and rce.db
(section 7 Phase A order: git -> latex/.bib -> pyfig -> mlflow -> wandb);
writes only via db.upsert_node/upsert_edge, no new graph mutation logic here.
`trace` reuses rce.query.trace() directly -- multi-hop traversal logic lives
in exactly one place.

A project is "initialized" once `<root>/.rce/graph.db` exists (`rce init`);
every other command requires that file and errors clearly if absent (no
guessing, per the constitution).

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

from rce import db, query
from rce.ingest import claims as claims_ingest
from rce.ingest import git as git_ingest
from rce.ingest import latex as latex_ingest
from rce.ingest import mlflow as mlflow_ingest
from rce.ingest import pyfig as pyfig_ingest
from rce.ingest import wandb as wandb_ingest
# S2: `rce judge`, the optional semantic layer. Unlike rce.mcp_server
# (behind a lazy import because it needs the third-party 'mcp' extra),
# rce.semantic.{backend,judge} use only stdlib urllib -- importing them
# here eagerly costs zero-dependency installs nothing, so `judge` gets a
# normal top-level subparser like every other subcommand, not the `mcp`
# subcommand's pass-through special case.
from rce.semantic import backend as semantic_backend
from rce.semantic import judge as semantic_judge

RCE_DIRNAME = ".rce"
DB_FILENAME = "graph.db"


class CliError(Exception):
    """User-facing error; caught once in main() -> "Error: <msg>" on stderr, exit 1."""


class _WarningCounter(logging.Handler):
    """Counts WARNING+ records from the extractors' shared "rce.ingest" logger
    for one ingest run, giving a skip count without changing those modules."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


@contextmanager
def _count_ingest_warnings():
    counter = _WarningCounter()
    ingest_logger = logging.getLogger("rce.ingest")
    ingest_logger.addHandler(counter)
    try:
        yield counter
    finally:
        ingest_logger.removeHandler(counter)


def _resolve_project_root(path_str: str) -> Path:
    root = Path(path_str).resolve()
    if not root.is_dir():
        raise CliError(f"{root} is not a directory")
    return root


def _require_db(project_root: Path) -> Path:
    path = project_root / RCE_DIRNAME / DB_FILENAME
    if not path.exists():
        raise CliError(
            f"no RCE project at {project_root} (missing {RCE_DIRNAME}/{DB_FILENAME}); "
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

    Includes `metric=` (bug fix): a `backed_by` edge can carry several
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
    better = review.get("better_match")
    better_note = f" better_match={better!r}" if better else ""
    return (
        f"\n      semantic_review: {flag}metric={review.get('metric')!r} "
        f"related={review.get('related')!r} reason={review.get('reason')!r}"
        f"{better_note} (model={review.get('model')!r})"
    )


def _current_claim_line(conn: Connection, edge: dict) -> int | None:
    """The claim node's *current* line number for a `backed_by` edge, for
    display only (bug fix companion to rce.ingest.claims no longer writing
    a `line` into each occurrence -- see that module's docstring for why:
    the line shifts with any unrelated edit above the claim, and evidence
    is deduped by whole-dict equality, so a stable per-occurrence identity
    requires dropping it there). The claim node's own `attrs["line"]` is
    always kept current by `upsert_node` on every re-ingest, so this is the
    single place display code reads it from instead."""
    if edge["type"] != "backed_by":
        return None
    claim_node = db.get_node(conn, edge["src"])
    if claim_node is None:
        return None
    line = claim_node["attrs"].get("line")
    return line if isinstance(line, int) else None


def _format_pending_line(index: int, edge: dict, conn: Connection) -> str:
    return (
        f"  [{index}] {edge['src']} --{edge['type']}--> {edge['dst']} "
        f"extractor={edge['extractor']} confidence={edge['confidence']:.2f} "
        f"evidence={_format_evidence_summary(edge['evidence'], display_line=_current_claim_line(conn, edge))}"
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
    project_root = _resolve_project_root(args.path)
    rce_dir = project_root / RCE_DIRNAME
    rce_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(rce_dir / DB_FILENAME)
    try:
        applied = db.migrate(conn)
        project_id = f"project:{project_root.name}"
        db.upsert_node(
            conn, project_id, "project", title=project_root.name,
            attrs={"path": str(project_root)},
        )
    finally:
        conn.close()
    print(f"Initialized RCE project at {rce_dir} (project node: {project_id})")
    if applied:
        print(f"Applied migrations: {applied}")
    # T5.5 review item 5: a nudge only -- RCE never edits the user's own
    # files (DESIGN.md section 2, "零习惯改变"), so this is printed,
    # not applied.
    print(f"Tip: add '{RCE_DIRNAME}/' to your project's .gitignore -- RCE will not do this for you.")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    project_root = _resolve_project_root(args.path)
    conn = db.connect(_require_db(project_root))
    try:
        with _count_ingest_warnings() as warnings:
            print(f"Ingesting {project_root}")
            try:
                commits = git_ingest.ingest_git_repo(conn, project_root)
                inventory = git_ingest.list_source_files(project_root)
            except git_ingest.GitIngestError as exc:
                raise CliError(f"git ingestion failed: {exc}") from exc
            print(f"  git: {commits} commit(s) ingested")
            # inventory["image"] lets the latex ingester reject "ghost figures"
            # (\includegraphics targets not actually tracked in the repo, T5.5
            # review item 2) -- this cli entry point always passes it; the
            # library function itself keeps it optional (None = no validation).
            latex_counts = latex_ingest.ingest_latex_repo(
                conn, project_root, inventory["tex"], inventory["bib"],
                image_paths=inventory["image"],
            )
            print(
                f"  latex: {len(inventory['tex'])} .tex, {len(inventory['bib'])} .bib "
                f"scanned -> {_format_counts(latex_counts)}"
            )
            # T6: static savefig() analysis; each edge's src commit is
            # resolved internally via git blame (batch3-fix), not HEAD.
            pyfig_counts = pyfig_ingest.ingest_pyfig_repo(
                conn, project_root, inventory["py"], inventory["image"],
            )
            print(f"  pyfig: {len(inventory['py'])} .py scanned -> {_format_counts(pyfig_counts)}")
            if args.mlruns:
                mlruns_path: Path | None = Path(args.mlruns).resolve()
            else:
                default_mlruns = project_root / "mlruns"
                mlruns_path = default_mlruns if default_mlruns.is_dir() else None
            if mlruns_path is not None:
                mlflow_counts = mlflow_ingest.ingest_mlflow_dir(conn, mlruns_path)
                print(f"  mlflow: {mlruns_path} -> {_format_counts(mlflow_counts)}")
            else:
                print("  mlflow: skipped (no --mlruns given and no mlruns/ directory found)")
            if args.wandb:
                entity, sep, wandb_project = args.wandb.partition("/")
                if not sep or not entity or not wandb_project:
                    raise CliError(f"--wandb expects 'entity/project', got {args.wandb!r}")
                try:
                    wandb_counts = wandb_ingest.ingest_wandb_project(conn, entity, wandb_project)
                except wandb_ingest.WandbError as exc:
                    raise CliError(f"wandb ingestion failed: {exc}") from exc
                print(f"  wandb: {args.wandb} -> {_format_counts(wandb_counts)}")
            else:
                print("  wandb: skipped (no --wandb given)")
            # Phase B (task B1): claim extraction + deterministic backed_by
            # candidate generation. Must run last -- it matches claim
            # numbers against experiment nodes' metrics, so mlflow/wandb
            # (just above) have to have already written those nodes, or
            # every claim would trivially get zero candidates.
            claims_counts = claims_ingest.ingest_claims_repo(conn, project_root, inventory["tex"])
            print(f"  claims: {_format_counts(claims_counts)}")
            skipped = warnings.count
        print("Ingest summary (whole graph):")
        _print_graph_counts(conn)
        print(f"  Skipped/unresolved during this run (see logs): {skipped}")
    finally:
        conn.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project_root = _resolve_project_root(args.path)
    conn = db.connect(_require_db(project_root))
    try:
        print(f"Project: {project_root}")
        _print_graph_counts(conn)
        if args.pending:  # purely additive -- omitting it reproduces the prior output exactly
            _print_pending_queue(conn, args.limit)
    finally:
        conn.close()
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    """Thin wrapper over db.set_edge_status, mirroring
    rce.mcp_server.confirm_edge's contract. Identifies the edge by its 4
    identity columns, or by `--index` into a freshly re-queried queue."""
    project_root = _resolve_project_root(args.path)
    conn = db.connect(_require_db(project_root))
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
    project_root = _resolve_project_root(args.path)
    conn = db.connect(_require_db(project_root))
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


def _print_edge(edge: dict, other_side: str, direction: str) -> None:
    evidence = json.dumps(edge["evidence"], sort_keys=True)
    print(
        f"  {direction} {edge[other_side]} [{edge['type']}] extractor={edge['extractor']} "
        f"confidence={edge['confidence']:.2f} status={edge['status']} evidence={evidence}"
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
            _print_edge(edge, "dst", "->")
        if not outgoing:
            print("  (none)")
        print(f"Incoming edges ({len(incoming)}):")
        for edge in incoming:
            _print_edge(edge, "src", "<-")
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
    rce.cli._current_claim_line), so the caller passes the claim node's
    current line here to keep the "file:line" rendering instead of falling
    back to a bare "file=...".
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
    """Indented, evidence-expanded text for `rce trace` (no --json)."""
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
        lines.append(f"{indent}    evidence: {_format_evidence_summary(hop['evidence'])}")
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
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Initialize an RCE project (.rce/graph.db) at a path")
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
    try:
        return args.func(args)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
