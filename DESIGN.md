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
path, and re-ingestion never overwrites a human decision. (This is about
protecting the one place a decision is actually recorded, not a blanket
"never touch human_fields" rule -- an `attempt` node's `human_fields` is
the one documented exception, and section 4 explains why it is not really
an exception at all.)

**Simplest thing that works.** Prefer an existing library over new code,
deterministic code over a model, a smaller model over a larger one, a single
file over a service. Abstract on the third repetition, not the first.

**Kept material is not a relation.** An old copy of a script, the hash of a
file, a confirmation the researcher made: each is material that can be
checked. That two pieces of material sit side by side — the same folder,
the same day, the same name — says nothing about how they are related. A
relation between them is recorded only when it has a basis of its own:
something the machine read, or something the researcher stated. "This
file existed when the definition was confirmed" and "this file was built
by that definition" are different sentences, and RCE never writes the
second because the first is true. (Adopted 2026-10-05 from the external
review of Section 9; it is the rule the result-provenance phase starts
from.)

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

*Superseded in location only (Section 8.10, rule 1): the graph is still a
single SQLite file per project, but it now lives at
`~/.rce/graphs/<id>/graph.db`, outside the project, because a project
folder may be cloud-synced. Everything else in this paragraph stands.*

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
than a position: checked against a real 23-row attempt timeline (row 14
was split into `14a`/`14b` without renumbering the rows after it, so the
column runs 1-13, `14a`, `14b`, 15-22 -- 23 physical rows, 22 distinct
labels, no label reused), several rows are *not* in chronological order
relative to their neighbors (e.g. rows dated 2026-07-09 appear after rows
dated 2026-07-22 because they were folded in from a separate parallel
review) -- exactly the pattern of a label the author assigns once and
keeps, not a row index that shifts when the table is resorted or edited.
This id convention holds only under that assumption: a project whose own
timeline reuses a `#` value on a *different* row without renumbering would
have re-ingestion quietly merge two different attempts onto one node, so
an ingest extractor for this format must inherit the same "skip and log,
never guess" discipline as every other connector (section 5) when a
row's `#` value repeats a different row's `#` value within the same parse
of the table -- editing a row's own description/date/variables text is
not this case; those are ordinary machine-parsed `attrs` and are expected
to change on re-parse like any other node's `attrs`.

**`rce.ingest.attempts` (task A2)** is this extractor: config-driven only,
via `.rce/attempts.toml` (file/heading/`[columns]` name mapping, optional
`steps_dir`) -- with no config present, `rce attempts` prints a
copy-pasteable template and exits 1 rather than guessing which table in the
project is the attempt timeline. The same clean-message-and-exit-1 handling
also covers a configured `[columns]` name no longer present in the table's
actual header (e.g. a renamed column) and a heading/table that can no
longer be located at all (see "Attempt orphans" below); both raise
`AttemptsConfigError` -- the latter via its `AttemptsTableNotFoundError`
subclass -- from inside `ingest_attempts_repo`, not only from `load_config`,
and `rce.cli.cmd_attempts` catches both around the same call so neither
surfaces as a raw traceback. `verdict`/`result` go to `human_fields` via
`set_human_fields` on *every* parse, resynced to whatever the row currently
says in the source file -- see "Attempt human_fields: resync from source,
not write-once" below for why this is not an exception to "humans own
judgement" (section 0) but the same rule pointed at a different authority.
A description referencing a step-number range (e.g. `(16-18)`) resolves,
when `steps_dir` is configured, by exact numeric filename prefix into
`attrs.step_files`; a number with no matching file is recorded in
`attrs.step_files_broken` for a future consistency check (A3), never fuzzy-matched.

Only `#`/date/variable-description/referenced-step-number/source-file-
and-line are machine-parsed and live in `attrs` -- they are facts about
the row's text, and a re-parse may refresh them like any other node's
`attrs`. `verdict` and `result` are the human's judgement call recorded in
prose next to the row (what the attempt showed, whether it stands) and
must be written to `human_fields` only, through `set_human_fields`, never
through the machine `upsert_node` path -- not because the value is frozen
once written (it is not; see below), but because `db.upsert_node` enforces
this structurally: its `UPDATE` column list has no `human_fields` entry, so
even a future extractor that carelessly puts `verdict`/`result` in its
`attrs` dict cannot make them land there instead.

### Attempt human_fields: resync from source, not write-once

The first implementation of task A2 wrote `verdict`/`result` to
`human_fields` once, on first sight of an id, and never again -- modeled
directly on `edges.status`, where re-ingestion must never touch a
confirmed or rejected edge. Testing against a real 23-row attempt timeline
exposed this as a mistake, not a stricter safeguard: a row's verdict was
changed from `☠️ 伪协整` to `🕒 决定复活重做` in the source file, the
project was re-ingested, and the graph did not move -- `rce attempts
--check` kept printing `[revived_dead_variables] OK: no issues found
(23/23 attempts checked)`, while the same map ingested fresh into an empty
database correctly reported the revival. `rce.consistency.
check_revived_dead_variables`'s logic was never wrong; the input it read
had been frozen.

