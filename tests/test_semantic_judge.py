"""Tests for rce.semantic.judge (S2) and the `rce judge` CLI command.

Every backend interaction goes through _FakeBackend below -- no real network
call, no rce.semantic.backend.LlmBackend instantiation touching urllib. This
mirrors tests/test_semantic_backend.py's own policy: the suite stays closed
and reproducible in CI with no local model server running.

The seeded fixture (`_seed_pending_edge`) reproduces the real-world S2 false
positive that motivated this task: a claim printing "1.58" (a bit-width)
gets deterministically matched to an unrelated metric
(`grad_norm_epoch=1.5786`) purely by rounding coincidence, while the
claim's actual referent is a run *param* (`quantization: "1_58b"`) the
deterministic matcher never looks at.
"""

from __future__ import annotations

import logging

import pytest

from rce import cli, db
from rce.semantic import judge as semantic_judge
from rce.semantic.backend import LlmError

# in-memory migrated db (tests/conftest.py's `conn` fixture) is reused as-is.


class _FakeBackend:
    """Duck-types the surface rce.semantic.judge actually calls on
    rce.semantic.backend.LlmBackend: `.model`, `.probe()`, `.complete_json()`.
    `responses` is popped in call order; an Exception instance is raised
    instead of returned, simulating a real LlmError from the backend."""

    def __init__(self, responses=None, model="fake-model", probe_error=None):
        self.model = model
        self._responses = list(responses or [])
        self._probe_error = probe_error
        self.calls: list[dict] = []

    def probe(self):
        if self._probe_error is not None:
            raise self._probe_error
        return [self.model]

    def complete_json(self, system, user, schema, name="response"):
        self.calls.append({"system": system, "user": user, "schema": schema, "name": name})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _seed_pending_edge(
    conn,
    *,
    claim_id="claim:paper.tex#abc123",
    experiment_id="experiment:run_a",
    sentence="Our model uses 1.58-bit quantization.",
    claim_raw="1.58",
    matched_metric="grad_norm_epoch",
    matched_value=1.5786,
    params=None,
    metrics=None,
    candidate_count=1,
):
    """One pending claim --backed_by--> experiment edge, shaped exactly like
    rce.ingest.claims writes it."""
    db.upsert_node(
        conn, claim_id, "claim", title=sentence,
        attrs={"sentence": sentence, "raw": claim_raw, "printed_number": claim_raw, "value": 1.58},
    )
    db.upsert_node(
        conn, experiment_id, "experiment", title=experiment_id.split(":", 1)[1],
        attrs={
            "run_id": experiment_id.split(":", 1)[1],
            "params": params if params is not None else {"quantization": "1_58b", "lr": "0.001"},
            "metrics": metrics if metrics is not None else {
                matched_metric: matched_value, "grad_norm_step": 0.2464,
            },
        },
    )
    db.upsert_edge(
        conn, claim_id, experiment_id, "backed_by", extractor="claims",
        evidence={
            "file": "paper.tex", "line": 2, "metric": matched_metric, "metric_value": matched_value,
            "claim_raw": claim_raw, "claim_value": 1.58, "candidate_count": candidate_count,
        },
        confidence=1.0, status="pending",
    )
    return claim_id, experiment_id


def _sole_edge(conn, claim_id, experiment_id):
    return db.query_edges(conn, src=claim_id, dst=experiment_id, type="backed_by")[0]


# -- rce.semantic.judge.review_pending_backed_by ----------------------------


