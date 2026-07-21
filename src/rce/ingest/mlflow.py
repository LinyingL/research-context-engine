"""Deterministic MLflow local FileStore ingester -- zero-model extractor
layer (T3). Read-only parse of a local `mlruns/<exp_id>/<run_id>/` tree; no
mlflow package dependency (Occam rule 1/5: meta.yaml is a flat key: value
map, params/metrics/tags are one-value-per-file). Writes via rce.db's
upsert_node/upsert_edge (idempotency inherited from there). Corrupted runs
and unresolvable connector keys are skipped + logged, never guessed: an
absent git SHA never becomes a placeholder Commit node, an ambiguous
artifact basename never guesses a Figure (HANDOFF-SPEC.md section 0/5).
Top-level `models/`/`.trash/` are MLflow-internal, not experiment dirs.
"""

from __future__ import annotations

import logging
import posixpath
from pathlib import Path
from sqlite3 import Connection

from rce import db

logger = logging.getLogger(__name__)

_ARTIFACT_IMAGE_EXTENSIONS = frozenset({".png", ".pdf", ".jpg", ".svg"})
_RESERVED_TOP_LEVEL_DIRS = frozenset({"models", ".trash"})

# "关键 tags" (T3 brief): tags/ also holds noisy housekeeping entries (e.g.
# mlflow.log-model.history, a large JSON blob); attrs keeps only this
# curated subset -- run-name/git-sha lookups below still read the full dict.
_KEY_TAG_NAMES = frozenset({
    "mlflow.runName", "mlflow.user", "mlflow.source.name", "mlflow.source.type",
    "mlflow.source.git.commit", "mlflow.source.git.branch",
    "mlflow.source.git.repoURL", "mlflow.parentRunId", "mlflow.note.content",
})

