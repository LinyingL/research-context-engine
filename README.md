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
- `rce lineage [<path>] [--orphans] [--json]` — read-only report over the
  data-lineage graph (`script --reads/writes--> dataset|figure`, built by
  `rce ingest`): **orphan inputs** (a data file read by a script but written
  by none — "where did this come from?"), **lineage chains** (a file with
  both a writer and a reader, shown as `written by file:line` /
  `read by file:line`), **broken links** (a script reads/writes a file that
  isn't actually on disk), and **duplicate copies** (a file a script reads
  has other same-named copies elsewhere in the project — which one did it
  actually read?). Every block prints only when it has something to say; an
  entirely empty result states what was scanned and why, never a bare `OK`.
  `--orphans` narrows the report to the first block alone. Exits 1 if any
  orphan input or broken link was found, 0 otherwise (duplicates/chains are
  informational and never affect the exit code).
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
- `rce attempts [<path>] [--path P] [--check] [-v]` — ingest a
  hand-maintained attempt-timeline table (a Markdown table logging every
  research path tried, one row per attempt) via `.rce/attempts.toml`, then
  either list what is registered, or run three deterministic consistency
  checks with `--check`: broken step references, stale verdicts, and
  revived dead variables. Config-gated on purpose — with no
  `.rce/attempts.toml` present it prints a copy-pasteable template and
  exits 1 rather than guessing which table in the project is the attempt
  timeline. See "`rce attempts` configuration" below for the file format.

`--path` defaults to `.` for `query`/`trace`/`status`/`confirm`/`attempts`,
so they also work by `cd`-ing into the project root first and dropping
`--path` entirely; `attempts` (like `init`/`ingest`) also accepts the
project root as a plain positional argument instead. `lineage` follows the
`init`/`ingest` convention directly: a plain positional `path` (default `.`),
no separate `--path` flag.

A global `-v`/`--verbose` flag (placed before the subcommand, e.g. `rce -v
attempts --check`) turns on INFO-level diagnostic logging — skip reasons,
orphan-node preservation, and similar detail that every extractor already
logs but which is otherwise invisible in normal use.

### `rce attempts` configuration

`rce attempts` never guesses which table in your project is the attempt
timeline — create `.rce/attempts.toml` (relative to the project root):

```toml
# All top-level keys below MUST appear before the [columns] table -- TOML
# nests any bare key written after a [table] header into that table.
file = "00-project-map.md"      # markdown file, relative to the project root
heading = "Attempt timeline"    # heading right above the table (prefix match is enough)
steps_dir = "repro/steps"       # optional: numbered step-script dir, for step-ref linking

# Optional, gates the "revived dead variable" check:
dead_variables = ["entropy weighting", "lnRate config ratio"]
active_verdicts = ["✅", "🕒"]   # verdict markers that count as "alive"

# Optional, gates the "stale verdict" check's looser date parsing (a bare
# "MM-DD", a "<=MM-DD"/"≤MM-DD" upper bound, or an "MM-DD~DD" range) --
# leave unset and only a full "YYYY-MM-DD" date parses.
date_year = 2026

[columns]                       # markdown header text for each field, as YOU wrote it
id = "#"
date = "Date"
description = "Path"
variables = "Variables"
result = "Result"
verdict = "Verdict"
```

Running `rce attempts` with no config prints this exact template (see
`rce.ingest.attempts.SAMPLE_CONFIG`) and exits 1.

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