def test_normal_review_writes_semantic_review_and_leaves_status_pending(conn):
    claim_id, experiment_id = _seed_pending_edge(conn)
    backend = _FakeBackend(responses=[
        {
            "related": False,
            "reason": "Coincidental rounding; the real referent is the quantization param.",
            "better_match": "quantization",
        },
    ])

    result = semantic_judge.review_pending_backed_by(conn, backend, dry_run=False)

    assert result.total_pending == 1
    outcome = result.reviewed[0]
    assert outcome.written is True
    assert outcome.error is None
    assert outcome.related is False
    assert outcome.better_match == "quantization"
    assert outcome.hallucination_dropped is False

    edge = _sole_edge(conn, claim_id, experiment_id)
    assert edge["status"] == "pending"  # constitution: judge never writes status
    review = dict(edge["evidence"]["semantic_review"])
    reviewed_at = review.pop("reviewed_at", None)
    assert reviewed_at  # non-empty timestamp, checked separately from the rest
    assert review == {
        "related": False,
        "reason": "Coincidental rounding; the real referent is the quantization param.",
        "better_match": "quantization",
        "model": "fake-model",
        "run_id": experiment_id,
        # Attribution: which occurrence (metric/value) the model actually saw.
        "metric": "grad_norm_epoch",
        "metric_value": 1.5786,
    }
    # occurrences evidence (the deterministic match) is untouched
    assert edge["evidence"]["occurrences"][0]["metric"] == "grad_norm_epoch"


def test_hallucinated_better_match_is_dropped_by_verifier(conn, caplog):
    claim_id, experiment_id = _seed_pending_edge(conn)
    backend = _FakeBackend(responses=[
        {"related": False, "reason": "Looks unrelated.", "better_match": "totally_made_up_param"},
    ])

    with caplog.at_level(logging.WARNING):
        result = semantic_judge.review_pending_backed_by(conn, backend, dry_run=False)

    outcome = result.reviewed[0]
    assert outcome.hallucination_dropped is True
    assert outcome.better_match is None
    assert outcome.related is False  # related/reason survive; only better_match is dropped
    assert "hallucination" in caplog.text.lower()

    edge = _sole_edge(conn, claim_id, experiment_id)
    review = edge["evidence"]["semantic_review"]
    assert review["better_match"] is None
    assert review["related"] is False


def test_illegal_related_type_is_rejected_and_nothing_written(conn):
    claim_id, experiment_id = _seed_pending_edge(conn)
    backend = _FakeBackend(responses=[
        {"related": "yes", "reason": "related is a string, not a bool", "better_match": None},
    ])

    result = semantic_judge.review_pending_backed_by(conn, backend, dry_run=False)

    outcome = result.reviewed[0]
    assert outcome.written is False
    assert outcome.error is not None

    edge = _sole_edge(conn, claim_id, experiment_id)
    assert "semantic_review" not in edge["evidence"]
    assert edge["status"] == "pending"


def test_oversized_reason_is_truncated_not_discarded(conn, caplog):
    """A model that gets `related`/`better_match` right but writes a
    multi-sentence `reason` must not lose its whole judgement to
    `[error]` -- only `reason` is trimmed; `related`/`better_match` are
    validated and stored normally."""
    claim_id, experiment_id = _seed_pending_edge(conn)
    long_reason = "x" * 1000
    backend = _FakeBackend(responses=[
        {"related": True, "reason": long_reason, "better_match": None},
    ])

    with caplog.at_level(logging.WARNING):
        result = semantic_judge.review_pending_backed_by(conn, backend, dry_run=False, max_reason_chars=300)

    outcome = result.reviewed[0]
    assert outcome.written is True
    assert outcome.error is None
    assert outcome.related is True  # the model's actual judgement survives
    assert outcome.reason.endswith(" ...")
    assert len(outcome.reason) <= 300 + len(" ...")
    assert outcome.reason != long_reason  # genuinely truncated, not passed through
    assert "truncat" in caplog.text.lower()

    edge = _sole_edge(conn, claim_id, experiment_id)
    review = edge["evidence"]["semantic_review"]
    assert review["reason"] == outcome.reason
    assert review["related"] is True


def test_llm_error_on_one_edge_is_skipped_and_others_still_processed(conn):
    _seed_pending_edge(conn, claim_id="claim:paper.tex#one", experiment_id="experiment:run_a")
    _seed_pending_edge(
        conn, claim_id="claim:paper.tex#two", experiment_id="experiment:run_b", matched_metric="accuracy",
    )
    backend = _FakeBackend(responses=[
        LlmError("model server timed out"),
        {"related": True, "reason": "Matches cleanly.", "better_match": None},
    ])

    result = semantic_judge.review_pending_backed_by(conn, backend, dry_run=False)

    assert result.total_pending == 2
    errored = [o for o in result.reviewed if o.error]
    written = [o for o in result.reviewed if o.written]
    assert len(errored) == 1
    assert len(written) == 1
    assert "timed out" in errored[0].error


