"""The full scan of a project's sources, in one place (DESIGN.md section 7
Phase A order), so that `rce ingest` and the entry points that must build
an index from a folder (`rce project claim` / `fork` / `other`, 9.4) run
the same extractors in the same order. Moved here verbatim from
`rce.cli.cmd_ingest`; the CLI prints through `echo`, the other callers
pass their own.

Order: git -> latex/.bib -> dataflow -> pyfig -> mlflow -> wandb ->
mdpaper -> claims (claims last: it matches claim numbers against
experiment metrics, so mlflow/wandb must have written those nodes first).
A project that is not a git repository degrades to a filesystem walk for
its inventory (W1); any other git failure stops the scan
(`IngestFailed`).

`ingest_records` is the second half of "build from this folder": the
researcher's own files that the index mirrors -- `.rce/mappings.toml`
and the attempt table named by `.rce/attempts.toml` -- exactly the calls
`rce mappings` and `rce attempts` make.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection
from typing import Callable, Iterator

from rce.ingest import attempts as attempts_ingest
from rce.ingest import claims as claims_ingest
from rce.ingest import dataflow as dataflow_ingest
from rce.ingest import files as files_ingest
from rce.ingest import git as git_ingest
from rce.ingest import latex as latex_ingest
from rce.ingest import mappings as mappings_ingest
from rce.ingest import mdpaper as mdpaper_ingest
from rce.ingest import mlflow as mlflow_ingest
from rce.ingest import pyfig as pyfig_ingest
from rce.ingest import wandb as wandb_ingest

Echo = Callable[[str], None]


class IngestFailed(Exception):
    """The scan could not run to the end (a git failure other than "not a
    repository", a malformed `--wandb`, a W&B error). The CLI prints it as
    its own error."""


def _format_counts(counts: dict[str, int]) -> str:
    return " ".join(f"{k}={v}" for k, v in counts.items())


class _WarningCounter(logging.Handler):
    """Counts WARNING+ records from the extractors' shared "rce.ingest" logger
    for one ingest run, giving a skip count without changing those modules."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


@contextmanager
def count_ingest_warnings() -> Iterator[_WarningCounter]:
    counter = _WarningCounter()
    ingest_logger = logging.getLogger("rce.ingest")
    ingest_logger.addHandler(counter)
    try:
        yield counter
    finally:
        ingest_logger.removeHandler(counter)


def ingest_sources(
    conn: Connection,
    project_root: Path,
    *,
    mlruns: str | None = None,
    wandb: str | None = None,
    echo: Echo = lambda _line: None,
) -> int:
    """Run every source extractor over `project_root`; returns how many
    warnings (skips/unresolved) the run logged. The caller holds the
    project's write guard."""
    with count_ingest_warnings() as warnings:
        try:
            commits = git_ingest.ingest_git_repo(conn, project_root)
        except git_ingest.NotAGitRepositoryError:
            # W1: a project root with no git repository at all is a
            # normal, supported case: commit/contributor nodes are simply
            # unavailable, the file inventory falls back to a plain
            # filesystem walk, and every other extractor still runs.
            echo(
                "  git: no git repository -- commit/contributor nodes unavailable; "
                "using filesystem scan for the file inventory"
            )
            commits = 0
            inventory = files_ingest.list_source_files(project_root)
        except git_ingest.GitIngestError as exc:
            raise IngestFailed(f"git ingestion failed: {exc}") from exc
        else:
            try:
                inventory = git_ingest.list_source_files(project_root)
            except git_ingest.GitIngestError as exc:
                raise IngestFailed(f"git ingestion failed: {exc}") from exc
            echo(f"  git: {commits} commit(s) ingested")
        # inventory["image"] lets the latex ingester reject "ghost figures"
        # (\includegraphics targets not actually tracked in the repo).
        latex_counts = latex_ingest.ingest_latex_repo(
            conn, project_root, inventory["tex"], inventory["bib"],
            image_paths=inventory["image"],
        )
        echo(
            f"  latex: {len(inventory['tex'])} .tex, {len(inventory['bib'])} .bib "
            f"scanned -> {_format_counts(latex_counts)}"
        )
        # W2: data lineage; needs no git at all.
        dataflow_counts = dataflow_ingest.ingest_dataflow_repo(
            conn, project_root, inventory["py"], inventory["r"], inventory["rmd"],
        )
        echo(
            f"  dataflow: {len(inventory['py'])} .py, {len(inventory['r'])} .R, "
            f"{len(inventory['rmd'])} .Rmd scanned -> {_format_counts(dataflow_counts)}"
        )
        # T6: static savefig() analysis; each edge's src commit is resolved
        # internally via git blame, not HEAD.
        pyfig_counts = pyfig_ingest.ingest_pyfig_repo(
            conn, project_root, inventory["py"], inventory["image"],
        )
        echo(f"  pyfig: {len(inventory['py'])} .py scanned -> {_format_counts(pyfig_counts)}")
        if mlruns:
            mlruns_path: Path | None = Path(mlruns).resolve()
        else:
            default_mlruns = project_root / "mlruns"
            mlruns_path = default_mlruns if default_mlruns.is_dir() else None
        if mlruns_path is not None:
            mlflow_counts = mlflow_ingest.ingest_mlflow_dir(conn, mlruns_path)
            echo(f"  mlflow: {mlruns_path} -> {_format_counts(mlflow_counts)}")
        else:
            echo("  mlflow: skipped (no --mlruns given and no mlruns/ directory found)")
        if wandb:
            entity, sep, wandb_project = wandb.partition("/")
            if not sep or not entity or not wandb_project:
                raise IngestFailed(f"--wandb expects 'entity/project', got {wandb!r}")
            try:
                wandb_counts = wandb_ingest.ingest_wandb_project(conn, entity, wandb_project)
            except wandb_ingest.WandbError as exc:
                raise IngestFailed(f"wandb ingestion failed: {exc}") from exc
            echo(f"  wandb: {wandb} -> {_format_counts(wandb_counts)}")
        else:
            echo("  wandb: skipped (no --wandb given)")
        # W3: Markdown papers, after mlflow/wandb for the same reason the
        # tex claims step must be last.
        md_counts = mdpaper_ingest.ingest_md_repo(
            conn, project_root, inventory["md"], image_paths=inventory["image"],
        )
        echo(
            f"  mdpaper: {len(inventory['md'])} .md scanned "
            f"({md_counts['md_skipped_non_paper']} skipped as README/CHANGELOG/LICENSE) "
            f"-> {_format_counts(md_counts)}"
        )
        claims_counts = claims_ingest.ingest_claims_repo(conn, project_root, inventory["tex"])
        echo(f"  claims: {_format_counts(claims_counts)}")
        return warnings.count


def ingest_records(conn: Connection, project_root: Path, *, echo: Echo = lambda _line: None) -> None:
    """Mirror the researcher's own files into the index: `.rce/mappings.toml`
    (as `rce mappings`) and, when `.rce/attempts.toml` loads, the attempt
    table (as `rce attempts`). A file that cannot be read is reported and
    skipped, never treated as a deletion (Section 4)."""
    try:
        report = mappings_ingest.ingest_mappings(conn, project_root)
        if report.file_present:
            echo(f"  mappings: {_format_counts(report.counts)}")
    except mappings_ingest.MappingsFileError as exc:
        echo(f"  mappings: not read ({exc})")
    try:
        config = attempts_ingest.load_config(project_root)
    except attempts_ingest.AttemptsConfigError:
        return
    try:
        counts = attempts_ingest.ingest_attempts_repo(conn, project_root, config)
        echo(f"  attempts: {_format_counts(counts)}")
    except attempts_ingest.AttemptsConfigError as exc:
        echo(f"  attempts: not read ({exc})")
