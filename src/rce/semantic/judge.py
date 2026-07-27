"""S2: the semantic judge -- reviews deterministic `claim --backed_by-->
experiment` candidates (rce.ingest.claims, DESIGN.md section 5 connector 7)
and annotates each with a model's opinion on whether the match is
meaningful or a numeric coincidence.

Real-world motivation (owner's own repository, sampled by hand): a claim
printing "1.58" (a bit-width) matched `grad_norm_epoch=1.5786` purely
because both round to the same value; a claim printing "25%" (a
quantization ratio) matched `grad_norm_step=0.2464` the same way. Neither
match is what the claim is actually about -- the genuine referent for the
"1.58" claim was a run *param* (`quantization=1_58b`), not any metric at
all. That is why `_gather_run_context` below hands the model the run's
full params + metric names, not just the one value the deterministic
matcher happened to hit: the correct answer can live somewhere the
deterministic layer never looked.

CONSTITUTIONAL CONSTRAINT (DESIGN.md section 2/4, "humans own judgement" --
this is the hard line the whole module exists to respect): the machine
write path may only ever produce `status` in {auto, pending}
(`rce.db._MACHINE_EDGE_STATUSES`); confirming or rejecting an edge is a
human-only write via `db.set_edge_status`. A local model is exactly as much
a "machine" as the deterministic extractors are -- it does not get a
special exemption. Concretely:

  * This module never imports or calls `db.set_edge_status`.
  * It never calls `db.upsert_edge` either (that would also require picking
    a status, and would fold the model's opinion into the candidate's
    *evidence occurrences*, muddying "what was actually observed" with
    "what a model guessed about it").
  * Every write goes through `db.set_edge_semantic_review`, which touches
    only the `evidence.semantic_review` sub-object and nothing else on the
    row -- `status` and `confidence` are structurally untouched by it.
  * A pending edge reviewed here is still pending after review. The model
    annotates; a human (via `rce confirm`) or the existing confirmation
    queue is what ever moves it to confirmed/rejected.

Backend-agnostic: `review_pending_backed_by` takes any object exposing
`.probe()`/`.complete_json()`/`.model` with `rce.semantic.backend.LlmBackend`'s
signatures (duck-typed deliberately, so tests substitute a stub with no
network involved -- see tests/test_semantic_judge.py). This module itself
does no I/O beyond calling that object and rce.db.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from sqlite3 import Connection
from typing import Any

from rce import db
from rce.semantic.backend import LlmError

logger = logging.getLogger(__name__)

# Where mlflow/wandb park hyperparameters vs. numeric metrics on an
# `experiment` node's attrs (see rce.ingest.mlflow.ingest_mlflow_dir /
# rce.ingest.wandb.transform_runs). Metric keys mirror
# rce.ingest.claims._METRIC_ATTR_KEYS -- duplicated rather than imported
# (that name is private to its module; Occam rule 5 accepts a one-line
# duplication over reaching into another module's underscored internals).
_PARAM_ATTR_KEYS = ("params", "config")
_METRIC_ATTR_KEYS = ("metrics", "summary_metrics")

# Sanity cap on the model's "reason" field -- it was asked for one sentence,
# so a multi-paragraph reply is treated as a malformed response rather than
# accepted verbatim. This is a validation-gate threshold the judge sets
# itself (not tuned against any dataset), so it is exposed as a keyword
# argument rather than hard-coded, and is called out in this task's
# owner_decisions_needed rather than presented as a settled standard.
_DEFAULT_MAX_REASON_CHARS = 300

# How many decimal places a metric VALUE is rounded to for prompt display
# (task requirement: "全部 metrics 名称，值可截断"). Every metric NAME is
# still sent in full -- this only trims the value's precision to keep the
# prompt compact; it is not a matching/scoring decision (mirrors
# rce.ingest.claims._MAX_SENTENCE_LEN's "display cap only" framing).
_METRIC_DISPLAY_NDIGITS = 6

# Minimal local JSON-Schema passed to LlmBackend.complete_json. `related`
# and `reason` get real type constraints; `better_match` deliberately has no
# "type" key. rce.semantic.backend's minimal validator
# (_check_type/_validate) only supports a single `type` string, not a
# JSON-Schema union like ["string", "null"] -- passing one would crash
# `_check_type`'s dict lookup on an unhashable list. Omitting "type" makes
# that property structurally unconstrained (accepts a JSON null OR a JSON
# string OR anything else a model might emit), so this module's own
# `_validate_and_sanitize` is what actually enforces "a real param/metric
# name on this experiment, or null" -- structural JSON-Schema validation
# could not express that check anyway, since it needs the run's own attrs,
# which are not available to rce.semantic.backend.
RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["related", "reason", "better_match"],
    "properties": {
        "related": {"type": "boolean"},
        "reason": {"type": "string"},
        "better_match": {},
    },
}

_SYSTEM_PROMPT = (
    "You are a careful reviewer for a research-provenance graph. You are shown one "
    "quantitative claim from a paper and one experiment metric that a deterministic tool "
    "already matched to it purely because both round to the same numeric value. Your only "
    "job is to annotate whether that match is semantically meaningful or a numeric "
    "coincidence -- you never approve or reject anything; a human always makes that call "
    "separately. If the match looks coincidental, you may name a better-fitting param or "
    "metric, but ONLY if it is literally present in the 'params' or 'metrics' lists you are "
    "given for that same experiment run -- never invent a name that is not in those lists. "
    "Reply with exactly one JSON object and nothing else: "
    '{"related": <bool>, "reason": "<one sentence>", "better_match": "<name from the lists '
    'above, or null>"}.'
)


class _InvalidJudgeResponse(Exception):
    """Internal only: the model's reply passed rce.semantic.backend's JSON-Schema
    check but still fails this module's own sanity checks (`related` not
    literally a bool, `reason` empty/oversized). The edge is skipped --
    nothing is written for it -- see review_pending_backed_by."""


@dataclass
class EdgeReview:
    """One pending backed_by edge's judge outcome, for CLI display and tests."""

    src: str
    dst: str
    related: bool | None = None
    reason: str | None = None
    better_match: str | None = None
    hallucination_dropped: bool = False
    written: bool = False
    error: str | None = None


