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


# -- T11: shared IMAGE_EXTENSIONS + produces counted by unique edge, not by file --


def test_jpeg_file_recognized_via_shared_image_extensions(conn):
    # Regression guard: _ARTIFACT_IMAGE_EXTENSIONS now reuses
    # git.IMAGE_EXTENSIONS, which includes .jpeg -- the old local frozenset
    # here did not, so a .jpeg run file could never match a figure: node.
    db.upsert_node(conn, "figure:chart.jpeg", "figure", title="chart.jpeg")
    run = _run_fixture(file_name="chart.jpeg")
    run["commit"] = ""  # isolate this test to the produces path

    counts = wandb_ingest.transform_runs(conn, [run])

    assert counts["produces"] == 1
    produces = db.query_edges(conn, src="experiment:run_a", dst="figure:chart.jpeg", type="produces")
    assert len(produces) == 1


def test_produces_count_is_not_inflated_by_duplicate_basename_files(conn):
    # Two run files with the same basename (different directories) both
    # resolve to the same single figure match, so both upsert the *same*
    # (experiment, figure) edge -- must count as 1, not 2.
    db.upsert_node(conn, "figure:overview.png", "figure", title="overview.png")
    run = _run_fixture()
    run["commit"] = ""  # isolate this test to the produces path
    run["files"] = {
        "edges": [
            {"node": {"name": "overview.png"}},
            {"node": {"name": "checkpoint_1/overview.png"}},
        ]
    }

    counts = wandb_ingest.transform_runs(conn, [run])

    assert counts == {"experiments": 1, "implements": 0, "produces": 1}
    produces = db.query_edges(conn, src="experiment:run_a", dst="figure:overview.png", type="produces")
    assert len(produces) == 1
    assert len(produces[0]["evidence"]["occurrences"]) == 2  # both files still recorded as evidence

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


# -- T11: fetch-layer structural validation -- malformed shape -> WandbError, never KeyError --


class _FakeHttpResponse:
    """Minimal stand-in for the context manager urlopen() returns."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


def _fake_urlopen(payloads: list[dict]):
    pages = list(payloads)
    return lambda request, timeout=None: _FakeHttpResponse(pages.pop(0))


def test_fetch_paginates_across_pages_with_well_formed_responses(monkeypatch):
    page1 = {"data": {"project": {"runs": {
        "edges": [{"node": {"name": "run_a"}}],
        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
    }}}}
    page2 = {"data": {"project": {"runs": {
        "edges": [{"node": {"name": "run_b"}}],
        "pageInfo": {"hasNextPage": False},
    }}}}
    monkeypatch.setattr(wandb_ingest.urllib.request, "urlopen", _fake_urlopen([page1, page2]))

    runs = wandb_ingest.fetch_wandb_runs("acme", "proj", api_key="a-key")
    assert [r["name"] for r in runs] == ["run_a", "run_b"]


@pytest.mark.parametrize(
    "project_data, match",
    [
        ({}, "runs"),  # 'runs' connection missing entirely
        ({"runs": {"pageInfo": {"hasNextPage": False}}}, "runs.edges"),  # 'edges' missing
        ({"runs": {"edges": [{}], "pageInfo": {"hasNextPage": False}}}, "node"),  # edge missing 'node'
        ({"runs": {"edges": []}}, "runs.pageInfo"),  # 'pageInfo' missing
        ({"runs": {"edges": [], "pageInfo": {"hasNextPage": True}}}, "endCursor"),  # hasNextPage w/o endCursor
    ],
)
def test_fetch_raises_wandberror_not_keyerror_on_malformed_shape(monkeypatch, project_data, match):
    payload = {"data": {"project": project_data}}
    monkeypatch.setattr(wandb_ingest.urllib.request, "urlopen", _fake_urlopen([payload]))

    with pytest.raises(wandb_ingest.WandbError, match=match):
        wandb_ingest.fetch_wandb_runs("acme", "proj", api_key="a-key")