def test_limit_truncates_deterministically_ordered_queue(conn):
    _seed_pending_edge(conn, claim_id="claim:paper.tex#one", experiment_id="experiment:run_a")
    _seed_pending_edge(
        conn, claim_id="claim:paper.tex#two", experiment_id="experiment:run_b", matched_metric="accuracy",
    )
    backend = _FakeBackend(responses=[{"related": True, "reason": "ok", "better_match": None}])

    result = semantic_judge.review_pending_backed_by(conn, backend, limit=1, dry_run=False)

    assert result.total_pending == 2  # true total, unaffected by truncation
    assert len(result.reviewed) == 1
    assert len(backend.calls) == 1


def test_dry_run_calls_model_but_writes_nothing(conn):
    claim_id, experiment_id = _seed_pending_edge(conn)
    backend = _FakeBackend(responses=[
        {"related": False, "reason": "coincidence", "better_match": "quantization"},
    ])

    result = semantic_judge.review_pending_backed_by(conn, backend, dry_run=True)

    outcome = result.reviewed[0]
    assert outcome.written is False
    assert outcome.related is False  # still computed, for preview
    assert len(backend.calls) == 1  # the model WAS called

    edge = _sole_edge(conn, claim_id, experiment_id)
    assert "semantic_review" not in edge["evidence"]


def test_rerun_overwrites_semantic_review_without_disturbing_occurrences(conn):
    claim_id, experiment_id = _seed_pending_edge(conn)
    backend_1 = _FakeBackend(responses=[
        {"related": False, "reason": "first pass", "better_match": "quantization"},
    ])
    semantic_judge.review_pending_backed_by(conn, backend_1, dry_run=False)

    backend_2 = _FakeBackend(
        responses=[{"related": True, "reason": "second pass, corrected", "better_match": None}],
        model="fake-model-v2",
    )
    semantic_judge.review_pending_backed_by(conn, backend_2, dry_run=False)

    edge = _sole_edge(conn, claim_id, experiment_id)
    review = edge["evidence"]["semantic_review"]
    assert review["related"] is True
    assert review["reason"] == "second pass, corrected"
    assert review["model"] == "fake-model-v2"
    assert edge["evidence"]["occurrences"] == [
        {
            "file": "paper.tex", "line": 2, "metric": "grad_norm_epoch", "metric_value": 1.5786,
            "claim_raw": "1.58", "claim_value": 1.58, "candidate_count": 1,
        }
    ]
    assert edge["status"] == "pending"


