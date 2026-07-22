"""Deterministic W&B public-project reader (T8) -- zero-model extractor
layer. Talks to the public W&B GraphQL API (https://api.wandb.ai/graphql)
via stdlib urllib only -- no `wandb` package dependency (Owner's explicit
constraint here). Field names in `_RUNS_QUERY` and the Basic-Auth scheme
below (username "api", password = API key) match wandb's own Python client
(wandb/apis/public/runs.py, wandb/sdk/internal/internal_api.py) -- checked
against the OSS source, not guessed.

Two layers: `fetch_wandb_runs` (network + auth only, no graph writes) and
`transform_runs` (pure function of already-fetched JSON -> graph writes, no
network -- what the test suite exercises with fixture JSON).

Per run -> Experiment node `experiment:<run id>` (wandb's GraphQL `name` is
the run's actual unique id; `displayName` is just its UI label). `commit`
-> `Commit --implements--> Experiment` iff that SHA is already a commit:
node in the graph (no placeholder ever created). Each run file's basename
-> `Experiment --produces--> Figure` iff it matches exactly one existing
figure: node; no/ambiguous match is skipped + logged, never guessed (both
policies mirror rce.ingest.mlflow). config/summaryMetrics keep wandb's own
large media-blob values (`_type`-tagged dicts: Table/Image/Histogram)
stripped and long strings truncated -- a curated subset, not the raw blob.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import posixpath
import urllib.error
import urllib.request
from sqlite3 import Connection
from typing import Any

from rce import db
from rce.ingest import git as git_ingest

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.wandb.ai"
_HTTP_TIMEOUT_SECONDS = 30
# T11: reuse git.IMAGE_EXTENSIONS as the single source of truth for what
# counts as an image file, rather than a local, narrower duplicate (the old
# literal set here was missing .jpeg/.eps/.gif/.tiff/.tif, so e.g. a .jpeg
# run file could never match a figure: node).
_ARTIFACT_IMAGE_EXTENSIONS = git_ingest.IMAGE_EXTENSIONS
_MEDIA_BLOB_MARKER = "_type"  # wandb's own marker for Table/Image/Histogram refs
_MAX_STRING_LEN = 4000  # longer config/summary string values are truncated, not stored whole

# Mirrors wandb/apis/public/runs.py's RunFragment selection. files(first:100)
# is capped, not paginated -- enough for typical per-run figure counts.
_RUNS_QUERY = """
query Runs($project: String!, $entity: String!, $cursor: String) {
  project(name: $project, entityName: $entity) {
    runs(first: 50, after: $cursor) {
      edges {
        node {
          name displayName state config summaryMetrics commit tags notes createdAt
          files(first: 100) { edges { node { name } } }
        }
      }
      pageInfo { endCursor hasNextPage }
    }
  }
}
"""

class WandbError(RuntimeError):
    """A user-facing W&B API failure: missing key, 401, bad project, or a
    network error -- always a specific, actionable message, never guessed."""

def _resolve_api_key(api_key: str | None) -> str:
    """Fail fast, before any request, if no key is available anywhere."""
    key = (api_key if api_key is not None else os.environ.get("WANDB_API_KEY", "")).strip()
    if not key:
        raise WandbError(
            "no W&B API key: pass api_key= or set the WANDB_API_KEY env var "
            "(even public W&B projects require a free key -- see https://wandb.ai/authorize)"
        )
    return key

def _post_graphql(base_url: str, api_key: str, variables: dict[str, Any]) -> dict[str, Any]:
    """One GraphQL POST via urllib; raises WandbError with a clear cause for
    any auth/network/API failure."""
    body = json.dumps({"query": _RUNS_QUERY, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/graphql", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    auth = base64.b64encode(f"api:{api_key}".encode()).decode("ascii")
    request.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise WandbError(
                "W&B API rejected the key (401 Unauthorized) -- check WANDB_API_KEY"
            ) from exc
        raise WandbError(f"W&B API returned HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise WandbError(f"could not reach W&B API at {base_url}: {exc.reason}") from exc
    except (TypeError, ValueError) as exc:
        raise WandbError(f"W&B API returned unparseable JSON: {exc}") from exc
    if payload.get("errors"):
        raise WandbError(f"W&B GraphQL API returned errors: {payload['errors']}")
    return payload

def fetch_wandb_runs(
    entity: str, project: str, api_key: str | None = None, base_url: str = DEFAULT_BASE_URL,
) -> list[dict[str, Any]]:
    """Fetch every run's raw GraphQL node for entity/project, paginating via
    the runs connection's cursor. Network + auth only -- no graph writes.

    T11: connection/edges/pageInfo/endCursor are all structurally validated
    below -- an unexpected shape (API change, non-standard server) raises
    WandbError with context, never a bare KeyError.
    """
    key = _resolve_api_key(api_key)
    runs: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        payload = _post_graphql(
            base_url, key, {"entity": entity, "project": project, "cursor": cursor}
        )
        project_data = (payload.get("data") or {}).get("project")
        if project_data is None:
            raise WandbError(
                f"W&B project not found or not accessible: {entity}/{project} "
                "(check the name and that the key can read it)"
            )
        connection = project_data.get("runs")
        if not isinstance(connection, dict):
            raise WandbError(
                f"W&B GraphQL response for {entity}/{project} is missing a "
                f"valid 'runs' connection: {project_data!r}"
            )
        edges = connection.get("edges")
        if not isinstance(edges, list):
            raise WandbError(
                f"W&B GraphQL response for {entity}/{project} has a malformed "
                f"'runs.edges' (expected a list): {connection!r}"
            )
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict):
                raise WandbError(
                    f"W&B GraphQL response for {entity}/{project} has a run "
                    f"edge missing its 'node': {edge!r}"
                )
            runs.append(node)
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict):
            raise WandbError(
                f"W&B GraphQL response for {entity}/{project} has a malformed "
                f"'runs.pageInfo' (expected a dict): {connection!r}"
            )
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            raise WandbError(
                f"W&B GraphQL pageInfo.hasNextPage is true but 'endCursor' is "
                f"missing for {entity}/{project}: {page_info!r}"
            )
    return runs

# -- transform layer: pure functions of already-fetched JSON; no network --

def _strip_media_blobs(value: Any) -> Any:
    """Drop wandb Table/Image/Histogram refs (`_type`-tagged dicts) and
    truncate oversized strings, recursively."""
    if isinstance(value, dict):
        if _MEDIA_BLOB_MARKER in value:
            return {"_type": value.get(_MEDIA_BLOB_MARKER), "_stripped": True}
        return {k: _strip_media_blobs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_media_blobs(v) for v in value]
    if isinstance(value, str) and len(value) > _MAX_STRING_LEN:
        return value[:_MAX_STRING_LEN] + "...<truncated>"
    return value

def _parse_json_blob(raw: Any, run_id: str, field_name: str) -> dict[str, Any]:
    """config/summaryMetrics arrive over GraphQL as JSON strings; an
    unparseable blob loses just that one field, not the whole run."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return _strip_media_blobs(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("run %s: unparseable %s JSON, skipping field: %s", run_id, field_name, exc)
        return {}
    return _strip_media_blobs(parsed) if isinstance(parsed, dict) else {}

