# RCE — Research Context Engine

[![tests](https://github.com/LinyingL/research-context-engine/actions/workflows/tests.yml/badge.svg)](https://github.com/LinyingL/research-context-engine/actions/workflows/tests.yml)

A **local-first** provenance engine for research projects. It reads your
existing git repo, LaTeX/.bib sources, and W&B/MLflow run history, and builds
a queryable evidence graph that answers one question: *where did this result
come from?* No cloud, no accounts, no lock-in — everything lives in one
SQLite file inside your project (`.rce/graph.db`).

## Quick start

```bash
pip install -e .            # zero required third-party dependencies
rce init /path/to/project   # creates .rce/graph.db
rce ingest /path/to/project [--mlruns DIR] [--wandb entity/project]
rce trace figure:overview.png --path /path/to/project --hops 4
```

None of the above touch any AI client — `rce` is a complete, useful tool on
its own from the command line.

- `rce init <path>` — initialize the graph database for a project.
- `rce ingest <path>` — scan git history, LaTeX/.bib sources, and
  (optionally) MLflow/W&B runs into the graph.
- `rce query <node-id> [--path P]` — show one node and its immediate,
  single-hop edges, with evidence.
- `rce trace <node-id> [--path P] [--hops N] [--json]` — walk the multi-hop
  provenance chain outward from a node (default 4 hops, must be >= 1);
  `--json` for scripting.
- `rce status [--path P] [--pending] [--limit N]` — whole-graph node/edge
  counts and the pending confirmation queue's size; `--pending` also lists
  each pending edge (src/dst/type/extractor/confidence/evidence), so it can
  be reviewed and acted on without the optional `mcp` extra.
- `rce confirm <src> <dst> <type> <extractor> --status confirmed|rejected`
  (or `rce confirm --index N --status ...` against the queue `--pending`
  just showed) — confirm or reject one pending edge; the baseline
  counterpart to the optional MCP server's `rce_confirm_edge` tool.
- `rce judge [--path P] [--limit N] [--dry-run]` — optional semantic layer:
  a local model reviews pending `backed_by` candidates and annotates each
  with its opinion (`evidence.semantic_review`). It never confirms or
  rejects anything — see "Design principles" below.

`--path` defaults to `.` for `query`/`trace`/`status`/`confirm`, so they
also work by `cd`-ing into the project root first and dropping `--path`
entirely.

## Optional: MCP

`rce mcp` starts an MCP (Model Context Protocol) stdio server exposing the
same graph — `rce_trace`, `rce_find`, `rce_status`, `rce_confirm_edge` — to
any MCP-capable client, including open-source and locally hosted-model
clients, not just one particular vendor's assistant. It is one of several
ways to consume the graph, not a requirement; install it separately with:

```bash
pip install "rce[mcp]"
```

Everything above works without it.

## Design principles

- **Deterministic first.** Git/LaTeX/.bib/run-log parsing is plain code
  (AST, regex, standard APIs) with zero model calls. `Claim` nodes and
  `backed_by` candidate edges are already populated this way: a claim's
  printed number is matched against experiment metrics with no model
  involved, and every candidate is written `status=pending` for a human to
  confirm or reject — the extractor itself never does, and neither can the
  semantic layer below.
- **Humans own judgement.** A local semantic layer (`rce judge`) is
  implemented: it reviews pending `backed_by` candidates and attaches a
  model's opinion to each as `evidence.semantic_review`. It is structurally
  forbidden from confirming or rejecting anything — it can only annotate;
  moving an edge to `confirmed`/`rejected` is a separate, human-only write
  path (`rce confirm`). Confidence-scored `supports` edges are a planned
  extension on top of this same annotation-only layer; `supports` is still
  schema-only until then.
- **Every edge is evidence-carrying.** No edge exists without a recorded
  extractor, an evidence pointer (file:line / commit SHA / run ID), and a
  confidence score.
- **No edge is invented.** If the graph has no provenance for something, RCE
  says so explicitly — it never guesses or fabricates a chain to look
  useful.

See `DESIGN.md` for the full product specification.