def test_multi_occurrence_edge_attributes_review_and_logs_skipped_metrics(conn, caplog):
    """Opus-review blocker: when one experiment contributes two or more
    matching metrics to the same claim, rce.ingest.claims calls upsert_edge
    repeatedly on the SAME (src, dst, type, extractor) key, so they merge
    into ONE edge with several occurrences (DESIGN.md section 4 -- this is
    exactly why candidate_count counts (experiment, metric) pairs). The
    judge only ever hands the model the last occurrence; before this fix the
    resulting semantic_review recorded no metric at all (unattributed) and
    no log recorded which occurrence(s) were skipped, so a human could be
    pushed to reject a candidate whose real supporting metric was never
    reviewed."""
    claim_id = "claim:paper.tex#multi"
    experiment_id = "experiment:run_multi"
    db.upsert_node(
        conn, claim_id, "claim", title="Our run reaches 1.58.",
        attrs={"sentence": "Our run reaches 1.58.", "raw": "1.58", "printed_number": "1.58", "value": 1.58},
    )
    db.upsert_node(
        conn, experiment_id, "experiment", title="run_multi",
        attrs={
            "run_id": "run_multi",
            "params": {"quantization": "1_58b"},
            "metrics": {"grad_norm_epoch": 1.58, "val_ratio": 1.58},
        },
    )
    # Two matches for the SAME (claim, experiment) pair collapse onto one
    # edge with two occurrences, exactly as rce.ingest.claims produces them.
    for metric_name, metric_value in (("grad_norm_epoch", 1.58), ("val_ratio", 1.58)):
        db.upsert_edge(
            conn, claim_id, experiment_id, "backed_by", extractor="claims",
            evidence={
                "file": "paper.tex", "line": 2, "metric": metric_name, "metric_value": metric_value,
                "claim_raw": "1.58", "claim_value": 1.58, "candidate_count": 2,
            },
            confidence=1.0, status="pending",
        )

    backend = _FakeBackend(responses=[
        {"related": True, "reason": "matches val_ratio", "better_match": None},
    ])

    with caplog.at_level(logging.WARNING):
        result = semantic_judge.review_pending_backed_by(conn, backend, dry_run=False)

    outcome = result.reviewed[0]
    assert outcome.written is True

    edge = _sole_edge(conn, claim_id, experiment_id)
    review = edge["evidence"]["semantic_review"]
    # Attribution: the reviewed occurrence's metric/value is now recorded,
    # so the verdict is traceable to exactly what the model saw.
    assert review["metric"] == "val_ratio"
    assert review["metric_value"] == 1.58
    # The skipped occurrence (grad_norm_epoch) is logged, never silent.
    assert "grad_norm_epoch" in caplog.text
    assert "skip" in caplog.text.lower()


def test_single_occurrence_edge_logs_nothing_about_skipped_metrics(conn, caplog):
    # Sanity counterpart: the common case (one occurrence) must not log a
    # spurious "skipped" warning.
    _seed_pending_edge(conn)
    backend = _FakeBackend(responses=[{"related": True, "reason": "ok", "better_match": None}])

    with caplog.at_level(logging.WARNING):
        semantic_judge.review_pending_backed_by(conn, backend, dry_run=False)

    assert "skip" not in caplog.text.lower()


def test_same_metric_occurrences_at_different_lines_log_no_self_contradictory_skip(conn, caplog):
    """Regression (blocker fix): before this fix, `_log_skipped_occurrences`
    compared occurrences by Python object identity, not by metric name. Two
    occurrence dicts for the exact same (metric, value) match but recorded
    at different source lines -- exactly what rce.ingest.claims used to
    write when an unrelated edit shifted the claim's line (see the sibling
    bug in rce.ingest.claims / tests/test_ingest_claims.py) -- are different
    objects even though they name the same metric. The old identity check
    therefore warned "reviewing only metric='accuracy' ... skipping
    unreviewed metric(s) ['accuracy']": the exact same metric name reported
    as both reviewed and skipped in the same sentence. Deduping by metric
    name means this case must log nothing at all -- there is no metric that
    was genuinely left unreviewed."""
    claim_id = "claim:paper.tex#dupe"
    experiment_id = "experiment:run_dupe"
    db.upsert_node(
        conn, claim_id, "claim", title="We reach 87.3.",
        attrs={"sentence": "We reach 87.3.", "raw": "87.3", "printed_number": "87.3", "value": 87.3},
    )
    db.upsert_node(
        conn, experiment_id, "experiment", title="run_dupe",
        attrs={"run_id": "run_dupe", "params": {}, "metrics": {"accuracy": 87.3}},
    )
    # Same (metric, value) match recorded twice at different lines -- the
    # shape rce.ingest.claims produced before its own line-shift fix.
    for line in (2, 4):
        db.upsert_edge(
            conn, claim_id, experiment_id, "backed_by", extractor="claims",
            evidence={
                "file": "paper.tex", "line": line, "metric": "accuracy", "metric_value": 87.3,
                "claim_raw": "87.3", "claim_value": 87.3, "candidate_count": 1,
            },
            confidence=1.0, status="pending",
        )
    edge = _sole_edge(conn, claim_id, experiment_id)
    assert len(edge["evidence"]["occurrences"]) == 2  # sanity: still two distinct occurrence dicts

    backend = _FakeBackend(responses=[{"related": True, "reason": "matches", "better_match": None}])

    with caplog.at_level(logging.WARNING):
        result = semantic_judge.review_pending_backed_by(conn, backend, dry_run=False)

    assert result.reviewed[0].written is True
    assert "skip" not in caplog.text.lower()  # no self-contradictory "skipped metric X" for X == reviewed