@dataclass
class JudgeRunResult:
    total_pending: int
    reviewed: list[EdgeReview] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ordered_pending_backed_by(conn: Connection) -> list[dict[str, Any]]:
    """Every pending `backed_by` edge, sorted deterministically.

    Mirrors rce.cli._ordered_edges's sort key exactly (same fields, same
    order) so a `--limit` here truncates the same "first N" a human would
    see from `rce status --pending`. Re-implemented rather than imported:
    rce.cli already imports this package, so importing back from rce.cli
    would be a circular import.
    """
    pending = [e for e in db.query_edges(conn, status="pending") if e["type"] == "backed_by"]
    return sorted(pending, key=lambda e: (e["src"], e["dst"], e["type"], e["extractor"]))


def _primary_occurrence(evidence: dict[str, Any]) -> dict[str, Any]:
    """The most recently written occurrence dict on an edge's evidence (the
    `metric`/`metric_value`/`claim_raw` fields rce.ingest.claims records).
    Falls back to treating `evidence` itself as the occurrence for a
    pre-T10 legacy bare-evidence row -- same convention as
    rce.cli._format_evidence_summary."""
    occurrences = evidence.get("occurrences")
    if isinstance(occurrences, list) and occurrences:
        return occurrences[-1]
    return dict(evidence) if isinstance(evidence, dict) else {}


def _round_for_display(value: Any, ndigits: int = _METRIC_DISPLAY_NDIGITS) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), ndigits)
    return value


