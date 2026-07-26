# RCE — Research Context Engine

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
rce trace figure:overview.png --hops 4
```

None of the above touch any AI client — `rce` is a complete, useful tool on
its own from the command line.

- `rce init <path>` — initialize the graph database for a project.
- `rce ingest <path>` — scan git history, LaTeX/.bib sources, and
  (optionally) MLflow/W&B runs into the graph.
- `rce query <node-id>` — show one node and its immediate, single-hop edges,
  with evidence.
- `rce trace <node-id> [--hops N] [--json]` — walk the multi-hop provenance
  chain outward from a node (default 4 hops); `--json` for scripting.
- `rce status` — whole-graph node/edge counts and the pending confirmation
  queue.

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
  (AST, regex, standard APIs) with zero model calls; a local 7B model is
  layered on top only for semantic edges, never load-bearing for the core
  graph.
- **Every edge is evidence-carrying.** No edge exists without a recorded
  extractor, an evidence pointer (file:line / commit SHA / run ID), and a
  confidence score.
- **No edge is invented.** If the graph has no provenance for something, RCE
  says so explicitly — it never guesses or fabricates a chain to look
  useful.

See `HANDOFF-SPEC.md` for the full product specification.