The mistake was applying "humans own judgement" to the wrong authority.
That rule protects the one place a human decision is actually recorded
from being silently overwritten by a machine re-ingest. For
`edges.status`, that place is the graph itself: a `backed_by` candidate is
machine-generated, and `rce confirm` writing `status` in the graph is the
*only* place a human ever records confirm/reject -- so re-ingestion
touching `status` would erase the one record of that decision that exists
anywhere. For an attempt's `verdict`/`result`, the decision is instead
recorded by the researcher in their *own source Markdown file*, in prose
next to the row -- the timeline is declared the project's single source of
truth, and the graph is a queryable mirror of it, not a second copy that
competes with it. Freezing `human_fields` on first sight did not defend
that decision; it defended a stale copy against the original, which is
backwards -- the researcher editing their own map file *is* them making
the judgement, and an engine that keeps mirroring the file faithfully is
not overriding anything. ("Keeps mirroring" describes what happens *when
this extractor runs*, not a standing live guarantee -- see "Staleness
window" below.)

The fix: `verdict`/`result` are resynced from the source file on every
parse (`rce.ingest.attempts.ingest_attempts_repo`), written via
`set_human_fields` whenever the freshly parsed value differs from what is
stored, and skipped otherwise so an unchanged row produces neither a
gratuitous write nor log noise. This is not an exception to "machine
ingestion never overwrites a human decision" -- it is that same rule,
pointed at the place the decision actually lives. `edges.status` and
attempt `human_fields` therefore behave oppositely on purpose:
`edges.status` must never be touched by re-ingestion because the graph is
the sole record of that decision (superseded by 9.1: the judgment ledger
`.rce/judgements.toml` is now that record, and the index derives
`edges.status` from it); attempt `human_fields` must always be
resynced by re-ingestion because the source file is the sole record and
the graph must not be allowed to go stale and start lying about what that
file currently says.

**Precondition this rests on.** There is currently exactly one place an
attempt's verdict is ever recorded: the source Markdown file --
`db.set_human_fields` has exactly one caller in the whole `src` tree, this
extractor. If a future feature lets someone edit an attempt's verdict
directly in the graph (mirroring how `rce confirm` edits `edges.status`
directly, rather than editing a source file), that edit and the source
file become two authorities that can disagree, and this resync would
silently discard the in-graph edit on the very next `rce attempts` run.
That case must be resolved explicitly when such a path is added -- e.g.
detect divergence and refuse to overwrite, or have the in-graph edit
itself rewrite the source file -- rather than left for whoever adds it to
discover by losing an edit. This precondition used to be enforced by
nothing but this paragraph; `tests/test_ingest_attempts.py::
test_set_human_fields_has_exactly_one_caller_in_src` now asserts the
call-site count directly via `ast` -- a mention of the name in a docstring
or comment is not mistaken for a call, and (fixed after a reviewer probe
found the gap) neither is an aliased import evading detection: `from
rce.db import set_human_fields as _write_hf` followed by a real call
through `_write_hf(...)` used to pass this test undetected, since that
call's own `func.id` is the alias, never the literal string
`"set_human_fields"`. The scan now also flags any `ImportFrom` of the name
as a site in its own right, aliased or not, regardless of whether the
alias is ever subsequently called. This makes the test a best-effort
static tripwire for the common ways a second caller gets added -- an
ordinary call, an attribute access, or an aliased import -- not a proof
that no second caller can ever exist undetected: a sufficiently indirect
reference (`getattr(db, "set_human_fields")`, constructing the call from a
string) is still outside what a syntactic `ast` scan can see. It closes the
concrete gap a reviewer found, not every gap that could theoretically be
constructed.

**Staleness window: only `rce attempts` refreshes these nodes.** `attempt`
nodes are written and resynced exclusively by `rce.ingest.attempts`
(above), which runs when the `rce attempts` subcommand runs -- `rce
ingest` (the deterministic pass over commits/figures/claims/etc., section
2) never touches them. So between one `rce attempts` run and the next, an
attempt's `human_fields`/`attrs` read via `rce query` or the MCP server can
be stale relative to whatever the researcher has since edited into the
source file -- the mirror is refreshed on demand, not continuously, and
nothing currently re-runs `rce attempts` as a side effect of `rce ingest`
or a query. This is a reasonable division of labor (a hand-maintained
Markdown table is not a thing to re-parse on every unrelated `rce ingest`
of commits and figures), not an oversight, but it means "the graph
reflects the file" is only true as of the last `rce attempts` invocation,
and a project relying on fresh attempt data before a query should run `rce
attempts` first, the same way `cmd_attempts` itself always re-ingests
before listing or `--check`ing (above).

### Attempt orphans: deletion resyncs from source too, not just verdicts

**Observation failure is not evidence of absence.** Everything else in this
section governs *which* previously-ingested nodes `_cleanup_orphans`
deletes once it runs -- but it must first be established that this run
actually observed the timeline at all, and "parsed zero rows" is not one
condition. `parse_attempts_table` can come back with either (a) the
heading and its table were both located, and the table's data section
genuinely has no rows (every row really was deleted, or this is a fresh,
empty timeline) -- a real, legitimate observation -- or (b) the configured
`heading`, or a table underneath it, could not be *located* in the source
file at all: an unrelated Markdown edit inserted a heading between the
tracked one and its table, the heading text drifted out of sync with
`.rce/attempts.toml`, or the table lost its separator row. These two used
to come back identically, as a bare `[]` with only a logged warning
(Blocker 1, found against the real 23-row project map this extractor was
built against): inserting one ordinary prose sub-heading (`### 说明:...`)
between `## 二、尝试途径总年表` and its table made `parse_attempts_table`
return `[]`; `ingest_attempts_repo`'s `seen_ids` therefore came back empty,
and `_cleanup_orphans` read that exactly the way it reads a genuinely
emptied table -- "none of these 23 attempts exist anymore." `rce attempts`
printed `attempts=0 created=0 updated=0 orphans_removed=23` and deleted
every node; `rce attempts --check` then printed `OK: no attempts to check`
and exited 0 -- an ordinary heading edit silently erased the graph's entire
attempt history and reported success on the way out. This is Section 0's
"never guess" principle again, pointed at a failure mode the OSError
read-failure path just above already defends against (that section's own
docstring: "not evidence the attempts are gone") but which had not yet been
extended to a *located-file, unlocatable-table* failure: not having
observed the table's rows is not evidence they don't exist, and reading
"not found" as "empty" is exactly as much a guess as fabricating a number
would be.

The fix keeps the two outcomes from ever being conflated in the first
place, structurally rather than by convention: `parse_attempts_table` now
raises `AttemptsTableNotFoundError` (a subclass of `AttemptsConfigError`)
when the heading or its table cannot be located, and returns a plain `[]`
-- unambiguous now -- only when the table *was* located and its data
section is genuinely empty. `ingest_attempts_repo` lets that exception
propagate rather than swallowing it (after logging which file/heading it
was looking for and noting explicitly that every existing node for this
file is left untouched): the row loop that would build `seen_ids` never
runs, so `_cleanup_orphans` is skipped structurally, not by a conditional
that could itself have the same bug. `rce.cli.cmd_attempts` catches it
alongside every other `AttemptsConfigError` (above) and exits 1 with a
clean message -- deliberately not the OSError path's silent all-zero-counts
return, because unlike a transient read failure this means the run's own
request (this heading, in this file) could not be satisfied, and the
operator should be told, not shown a quiet success line.

The resync above covers a row that still exists but whose verdict changed.
A row *deleted* (or renumbered) out of the table entirely -- once the
table has actually been located and parsed, per the above -- used to be
handled differently, and inconsistently with the principle that same
section just established: `rce.ingest.attempts._cleanup_orphans` preserved
any orphaned `attempt` node that still carried `human_fields`, deleting
only one with none -- modeled on `rce.ingest.claims`'s own orphan cleanup,
which preserves a claim node whose `backed_by` edge carries a
confirmed/rejected `edges.status`.

That model does not transfer as a `human_fields` check. Testing against a
real 23-row map confirmed it concretely: deleting row 15 from the source
file and re-ingesting left `rce attempts`' listing still printing 23 rows
including `#15`, `rce attempts --check` still reporting a finding about a
row that no longer existed anywhere in the project, and exit code 1 -- with
no way to clear either short of dropping the whole database. Worse, the
guard behind this was never actually reachable in practice: this same
extractor's resync (above) writes `human_fields` on *every* node's first
parse, even a row whose verdict/result are both blank, since
`{"verdict": "", "result": ""}` is still a truthy dict -- so
`if node["human_fields"]:` was true for every attempt node this extractor
has ever produced, and the delete branch beneath it was dead code outside a
hand-built test. `human_fields` looking truthy is not evidence of a
decision *in the graph* the way a confirmed/rejected `edges.status` is --
it is simply the last mirrored copy of whatever the row's verdict cell
said, and the row no longer exists to mirror. Checking it, as that version
of `_cleanup_orphans` did, was applying claims's guard to the wrong
authority: an attempt's verdict/result decision is recorded in the
researcher's own source file, never in an edge, and the graph only mirrors
it (see "resync from source" above); `human_fields` is simply not where a
human decision about *this node* lives.

Removing that wrong check does not mean no check is needed at all, though
-- and the fix that first replaced it went one step too far by concluding
exactly that. That version deleted an orphaned node -- and every edge
touching it, in either direction, from any extractor -- unconditionally,
reasoning that an attempt node's only edge type, `uses`
(`attempt --uses--> commit`, written by `rce.consistency` below, tagged
`extractor="attempts_consistency"`), is "a deterministic, always-`auto`
fact that no human ever confirms or rejects." That assumption was never
actually *enforced* anywhere in the codebase, and a real end-to-end run
against the project map disproved it directly (Blocker 2): `rce confirm`
carries no allowlist restricting which edge types a human may judge --
`rce confirm --status confirmed <attempt-node> <commit> uses
attempts_consistency` succeeds today exactly like confirming a `backed_by`
edge would. Once that `uses` edge was confirmed, deleting the row it
belonged to and re-ingesting silently destroyed both the edge and the node
carrying the confirmed decision -- no log line, no count, nothing to
distinguish it from an ordinary, harmless cleanup.

The corrected fix restores a preserve check, keyed on the same signal
claims already uses -- `edges.status` in `('confirmed', 'rejected')` -- not
on `human_fields`, which was the wrong signal for the reasons just given.
Before deleting an orphaned attempt node, `_cleanup_orphans` now inspects
every edge touching it, `src` or `dst`, from any extractor; if any carries
that status, the node and all of its edges are left exactly as they are,
logged (which node, which file, how many edges carry a decision), and
counted under a new `orphans_preserved_with_human_decision` key, visible in
`rce attempts`'s own summary line rather than only in the log. Only when
none of a node's edges are confirmed or rejected does the delete proceed as
before: every edge touching the node is removed first (counted under a new
`orphan_edges_removed` key, mirroring claims's `backed_by_edges_removed`),
then the node itself.

`edges.status` (claims) and `attempt` orphans (this section) now agree on
*when* to preserve a node: both refuse to delete once a confirmed/rejected
edge is present, because in both cases the graph's own `edges.status` is
somewhere a human decision can genuinely be recorded, and `rce confirm`
does not care which node type or extractor an edge belongs to. What still
differs, deliberately, is *scope*: claims's own check and delete are both
scoped to edges its own extractor produced (`extractor="claims"`), so a
hypothetical second claims-like extractor's candidates would not interfere
with each other's bookkeeping; attempts' check and delete both span every
extractor touching the node, because `nodes.id` is a foreign key
`edges.src`/`dst` reference with no cascade (`db.delete_node`'s own
docstring) -- an unconfirmed `uses` edge left in place by an earlier
`--check` run would otherwise make the node's own delete raise
`sqlite3.IntegrityError`, and there is no reason to scope the
*human-decision protection* narrowly just because claims happens to scope
its own extractor-local bookkeeping that way. `human_fields` resync
(above) still behaves oppositely from `edges.status` on purpose, unaffected
by either fix: an attempt's `human_fields` must always be resynced from the
source file, because the file, not the graph, is the sole record of a
verdict; an edge's `status`, and now an attempt node carrying a
confirmed/rejected edge, must never be overwritten or deleted by
re-ingestion, because the graph is the sole record of *that* decision,
wherever in the graph it happens to be attached. (Superseded by 9.1: the
graph is no longer that record -- the judgment ledger is -- so claims and
attempts cleanup now delete such orphans like any other; 9.6 says how the
judgment is shown.)

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

### Attempt consistency checks (task A3)

The attempt timeline (above) is not a place RCE hands a judgement back to
the researcher. The researcher already wrote the judgement — `verdict` and
`result`, in their own words, next to the row. What the researcher cannot
easily do by hand is notice that a row's step reference no longer resolves
to a file, that a script a row depends on was edited after the verdict was
recorded, or that a dead variable's name has quietly resurfaced in a row
that is still marked alive. `rce.consistency` (`rce attempts --check`) is
three narrow, fully deterministic checks for exactly that — no model
anywhere in this module, same "code beats models wherever code suffices"
rule as every other extractor. Each reads `attempt` nodes already written by
`rce.ingest.attempts` rather than re-parsing the source Markdown itself, so
`rce attempts --check` always re-ingests first.

1. **Broken reference.** An attempt's step reference (`attrs.step_refs`,
   resolved into `attrs.step_files`/`attrs.step_files_broken` at ingest
   time — see the `attempt` node description above) that matched no file
   under `steps_dir`. The finding also reports the nearest step numbers
   that DO exist in the directory, on either side of the missing one, so a
   human can tell a rename from an actual deletion at a glance instead of
   opening the folder.
2. **Stale verdict.** An attempt whose recorded date is earlier than the
   last time one of its resolved dependency scripts was touched — the
   verdict was written, then the code changed again. The script's
   last-touch time comes from `rce.ingest.git.read_commits` (reused, not
   reimplemented): the most recent commit whose changed-file list includes
   that script. A script the check cannot find in git history at all —
   untracked, or the project has no git repository — falls back to the
   file's own mtime, and the finding says so explicitly (`basis="git"` vs.
   `basis="mtime"`) rather than presenting both as equally reliable. Every
   script resolved via git also gets an `attempt --uses--> commit` edge
   (the edge type migration 0002 added), evidence `{"script",
   "commit_time"}` — a deterministic fact worth keeping regardless of
   whether that particular script triggered a finding. A mtime fallback has
   no commit to point at, so no edge is written for that case.

   **Date parsing.** The comparison needs the attempt's own date column
   parsed into a definite date. A real hand-written timeline's dates are
   rarely a plain `YYYY-MM-DD` — a range ("07-08~09"), an upper bound
   ("≤07-07"), a bare month-day with no year ("07-26"), a trailing
   annotation ("07-10 冻结"). Inferring the missing year or picking a bound
   of a range would be exactly the fabrication section 0 forbids, so none
   of these parse *unless* the human declares `date_year` in
   `.rce/attempts.toml` (below) — a human stating "this table's dates are
   all in year Y", never rce inferring it. Once `date_year` is declared,
   `rce.consistency._parse_attempt_date` accepts, in addition to a full
   `YYYY-MM-DD`:
     - `MM-DD` (e.g. `"07-26"`) — combined with `date_year`.
     - `<=MM-DD` / `≤MM-DD` (e.g. `"≤07-07"`) — the prefix is stripped and
       the date after it used as-is; it marks the true date as possibly
       earlier, but does not change which date this check takes.
     - `MM-DD~DD` or `MM-DD~MM-DD` (e.g. `"07-08~09"`, a range spanning a
       month boundary) — the LATER end is taken, because a verdict is only
       safely dated once the attempt is actually finished, i.e. the end of
       the range, never its start.
     - Trailing free text after the date (e.g. `"07-10 冻结"`) is ignored.

   An attempt whose date still does not parse (bare `MM-DD` with no
   `date_year` configured, or genuinely unparseable text) is skipped from
   this one check individually, logged, and counted in the check's own
   coverage figures — never guessed at, and never silently folded into "0
   findings" (see "Honest coverage reporting" below).
3. **Revived dead variable.** A *living* attempt (its verdict contains one
   of the configured `active_verdicts` markers) whose description or
   variable list mentions one of the configured `dead_variables` entries.
   Matching is a case-insensitive substring test, deliberately
   conservative, and every finding carries the actual matched field text
   verbatim so a human judges it themselves — this check only ever reports
   a hit, it never files, dismisses, or otherwise disposes of one.

Each check is independently gated on its own config prerequisite, and a
missing prerequisite is reported as *skipped*, never silently folded into
"no findings" — the two look identical in a bug but must never look
identical in this tool's output. Checks 1 and 2 need `steps_dir`; check 2
also optionally reads `date_year` (above); check 3 needs both
`dead_variables` and `active_verdicts` declared. All of these are optional
top-level keys in the same `.rce/attempts.toml` task A2 already introduced
— no second config file. They **must** appear before the `[columns]` table
in the file: TOML nests any bare key written after a `[table]` header into
that table, so a list or int written after `[columns]` is silently read as
e.g. `columns.dead_variables` instead and never seen by `load_config` at
all — a real bug this project's own `SAMPLE_CONFIG` template shipped with
until it was caught against a real 23-row timeline (see `rce.ingest.
attempts` for the fix and its regression test).

```toml
steps_dir = "复现包_分步"                    # optional; gates checks 1 and 2
dead_variables = ["信息熵", "lnRate 配置比例"]  # optional; gates check 3, paired with:
active_verdicts = ["✅", "🕒"]                # which verdict markers count as "alive"
date_year = 2026                             # optional; gates check 2's looser date forms above
```

`dead_variables`/`active_verdicts` default to unset (not `[]`) when absent,
so an explicitly declared empty list (a valid, if pointless, configuration)
is never confused with "never declared, skip and say why." `date_year`
similarly defaults to unset, in which case check 2 only ever accepts a
plain `YYYY-MM-DD`, exactly as before this key existed.

**Honest coverage reporting.** `CheckResult` records not just findings but
`total`/`checked`: how many attempts the check considered, and how many it
actually evaluated (the rest skipped per-item, per above — currently only
check 2 has a per-item skip; checks 1 and 3 always have `checked == total`
since neither has a comparable "this one item is unparseable" case). A
check where `checked` is far below `total` — in the extreme, `checked ==
0` — must never render as "OK, no issues found": a real 23-row timeline
with no `date_year` configured has every single date fail to parse, and
printing that as a clean bill of health hides that the check ran and
verified nothing at all. `rce attempts --check`'s report always states the
coverage figure (`rce.cli._print_consistency_report`), and explicitly flags
zero coverage as not a clean result, rather than only ever printing
`findings`.

`rce attempts` with no `--check` lists what is registered — `#`, date,
verdict, and how many step files a row resolved to — the same non-judging
posture as the rest of this section. `--check` runs all three and exits
non-zero if any of them reports a finding; a skipped check never affects
the exit code, since a missing config declaration is a configuration gap,
not itself "a problem found" in the project.

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