def transform_runs(conn: Connection, runs: list[dict[str, Any]]) -> dict[str, int]:
    """Write each run's Experiment node + implements/produces edges via
    rce.db. Idempotent via db.upsert_node/upsert_edge.

    T11: counts["produces"] counts distinct (experiment, figure) edges
    actually affected, not files scanned -- mirrors rce.ingest.mlflow.
    """
    counts = {"experiments": 0, "implements": 0, "produces": 0}
    produces_edges: set[tuple[str, str]] = set()

    figure_basenames: dict[str, list[str]] = {}  # built once, mirrors rce.ingest.mlflow
    for node in db.get_nodes_by_type(conn, "figure"):
        path_part = node["id"].split(":", 1)[1]
        figure_basenames.setdefault(posixpath.basename(path_part), []).append(node["id"])

    for run in runs:
        run_id = run.get("name")  # wandb's real run id; displayName is just the UI label
        if not run_id:
            logger.warning("skipping run with no 'name' (run id): %r", run.get("displayName"))
            continue

        experiment_id = f"experiment:{run_id}"
        db.upsert_node(
            conn, experiment_id, "experiment",
            title=run.get("displayName") or run_id,
            attrs={
                "run_id": run_id,
                "state": run.get("state"),
                "tags": run.get("tags") or [],
                "notes": run.get("notes"),
                "created_at": run.get("createdAt"),
                "config": _parse_json_blob(run.get("config"), run_id, "config"),
                "summary_metrics": _parse_json_blob(run.get("summaryMetrics"), run_id, "summaryMetrics"),
            },
        )
        counts["experiments"] += 1

        sha = (run.get("commit") or "").strip()
        if sha:
            commit_id = f"commit:{sha}"
            if db.get_node(conn, commit_id) is not None:
                db.upsert_edge(
                    conn, commit_id, experiment_id, "implements", extractor="wandb",
                    evidence={"run_id": run_id, "sha": sha}, confidence=1.0, status="auto",
                )
                counts["implements"] += 1
            else:
                logger.warning(
                    "run %s references commit %s not found in graph; skipping "
                    "implements edge (no placeholder commit created)", run_id, sha,
                )

        file_edges = ((run.get("files") or {}).get("edges")) or []
        for file_edge in file_edges:
            file_name = ((file_edge or {}).get("node") or {}).get("name", "")
            if not file_name or posixpath.splitext(file_name)[1].lower() not in _ARTIFACT_IMAGE_EXTENSIONS:
                continue
            basename = posixpath.basename(file_name)
            matches = figure_basenames.get(basename, [])
            if len(matches) != 1:
                logger.warning(
                    "run %s file %s matches %d figure nodes by basename; skipping "
                    "produces edge", run_id, basename, len(matches),
                )
                continue
            db.upsert_edge(
                conn, experiment_id, matches[0], "produces", extractor="wandb",
                evidence={"run_id": run_id, "file_name": file_name},
                confidence=1.0, status="auto",
            )
            produces_edges.add((experiment_id, matches[0]))

    counts["produces"] = len(produces_edges)
    return counts

def ingest_wandb_project(
    conn: Connection, entity: str, project: str,
    api_key: str | None = None, base_url: str = DEFAULT_BASE_URL,
) -> dict[str, int]:
    """Fetch + transform in one call -- what cli.py uses. WandbError
    propagates uncaught (cli.py wraps it into CliError, as with GitIngestError)."""
    runs = fetch_wandb_runs(entity, project, api_key=api_key, base_url=base_url)
    return transform_runs(conn, runs)
