"""Tests for rce.ingest.wandb (T8). Transform layer uses fixture JSON only;
fetch-layer error paths are tested by monkeypatching urllib.request.urlopen
-- no real network call is ever made, per Owner's design.
"""

import json
import urllib.error

import pytest

from rce import db
from rce.ingest import wandb as wandb_ingest

def _run_fixture(
    run_id: str = "run_a", sha: str = "c" * 40, file_name: str = "overview.png", **overrides
) -> dict:
    """A well-formed raw GraphQL run node, shaped like _RUNS_QUERY's selection."""
    fixture = {
        "name": run_id,
        "displayName": "golden-run",
        "state": "finished",
        "config": json.dumps({"lr": 0.01, "big_table": {"_type": "table-file", "data": ["x"] * 500}}),
        "summaryMetrics": json.dumps({"accuracy": 0.87}),
        "commit": sha,
        "tags": ["baseline"],
        "notes": "first pass",
        "createdAt": "2026-01-01T00:00:00Z",
        "files": {"edges": [{"node": {"name": file_name}}]},
    }
    fixture.update(overrides)
    return fixture

# -- transform layer: fixture JSON only, no network --

def test_transform_creates_node_and_both_edges_strips_media_blob(conn):
    sha = "c" * 40
    db.upsert_node(conn, f"commit:{sha}", "commit", title="c")
    db.upsert_node(conn, "figure:overview.png", "figure", title="overview.png")

    counts = wandb_ingest.transform_runs(conn, [_run_fixture(sha=sha)])

    assert counts == {"experiments": 1, "implements": 1, "produces": 1}
    node = db.get_node(conn, "experiment:run_a")
    assert node["type"] == "experiment" and node["title"] == "golden-run"
    assert node["attrs"]["config"]["lr"] == 0.01
    assert node["attrs"]["config"]["big_table"] == {"_type": "table-file", "_stripped": True}
    assert node["attrs"]["summary_metrics"] == {"accuracy": 0.87}

    implements = db.query_edges(conn, src=f"commit:{sha}", dst="experiment:run_a", type="implements")
    assert len(implements) == 1
    edge = implements[0]
    assert edge["extractor"] == "wandb" and edge["confidence"] == 1.0 and edge["status"] == "auto"
    # (T10) db.upsert_edge now wraps evidence as {"occurrences": [...]}.
    assert edge["evidence"] == {"occurrences": [{"run_id": "run_a", "sha": sha}]}

    produces = db.query_edges(conn, src="experiment:run_a", dst="figure:overview.png", type="produces")
    assert len(produces) == 1
    assert produces[0]["evidence"] == {"occurrences": [{"run_id": "run_a", "file_name": "overview.png"}]}

def test_transform_conservatively_skips_unresolvable_connectors(conn):
    fake_sha = "d" * 40  # never ingested as a commit: node
    db.upsert_node(conn, "figure:a/overview.png", "figure", title="a/overview.png")
    db.upsert_node(conn, "figure:b/overview.png", "figure", title="b/overview.png")  # ambiguous basename

    counts = wandb_ingest.transform_runs(conn, [_run_fixture(run_id="run_b", sha=fake_sha)])

    assert counts["implements"] == 0 and counts["produces"] == 0
    assert db.get_node(conn, f"commit:{fake_sha}") is None  # never a placeholder
    assert db.query_edges(conn, type="implements") == []
    assert db.query_edges(conn, type="produces") == []

def test_transform_is_idempotent(conn):
    sha = "c" * 40
    db.upsert_node(conn, f"commit:{sha}", "commit", title="c")
    db.upsert_node(conn, "figure:overview.png", "figure", title="overview.png")

    first = wandb_ingest.transform_runs(conn, [_run_fixture(sha=sha)])
    second = wandb_ingest.transform_runs(conn, [_run_fixture(sha=sha)])

    assert first == second == {"experiments": 1, "implements": 1, "produces": 1}
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE type='implements'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE type='produces'").fetchone()[0] == 1

def test_transform_skips_run_with_no_name(conn, caplog):
    run = _run_fixture()
    del run["name"]

    with caplog.at_level("WARNING", logger="rce.ingest.wandb"):
        counts = wandb_ingest.transform_runs(conn, [run])

    assert counts == {"experiments": 0, "implements": 0, "produces": 0}
    assert any("no 'name'" in r.message for r in caplog.records)

def test_transform_unparseable_config_json_skips_field_not_whole_run(conn, caplog):
    run = _run_fixture(config="{not valid json")

    with caplog.at_level("WARNING", logger="rce.ingest.wandb"):
        counts = wandb_ingest.transform_runs(conn, [run])

    assert counts["experiments"] == 1  # run itself still ingested
    node = db.get_node(conn, "experiment:run_a")
    assert node["attrs"]["config"] == {}  # unparseable field dropped, not guessed
    assert any("unparseable config JSON" in r.message for r in caplog.records)

# -- fetch layer: error paths only, via monkeypatched urlopen -- never real network --

def test_fetch_requires_api_key_before_any_network_call(monkeypatch):
    monkeypatch.delenv("WANDB_API_KEY", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("must not attempt a network call with no API key")

    monkeypatch.setattr(wandb_ingest.urllib.request, "urlopen", _boom)
    with pytest.raises(wandb_ingest.WandbError, match="WANDB_API_KEY"):
        wandb_ingest.fetch_wandb_runs("acme", "proj", api_key=None)

@pytest.mark.parametrize(
    "make_exc, match",
    [
        (lambda req: urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs=None, fp=None), "401"),
        (lambda req: urllib.error.URLError("simulated DNS failure"), "could not reach"),
    ],
)
def test_fetch_wraps_http_and_network_errors(monkeypatch, make_exc, match):
    def _raise(request, timeout=None):
        raise make_exc(request)

    monkeypatch.setattr(wandb_ingest.urllib.request, "urlopen", _raise)
    with pytest.raises(wandb_ingest.WandbError, match=match):
        wandb_ingest.fetch_wandb_runs("acme", "proj", api_key="a-key")
