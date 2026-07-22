"""RCE command-line interface (T4): `rce init` / `ingest` / `status` / `query`.

stdlib argparse only (HANDOFF-SPEC.md section 0, Occam rule 1). Orchestrates
the existing extractors (rce.ingest.git/latex/pyfig/mlflow/wandb) and rce.db
(section 7 Phase A order: git -> latex/.bib -> pyfig -> mlflow -> wandb);
writes only via db.upsert_node/upsert_edge, no new graph mutation logic here.

A project is "initialized" once `<root>/.rce/graph.db` exists (`rce init`);
every other command requires that file and errors clearly if absent (no
guessing, per the constitution).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection

from rce import db
from rce import mcp_server
from rce.ingest import git as git_ingest
from rce.ingest import latex as latex_ingest
from rce.ingest import mlflow as mlflow_ingest
from rce.ingest import pyfig as pyfig_ingest
from rce.ingest import wandb as wandb_ingest

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
    # files (HANDOFF-SPEC.md section 2, "零习惯改变"), so this is printed,
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
            skipped = warnings.count
        print("Ingest summary (whole graph):")
        _print_graph_counts(conn)
        print(f"  Skipped/unresolved during this run (see logs): {skipped}")
    finally:
        conn.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project_root = _resolve_project_root(".")
    conn = db.connect(_require_db(project_root))
    try:
        print(f"Project: {project_root}")
        _print_graph_counts(conn)
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
    project_root = _resolve_project_root(".")
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
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("query", help="Show a node and its incoming/outgoing edges with evidence")
    p.add_argument("node_id", help="node id, e.g. figure:overview.png")
    p.set_defaults(func=cmd_query)

    # `mcp`'s own args (--path etc.) are parsed by rce.mcp_server.main itself,
    # not by this parser -- see the argv[0] == "mcp" interception in main()
    # below. Registered here only so it shows up in `rce --help`'s command
    # list; its own --help is served by mcp_server's parser instead (prog
    # "rce mcp"), which is why it takes no arguments here.
    sub.add_parser(
        "mcp",
        help="Start the MCP stdio server (product's primary interface); args pass through, e.g. 'rce mcp --path .'",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "mcp":
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