Also now, though it predates this roadmap section's own last update: the W2
data-lineage extractor (`rce.ingest.dataflow`) statically scans `.py`/`.R`/
`.Rmd` sources for recognized read/write calls (`pd.read_csv`/`to_csv`,
`open()`, `read.csv`/`write.csv`, `ggsave`, ...) into
`script --reads/writes--> dataset|figure` edges — no model, no git required
(unlike `rce.ingest.pyfig`'s `generates` edge, whose src is resolved via
`git blame`). `rce lineage` (W4) is the read-only report surface over that
graph, and the one new user-facing exit this round: it writes nothing and
re-parses no source file, only querying edges `rce ingest` already wrote.
Four blocks, each scoped precisely rather than uniformly: **orphan inputs**
(a `dataset` read by a script but written by none — "where did this input
data actually come from", scoped to `dataset` only, since a `figure` is
normally an output, never an input a script reads back) and **duplicate
copies** (a read `dataset` whose basename also exists at other paths in the
project — "which of the N copies did the script actually read", again
`dataset`-only for the same reason) sit next to **lineage chains** (a
`dataset` *or* `figure` with both a writer and a reader — a produced-and-
consumed plot is exactly as traceable a chain link as a produced-and-
consumed dataset, so `figure` is included here) and **broken links** (any
`reads`/`writes` occurrence whose evidence carries `missing: true`,
regardless of node type — a script pointing at a file that genuinely isn't
on disk is the same finding whether that file would have been a dataset or
a figure). `--orphans` narrows the report to the first block alone; `--json`
gives the same four blocks as structured output. The command exits 1 if
either orphan inputs or broken links were found (the two blocks that
represent an actual gap) and 0 otherwise — chains and duplicates are
informational and never affect the exit code. An empty block is simply
omitted; only when every block is genuinely empty does the report say so
explicitly, stating what was scanned (script/edge/target counts) and that
none of it matched any of the four patterns — never a bare success line
with no content behind it, the same "a missing finding is a normal outcome,
but it must be stated, not indistinguishable from nothing having run"
posture Section 0 already asks of every other check in this document (see
e.g. `rce attempts --check`'s own coverage reporting above).

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

## Section 8 — The app: node canvas and native shell (task V4)

Section 7's "later" web view exists (tasks V1–V3: decision tree, lineage,
multi-project, auto-refresh, writing attempt rows back into the map file).
The researcher's verdict on it was exact: *"it still isn't an app"* — and,
asked what an app would be, two answers: **not a browser tab**, and **draw
the graph in the app, ComfyUI-style, and let me add the mappings between
figures, code and datasets myself.** This section is the design for both.
Every UI decision below is binding for whoever implements it; the visual
system is the one already carried by `src/rce/webapp/app.html` (paper /
ink / clay / ochre / olive / gray tokens, Songti for the brand mark and
headings, PingFang for UI, a mono face for paths). Nothing here introduces
a new palette, a new font, or a third-party dependency.

### 8.0 Why a canvas, and why ComfyUI is the right reference

The decision tree answers "what did I try and what did I decide"; the
lineage report answers "who wrote this file". Neither answers the question
a researcher asks while *building*: **which dataset feeds which script
produces which figure — and where is the link I know exists but the
machine could not see?** That is a graph question, and the honest UI for a
graph the user is expected to *edit* is a node editor: boxes with typed
sockets, links drawn between sockets, an infinite canvas you pan and zoom.
ComfyUI is the reference because its conventions are already muscle memory
for anyone who has used a node editor: drag from an output socket to an
input socket to connect; drag a node's title to move it; drag empty canvas
to pan; wheel to zoom; only *compatible* sockets accept a link. We adopt
those conventions wholesale and change only what the domain demands.

What we deliberately do **not** copy: ComfyUI's dark theme (this is a paper
tool), node creation from a blank search box (RCE never invents research
objects — every node is a file that exists in the project), and executing
the graph (RCE observes a pipeline; it does not run one).

### 8.1 Nodes: the object model on the canvas

Only three node types appear in v1 — exactly the three the researcher
named — plus attempts as *frames*, not nodes:

| Canvas node | Graph node type | Left sockets (inputs) | Right sockets (outputs) |
|---|---|---|---|
| 数据集 | `dataset` | 来源 (a script that `writes` it) | 数据 (feeds a script's 读取) |
| 脚本 | `script` | 读取 (datasets it `reads`) | 写出 (datasets it `writes`) · 生成 (figures it `generates`/`writes`) |
| 图表 | `figure` | 生成自 (the script that produced it) | — |

Socket compatibility is the whole grammar of what a human may draw:
`数据集.数据 → 脚本.读取` creates a `reads` edge; `脚本.写出 → 数据集.来源`
creates `writes`; `脚本.生成 → 图表.生成自` creates `generates`. Any other
pairing is refused at drop time with a one-line explanation in product
language ("只能把数据集接到脚本的「读取」插口"). This is how the canvas stays
honest: the user can only assert relationships the object model already
knows how to store, and every link on screen is a real edge type.

**Attempts are frames, not nodes.** An attempt row (`attempt` node) owns
step scripts (`attrs.step_files`). On the canvas an attempt is drawn as a
ComfyUI-style *group frame*: a dashed, lightly tinted rectangle behind its
scripts, titled `#16 · TopicShift→波动+采用` in the attempt's verdict color
(the same badge colors the decision tree already uses). Frames are
scope, not objects — you cannot connect a socket to a frame.

**Ghost nodes.** An attempt's `step_files` routinely includes outputs no
extractor produced a node for (a knitted `.pdf`, a `.docx`) — in the
researcher's real project every `17-….pdf`/`18-….pdf` is such a file. Those
appear on the canvas as *ghost nodes*: dashed border, muted title, tag
「尚未入图」, typed by extension (image/pdf → 图表; csv/dta/parquet/xlsx/rds
/feather → 数据集; py/R/Rmd/jl → 脚本 — the same extension classification
`rce.ingest.dataflow` already applies, a deterministic classification, not
an inference about relationships). The moment the user connects a ghost, it
becomes a real node (the mapping ingest upserts it, see 8.5). This is the
only way a node comes into existence from the canvas, and it is exactly the
case the researcher asked for: "my figure is not linked to my code — let me
link it."

### 8.2 Visual language

*Node anatomy.* A rounded card (radius 6px) on `--paper-alt`, 1px
`--line-strong` border, soft shadow only while dragging. A 26px title bar
carries a 3px left rule in the type color and a small mono type label
(`数据集` / `脚本` / `图表`); the body shows the file's basename in the UI
face at 13px and its directory in the mono face at 11px `--ink-soft`,
truncated from the left so the meaningful tail survives. Sockets are 10px
circles sitting *on* the card edge, labeled inside the card in 11px mono.
Type colors: 数据集 `--ochre` (raw material), 脚本 `--olive` (the work),
图表 `--ink` (the product). Clay is **not** a type color — in this app clay
means "a human did this" or "something is wrong", and we keep it that way.

*Node states.* Selected: 2px `--clay` border. Hovered: border to
`--line-strong` at full opacity. Missing on disk (the graph knows it, the
filesystem does not): dashed border + tag 「文件不存在」 in clay. Orphan
input (a dataset no script writes — the lineage report's own definition):
a clay dot on its 来源 socket, the same red dot the decision tree uses.
Ghost: see 8.1.

*Links.* Cubic Béziers with horizontal tangents (ComfyUI's curve), drawn
under nodes. **Machine-extracted edges** (any extractor other than
`mapping`): 1.5px `--ink` at 45% opacity, solid; `pending` status → dashed
(a candidate awaiting `rce confirm`); `rejected` → not drawn. **Human
mappings** (extractor `mapping`, 8.5): 2.25px `--clay`, solid, with a 6px
clay dot at the curve's midpoint. The two must never be confusable at a
glance — that is Section 4's "machine annotation vs. human judgement" rule
made visible. Hover shows a small card: for a machine link 「dataflow 提取 ·
第 15 行」 (extractor + evidence line when present), for a human link 「你于
2026-09-06 标注 · <note>」. A link whose endpoints are both selected/hovered
brightens to full opacity; while dragging a new link, compatible target
sockets pulse with a `--clay-soft` halo and incompatible ones dim.

*Canvas.* `--paper` background with a 24px dot grid in `--line` (dots, not
lines — lines fight the Béziers). A quiet toolbar floats top-left inside
the canvas: scope selector (8.7), search field 「查找节点…」, and buttons
适应全部 · 100% · ＋ · －. Bottom-left: a zoom readout in mono (`72%`).
No minimap in v1.

### 8.3 Interaction

| Gesture | Effect |
|---|---|
| Drag empty canvas · two-finger scroll · Space+drag | Pan |
| ⌘/Ctrl + wheel · pinch | Zoom 25%–250% about the cursor |
| Drag node title or body | Move (8px grid snap while ⇧ held); position persisted, debounced 400ms |
| Drag from an output socket | Start a link; compatible inputs highlight; drop on one → confirm popover (below); drop elsewhere → cancel, nothing happens |
| Click node | Select; opens the existing slide-out panel (file preview, Open, Reveal in Finder) |
| Double-click node | Open with default application (existing `POST /api/open`) |
| Click link | Select it; the hover card stays pinned |
| Right-click human link · ⌫ with a human link selected | 「删除标注」 with confirm |
| Right-click machine link | 「标记为错误提取」→ `set_edge_status(..., "rejected")` — the one human-only status path Section 4 already allows, reused rather than a second mechanism |
| Esc | Clear selection / cancel a link drag |
| F | Fit all visible nodes |

The **confirm popover** for a new link is the canvas's only write dialog:
one line stating the assertion in product language (`17-叙事更替与汇率波动.Rmd
生成 → 17-叙事更替与汇率波动.pdf`), an optional 备注 field, buttons 确认标注 /
取消. Confirm writes the mapping (8.5); the link appears as a human link
once the re-ingest lands (the existing generation poll does the
re-render; the UI draws it optimistically in the meantime and reverts with
an error chip if the write fails). Exactly the same "preview → explicit
confirm → file write → graph follows" shape as writing an attempt row.

### 8.4 Layout

**Auto-layout is a suggestion; an arrangement is a memory, and each view
keeps its own.** Until the researcher moves something, a view (a scope,
8.7) is laid out fresh each time it is drawn, among the cards visible in
that view only. The first time they move a card in a view, the whole view
is *pinned*: every visible card's position at that moment is saved for
that view, and from then on nothing in it moves unless they move it — the
ComfyUI contract. A card that appears later in a pinned view (a new script
after a re-ingest, a ghost) is placed by the steps below, clear of the
pinned cards, and joins the arrangement the next time anything is moved.
The same card may sit in different places in two views — an attempt's view
and 全部 are two pictures of one project, like two diagrams of one model.

(Two earlier rules failed on the researcher's real project and are
recorded so they are not tried again. *Global positions with a global
layout* scattered the ten cards of the current attempt across 4,000px, and
the default view opened as dust at 25% zoom. *Global positions with a
per-view layout, only moved cards saved* meant that nudging one card pulled
it out of its pipeline: on the next layout its neighbours re-packed without
it and the pipeline came apart.)

The layout of unpinned cards, client-side and with no library:

1. **Islands.** Split the cards into connected components (links taken as
   undirected). A research project is many small pipelines, not one deep
   graph; each pipeline is laid out on its own. For this purpose only, a
   card with no links counts as linked to the step script of its own
   attempt that shares its numeric step prefix (`17-….pdf` with
   `17-….Rmd`), so an output waiting to be connected sits beside the
   script that most plausibly made it — placement, never an asserted edge.
2. **Layer** within an island = longest path from a source. A dataset
   nobody writes is layer 0; a script is 1 + max(layer of the datasets it
   reads); a dataset or figure some script writes is that script's layer +
   1; a step-prefix companion sits one layer right of its script. (This is
   what makes the researcher's own pipeline read correctly: `16.py` writes
   `topicshift_monthly.csv`, which `17.Rmd` and `18.Rmd` read, so 17/18
   sit two columns right of 16 rather than beside it.) Cycles, should a
   pipeline contain one, are broken at the edge that closes them and that
   edge is drawn dashed in clay with a hover note 「检测到循环」.
3. **Order within a layer** by one barycenter pass (mean y of already
   placed neighbors), ties broken by the numeric step prefix of the path
   so step order survives. Columns 320px apart, rows packed with 24px
   gaps. A layer taller than 12 cards wraps into balanced side-by-side
   sub-columns of at most 12 (a 220px gap between them, inside the same
   layer band, which widens to hold them) so no island becomes a tower.
4. **Loose cards** — cards with no links and no step-prefix script — are
   not islands of one. They are gathered into a single grid block titled
   「未连线」 (quiet mono caption, `--ink-soft`), ordered by type then path,
   and shaped like the page rather than a strip: at least 4 columns, more
   when needed to bring the block toward 1.6:1.
5. **Packing.** Islands are placed in rows, left to right, 96px apart,
   wrapping to a new row at a target width. The target is at least W =
   max(widest island, √(1.6 × total island area)); the packer tries wider
   targets up to 2.5 × W and keeps the page closest to 1.6:1 (W alone
   leaves ragged rows and a page taller than it is wide). Order: the
   island holding the current attempt's scripts first, then by card count
   descending, ties by the smallest step prefix; 「未连线」 last. Blocks
   are packed clear of any pinned card.

「重新排列」 in the toolbar overflow forgets *this view's* arrangement and
therefore asks first (「将丢弃你在这个视图里摆放的位置」). The camera: on
entering a view with no saved viewport it fits all — the scope the
researcher opens on must be readable without touching anything — and until
they pan or zoom, it re-fits when the window is resized. Fit-all never
moves cards. When an unpinned view re-lays itself out because its links
changed (a link was just confirmed), the camera holds the card the
researcher was working on at the same point on screen, so the canvas moves
under their hand rather than away from it.

### 8.5 Human mappings are a file: `.rce/mappings.toml`

Section 4's doctrine — the researcher's own file is the truth and the graph
resyncs from it — applies to hand-drawn links exactly as it applies to
attempt verdicts. A link the user draws is therefore **appended to
`.rce/mappings.toml`**, a file they can read, diff, and commit; the graph
edge is derived from it by an ingest, never written directly by the UI.

```toml
# 手工标注的映射。RCE 只记录它能读到的；它读不到的，由你在这里补上。
# 此文件是唯一真相：图谱里的人工连线从它派生，删掉 graph.db 也不会丢。
[[mapping]]
from = "复现包_分步/17-叙事更替与汇率波动.Rmd"
to   = "复现包_分步/17-叙事更替与汇率波动.pdf"
type = "generates"                 # reads | writes | generates
note = "knitr 渲染产出"             # 可选
date = "2026-09-06"                # 标注日期（本地日期，写入时填）
```

Rules:

- `from`/`to` are project-relative paths, confined to the project root by
  the same resolve-then-`relative_to` check every other path takes (the
  write path is never less confined than the read path — a lesson already
  paid for in the V3 review). `type` must be one of the three; the
  (`from` type, `to` type, `type`) triple must satisfy the socket grammar
  in 8.1 or the ingest refuses that entry with a line number and keeps the
  others.
- The ingest (`rce mappings`, also run by the watcher — the file joins the
  watch set) upserts missing endpoint nodes (the ghost → real transition,
  typed by extension) and upserts each edge with `extractor = "mapping"`
  and status `confirmed` — the human wrote the file; the ingest is their
  hand, the same way `rce attempts` writes `human_fields.verdict` from the
  human's own table. No machine extractor may write `extractor =
  "mapping"`, and no machine re-ingest may remove or downgrade a `mapping`
  edge; the only thing that removes one is its entry disappearing from the
  file (resync-from-source, exactly as attempt orphans in Section 4).
- Writes from the canvas (add, delete) go through the same discipline as
  the map file: backup to `.rce/backups/`, atomic replace with fsync,
  re-ingest under the watcher's ingest lock, generation bump. The
  TOML is emitted by a small fixed-schema writer (stdlib has no TOML
  writer; the schema above is all it ever needs to emit) that preserves
  the header comment and entry order and appends new entries at the end.
- Duplicate assertions (same `from`, `to`, `type`) are refused at write
  time with 「这条映射已存在」; a mapping that contradicts a machine edge is
  *allowed* — the human is asserting the machine missed nothing/was wrong,
  and both lines will show, distinguishable by style.

### 8.6 Layout state is not truth: `canvas.json`

Arrangements and viewports are UI state — machine-managed JSON kept beside
the graph (8.10), safe to delete (everything re-lays out), not something
the researcher is expected to read. One entry per view:
`{"views": {"all": {"positions": {"script:复现包_分步/16-….py": [x, y], …},
"viewport": {"x": …, "y": …, "zoom": …}}, "attempt:…#16": {…}}}`. Written
atomically by a debounced `POST /api/canvas/layout` that names its scope;
a scope the project does not have is refused, so the file cannot grow keys
a page invents. Never backed up (there is nothing irreplaceable in it). A
missing, corrupt, or older-format file degrades to "nothing saved", never
to an error the user sees.

### 8.7 Scope and search

The researcher's project has dozens of scripts and datasets; the whole
graph at once is a hairball. The toolbar's scope selector defaults to
**the current attempt** (the ✅ row when there is exactly one; else the
most recent) and offers 全部 plus every attempt. Scoping to an attempt
shows its step scripts, every dataset/figure they touch, and one hop
further along `writes → reads` chains so upstream generators stay visible;
its frame is drawn; other attempts' nodes are simply absent, not dimmed.
Each view keeps its own arrangement and viewport (8.4, 8.6). Frames are
drawn in attempt views only: in 全部 an attempt's scripts are spread
through a larger pipeline and a bounding frame would swallow cards that
are not its own. Switching scope clears the selection and any pinned link
card — they belong to the view that was left. Search
(「查找节点…」) highlights matching nodes and dims the rest without
changing scope; Enter fits the camera to the matches.

### 8.8 Product language

All UI copy is Chinese product language — not only what V4 adds. The
three tabs read 「决策树」「血缘」「画布」, and the English left over from
V1–V3 is translated in the same pass. The brand mark stays `RCE`; its
subtitle becomes 「研究脉络」 (the old "decision tree & lineage" named two
of three views, in the wrong language). What stays as it is: file names
and paths, the researcher's own text (attempt titles, verdicts, column
names), and raw engine error strings — which remain English and live on
hover titles, behind a Chinese framing (「无法标注：…」). Nothing on the
canvas says "node", "edge", or "socket" to the user — it says 数据集, 脚本,
图表, 连线, 插口, 标注.

Binding glossary (one term per concept, everywhere it appears):

| Was | Is |
|---|---|
| Reads / READS | 读取 |
| Writes / WRITES | 写出 |
| No recorded reads or writes. | 没有读到读写记录。 |
| Orphan inputs | 无来源输入 |
| READ BY | 被这些脚本读取 |
| WRITTEN BY | 由这些脚本写出 |
| Lineage chains | 血缘链 |
| Broken links | 断链 |
| (reads, not found) / (writes, not found) | （读取，文件不存在）/（写出，文件不存在） |
| Duplicate copies | 同名拷贝 |
| OTHER COPIES | 其它拷贝 |
| Open | 打开 |
| Reveal in Finder | 在 Finder 中显示 |
| Close | 关闭 |
| Loading… | 载入中… |

Anything not in the table is translated in the same register — short,
concrete, no jargon — and the same English source always gets the same
Chinese.

**Errors.** A hover title is acceptable for a passive chip; it is not
acceptable as the only place an error lives when that error has just
blocked something the researcher tried to do (saving an attempt row,
confirming a link, switching project, opening a file, stopping the
service). Those show, in order: a specific Chinese sentence when the
engine names the cause with a machine-readable code (the attempt form's
own cases — a number that already exists, a number that is not in the
table, an invisible line-break character in a cell, a table that can no
longer be found — each get one); otherwise the Chinese framing; and in
both cases a 「详情」 toggle that reveals the engine's raw text inline, in
small mono `--ink-soft`. One click, never a hover. The command line stays English (it is a developer surface, and
its messages are quoted in docs and tests).

### 8.9 The native shell: `RCE.app`

"Not a browser tab" means: a real macOS application window — Dock icon,
menu bar, ⌘-shortcuts, no address bar, no browser chrome. The engine stays
Python and the interface stays `app.html`; the shell is a native window
that shows it, the same architecture Obsidian, Notion and VS Code use, and
the one that keeps every feature written once. The researcher chose this
over a SwiftUI rewrite knowingly.

- **Build.** A single Swift source file (AppKit + WebKit) shipped inside
  the package (`src/rce/webapp/shell/RCEShell.swift`) and compiled by `rce
  app` with the system `swiftc` — verified present on the researcher's
  machine with Xcode 16 — into `~/Applications/RCE.app/Contents/MacOS/RCE`.
  Zero third-party dependencies, no Xcode project, no signing beyond
  ad-hoc. If `swiftc` is absent, `rce app` falls back to the existing
  launcher-script bundle and says so in one line.
- **Lifecycle.** On launch the shell probes `http://127.0.0.1:7357/api/
  projects`; if nothing answers it spawns `rce serve --port 7357
  --no-browser` (the absolute venv path baked in at build time, exactly
  as the launcher script does) as a child `Process` and shows a placeholder
  page — paper background, the serif brand mark, 「正在启动引擎…」 — until the
  probe succeeds (≤10s; on timeout the placeholder shows the last lines of
  `~/.rce/serve.log`). On quit it POSTs `/api/shutdown` and terminates the
  child *only if it spawned it*; an engine the user started from a terminal
  is left alone.
- **Window.** 1280×840 default, 900×600 minimum, frame autosaved under the
  name `RCEMain`, title `RCE` (`RCE — <project label>` once the bridge
  reports one). Standard traffic lights, full-size content view off (the
  page has its own header). Closing the window quits the app.
- **Menus.** RCE (关于 RCE · 退出 ⌘Q) · 文件 (新增尝试 ⌘N · 在 Finder 中显示项目
  ⌘⇧R · 关闭 ⌘W) · 编辑 (the standard undo/cut/copy/paste/select-all set —
  without it no form field pastes) · 视图 (决策树 ⌘1 · 血缘 ⌘2 · 画布 ⌘3 ·
  重新载入 ⌘R · 放大 ⌘+ · 缩小 ⌘- · 实际大小 ⌘0 · 适应全部 F) · 窗口 (最小化 ⌘M ·
  缩放) · 帮助 (打开项目地图).
- **Bridge.** Native → page: `evaluateJavaScript("window.RCE && RCE.command('canvas')")`
  for every menu item; the page exposes one `window.RCE.command(name)`
  dispatcher and works unchanged in a plain browser (no bridge, no menus,
  nothing else lost). Page → native: a single `webkit.messageHandlers.rce`
  channel carrying `{type: "title", text}` on project switch. Links to any
  origin other than `127.0.0.1` open in the default browser. Developer
  extras off; JavaScript on; no other WebKit preferences touched.
- **Icon.** Generated at build time by the same toolchain (a tiny Swift
  program draws PNGs at 16–1024px; `iconutil` folds them into `.icns`):
  paper (`#F7F2E9`) squircle, and — echoing the canvas — three sockets in a
  left-to-right flow joined by two ink Béziers: an ochre dot (数据集), an
  olive rounded square (脚本), a clay dot (图表). At 16–32px the links drop
  and only the three marks remain. No text on the icon. The bundle is
  `dev.researchos.rce`, `LSUIElement` **false** (it is a windowed app now),
  replacing the V3 launcher-only bundle in place.
- **Origin.** WebKit sends the portless `Origin: http://127.0.0.1` on
  same-origin POSTs — the Safari behaviour the V3 fix already accepts; the
  shell adds no new origin shape.

### 8.10 Resilience rules surfaced by V3 in use

Three failures seen in the first week of real use become rules:

1. **The derived graph never lives inside a cloud-synced folder.**
   Observed: the researcher's project sits in `~/Documents`, which this Mac
   syncs to iCloud Drive ("Desktop & Documents Folders"). `sqlite3.connect`
   on `.rce/graph.db` blocked for over a minute — a file the sync provider
   has evicted or is mid-transfer is materialized on `open()`, and there is
   no non-blocking way to open it — so every DB-backed endpoint hung while
   `/api/generation` and `/api/projects` kept answering. SQLite under a
   file-provider sync is also a documented corruption path. The rule:
   `graph.db` moves out of the project to `~/.rce/graphs/<id>/graph.db`,
   `<id>` a stable hash of the project's *resolved* path, resolved by one
   helper (`rce.paths.graph_db_path(project_root)`) that every `_require_db`
   copy (cli, mcp_server, webapp) calls — no module computes the path
   itself. `.rce/` inside the project keeps only what the researcher owns
   and may want under git: `attempts.toml`, `mappings.toml` (8.5),
   `backups/`. `canvas.json` (8.6) lives beside the graph, not in the
   project — it is derived too. A legacy in-project `graph.db` is migrated
   on first touch by any subcommand: copied, checked with `PRAGMA
   integrity_check`, then removed, one log line; the migration and `rce
   init` both leave a one-line `.rce/README` saying where the graph went
   — the researcher who opens `.rce/` and finds no `graph.db` must find
   the answer in the same folder. `rce status` and
   `/api/summary` report the graph's actual location so nothing is hidden.
   Before opening, the server checks macOS's dataless flag on the file
   (`st_flags & SF_DATALESS`) and, if set, answers with a header state
   「图谱文件正在从云端下载…」 instead of blocking a handler thread.
2. **A project whose graph vanishes mid-serve must degrade, not deadlock.**
   Observed: the watcher raised the same "graph database disappeared" error
   once per 2-second poll, forever, into the log. The rule: a read opens
   its own connection and surfaces `ProjectNotInitializedError` as a header
   state (「项目不可用 — 图谱文件已不存在」) that leaves the project switcher
   usable; the watcher, on a missing graph, logs once, sets `last_error`,
   and stops re-ingesting that root until the file reappears. While a
   project state (「项目不可用…」/「图谱文件正在从云端下载…」) is showing, the
   refresh chip (「重扫失败…」) is suppressed — both are true, but one
   cause deserves one message, and the project state is the cause.
3. **A registry entry whose directory is gone is shown as such and can be
   removed from the switcher** (「移除失效项目」 next to a disabled entry), so
   the researcher never has to hand-edit `~/.rce/projects.json`.

### 8.11 Deliberately later

Minimap; multi-select and box-select; adding an arbitrary project file as a
node from a picker (ghosts from `step_files` cover the researcher's actual
case); frames for anything other than attempts; drawing `cites`/`supports`
links (claims and references are not on this canvas); a Windows/Linux
shell. A project folder that is *moved* gets a new graph id and so an
empty graph (re-ingest is cheap and deterministic); noticing the orphaned
graph directory and offering to re-attach it is later work.

### 8.12 Rulings made during implementation

Decisions the implementers took where this section was silent, reviewed
and adopted as design:

- **A `reads` mapping is written the way the canvas is drawn.** In
  `mappings.toml`, `type = "reads"` has `from` = the dataset and `to` = the
  script (the link runs 数据 → 读取); the graph still stores `script
  --reads--> dataset`, as the dataflow extractor does. A reversed entry is
  refused with a "swap from and to" hint, never silently flipped.
- **A missing `mappings.toml` is not a deletion; an empty one is.** Failing
  to find the source is not evidence that the researcher retracted
  anything (the attempt-orphans doctrine of Section 4). To retract every
  mapping, empty the file.
- **The file outranks `rce confirm`.** A mapping edge is re-confirmed on
  every ingest; the way to retract one is to delete its entry.
- **Undo restores what was there.** 「撤销」 after 「标记为错误提取」 returns the
  link to the status it had before (a link the researcher had confirmed
  comes back confirmed), not to a default.
- **Rejected links are wrong everywhere.** The 血缘 report skips rejected
  edges just as the canvas does, so the two views never disagree about
  whether a dataset has a source.
- **Mappings are read at startup.** The first poll of a project ingests
  `mappings.toml` once, so edits made while the app was closed are not
  lost; attempts are still only re-read when their file changes.
- **The app only stops what it started.** On quit the shell names its
  child's pid to `/api/shutdown`; an engine started from a terminal
  refuses and keeps serving.
- **The last tab is remembered; the scope is not.** Reopening the app
  returns to the view last used, always on the current attempt.
- **A frame holds its own.** A frame is drawn around its members outside
  the 「未连线」 block, so one stray step file cannot stretch it across the
  page.

## Section 9 — Human records outlive the index (task V5)

*Status: APPROVED FOR IMPLEMENTATION, 2026-10-05. Drafted from a
four-reader inventory of the code, revised against five adversarial
design reviews (73 findings, 8 of them blocking), then reviewed by the
researcher's external reviewer at commit 976059a: "the main design can
pass; five conventions must change first". All five are adopted below
(9.3 ordering; 9.11 what a fingerprint proves, references, coverage of a
comparison, crash order). The researcher then delegated the remaining
decisions to the design lead ("你自行决定") and asked for a complete,
working app to try; 9.10 records what was decided.*

**The goal, in the researcher's words: a judgment the user has made is kept
for the long term, independently of the machine index, which can always be
rebuilt.** A stable project identity answers "after the folder moves, find
the same data again". It does not answer "after the database is rebuilt,
get the human records back". This section answers both.

### 9.0 What was found (2026-10-04, read from the code and by experiment)

Section 4 already says the researcher's own file is the truth and the graph
follows it. Two kinds of human labor obey that rule today (attempt verdicts
in the Markdown table; hand-drawn links in `.rce/mappings.toml`). The rest
does not:

- A confirm or a reject on a machine-extracted link exists only as
  `edges.status` in `~/.rce/graphs/<path hash>/graph.db`. No time, no note,
  no history, and no record of the evidence the researcher was looking at.
  `updated_at` is overwritten by every later scan. Undo deletes its own
  memory.
- Rebuilding the database loses every confirm and reject.
- Moving or renaming the project folder strands the database and the
  canvas arrangement under the old path's hash. Worse: a *different*
  project later created at the old path silently inherits the old one's
  judgments, and the moved project's stale registry entry still serves the
  stranded database from a path that no longer exists.
- When the evidence under a judgment changes, the judgment is silently
  carried over. (Experiment: a confirmed read stayed confirmed after the
  call was deleted from the script; a confirmed claim–metric link stayed
  confirmed, still quoting 0.873, after the metric became 0.95.) When the
  *identity* of the judged thing changes (file renamed, sentence reworded),
  the judgment stays on the old id with nothing pointing to the new one.
- Canvas arrangements were classified "safe to delete" (8.6) and sit beside
  the database. The researcher counts them as labor; so they are.
- Two processes writing the same record file lose updates (600 concurrent
  position writes → 201 survive, 279 raise), because every lock is
  in-process and the temp-file name is fixed.

### 9.1 The rule

Two stores of opposite nature, and one direction between them.

- **The record** (人工记录) lives inside the project, in `.rce/`, as plain
  files the researcher can read, diff, commit, and back up with the
  project. It is never derived from anything.
- **The index** (机器索引) is `graph.db` under `~/.rce/graphs/<project
  id>/`. It is derived from the project's sources plus the record. Deleting
  it loses nothing.

**The index may forget; the record may not. Whatever the index knows about
a human judgment, it learned from the record.** Every human action is
written to the record first and only then reflected in the index — through
one code path, whichever surface it came from (canvas, CLI, MCP). No
surface may write a human status to the index directly. The record files
join the watch set and are applied on first sight of a project, as
`mappings.toml` already is (8.12), so an index can lag the record only
until the next poll.

One consequence for Section 4: orphan claim and attempt nodes were kept
forever when a link on them had been judged, "because the graph is the
sole record of that decision". It no longer is. Cleanup removes such
orphans like any other; the ledger keeps the judgment (9.6 says how it is
shown).

### 9.2 Inventory: what counts as human labor, and where each lives

| Human labor | Today | Source of truth after V5 | Backup |
|---|---|---|---|
| Attempt verdict and result (尝试结论) | the researcher's Markdown table | unchanged | before each app write (as today), **and** a snapshot the first time RCE sees the file changed each day — which is what covers hand edits |
| Hand-drawn link and its note (手工连线、备注) | `.rce/mappings.toml` | unchanged | same |
| Confirm / reject of a machine link (确认、拒绝) | `edges.status` in the index only | **`.rce/judgements.toml`** (9.3) | daily snapshot; and the file is append-only, so it is its own history |
| Undo, withdraw (撤销、撤回) | an evidence key, erased on use | entries in `judgements.toml` | same |
| Note on a judgment (备注) | does not exist | optional `note` on the entry | same |
| Canvas arrangement, per view (画布位置) | `canvas.json` beside the index | **`.rce/canvas.json`** | daily snapshot, and before 「重新排列」 discards an arrangement |
| Variable definition, construction and the reason for it (变量定义卡) | nowhere — a shorthand name in the attempt table | **`.rce/variables/<id>/`** (9.11) | daily snapshot; each confirmed version is also kept as a frozen byte copy that is never rotated |
| Attempt-table configuration | `.rce/attempts.toml`, hand-written | unchanged | daily snapshot when changed; RCE never writes it |

Snapshots go to `.rce/backups/`, newest 20 per file. A snapshot per day
rather than per write, because a review session of thirty clicks must not
rotate the last good copy away.

Not human labor, and said so: the last-used tab; the project registry
(`~/.rce/projects.json`); a model's second opinion from `rce judge`
(`semantic_review` — a machine annotation; it can be recomputed, at a
cost, and its persistence is out of scope here).

### 9.3 The judgment ledger: `.rce/judgements.toml`

Append-only. An RCE write is the file's existing bytes plus the new
entry's bytes; existing text, comments and unknown keys are never
re-emitted. History is therefore not a feature to build — it is the file.

```toml
# 你对机器提取结果的判断。RCE 只追加、不改写；图谱里的确认/否决从这里派生。
[[judgement]]
id       = "j-0b6f3c1e9a4d4f7ea2c5d8e1f0a3b6c9"   # random, unique
seq      = 41                            # assigned under the lock: the order of appending
at       = "2026-10-04T21:15:03+02:00"   # for display only — never decides anything
verdict  = "rejected"       # confirmed | rejected | withdrawn | undone
src      = "script:复现包_分步/17-叙事更替与汇率波动.Rmd"
dst      = "dataset:复现包_分步/Data/panel_pricing.csv"
type     = "reads"
extractor = "dataflow"
via      = "canvas"         # canvas | cli | mcp | migrated | recovered
note     = "这里读的是旧版面板"   # optional
[judgement.basis]           # what the researcher was looking at (9.6)
calls = ["read.csv"]
```

- A link is identified as it is in the index: (`src`, `dst`, `type`,
  `extractor`). Node ids are project-relative (verified: none contains the
  project root), so the ledger is valid wherever the folder goes. The
  ledger never holds `extractor = "mapping"`: a hand-drawn link has one
  authority, `mappings.toml`, and every write path refuses a second.
- **Two different acts, two verdicts.** 「撤销」 takes back the researcher's
  *last act* on a link: an `undone` entry naming the entry it cancels
  (`undoes = "<id>"`), after which the link is whatever it was before — a
  link confirmed, then rejected by a mis-click, then undone, is confirmed
  again (8.12's rule, kept). 「撤回」 takes back the *judgment*: a
  `withdrawn` entry, after which the link is whatever the machine says.
- **The state of a link** is its last entry *in the order of appending*,
  among entries not cancelled by an `undone`. That order is the file's
  order, and each entry RCE writes carries `seq`, one more than the
  highest in the file, assigned while the project lock is held. **A
  clock never decides.** `at` is there to be read by a person: a clock
  that is corrected, or another machine's clock running behind, must not
  be able to put a withdrawal before the confirmation it withdrew. (An
  earlier draft ordered by `at`; the external review caught it.)
- **Two histories are a conflict, not a race.** If `seq` repeats or runs
  backwards, two machines appended to different copies of the file and
  something merged them. RCE does not pick a winner — not by time, not by
  position. Every link that has an entry at or after the first anomaly is
  shown as 「记录冲突，待处理」 at the machine's status, with both
  histories, until the researcher writes a new entry for it; that entry,
  appended last and by them, settles it. A hand-written entry without
  `seq` takes its place by position and is not an anomaly.
- **`basis`** is the heart of 9.6: the substantive facts the link rested on
  when the researcher judged it. It is stored readable, so that in a year
  the researcher can see *what* they confirmed, not merely *that* they did.
- Hand edits are legal; it is the researcher's file. An entry that fails
  validation makes the whole ledger unreadable, and RCE names the line.

**A ledger RCE cannot trust is never built upon.** While the ledger is
missing though the project has one (`.rce/project.toml` records that it
does, from the first entry on), still in the cloud, or unreadable:

- the index keeps the human state it had (Section 4: failing to read the
  source is not evidence of deletion); a brand-new index shows none and
  says why;
- **every write to the ledger is refused** (「判断记录文件当前无法读取，请先
  恢复它」). RCE never replaces a file it could not parse and never
  recreates one that should exist — otherwise one click on a morning when
  the sync service is mid-transfer would found a new one-entry ledger and
  the next read would retract everything else.

**A ledger that has shrunk is not obeyed silently.** The index keeps a
copy of every entry it has applied. That copy is a safety net, never a
second authority: if a readable ledger lacks entries the index applied (a
sync service kept the other machine's file; a truncation that still
parses; a zero-byte download; a restore from backup), RCE changes nothing
and asks — 「记录文件比图谱少了 N 条判断」 with two answers, 「以文件为准」
(this is what a deliberate restore wants) and 「把缺少的补回文件」 (appended
with `via = "recovered"`). The same question, mirrored, when the file has
entries the index never saw is not a question: they are simply applied.

`mappings.toml` keeps its format and its 8.12 rules.

### 9.4 Project identity

A project is identified by a file it carries, not by where it sits.

```toml
# .rce/project.toml
id      = "p-3f9c2a7e5b1d4c68a0e7b2d94c1f6a35"   # random, assigned once
created = "2026-10-04"
ledger  = true                  # set when judgements.toml gets its first entry
# forked_from = "p-…"           # only on a folder declared an independent branch
```

The index lives at `~/.rce/graphs/<id>/` and remembers its home in
`home.json` beside `graph.db`: the folder's canonical path (as the file
system stores it) and its device and inode numbers. "This folder is the
home" means *the same directory*, whatever it is spelled as — a change of
letter case, a Unicode-normalization variant or a symlink is not a move,
and only rewrites the spelling.

**The identity check is the first thing every entry point does** — every
CLI subcommand, `rce serve` with or without a path, a project switch in
the app, the MCP server — before anything is written: no registry entry or
recency bump, no README, no scan, no project node.

| Situation | What it is | What happens |
|---|---|---|
| id present, an index for it exists, its home is this directory | the normal case | open |
| …its home is elsewhere, and the old home is **confirmed** gone (its volume is mounted, its parent is readable, the folder is not there) or confirmed to carry a different id | **moved or renamed** | adopt: update `home.json` and the registry entry (path and label); one log line |
| …its home is elsewhere, and the old home still carries this id | **a copy** — two live folders, one identity | stop and ask (below) |
| …its home is elsewhere, and the old home **cannot be checked** (volume not mounted, unreadable, its identity file still in the cloud) | cannot tell a move from a copy | stop and ask, with a third answer 「原位置暂时不可用，先只读打开」 |
| id present, no index for it on this machine | restored, cloned, synced from another Mac, or the index was deleted | build the index from sources and the record |
| no `project.toml`, but `.rce/` holds record files | the identity file was lost, or `.rce/` was copied in from elsewhere | stop and ask; never mint an id silently over existing records |
| `project.toml` exists but cannot be read (in the cloud, unparseable, or a sync conflict copy sits beside it) | cannot tell who this is | stop: 「项目身份文件无法读取」; nothing is written |
| no `project.toml`, no record files | not an RCE project, or one from before V5 | `rce init`; and see 9.5 for what is checked first |

"Gone" is never inferred from failing to look. That is the Section 4
error in another coat: an unmounted disk is not a deleted folder.

**A copy is never guessed at.** Until the researcher answers, nothing is
written — not the index, not the record, not the registry. The answers:

- 「作为独立分支继续」 (`rce project fork`): this folder gets a new id and
  `forked_from`. Its record files came with the copy, so its judgments
  start equal to the original's and diverge from here; it gets its own
  index. (If `project.toml` is tracked by git, RCE says that committing it
  will carry the new identity into whatever branch it is merged to.)
- 「这里才是原项目」 (`rce project claim`): the index's home becomes this
  folder **and the index is rebuilt from this folder** (9.8), so nothing
  from the other folder's scans or record survives in it. The other folder
  is asked the same question when it is next opened.
- 「这是另一个项目」: for a folder that merely received a copy of someone
  else's `.rce/`. A new id with no `forked_from`; the copied ledger,
  arrangement and variable cards are moved into `.rce/backups/`, since
  they speak about another project's files. (A variable reference such
  as `topicshift@v2` resolves within one project id: versions confirmed
  before a fork are the same in both folders, later ones are not.)

**Identity is re-checked at every write, not only at open.** A running
engine holds a folder open for hours; the folder can be moved in Finder
under it, or claimed elsewhere. Under the project lock (9.7), each write
to a record file or to the index first confirms that the served folder
still exists, still carries the id, and is still that id's home. If not,
it writes nothing and the page says 「项目已移动或已在别处认领，请重新打开」.
Record writers create `.rce/` only inside a folder that exists; they never
re-create a folder that has gone.

Consequences worth stating. A new, unrelated project created at a path an
old project used to occupy has a different id (or none) and inherits
nothing. A registry entry is `{id, path, label}`, keyed by id; a moved
project updates its entry instead of adding one, and an entry whose folder
is gone is shown as 「找不到项目文件夹（可能已移动）」 with 「选择新位置…」 —
the folder chosen goes through the same identity check, so a moved project
can be re-attached from the app, which is how the app (started with no
path) finds it again; RCE never searches for a folder by itself. The
`project` node in the index is `project:<id>`. The same project synced to
two Macs is one identity with one local index per machine.

### 9.5 Migrating what exists

Before V5 the judgments sit in indexes keyed by a path hash — and a path
hash does not prove whose they are. So migration is **an explicit act,
never a side effect of opening a folder**, and it accounts for every old
index on the machine, not just the obvious one.

**What is looked for.** On every open, whether or not the folder already
has an id, RCE checks for un-retired pre-V5 databases that may hold this
project's judgments: the index at this path's hash; a legacy
`.rce/graph.db` inside the folder (pre-8.10); and — listed by `rce migrate
--list` for the researcher to recognise — every other un-retired index
under `~/.rce/graphs/`, each with the project path it recorded, whether
that path still exists, and how many judgments it holds. Indexes stranded
by a move before V5 are found this way, and so is the second Mac's own
index when the identity file arrived by sync.

**What the researcher is shown before anything is exported**: where the
index says it came from, how many confirms and rejects and arranged views
it holds, and how many of the judged links' endpoints a scan of *this*
folder actually produces. A folder that merely reuses an old project's
path will show a low match; 「这不是这个项目的」 leaves that index alone.
Until a pre-V5 project is migrated it opens for reading; writing a human
record waits for the migration, so that nothing new lands in the old
store.

**The steps**, under the project lock from the first to the last:

1. **Export.** Every confirmed or rejected machine link in the old index
   becomes ledger entries (`via = "migrated"`; a reject that remembered a
   prior confirmation becomes two entries, in order; `mapping` links are
   skipped — their truth is already a file). An arrangement is copied to
   `.rce/canvas.json` only if none exists there. Exporting twice adds
   nothing. Exporting a second old index merges by content; a migrated
   entry that contradicts the state the ledger already has for that link
   is recorded and the link is put under review (9.6) — two machines
   disagreed, and a migration date must not decide between them.
2. **Basis.** The evidence at the moment of the original judgment was
   never kept. The old index's evidence is an accumulation of everything
   ever seen. So: if those accumulated occurrences yield exactly one basis
   and it equals what a fresh scan yields, that basis is recorded
   (`basis_recorded = "at-migration"`). Otherwise the entry records what
   the old index held and the link comes up **under review** — a judgment
   that was already silently carried over to changed evidence is not
   re-certified on the new evidence by the act of migrating it.
3. **Identity.** `.rce/project.toml` is created exclusively and never
   overwritten; it carries `migrating_from` until step 5, and any open of
   such a folder resumes here rather than treating it as migrated.
4. **Rebuild and verify.** Build a new index for the new id from scratch —
   sources, then the record — and reconcile it against the *old index's own
   count*, not against what the exporter says it exported: **M** judged
   links in the old index, each matched by key to the ledger and found in
   the new index as exactly one of *applied*, *under review*, or *under
   review because no scan produces the link any more*; unmatched must be
   zero. A source file that could not be read during this scan is a fourth
   count, 「来源文件暂不可读」, and any number other than zero there stops
   the migration — an evicted file must not be mistaken for a vanished
   link. Attempt verdicts and hand-drawn links in the new index must equal
   *their source files*; where the old index differed it is listed as a
   stale mirror, not a failure. The tally is printed.
5. **Only then retire the old index**, and only if no other process holds
   it open (an engine or MCP server still running old code would go on
   writing into a retired file: 「请先退出 RCE 与 MCP 服务」). It is renamed
   into `~/.rce/graphs/.retired/<hash>-<date>/`, not deleted, and the
   command says where it went.

If step 4 does not balance, nothing is retired: the half-built index is
removed, the old one keeps serving, what did not match is shown, and a
retry duplicates nothing. The researcher's own project is migrated by hand
at acceptance, after a backup, as the graph move was.

### 9.6 When the evidence changes

A judgment is a statement about particular evidence. It is applied only
while that evidence stands; otherwise it is kept, shown, and waits.

**Basis.** Each extractor defines, for its links, a small canonical record
of the facts a link rests on, and nothing positional. Line numbers,
timestamps, run counters, the name of a receiving variable and the
spelling of an expression are not facts.

| Extractor | Basis |
|---|---|
| `dataflow` reads / writes | the set of bare call names that produced the link (`read_csv`, `open`; in R the function without its package prefix: `read.csv`, `read_dta`). The path is not repeated: the resolved path *is* the link's `dst` |
| `claims` backed_by | the claim's normalized sentence and printed number; the names of the metrics of that experiment that match it, each value rounded to the claim's printed precision |
| `pyfig` generates | the bare call name |
| `mlflow` / `wandb` produces | the artifact path |
| `latex` / `mdpaper` includes, cites; `git`; `attempts_consistency` | the link's identity alone |

**What a scan must report**, which it does not today. The basis compared
is the one produced by *this scan*, not the index's accumulated evidence.
And a link can only be said to be "no longer produced" by a scan that
actually read its source. So every extractor reports, per source, one of
*read and parsed* / *unreadable* / *unparseable* (today an unparseable
script and a script with no calls look the same). A link's source is the
file named in its evidence, or for experiment links the tracking store
read in that run; a file absent from a successfully read inventory is an
observation too. A scan speaks only for the extractors it ran and the
sources it read; everything else keeps its previous state.

**On every scan, for each link whose ledger state is a verdict:**

- **Same basis** → the judgment applies. A line inserted above, a renamed
  variable, a rescan: nothing happens, and nothing is written.
- **Anything else** → the judgment is **not applied**. The link is shown
  at the machine's own status and marked 「待复核」, with the old verdict,
  its date, its note, its basis, and the reason, which is one of:
  - 「依据已变化」 — the link is still produced, on a different basis;
  - 「机器不再得出这条关联」 — both ends are still in the scan, the link is
    not (the call was removed; the metric no longer rounds to the printed
    number);
  - 「关联的一端不在本次扫描结果里」 — a file was renamed or removed, a
    sentence was reworded so that the claim has a new id.

  The wording says what the scan did, never that something "no longer
  exists": a path the extractor could no longer resolve is not a deleted
  file. A source that could not be read is none of these — it is 「来源文件
  暂不可读」, and the judgment stays as it was.
- The ledger is not touched by any of this. The researcher settles a
  review with a new entry: 「仍然成立」 (the same verdict, recorded on the
  basis as it is now), the opposite verdict, or 「撤回」. If the same link
  comes back with the same basis, the judgment applies again by itself,
  because the facts it was made on are back.

**No transfer, but a prompt.** RCE never moves a judgment onto another
link by itself. But when a judged link stops being produced, the review
item lists the links that *appeared in the same scan from the same source
with the same type and the same basis* — the renamed script's read of the
same file — and each of those links carries the hint 「可能对应一条待复核的
旧判断」. Applying the old verdict to one of them is the researcher's click
and a new ledger entry. This is how "the new state has a prompt" is met
when an edit changes an id.

**Where it shows.** A link under review is marked as such in every place
that shows links — canvas, 血缘, 决策树, `rce lineage`/`trace`, MCP —
because a link the researcher had rejected reappears at the machine's
status, and must not pass for an ordinary one. The header counts
「待复核：N」; a link under review is counted there and not in 「待确认」.
Clicking the count lists the items in the existing side panel with the
three actions; `rce review` prints the same list. The canvas gets nothing
else in V5.

What V5 does **not** change: machine links nobody judged, which a scan no
longer produces, stay in the index and in the views as they do today.
Cleaning those up is a separate decision (9.10).

### 9.7 One writer at a time

Every write to a record file, and every scan that writes the index, takes
a cross-process lock on the project: `flock` on
`~/.rce/locks/<project id>.lock` — local, never synced, never evicted, and
the same file for every spelling of the folder (before a project has an
id, the lock is keyed by its canonical path). Temp-file names are unique
per write. Two engines, or an engine and a CLI command, may run against
one project; they take turns.

Record files sit in a folder that may be cloud-synced. Before reading
one, RCE checks whether the sync service has evicted it (8.10's dataless
flag), asks for the download, and says 「记录文件正在从云端下载…」 rather
than blocking. If the sync service leaves a conflict copy of a record file
(`judgements 2.toml`), RCE says so and merges nothing by itself; entry ids
make a merge tool possible, and that tool is not V5.

### 9.8 Commands

- `rce records` — the inventory of 9.2 for this project: where each kind
  lives, how many, the newest snapshot. `rce records --verify` checks, per
  link, that the index's human state is what the record implies, and exits
  non-zero if not.
- `rce rebuild` — build a fresh index beside the current one, apply the
  record, and compare human state per link before and after: on unchanged
  sources, a judgment applied before and not applied after is a failure,
  and any unreadable source blocks the swap. Then swap; the previous index
  is kept one generation.
- `rce review` — the list of 9.6. `rce confirm` gains `--note` and the
  `withdrawn` and `undone` verdicts and writes the ledger like every other
  surface; MCP's `confirm_edge` is narrowed to the same verdicts and, like
  the canvas, refuses hand-drawn links.
- `rce migrate`, `rce migrate --list`, `rce migrate --from <dir>` — 9.5.
- `rce project fork` / `rce project claim` — 9.4.

### 9.9 Acceptance

Each scenario runs on a fixture that holds, at the start: one confirmed
machine link, one rejected, one confirmed-then-rejected-then-undone (so:
confirmed), one hand-drawn link with a note, an attempt table with
verdicts, and a canvas view arranged by hand. "All records present" means
all six, each readable through the app and through `rce records`.

1. **Move.** Move the folder elsewhere and open it there — from the CLI,
   and from the app through 「选择新位置…」: all records present; one
   registry entry, at the new path; the old path is not servable; the
   index directory did not change. Move it *while an engine is serving
   it* and click a judgment: nothing is written at the old path, and the
   page says to reopen.
2. **Rename**, including a change of letter case only: as 1, with no copy
   question.
3. **Copy.** Open a copy: blocked, with the answers, and nothing written
   (registry included) until one is taken. *Fork*: new id, `forked_from`,
   all records present in both; a later judgment in the copy does not
   appear in the original. *Claim*: the index is rebuilt from the
   claiming folder, `rce records --verify` passes there, and the original
   is asked on its next open. With the original on a volume that is not
   mounted: the third answer, read-only, and no adoption.
4. **Path reuse.** After V5: move the project away, create an unrelated
   project at the old path — it has no judgments, no arrangement, its own
   id. Before V5: the same, then upgrade — the new folder is shown the
   old index with its low match, declines, and inherits nothing.
5. **Rebuild.** Delete `~/.rce/graphs/<id>/` and open the project: all
   records present; `rce records --verify` passes. `rce rebuild` on a
   healthy project: the same, and the per-link comparison is clean.
6. **Repeated scans.** Run every scan three times: the ledger is
   byte-identical, the index's human state is identical, nothing comes
   under review.
7. **Backup and restore.** Copy `.rce/` aside; make further judgments;
   restore the copy over `.rce/`: RCE reports that the file has N fewer
   judgments than the index and asks; 「以文件为准」 gives exactly the
   copy's records. Restore the whole project folder from an earlier copy
   on a machine with no index for it: the same records, no question.
8. **Evidence changes.** (a) Insert lines above a judged call; rename its
   receiving variable or the import alias: the judgment applies, nothing
   is flagged. (b) Change which function is called: 「待复核 · 依据已变化」,
   the old verdict and basis visible, the link at the machine's status and
   marked in every view; 「仍然成立」 clears it and the ledger shows both
   entries. (c) Delete the call; change a metric so it no longer rounds to
   the claim's number: 「待复核 · 机器不再得出这条关联」; put it back: the
   judgment applies again with no new entry. (d) Rename the script: the old
   judgment is under review with its reason, the new read is listed beside
   it as a candidate and carries the hint; nothing is carried across until
   the researcher clicks. (e) Make the script unparseable, and separately
   unreadable: nothing comes under review; the judgment is unchanged and
   the source is reported.
9. **Migration.** A pre-V5 index holding: a confirmed link; a rejected one
   with and one without a remembered prior status; a judged link whose
   stored evidence shows two different bases; a judged link the script no
   longer produces; a preserved orphan claim and a preserved orphan
   attempt, each with a judgment; a judged hand-drawn link; arranged
   views. Migrate: the tally starts from the old index's own count and
   balances with nothing unmatched; the two-bases link is under review,
   not re-certified; the old index is retired only afterwards, and not
   while another process holds it. Make verification fail on purpose, and
   kill the process between each pair of steps: nothing is retired, the
   old index still serves, and a retry resumes and duplicates nothing. A
   second old index for the same project (another machine's) merges, and
   its one contradicting verdict comes under review.
10. **Two writers.** Two processes each make 300 judgments and 300
    position changes on one project at once: every one is in the record
    afterwards, and nothing raises.
11. **A record RCE cannot trust.** Make the ledger unparseable; remove
    it; make it zero bytes; truncate it at an entry boundary: in each
    case the index keeps what it had, the app says what is wrong, **a
    click on confirm or reject writes nothing**, and repairing the file
    restores normal operation. Truncated and zero-byte ask the 9.3
    question.
12. **History.** Confirm; reject; undo — the link is confirmed; confirm
    again with a note; withdraw — the link is the machine's. The ledger
    holds five entries in order, the app shows the last state, and the
    others as its history. **Set the system clock back an hour between two
    of them: the later act still wins.** Merge two copies of the ledger
    that each appended after the same entry: the links they touch show
    「记录冲突，待处理」, nothing is decided by time, and a new entry by
    the researcher settles each.

### 9.10 Not in V5, said plainly

The result-review flow (entering from a figure, a PDF or a claim) — the
next phase; it will read the review list this phase creates. Folding the
attempt table's stale-verdict check into that list. Removing from the
views machine links nobody judged that a scan no longer produces. Merging
two diverged copies of a record file. Keeping `rce judge` annotations
across a rebuild. Any new canvas feature.

One limit the researcher should approve with open eyes: **a judgment on a
claim loses its link whenever the claim's id changes, and that id changes
often.** Verified causes: rewording the sentence; changing the printed
number; renaming the section heading (every claim in the section);
renaming or moving the file (every claim in it); and, in Chinese prose
written one paragraph per line, *any* edit in the paragraph, because 。！？
are not yet treated as sentence ends. V5 guarantees such a judgment is
kept, shown under review with its original sentence, and offered against
the candidates that replaced it — not that it stays attached. **Decided
2026-10-05:** the sentence splitter is fixed in this phase (。！？ become
sentence ends), because the cheapest moment to change how a claim is
identified is now, while the researcher's project holds no judgment on any
claim. Reviews on claims are computed from the start; the external
reviewer's condition — no reminders at volume while unrelated edits still
raise them — is met by fixing the cause first.

**Decided 2026-10-05, under the researcher's delegation.**

- A project from before V5 is read-only for human records until it is
  migrated; RCE writes a human record only after the migration's tally
  has balanced.
- V5 is built in stages, each with its own acceptance run, and delivered
  to the researcher as one working app: the record and the lock; project
  identity; scan reports and per-scan basis; the ledger driving the
  index, with review states; migration; `records` / `rebuild` / `verify`;
  the app's review list, history and blocking states; variable cards.
  The order of entries, immutable references and the snapshot-before-entry
  rule are fixed from the first stage — a format written to the
  researcher's disk is the one thing that cannot be changed cheaply later.
- Cards are written in their files; the app shows them and offers the
  actions RCE records. The cost of filling a card is measured on a real
  one before a form is considered.
- The first card is one variable taken end to end — `TopicShift` — before
  `RV` and `NetBuyRate`. RCE's maintainers may draft a card from the
  source with the evidence attached; it stays a draft until the researcher
  has read the meaning and written the reason.

### 9.11 Variable definition cards

*Added at the researcher's direction after draft 2, and revised after two
adversarial design reviews (27 findings, 3 blocking): "protecting human
labor" must protect not only the judgments but the variable definitions
those judgments were made on.*

**The gap.** In the researcher's project a variable exists only as a
shorthand in the attempt table — `TopicShift→RV/NetBuyRate(月)`, `私有
Shannon 熵→lnRate(月,ECM)` — and as a substring in the dead-variable list.
Nothing records what `RV` means, which returns it uses, how it is
aggregated, what happens to missing trading days, or why that definition
was chosen over another. A script path does not answer "what is this
indicator and why is it built this way"; a name does not answer "how was
it computed". The card keeps both, and keeps them apart: **the research
definition is the researcher's statement; the implementation is where RCE
can check that statement against the files.**

**Layout: what the researcher writes and what RCE records never share a
file.** One directory per variable; its name is the variable's id.

```
.rce/variables/topicshift/
    v1.toml         the researcher's text for version 1 — RCE never writes into it
    v2.toml
    log.toml        appended by RCE when the researcher acts: confirmed, reaffirmed,
                    corrected, abandoned, revived — an append-only ledger, exactly 9.3
    frozen/<hash>.toml    a byte copy of each version as confirmed (and as corrected)
.rce/variables/_code/<hash>.<ext>   a copy of each implementing script as confirmed
```

(The first draft put status and RCE's checks inside the researcher's
blocks of one file. That cannot be done by appending — a table appended at
the end of a TOML file attaches to the *last* version, not the one being
confirmed, verified with the parser — so it would have had RCE rewriting
the researcher's text. It is recorded here so it is not tried again.)

A version file, written by the researcher:

```toml
name    = "TopicShift（叙事更替）"
aliases = ["TopicShift"]        # the spellings used in the attempt table

# 含义
meaning     = '''相邻两月新闻主题分布的差异，衡量叙事更替的幅度'''
unit        = "无量纲，0–1"
granularity = "月"

# 输入 — a dataset, or another variable at a pinned version
[[input]]
dataset      = "复现包_分步/Data/theme_counts_2017_2024.csv"
fields       = ["month", "theme", "count"]
data_version = '''2017-01 至 2024-12，2026-07 下载'''

# 构建口径
[construction]
formula     = '''TS_t = 1 − cos(p_t, p_{t−1})，p_t 为当月各主题占比向量'''
filter      = '''剔除当月文章数 < 30 的月份'''
aggregation = '''日度主题计数先按月求和，再归一'''
missing     = '''缺月不插值，记为缺失'''
transform   = '''不标准化'''
params      = '''主题数 K=12'''

# 实现依据
[implementation]
script       = "复现包_分步/16-构造叙事更替指标.py"
output       = "复现包_分步/Data/topicshift_monthly.csv"
field        = "topicshift"
code_version = '''复现包 2026-07-26 版'''    # optional, in the researcher's words

# 人工决策
[decision]
why        = '''余弦距离对主题总量不敏感；试过 JS 散度，对稀疏主题过于敏感'''
decided_by = "LL"
adopted_on = "2026-07-26"      # when the definition was adopted — may predate the card
```

The five blocks are the researcher's list. Free text is written in
literal strings (`'''…'''`) so that a formula with backslashes or quotes
is kept exactly; the template says so. A version file is rejected on
read, with the line named, if it has a key the schema does not know —
which is what catches a line that slid under the wrong heading. A draft
may be incomplete; to be confirmed a version needs a name, meaning, unit,
granularity, at least one input, a formula and a reason.

**Who writes what.** The version file is the researcher's, all of it. RCE
never fills in a formula, a filter or a reason — reading code and guessing
what it computes is what Section 0 forbids. Everything RCE knows goes into
`log.toml`.

**Confirming** is the act that turns a draft into a definition results
may rely on. RCE appends one entry:

```toml
[[entry]]
id      = "v-5e0c…"                         # this entry is the version's immutable identity
seq     = 7                                 # order of appending (9.3) — the clock decides nothing
at      = "2026-10-05T10:12:00+02:00"      # RCE's clock, for display — never typed
act     = "confirmed"
version = 1
attested = "unknown"   # the researcher's own statement, asked at confirmation:
                       # was the output file as it stands built with THIS definition? yes | no | unknown
content = "sha256:9c1f…"       # of the version file's parsed content; comments and layout are free
frozen  = "frozen/9c1f….toml"
[entry.checked]                 # read from disk now; each item is 已核对, 不符, or 未核对 with its reason
script  = { result = "已核对", sha256 = "71ab…", copy = "_code/71ab….py" }
writes  = { result = "已核对", call = "to_csv" }     # this parse of the script writes the output
reads   = [{ dataset = "…theme_counts_2017_2024.csv", result = "已核对", call = "read_csv" }]
field   = { result = "已核对" }                      # found in the CSV header
[entry.observed]                # what was on disk at that moment — an observation, nothing more
output  = { sha256 = "0d4e…", size = 18230 }
inputs  = [{ dataset = "…theme_counts_2017_2024.csv", sha256 = "44ab…", size = 912004 }]
```

- The checks are made **by parsing the script now**, not by asking the
  index (which keeps links a script stopped producing): does this parse
  yield a write to the output and reads of the inputs. `不符` — the
  script parsed and does no such thing — is a finding and is shown as
  one; `未核对` says why (the script could not be parsed; the output is
  not a CSV; the file is still in the cloud; the link exists only as the
  researcher's own hand-drawn mapping, which is their statement and not a
  check). No outcome blocks confirmation: a variable computed in an Rmd
  chunk and never written to a file is still a variable.
- **What each thing proves, and no more** (Section 0, "kept material is
  not a relation"). *Confirming* means the researcher endorses this
  definition. `observed` means a file with this hash was on disk when
  they did. Neither says the file was built by the definition: the script
  may already compute the new definition while the output on disk is last
  month's — and the design lets a version be confirmed when the checks
  say `不符`. So RCE never infers "this output belongs to this version"
  from a matching hash. What can carry that weight is a statement or
  evidence of its own: at confirmation the researcher is asked, once,
  whether the output as it stands was built with this definition — 是 /
  否 / 不确定 — and the answer is recorded as theirs (`attested`). They
  know it at that moment and will not in two months. RCE does not run
  scripts, so it has no run evidence to offer; if it ever records runs,
  that will be a second kind of basis. A `yes` standing beside a `不符` is
  shown as exactly that, not resolved. `adopted_on` is the researcher's
  statement of when the definition was adopted and may predate the card;
  `at` is when the statement was made. The next phase attributes results
  from these items one by one.
- **The implementing script is copied** to `_code/`, named by its hash.
  The researcher's project is not a git repository; a hash can be compared
  in a year but not read. With the copy, the card can show 「查看当时的代码」.
  Scripts are small; a copy is written once and never rotated.

**Versions, and the rule that protects history.**

- A card has at most one draft, and it is always the highest number. A
  draft is edited freely; the view marks it 「草稿 · 改动不留版本」.
- **Confirming freezes the whole version.** After that, any change to the
  content of `v<n>.toml` is detected by comparing it with the hash in its
  own log entry — **without needing the index**, so the check still works
  after a rebuild, on another Mac, after a restore. RCE applies nothing
  and asks: 「v2 的定义在确认后被改动了」 — 「另存为新版本」 (the edited text
  becomes the draft `v3`; `v2` is restored from its frozen copy, byte for
  byte), or 「这是更正」 (a typo, a clearer wording, a script that moved: a
  `corrected` entry with both hashes and a new frozen copy, so the wording
  before and after can both be read).
- To change how a variable is built is to write the next version: `rce
  variable revise` copies the current version file, byte for byte, to the
  next number as a draft, and refuses while a draft is open. When the new
  one is confirmed, the old one is *shown* as superseded from that
  entry's time; its file is not touched.
- **A result points at one confirmed text, not at a name and not at a
  number.** `v2` is the label a person reads. What a reference stores is
  the variable's id, that label, the id of the log entry it relies on (the
  `confirmed` entry, or the `corrected` entry current when the reference
  was made) and the content hash that entry carries; it is written
  `topicshift@v2·9c1f2a3b`. A reference resolves only when the log holds
  that entry with that hash. If it does not — the project was restored
  from a backup older than the version; the card was rebuilt — it shows
  「引用暂不可解析」 and stays that way: it is **never** re-pointed at
  whatever is called `v2` now. This is why a correction does not rewrite
  history (the result keeps the hash it was made against, and both
  wordings can be read), and why "numbers are not reused" — which RCE
  still does its best to keep, leaving a `removed` entry behind a version
  that a restore took away — no longer has to be true for a reference to
  be safe. Only a confirmed version can be referred to.
- In use = the highest confirmed version. `abandoned` and `revived` are
  entries about the whole variable, each with the researcher's reason;
  while abandoned no version is in use, and every version still resolves.

**Variables built from variables.** An input is either a dataset or
`variable = "returns@v1"` — pinned. When the upstream variable gains a
newer version the card says 「上游 returns 已有 v2」 and nothing else
happens: this version was built on that one. `transform` describes what
the output file contains; a lag or a standardization applied inside one
model belongs to that result (next phase). The same construct at two
frequencies is two cards — granularity is part of the definition.

**Dead variables.** Abandoning a card records, in the researcher's words
and with a date, why a variable died — which the substring list in
`attempts.toml` cannot. In V5 that list remains the only input to the
revived-dead-variable check; the 「变量」 view shows any disagreement in
either direction (「卡片已弃用，attempts.toml 未列入」 and the reverse), using
`aliases`. Unifying the two is later. `aliases` also gives the next phase
something deterministic to map the attempt table's shorthand onto.

**When the implementation moves under a confirmed version** (stage (b)).
RCE compares the script — its code with comments and blank lines removed;
for an Rmd, its code chunks — and the input files with what the
`confirmed` entry recorded. The researcher may narrow the comparison by
naming `chunk` (an Rmd chunk label) or `function` in `[implementation]`;
the region is found exactly or the version is under review for that
reason. A difference puts the version under review — 「实现脚本在确认后有
改动」 or 「输入数据在确认后有变化」 — and RCE says plainly that it cannot
tell whether the definition changed. Reviews raised by one changed script
are one item, 「此脚本的改动涉及 N 个变量」, answerable together (「全部口径
未变」) or one by one: 「口径未变」 appends `reaffirmed` with the new
fingerprints and, after an input change, the researcher's new
`data_version` note; 「口径已变」 opens the next draft. An input that
cannot be read (still in the cloud) raises nothing.

**"Nothing changed" always says how far it looked.** Narrowing to a chunk
or a function does not see a parameter defined outside it or a function it
calls; a file above 50 MB is compared by size, and content can change at
the same size. Both are legitimate ways to keep a scan cheap, and both are
blind spots, so every comparison carries its coverage and the view words
it: 「全文未变」; 「指定范围未变（范围外未比对）」; 「大小未变（内容未比对）」;
「修改时间变化，内容未比对」. The bare phrase "实现及数据未变化" is never
shown. A `reaffirmed` entry records the coverage it was made under, and
「完整比对」 hashes the large files on request.

**A confirmation is written snapshot first, entry last.** Confirming (and
correcting) touches several files; the log entry is the commit point. In
order: the code copy and the frozen copy are written and synced to disk;
both are read back and their hashes checked; only then is the entry that
names them appended. A crash at any point leaves at worst copies no entry
refers to — harmless, and `rce records --clean` removes them — and never
an entry that refers to a copy which is not there. On reading, an entry
whose copy is missing anyway (a sync that has not delivered it) is shown
as 「确认记录引用的副本缺失」: the hash in the entry still detects an edit,
but the version cannot be restored from it until the copy returns.

**Safety, as for the ledger.** `log.toml` is a 9.3 ledger and gets 9.3's
protections: while it is missing, in the cloud or unreadable, the card is
frozen and writes to *that card* are refused; a log that has lost entries
the index applied — an `abandoned` as much as a `confirmed` — is asked
about, never obeyed. Two directories with the same id up to letter case,
or a sync conflict copy beside any file of a card, make that card
unreadable until one is removed. `rce variable new` creates the directory
exclusively and refuses an id already present in the directory, the
index, or the snapshots. Snapshots go to `.rce/backups/variables/<id>/`.
What V5 cannot detect, and says so: a whole card directory that never
arrived on a machine with no index for the project.

**In the app.** One plain view, 「变量」: the cards, the version in use,
the history from the log, each check with its result, what is under
review, 「查看当时的代码」. The app may write what RCE authors — confirm,
reaffirm, open the next draft, abandon or revive with a note — and never
the researcher's text: a card is written in its file, from a commented
template that `rce variable new` creates. In the index, V5 keeps a copy
of each card for this view and for the shrink check, and promises no node
type to later phases.

**What V5 covers and does not.** Cards are written by the researcher for
the variables that actually enter a model, a figure or a claim; RCE does
not go looking for variables and does not create cards. Not in V5: links
from attempts, figures and claims to variable versions; variables on the
canvas; a form for writing a card in the app; discovering variables from
data columns; replacing the dead-variable list; renaming a variable's id.

**Acceptance (continues 9.9).**

13. **Life of a card.** `rce variable new` → a draft from the template;
    edit it freely; confirm with one input deliberately absent and the
    output not a CSV: confirmed, with those checks recorded as 「未核对」
    and their reasons, the script copied to `_code/`, the output
    fingerprinted. Revise and confirm: `v2` in use, `v1` shown superseded
    from the entry's time, `v1.toml` byte-identical. `revise` with a draft
    open is refused. **Kill the process between each pair of writes of a
    confirmation and start again**: there is never an entry whose frozen
    or code copy is missing; leftover copies are removed by `rce records
    --clean`. The confirmation asks the attestation question, and a
    matching output hash alone never makes the view say the output was
    built with the version.
14. **History is not overwritten — with or without an index.** Edit a
    confirmed version's formula by hand: nothing is applied and the
    question is asked. 「另存为新版本」: the edit becomes draft `v3` and
    `v2.toml` is byte-identical to its frozen copy. 「这是更正」: a
    `corrected` entry, and both wordings can be read. **Repeat with
    `~/.rce/graphs/<id>/` deleted before the edit is seen: the question is
    still asked.** Change only a comment: no question. **A reference
    made before a correction still shows the wording it was made
    against; with the card restored from a backup that predates the
    version, the reference shows 「引用暂不可解析」 and a newly written
    `v2` does not capture it.**
15. **Survival.** Scenarios 1–7 and 10–11 of 9.9 with two cards added to
    the fixture (one with two versions, a correction and an `abandoned`
    entry): after a move, a rename, a copy with each answer, a rebuild, a
    restore and two concurrent writers, every version, entry, frozen copy
    and code copy is present and `rce records --verify` passes. A log made
    unparseable, removed or truncated — including one that lost only its
    `abandoned` entry — freezes that card, refuses writes to it and asks;
    the other cards keep working. A sync conflict copy beside `v2.toml`
    makes that card unreadable and says why. A version removed by
    「以文件为准」 does not give its number to the next revision.
16. **Dead and back.** Abandon a card with a reason; revive it: the view
    shows the state and both entries in order, and flags the disagreement
    with `attempts.toml` in whichever direction it exists.
17. **The implementation moves** (stage (b)). Edit a comment in the
    script: nothing. Change its code: one review item for the script,
    naming every card that points at it; 「全部口径未变」 appends one
    `reaffirmed` per card; 「口径已变」 on one of them opens its next draft.
    With `chunk` named, a change outside the chunk raises nothing and a
    removed chunk raises 「找不到该代码块」. Make the script unreadable:
    nothing comes under review. The view says 「指定范围未变（范围外未比对）」
    for the narrowed card and 「大小未变（内容未比对）」 for an input above
    50 MB whose bytes changed at the same size — never an unqualified
    "unchanged" — and 「完整比对」 then finds the change.
