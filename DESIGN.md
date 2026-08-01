# Design

RCE builds a provenance graph over a research project by reading the tools
researchers already use. It never asks you to change how you work, and it
never guesses.

> Section numbers below are the ones cited throughout the source code
> (`see DESIGN.md section 5`, etc.). They are inherited from the internal
> specification this document is derived from, so the numbering has gaps:
> the missing sections covered project scope, budgets, and working
> discipline, which are not part of the public design.

## Section 0 — Principles

**Deterministic first.** Most of the graph comes from parsing, not inference:
filenames in `\includegraphics`, cite keys, git SHAs recorded by experiment
trackers. Code beats models wherever code suffices. A language model is an
optional enhancement layer, never a prerequisite.

**Evidence or nothing.** Every edge carries the extractor that produced it, a
pointer to the evidence (`file:line`, run id, commit SHA), a confidence value,
and a status. An edge without evidence cannot be stored — the database
rejects it. `backed_by` is the one exception to `file:line` being a stored
literal: its pointer is `file` plus the claim's *current* line, and the line
half is resolved at query time rather than persisted, precisely because an
unrelated edit elsewhere in the file shifts it (section 4, connector 7).

**Never guess.** When a path cannot be resolved, a SHA is not in the graph, or
a filename matches more than one candidate, the extractor skips and logs. A
missing edge is a normal outcome. A fabricated one is a defect.

**Humans own judgment.** Machine ingestion can write `auto` and `pending`
edges only. Confirming or rejecting an edge goes through a separate write
path, and re-ingestion never overwrites a human decision.

**Simplest thing that works.** Prefer an existing library over new code,
deterministic code over a model, a smaller model over a larger one, a single
file over a service. Abstract on the third repetition, not the first.

## Section 2 — Architecture decisions

Three layers, only the first of which is mandatory:

```
  deterministic parsing   always on, no model, no network
           |
  semantic enhancement    optional, local model, off by default (implemented: annotation-only)
           |
  human confirmation      always available, never required
```

The engine is fully usable with the first layer alone. That is the default
install and the intended baseline, not a degraded mode.

The semantic layer is implemented as `rce judge` (S2): it reviews pending
`backed_by` candidates and writes a model's opinion into each edge's
`evidence.semantic_review`, but it never writes `status` — see "Machine
annotation vs. human judgement" below for why that split is load-bearing,
not incidental.

Storage is a single SQLite file at `.rce/graph.db` inside the project,
alongside `.git` rather than inside it. Two tables — `nodes` and `edges` —
plus a migrations table. No database server, no message queue, no daemon.

The confirmation queue is not a third table; it is `edges` filtered by
`status = 'pending'`.