def _gather_run_context(experiment_node: dict[str, Any]) -> dict[str, Any]:
    """The full experiment context the model needs (task requirement: "该
    experiment 的完整上下文 -- run 名、全部 params、全部 metrics 名称"):
    the run's title/id, every param (mlflow's `params` / wandb's `config`),
    and every numeric metric (mlflow's `metrics` / wandb's `summary_metrics`
    -- same attrs keys and same numeric-only filter rce.ingest.claims uses
    to build its own match candidates, so "what the judge can see" and
    "what the deterministic layer matched against" never silently diverge).
    """
    attrs = experiment_node.get("attrs") or {}
    params: dict[str, Any] = {}
    for key in _PARAM_ATTR_KEYS:
        value = attrs.get(key)
        if isinstance(value, dict):
            params.update(value)
    metrics: dict[str, float] = {}
    for key in _METRIC_ATTR_KEYS:
        value = attrs.get(key)
        if isinstance(value, dict):
            for name, val in value.items():
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    continue  # non-numeric metric value -- not comparable, not sent
                metrics[name] = float(val)
    title = experiment_node.get("title") or attrs.get("run_id") or experiment_node["id"]
    return {"run_id": experiment_node["id"], "title": title, "params": params, "metrics": metrics}


def _valid_match_names(context: dict[str, Any]) -> set[str]:
    """Every name the model is allowed to name in `better_match` -- the
    verifier's whitelist. Anything else is a hallucination by definition,
    regardless of how plausible it sounds."""
    return set(context["params"]) | set(context["metrics"])


def _build_prompt(
    claim_node: dict[str, Any], occurrence: dict[str, Any], context: dict[str, Any]
) -> str:
    """The user-turn prompt: (a) the claim's full sentence and its printed
    number, (b) the metric the deterministic layer matched it to, (c) the
    full run context from `_gather_run_context` (task requirement 2)."""
    attrs = claim_node.get("attrs") or {}
    sentence = attrs.get("sentence") or claim_node.get("title") or ""
    printed_number = occurrence.get("claim_raw") or attrs.get("raw") or attrs.get("printed_number")
    matched_metric = occurrence.get("metric", "<unknown>")
    matched_value = occurrence.get("metric_value")
    candidate_count = occurrence.get("candidate_count")
    rounded_metrics = {
        name: _round_for_display(val) for name, val in sorted(context["metrics"].items())
    }

    return (
        f'Claim (from the paper): "{sentence}"\n'
        f"Printed number in that claim: {printed_number}\n\n"
        f"The deterministic matcher found this metric rounds to the same value:\n"
        f"  metric: {matched_metric} = {matched_value}\n"
        f"  (this claim matched {candidate_count} candidate(s) in total across all experiments)\n\n"
        f"Full context of the experiment run this metric came from -- use this to judge "
        f"whether the match above is meaningful, or whether something else in this SAME run "
        f"is a better fit:\n"
        f"  run: {context['title']} ({context['run_id']})\n"
        f"  params: {context['params']!r}\n"
        f"  metrics (values rounded for brevity): {rounded_metrics!r}\n\n"
        "Reply with the JSON object described in the system prompt."
    )


def _validate_and_sanitize(
    instance: dict[str, Any], valid_names: set[str], max_reason_chars: int
) -> tuple[bool, str, str | None, bool]:
    """The constitutionally-required verifier (task requirement 3): even
    after rce.semantic.backend's own JSON-Schema check, `better_match` must
    be independently confirmed against the run's *actual* param/metric
    names -- schema validation alone cannot know those, since they are
    specific to the experiment being reviewed, not part of the static
    schema. Returns (related, reason, better_match, hallucination_dropped);
    raises _InvalidJudgeResponse if `related`/`reason` themselves don't pass
    sanity checks, in which case nothing about this response is trusted.
    """
    related = instance.get("related")
    if not isinstance(related, bool):
        raise _InvalidJudgeResponse(f"'related' is not a boolean: {related!r}")

    reason = instance.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise _InvalidJudgeResponse(f"'reason' is not a non-empty string: {reason!r}")
    reason = reason.strip()
    if len(reason) > max_reason_chars:
        raise _InvalidJudgeResponse(
            f"'reason' is {len(reason)} chars, over the {max_reason_chars}-char sanity cap for "
            "a one-sentence annotation"
        )

    better_match = instance.get("better_match")
    hallucination_dropped = False
    if better_match is not None:
        if not isinstance(better_match, str) or better_match not in valid_names:
            logger.warning(
                "judge: model's better_match %r is not a real param/metric name on this "
                "experiment run (valid names: %s) -- dropping it as a hallucination; "
                "related/reason are kept",
                better_match, sorted(valid_names),
            )
            better_match = None
            hallucination_dropped = True

    return related, reason, better_match, hallucination_dropped


