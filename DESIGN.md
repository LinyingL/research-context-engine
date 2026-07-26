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
rejects it.

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
  semantic enhancement    optional, local model, off by default
           |
  human confirmation      always available, never required
```

The engine is fully usable with the first layer alone. That is the default
install and the intended baseline, not a degraded mode.

Storage is a single SQLite file at `.rce/graph.db` inside the project,
alongside `.git` rather than inside it. Two tables — `nodes` and `edges` —
plus a migrations table. No database server, no message queue, no daemon.

The confirmation queue is not a third table; it is `edges` filtered by
`status = 'pending'`.

Evidence accumulates rather than overwrites: when the same figure is included
twice in one section, both call sites are kept as occurrences on one edge.

`nodes.human_fields` and `edges.status` are the human-owned columns. The
machine write path (`upsert_node` / `upsert_edge`) structurally cannot set
them to a human value; a separate function does, and re-ingestion leaves
confirmed and rejected edges alone.

## Section 4 — Object model

Eight node types: `project`, `experiment`, `commit`, `figure`, `section`,
`claim`, `reference`, `contributor`.

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
| `figure --supports--> section` | a figure substantiates an argument | semantic, planned |

`backed_by` candidates are generated deterministically by `rce.ingest.claims`
(no model involved): a claim's printed number is matched against experiment
metrics by rounding both to the precision the claim itself was printed with,
never a tuned tolerance. Every candidate is written `status=pending` — the
extractor never confirms or rejects one; that judgement is left to the
semantic layer or a human via the confirmation queue. `supports` still has no
extractor and exists in the schema only; it belongs to the optional
local-model layer described in section 7.

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

## Section 7 — Roadmap

**Now.** The deterministic layer across git, LaTeX/BibTeX, `savefig()` static
analysis, MLflow's local store, and W&B's public API; deterministic
`claim --backed_by--> experiment` candidate generation from numbers in prose
(pending status, no model); a command line that ingests and traces
provenance; an optional MCP server.

**Next.** The semantic layer: a local model that reviews the deterministic
`backed_by` candidates and additionally proposes `supports` edges with
confidence scores, every proposal verified against the graph before it is
stored and queued for human confirmation when uncertain. It stays optional
and local by construction — the point is that your unpublished results never
need to leave the machine.

**Later.** A local read-only web view over the same graph, and periodic
digests of what changed, what went stale, and what is waiting for review.

## Interfaces

The command line is complete on its own: ingest, inspect, and trace
provenance with no assistant involved.

An optional MCP server exposes the same graph to any MCP-capable client,
including open-source clients and ones backed by local models. MCP is a
protocol, not a vendor — the engine depends on no particular AI product, and
the default install pulls none in.