def _parse_meta_yaml(text: str) -> dict[str, str]:
    """Naive 'key: value' per line (Occam rule 1/5, no PyYAML). Blank/'#'
    lines are skipped silently; a non-blank line without ':' is skipped +
    logged. Matching-quote wrapping (e.g. experiment_id: '0') is stripped."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            logger.warning("skipping unparseable meta.yaml line: %r", stripped[:120])
            continue
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result

def _read_flat_dir(dir_path: Path) -> dict[str, str]:
    """One value per file (params/, tags/): filename -> stripped content."""
    result: dict[str, str] = {}
    for entry in sorted(dir_path.iterdir()) if dir_path.is_dir() else []:
        if not entry.is_file():
            continue
        try:
            result[entry.name] = entry.read_text(errors="replace").strip()
        except OSError as exc:
            logger.warning("cannot read %s: %s", entry, exc)
    return result

def _read_metrics_dir(dir_path: Path) -> dict[str, float]:
    """metrics/<name>: one 'timestamp value step' line per logged point;
    last line is the final value. Unparseable file skipped + logged."""
    result: dict[str, float] = {}
    for entry in sorted(dir_path.iterdir()) if dir_path.is_dir() else []:
        if not entry.is_file():
            continue
        lines = [l for l in entry.read_text(errors="replace").splitlines() if l.strip()]
        parts = lines[-1].split() if lines else []
        if len(parts) < 2:
            logger.warning("skipping unparseable metric file %s", entry)
            continue
        try:
            result[entry.name] = float(parts[1])
        except ValueError:
            logger.warning("skipping non-numeric metric value in %s: %r", entry, parts[1])
    return result

def _iter_run_dirs(mlruns_root: Path):
    for exp_dir in sorted(p for p in mlruns_root.iterdir() if p.is_dir()):
        if exp_dir.name in _RESERVED_TOP_LEVEL_DIRS:
            continue
        yield from sorted(p for p in exp_dir.iterdir() if p.is_dir())

def ingest_mlflow_dir(conn: Connection, mlruns_root: str | Path) -> dict[str, int]:
    """Per run: upsert Experiment node `experiment:<run_id>` (attrs:
    params/metrics/key tags); `Commit --implements--> Experiment` if
    tags/mlflow.source.git.commit names a SHA already in the graph;
    `Experiment --produces--> Figure` per artifacts/ image file whose
    basename uniquely matches an existing figure: node. No/ambiguous match
    is skipped + logged, never guessed; a run dir with no readable
    meta.yaml is corrupted and skipped entirely. Idempotent via db.upsert_*.
    Returns counts of nodes and each edge type written.
    """
    mlruns_root = Path(mlruns_root)
    counts = {"experiments": 0, "implements": 0, "produces": 0}
    if not mlruns_root.is_dir():
        logger.warning("mlruns directory not found: %s", mlruns_root)
        return counts

    # Built once (this module never writes figure: nodes) via
    # db.get_nodes_by_type, keeping raw SQL confined to db.py.
    figure_basenames: dict[str, list[str]] = {}
    for node in db.get_nodes_by_type(conn, "figure"):
        path_part = node["id"].split(":", 1)[1]
        figure_basenames.setdefault(posixpath.basename(path_part), []).append(node["id"])

    for run_dir in _iter_run_dirs(mlruns_root):
        meta_path = run_dir / "meta.yaml"
        if not meta_path.is_file():
            logger.warning("skipping run dir with no meta.yaml: %s", run_dir)
            continue
        try:
            meta = _parse_meta_yaml(meta_path.read_text(errors="replace"))
        except OSError as exc:
            logger.warning("cannot read %s: %s", meta_path, exc)
            continue

        run_id = run_dir.name
        tags = _read_flat_dir(run_dir / "tags")
        experiment_id = f"experiment:{run_id}"
        db.upsert_node(
            conn, experiment_id, "experiment",
            title=tags.get("mlflow.runName") or meta.get("run_name") or run_id,
            attrs={
                "run_id": run_id,
                "experiment_id": meta.get("experiment_id", run_dir.parent.name),
                "status": meta.get("status"),
                "start_time": meta.get("start_time"),
                "end_time": meta.get("end_time"),
                "artifact_uri": meta.get("artifact_uri"),
                "params": _read_flat_dir(run_dir / "params"),
                "metrics": _read_metrics_dir(run_dir / "metrics"),
                "tags": {k: v for k, v in tags.items() if k in _KEY_TAG_NAMES},
            },
        )
        counts["experiments"] += 1

        sha = tags.get("mlflow.source.git.commit", "").strip()
        if sha:
            commit_id = f"commit:{sha}"
            if db.get_node(conn, commit_id) is not None:
                db.upsert_edge(
                    conn, commit_id, experiment_id, "implements", extractor="mlflow",
                    evidence={"run_id": run_id, "sha": sha}, confidence=1.0, status="auto",
                )
                counts["implements"] += 1
            else:
                logger.info(
                    "run %s references commit %s not found in graph; skipping "
                    "implements edge (no placeholder commit created)", run_id, sha,
                )

        artifacts_dir = run_dir / "artifacts"
        for artifact_path in sorted(artifacts_dir.rglob("*")) if artifacts_dir.is_dir() else []:
            if not artifact_path.is_file() or artifact_path.suffix.lower() not in _ARTIFACT_IMAGE_EXTENSIONS:
                continue
            matches = figure_basenames.get(artifact_path.name, [])
            if len(matches) != 1:
                logger.warning(
                    "run %s artifact %s matches %d figure nodes by basename; skipping "
                    "produces edge", run_id, artifact_path.name, len(matches),
                )
                continue
            db.upsert_edge(
                conn, experiment_id, matches[0], "produces", extractor="mlflow",
                evidence={
                    "run_id": run_id,
                    "artifact_path": artifact_path.relative_to(artifacts_dir).as_posix(),
                },
                confidence=1.0, status="auto",
            )
            counts["produces"] += 1

    return counts