def review_pending_backed_by(
    conn: Connection,
    backend: Any,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    max_reason_chars: int = _DEFAULT_MAX_REASON_CHARS,
) -> JudgeRunResult:
    """Review every pending `claim --backed_by--> experiment` candidate
    (task requirement 1), writing each model opinion into
    `evidence.semantic_review` via `db.set_edge_semantic_review` -- never
    `db.upsert_edge`/`db.set_edge_status` (see module docstring).

    `backend` is duck-typed to `rce.semantic.backend.LlmBackend`'s
    `.complete_json(system, user, schema, name=...)` / `.model` surface --
    tests substitute a stub, no network involved. Health-checking the
    backend (so "the server isn't running" fails fast with one clear
    message instead of once per edge) is the caller's job, e.g.
    `rce.cli.cmd_judge` calls `backend.probe()` before this function runs at
    all; this function itself does not probe.

    `limit` truncates the deterministically-sorted pending queue (see
    `_ordered_pending_backed_by`) to at most that many edges; `dry_run`
    still calls the model and runs the verifier (so a user can preview what
    would be written) but skips the `db.set_edge_semantic_review` write
    entirely.

    A backend/validation failure on one edge (LlmError from
    `complete_json`, or this module's own `_InvalidJudgeResponse`) is
    recorded on that edge's `EdgeReview.error` and does not stop the run --
    every other pending edge is still reviewed. Each edge review is
    independent and unrelated model errors on one candidate must not deny
    review to the rest of the queue.
    """
    pending = _ordered_pending_backed_by(conn)
    total_pending = len(pending)
    selected = pending if limit is None else pending[:limit]

    reviewed: list[EdgeReview] = []
    for edge in selected:
        outcome = EdgeReview(src=edge["src"], dst=edge["dst"])
        claim_node = db.get_node(conn, edge["src"])
        experiment_node = db.get_node(conn, edge["dst"])
        if claim_node is None or experiment_node is None:
            outcome.error = (
                f"edge endpoint(s) missing from graph (claim found={claim_node is not None}, "
                f"experiment found={experiment_node is not None})"
            )
            reviewed.append(outcome)
            continue

        context = _gather_run_context(experiment_node)
        occurrence = _primary_occurrence(edge["evidence"])
        user_prompt = _build_prompt(claim_node, occurrence, context)

        try:
            instance = backend.complete_json(
                _SYSTEM_PROMPT, user_prompt, RESPONSE_JSON_SCHEMA, name="semantic_review"
            )
        except LlmError as exc:
            logger.warning(
                "judge: backend error reviewing %s --backed_by--> %s: %s", edge["src"], edge["dst"], exc
            )
            outcome.error = str(exc)
            reviewed.append(outcome)
            continue

        try:
            related, reason, better_match, hallucination_dropped = _validate_and_sanitize(
                instance, _valid_match_names(context), max_reason_chars
            )
        except _InvalidJudgeResponse as exc:
            logger.warning(
                "judge: rejecting malformed response for %s --backed_by--> %s: %s",
                edge["src"], edge["dst"], exc,
            )
            outcome.error = str(exc)
            reviewed.append(outcome)
            continue

        outcome.related = related
        outcome.reason = reason
        outcome.better_match = better_match
        outcome.hallucination_dropped = hallucination_dropped

        if not dry_run:
            semantic_review = {
                "related": related,
                "reason": reason,
                "better_match": better_match,
                "model": getattr(backend, "model", None),
                "reviewed_at": _now_iso(),
                "run_id": edge["dst"],
            }
            db.set_edge_semantic_review(conn, edge["src"], edge["dst"], edge["type"], edge["extractor"], semantic_review)
            outcome.written = True

        reviewed.append(outcome)

    return JudgeRunResult(total_pending=total_pending, reviewed=reviewed)