Evidence accumulates rather than overwrites: when the same figure is included
twice in one section, both call sites are kept as occurrences on one edge.
This "one occurrence per call site" framing belongs to `includes`/`cites`/
`generates`; `backed_by` occurrences are identified differently — (file,
metric, metric_value, claim_raw, claim_value), deliberately never the
claim's line — so they no longer distinguish call sites at all. Two
occurrences on one `backed_by` edge mean the same claim matched two
different (metric, value) pairs on the same experiment, not the same match
observed at two source locations (section 4's `backed_by` row and "Machine
annotation vs. human judgement" below).

An edge's evidence can also carry sibling keys alongside `occurrences` —
`semantic_review` (below) and `candidate_count` (a `backed_by`-only,
edge-level fact: how many (experiment, metric) pairs the claim matches in
total) are both stored this way, overwritten wholesale to their latest
value on every write, never accumulated like `occurrences` is. Folding
either into an occurrence's own identity instead used to be a real bug —
see connector 7's confidence discussion below for `candidate_count`'s
history.

`nodes.human_fields` and `edges.status` are the human-owned columns. The
machine write path (`upsert_node` / `upsert_edge`) structurally cannot set
them to a human value; a separate function does, and re-ingestion leaves
confirmed and rejected edges alone.

## Section 4 — Object model

Nine node types: `project`, `experiment`, `commit`, `figure`, `section`,
`claim`, `reference`, `contributor`, `attempt`.

Node ids are deterministic, so repeated ingestion converges instead of
duplicating: `commit:<sha>`, `figure:<repo-relative-path>`,
`section:<tex-file>#<slug>`, `reference:<lowercased-bib-key>`,
`experiment:<run-id>`, `contributor:<lowercased-email>`.

`claim:<tex-file>#<content-hash>` is content-addressed rather than
position-addressed: the hash covers the owning section's slug, the claim's
sentence (whitespace/case-normalized), the number's own printed literal, and
its position among any other numbers in the same sentence -- never the line
number. Inserting, deleting, or reordering unrelated lines therefore leaves
every claim's id unchanged; only editing the claim's own text or its printed
number produces a new id, which is correct -- at that point a human's
earlier confirm/reject on the old claim genuinely no longer applies. A
line-anchored id would instead let an unrelated edit shift a claim onto
another claim's former id, silently inheriting that claim's human verdict.

**`attempt` (migration 0002).** A researcher's project often has a
hand-maintained Markdown table logging every research path tried -- an
attempt timeline, one row per attempt, with a stable `#` column, a
date, a path/variable description, and a human-written result and verdict
(e.g. "confirmed", "dead end", "direction rejected"). RCE never asks the
researcher to change this habit; an `attempt` node is a machine-parsed
mirror of one such row, kept queryable and linkable to the rest of the
graph rather than left as inert prose.

Id convention: `attempt:<source-file-relative-path>#<# column value>` --
e.g. `attempt:00-项目地图_唯一真相.md#16`, or `#14a` for a row the author
split into sub-attempts (`14a`/`14b`) without renumbering the rows after
it. This assumes the `#` column is a stable manually-assigned label rather
than a position: checked against a real 22-row attempt timeline, the
column runs 1-22 with no reused number, and several rows are *not* in
chronological order relative to their neighbors (e.g. rows dated
2026-07-09 appear after rows dated 2026-07-22 because they were folded in
from a separate parallel review) -- exactly the pattern of a label the
author assigns once and keeps, not a row index that shifts when the table
is resorted or edited. This id convention holds only under that
assumption: a project whose own timeline reuses or renumbers `#` values on
edit would have re-ingestion quietly merge two different attempts onto one
node, so an ingest extractor for this format must inherit the same "skip
and log, never guess" discipline as every other connector (section 5) if a
row's `#` value collides with a different row's already-stored title/attrs.

Only `#`/date/variable-description/referenced-step-number/source-file-
and-line are machine-parsed and live in `attrs` -- they are facts about
the row's text, and a re-parse may refresh them like any other node's
`attrs`. `verdict` and `result` are the human's judgement call recorded in
prose next to the row (what the attempt showed, whether it stands) and
must be written to `human_fields` only, through `set_human_fields`, never
through the machine `upsert_node` path -- the same "humans own judgement"
split (section 0/2) that already governs every other node and edge, not a
special case invented for this type. `db.upsert_node` enforces this
structurally: its `UPDATE` column list has no `human_fields` entry, so
even a future extractor that carelessly puts `verdict`/`result` in its
`attrs` dict cannot make them land there.

Edge types, grouped by the layer that produces them:

| Edge | Meaning | Layer |
| --- | --- | --- |
| `commit --implements--> experiment` | a run recorded this commit's SHA | deterministic |
| `experiment --produces--> figure` | a run artifact matches a tracked image | deterministic |
| `commit --generates--> figure` | a `savefig()` call writes this image | deterministic |
| `section --includes--> figure` | `\includegraphics` in that section | deterministic |
| `section --cites--> reference` | `\cite` and its natbib/biblatex variants | deterministic |
| `* --authored_by--> contributor` | git author, run owner | deterministic |
| `claim --backed_by--> experiment` | a number in the prose matches a run metric | deterministic candidate, pending judgement |
| `attempt --uses--> commit` | the last commit to touch a script file the attempt depends on | deterministic |
| `figure --supports--> section` | a figure substantiates an argument | semantic, planned |

`backed_by` candidates are generated deterministically by `rce.ingest.claims`
(no model involved): a claim's printed number is matched against experiment
metrics by rounding both to the precision the claim itself was printed with,
never a tuned tolerance. Every candidate is written `status=pending` — the
extractor never confirms or rejects one; that judgement is left to the
semantic layer or a human via the confirmation queue.

Confidence on a `backed_by` candidate is always `1.0`, whether a claim
matches one experiment or several. Precision-matching is deterministic and
exact — the rounding rule either finds a candidate or it doesn't, with no
tunable tolerance — so confidence expresses the reliability of the match
*rule*, not which candidate is the correct one. That second question is a
judgement call, and judgement belongs to the semantic layer or a human, never
the extractor; diluting confidence to `1/N` across `N` candidates used to
smuggle a guess about "which one" into a decimal that looked precise but
wasn't. Instead, ambiguity is recorded plainly: every candidate edge's
evidence carries `candidate_count`, the count of (experiment, metric) pairs
that claim matched — not the count of distinct experiments, since one
experiment can contribute more than one matching metric to the same claim
— so a reviewer sees "3 candidates" rather than inferring it from a
confidence of `0.33`. `candidate_count` is a sibling of `occurrences`
(`evidence.candidate_count`, not nested inside any one occurrence) and is
overwritten to the latest total on every re-ingest, never accumulated: it
describes the whole claim across every experiment, not the one occurrence
just written, and folding it into an occurrence's own identity instead
used to be a real bug — incrementally adding new matching experiments
changed the count on every re-ingest, which made an *already-existing*
edge's unchanged occurrence look "new" each time and grew a single edge's
occurrence list without bound.

`supports` still has no extractor and exists in the schema only; it belongs
to a future extension of the semantic layer described in section 7.
`backed_by` candidates, meanwhile, are reviewable today: `rce judge` (S2)
attaches a model's opinion to each pending candidate as
`evidence.semantic_review` — see "Machine annotation vs. human judgement"
below. This is annotation on top of the existing `backed_by` edge, not a
new edge type, so it needed no change to the table above.

### Machine annotation vs. human judgement

`rce judge` (`rce.semantic.judge`) is a second machine that looks at the
graph, not a shortcut around "humans own judgement." Section 0's rule does
not carve out an exception for a model just because its guess is often
better than the deterministic rounding-coincidence match it is reviewing —
a wrong guess dressed up as fluent prose is exactly the failure mode "never
guess" exists to prevent. Concretely, this is enforced two ways:

1. **A narrow write path.** `db.set_edge_semantic_review` is the only
   function `rce.semantic.judge` calls to persist anything. It writes
   `evidence.semantic_review` (`related`, `reason`, `better_match`, the
   `metric`/`metric_value` of the one occurrence actually reviewed, plus
   `model`/`reviewed_at`/`run_id` for traceability) and nothing else on the
   row — not `status`, not `confidence`. It does not call `upsert_edge` or
   `set_edge_status` itself, so there is no code path by which a judge run
   could move an edge to `confirmed` or `rejected`, however confident the
   model's own `related: true` sounds. A `pending` candidate reviewed by
   the judge is still `pending` afterward; a human decides via `rce
   confirm`, same as any other candidate.
2. **A verifier the model cannot talk its way past.** `better_match` names a
   param or metric the model claims exists on the *same* experiment run —
   information only available at review time, so no static JSON-Schema can
   check it. `rce.semantic.judge`'s own verifier checks `better_match`
   against that run's actual param/metric names before anything is stored;
   a name that is not literally present is discarded as a hallucination,
   logged, and never written (`related`/`reason` are kept regardless — one
   field failing verification does not discard the whole review).
3. **Trim the response, never the judgement.** `reason` is meant to be one
   sentence; a model that instead writes several is not treated as a
   validation failure. `reason` is truncated at 300 characters (plus a 4-character marker, so a stored value can be 304) — truncated to
   that length with a trailing `" ..."` marker and the truncation logged
   (never silent) — while `related` and a verified `better_match` are still
   validated and stored normally. Discarding the whole response over a
   wordier-than-asked-for `reason` would throw away the one thing a human
   reviewer actually wants (the model's `related`/`better_match` verdict)
   over nothing worse than a run-on sentence.

The result reads like a second opinion, not a verdict: "this candidate is
probably a numeric coincidence, and `quantization` on this same run looks
like a better fit" is exactly the kind of note a human reviewer wants
sitting next to a `pending` edge before they decide — and exactly the kind
of note that must never quietly become the decision itself.

**Known limitation.** A `backed_by` edge carries one occurrence per distinct
metric of *that* experiment matching the claim, so an edge has more than one
occurrence only when a single experiment logs several metrics that all round
to the claim's printed value. `rce judge` reviews exactly one occurrence per
edge (the most recently written one) and records which one it saw in
`semantic_review.metric`/`.metric_value`; any other occurrence on that edge
is logged as skipped, never itself sent to the model.

Note that `evidence.candidate_count` is *not* what triggers this. It is an
edge-level, claim-global figure — how many (experiment, metric) pairs the
claim matched across the whole graph — so the ordinary case of a claim
matching twenty different experiments gives every one of those edges
`candidate_count = 20` and exactly one occurrence, and each is reviewed in
full. Only the several-metrics-on-one-experiment case is partially reviewed.

## Section 5 — Connection keys

The deterministic layer joins objects on evidence that already exists in the
project. Nothing here requires metadata entry:

1. `\includegraphics{path}` against tracked image files. Extensionless
   includes are resolved by trying known image extensions in pdflatex's
   search order.
2. `\cite{key}` — and `\citep`, `\citet`, `\citealp`, `\parencite`,
   `\textcite`, `\autocite` — against `.bib` entries, matched
   case-insensitively.
3. The git commit SHA that MLflow and W&B record with each run.
4. Run artifact filenames against tracked images, matched by basename and
   only when the match is unique.
5. String literals in `savefig()` calls, including same-file module-level
   string constants recovered by constant folding. A name that is bound more
   than once anywhere in the file is never folded, and a file containing
   `from x import *` gives up folding entirely — the set of names such an
   import binds is not statically knowable.
6. `\label` and `\ref` within a document.
7. Numbers in prose — percent/fraction/plain forms with a decimal point,
   `\SI{}{\percent}`, and bare `$...$` math-mode numbers — against experiment
   metrics. A candidate `backed_by` edge is written (`status=pending`) when
   both round to the same value at the precision the claim itself was
   printed with; the final confirm/reject judgement is deferred to the
   semantic layer or a human.

Every one of these skips and logs rather than guessing when the key does not
resolve cleanly.

**Known limitation (connector 7).** A number immediately followed by a
hyphen and a letter (`1.58-bit`, `3-fold`) is always treated as a compound
modifier and skipped, never scanned as a claim — a deterministic syntax
rule, not a tuned threshold. It has no false-claim direction, only a
false-skip one: a genuine claim shaped the same way (`a 2.3-point
improvement`) is skipped too. Section 0's "never guess" accepts that cost.
For the same reason, a `\begin{env}`'s own argument groups (e.g. the layout
length after `\begin{subfigure}`, or a `\begin{tabular}` column spec), the
URL argument of `\url`/`\href`, and a bare `https://...` typed directly in
prose are all blanked before number-scanning — a DOI or arXiv id's digits
are not a claim about the paper's own results.

**Known limitation (connector 7, no local run store).** A paper repository
that never has a local MLflow/W&B run history — the normal case for a
`showyourwork`-style repository, and for most already-published LaTeX
repositories in general — gets zero `experiment:` nodes. With no experiment
metrics to compare against, every quantitative claim's `backed_by` candidate
set is structurally empty, not merely small: the claim node is still
created, but no candidate edge of any kind follows. This is not a bug. The
deterministic layer only connects evidence that already exists in the
project; a repository with no recorded runs has no run evidence to connect,
and RCE does not invent a placeholder experiment to compare against. A user
comparing RCE's output across repositories should expect an all-claims,
no-`backed_by` result from this class of repo rather than mistake it for a
broken extractor.

## Section 7 — Roadmap

**Now.** The deterministic layer across git, LaTeX/BibTeX, `savefig()` static
analysis, MLflow's local store, and W&B's public API; deterministic
`claim --backed_by--> experiment` candidate generation from numbers in prose
(pending status, no model); a command line that ingests and traces
provenance; an optional MCP server. The semantic layer's first slice
(`rce judge`, S2): a local, vendor-neutral OpenAI-compatible client
(`rce.semantic.backend`, off by default, no third-party dependency to
install) reviews pending `backed_by` candidates and writes its opinion into
`evidence.semantic_review` — related or not, a one-sentence reason, and an
optional `better_match` naming a param or metric on that *same* experiment
run the deterministic matcher never considered. Every `better_match` is
verified against the run's actual param/metric names before being stored; a
name that doesn't exist there is discarded as a hallucination, logged, and
never written. `status` is untouched either way — see "Machine annotation
vs. human judgement" below.

**Next.** Proposing `supports` edges (a figure substantiates an argument)
with confidence scores, every proposal verified against the graph before it
is stored and queued for human confirmation when uncertain. Still optional
and **local by default, not local by construction**: `rce.semantic.backend`
talks to whatever OpenAI-compatible server `RCE_LLM_BASE_URL` (or the
`base_url` constructor argument) names, and that is a plain configuration
value, not something the code structurally confines to this machine. The
default points at a local server, and every `rce judge` run whose base URL's
hostname is not `localhost`/`127.0.0.1`/`::1`/`*.local` prints a prominent
warning before sending that run's experiment params and metric names
anywhere, so pointing the semantic layer at a remote endpoint is possible
but never silent. This is a hostname-*shape* check (string comparison
against a short allow-list), not DNS resolution or a network reachability
probe — RCE never resolves or contacts the address to decide whether to
warn. **Known limitation:** the `*.local` exemption trusts the suffix by
name only; a hostname that merely ends in `.local` (whether or not it is
actually mDNS/LAN-only) is treated as local and, like `localhost`, produces no warning at all, since a hostname-shape check has no way to verify what a name
actually resolves to or where it's reachable from.

**Later.** A local read-only web view over the same graph, and periodic
digests of what changed, what went stale, and what is waiting for review.

## Interfaces

The command line is complete on its own: ingest, inspect, and trace
provenance with no assistant involved. `rce judge` is the one subcommand
that talks to a model, and only when invoked — every other subcommand
(`init`/`ingest`/`status`/`query`/`trace`/`confirm`) works identically
whether or not a local model server exists.

An optional MCP server exposes the same graph to any MCP-capable client,
including open-source clients and ones backed by local models. MCP is a
protocol, not a vendor — the engine depends on no particular AI product, and
the default install pulls none in.