def test_judge_never_calls_set_edge_status(conn, monkeypatch):
    """Constitutional gate: the machine write path structurally cannot move
    an edge to confirmed/rejected. This spies on db.set_edge_status itself
    -- if rce.semantic.judge ever called it (on the normal, hallucination,
    or backend-error path), this test fails regardless of what value would
    have been passed."""

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("rce.semantic.judge must never call db.set_edge_status")

    monkeypatch.setattr(db, "set_edge_status", _must_not_be_called)

    _seed_pending_edge(conn, claim_id="claim:paper.tex#one", experiment_id="experiment:run_a")
    _seed_pending_edge(
        conn, claim_id="claim:paper.tex#two", experiment_id="experiment:run_b", matched_metric="accuracy",
    )
    backend = _FakeBackend(responses=[
        {"related": False, "reason": "unrelated", "better_match": "made_up_name"},  # hallucination path
        LlmError("boom"),  # backend-error path
    ])

    result = semantic_judge.review_pending_backed_by(conn, backend, dry_run=False)

    assert len(result.reviewed) == 2
    for edge in db.query_edges(conn, type="backed_by"):
        assert edge["status"] == "pending"


# -- `rce judge` CLI wiring --------------------------------------------------


def test_cli_judge_writes_annotations_and_status_pending_surfaces_them(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    assert cli.main(["init", str(project)]) == 0
    capsys.readouterr()

    conn = db.connect(project / ".rce" / "graph.db")
    try:
        _seed_pending_edge(conn)
    finally:
        conn.close()

    monkeypatch.setattr(
        cli.semantic_backend, "LlmBackend",
        lambda *a, **kw: _FakeBackend(responses=[
            {"related": False, "reason": "Coincidental rounding.", "better_match": "quantization"},
        ]),
    )

    assert cli.main(["judge", "--path", str(project)]) == 0
    out = capsys.readouterr().out
    assert "reviewed=1 written=1 errors=0" in out
    assert "better_match='quantization'" in out

    # status --pending surfaces the model's verdict so a human sees which
    # candidate to look at first.
    assert cli.main(["status", "--path", str(project), "--pending"]) == 0
    out = capsys.readouterr().out
    assert "semantic_review" in out
    assert "FLAGGED" in out  # related=False
    # A pending edge can have several (experiment, metric) candidate pairs
    # (see candidate_count); the display must say which metric this
    # particular verdict is about, not just "some match was reviewed".
    assert "metric='grad_norm_epoch'" in out


def test_cli_judge_dry_run_flag_writes_nothing(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    cli.main(["init", str(project)])
    capsys.readouterr()
    conn = db.connect(project / ".rce" / "graph.db")
    try:
        _seed_pending_edge(conn)
    finally:
        conn.close()

    monkeypatch.setattr(
        cli.semantic_backend, "LlmBackend",
        lambda *a, **kw: _FakeBackend(responses=[{"related": True, "reason": "ok", "better_match": None}]),
    )

    assert cli.main(["judge", "--path", str(project), "--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out.lower()

    conn = db.connect(project / ".rce" / "graph.db")
    try:
        edge = db.pending_edges(conn)[0]
        assert "semantic_review" not in edge["evidence"]
    finally:
        conn.close()


def test_cli_judge_backend_unavailable_reports_clear_error_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    cli.main(["init", str(project)])
    capsys.readouterr()

    monkeypatch.setattr(
        cli.semantic_backend, "LlmBackend",
        lambda *a, **kw: _FakeBackend(probe_error=LlmError("could not reach an LLM server at http://x")),
    )

    assert cli.main(["judge", "--path", str(project)]) == 1
    err = capsys.readouterr().err
    assert "Error" in err and "semantic backend unavailable" in err

    # `judge` failing must never affect any other subcommand.
    assert cli.main(["status", "--path", str(project)]) == 0
