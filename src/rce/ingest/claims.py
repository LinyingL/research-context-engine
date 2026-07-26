r"""Deterministic claim extraction + backed_by candidate generation (Phase B,
task B1) -- zero-model extractor, DESIGN.md section 5 connector 7. Only
generates candidates; never confirms one -- every edge is `status="pending"`
(upsert_edge structurally cannot write "confirmed", see rce.db).

Line-by-line, reusing rce.ingest.latex's `_strip_comment` and its
`parse_tex_file` section list (no section-parsing logic duplicated here,
Occam rule 5). Skipped, never guessed, by blanking (spaces, so line/column
positions never shift) before number-scanning runs: non-prose environment
bodies -- tables (`tabular`/`tabularx`/`longtable`/`tabu`/`array`),
equations (`equation*`/`align*`/`gather*`/`multline*`/`eqnarray*`/
`displaymath`), verbatim-like bodies (`verbatim`/`lstlisting`/`minted`) --
plus `\[ ... \]`/`$$ ... $$` display-math spans; every command's optional
`[...]` argument (a key=value option block, never prose); and the required
`{...}` argument(s) of a whitelisted set of commands whose argument is a
typographic value or identifier rather than prose -- `\ref`, `\label`, the
`\cite` family, `\input`, `\include`, `\vspace`, `\hspace`, `\scalebox`,
`\resizebox`, `\setlength` -- so e.g. `\ref{fig:2.1}` never presents "2.1"
as a claim (a label target routinely contains a literal decimal-point
substring pre-compile, unlike a `\cite` key). Deliberately a whitelist:
`\textbf`/`\emph` keep their braces untouched because their argument may
itself carry a real prose claim.

Recognised forms, all requiring the number as literally printed:
`\SI{87.3}{\percent}` and `87.3\%` (unit_form "percent"); `$92.1$` and bare
`0.873` (unit_form "fraction" if in [0, 1] else "plain"). The bare/math forms
require a decimal point, matching the spec's own examples -- this is also
how integer-only section/figure/table numbers, page references, and years
are excluded, with no separate numeric-range guess.

`_normalize`/`_round_half_up` implement the no-guessed-tolerance match rule:
two values are a candidate match iff equal once both are rounded to the
precision the *claim itself* was printed with -- no tunable epsilon anywhere
in this module.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from rce import db
from rce.ingest.latex import _strip_comment, parse_tex_file

logger = logging.getLogger(__name__)

_MAX_SENTENCE_LEN = 240  # display/storage cap only, not a matching decision

# Environments whose contents are not prose -- blanked out (replaced with
# spaces, preserving line/column positions) before number-scanning runs.
# Table cells are the worst contamination path (a metric-shaped number is
# exactly what lives there), so every table-like name in common use is
# listed, not just plain `tabular`. Ordering within the alternation doesn't
# matter -- the mandatory literal `\}` right after it already forces a
# full-name match (`tabular` can't partially match `tabularx}`).
_SKIP_ENV_NAMES = (
    r"tabularx|tabular|tabu|longtable|array"
    r"|equation\*?|align\*?|gather\*?|multline\*?|eqnarray\*?|displaymath"
    r"|verbatim|lstlisting|minted"
)
_SKIP_BEGIN_RE = re.compile(rf"\\begin\{{(?:{_SKIP_ENV_NAMES})\}}")
_SKIP_END_RE = re.compile(rf"\\end\{{(?:{_SKIP_ENV_NAMES})\}}")
# `\begin{tabularx}{\linewidth}{lcc}`-style trailing args need no special
# handling: once the begin marker is seen, _blank_skip_regions already
# blanks the rest of that line regardless of what follows.

# `\[ ... \]` has unambiguous open/close tokens, so it folds into the same
# begin/end depth counter as the environments above. `$$` is not a distinct
# open-vs-close token -- both delimiters of a pair are the same two
# characters -- so it is a separate open/close toggle instead.
_DISPLAY_BRACKET_OPEN_RE = re.compile(r"\\\[")
_DISPLAY_BRACKET_CLOSE_RE = re.compile(r"\\\]")
_DOLLAR_DOLLAR_RE = re.compile(r"\$\$")

# Every command's optional `[...]` argument is a key=value option block,
# never prose (e.g. width=0.8\textwidth in
# `\includegraphics[width=0.8\textwidth]{overview.png}`) -- blanked
# regardless of which command it follows. `[A-Za-z]+` (not an alternation
# of specific names) matches a command name in full, so there is no
# partial-prefix risk against _ARG_BLANK_CMDS below.
_OPTIONAL_ARG_RE = re.compile(r"\\[A-Za-z]+\*?((?:\[[^\[\]]*\])+)")

# Commands whose required {...} argument is a typographic value or
# identifier (a label/cite key, a file path, a length, a scale factor)
# rather than prose. Deliberately a whitelist: anything not listed here
# (`\textbf{...}`, `\emph{...}`, a plain paragraph) keeps its braces
# untouched, since its argument may itself carry a real claim.
# `(?![A-Za-z])` blocks a same-prefix false match (e.g. "include" against
# "includegraphics", not in this list -- its {path} keeps its braces;
# missing a decimal-bearing filename is the safe failure direction here).
_ARG_BLANK_CMDS = (
    r"ref|label|cite(?:p|t|alp)?|Citep|Citet|parencite|textcite|autocite"
    r"|input|include|vspace|hspace|scalebox|resizebox|setlength"
)
_ARG_BLANK_RE = re.compile(
    rf"\\(?:{_ARG_BLANK_CMDS})(?![A-Za-z])\*?"
    rf"((?:\s*(?:\[[^\[\]]*\]|\{{[^{{}}]*\}}))+)"
)


def _blank_command_args(line: str) -> str:
    """Blank every command's optional `[...]` argument, plus the
    whitelisted commands' required `{...}` argument(s) -- replaced with
    spaces so column offsets (and _extract_sentence) stay correct. Runs
    before number-scanning so e.g.
    `\\includegraphics[width=0.8\\textwidth]{overview.png}` and
    `\\ref{fig:2.1}` never present a scannable digit; `\\SI{87.3}{\\percent}`
    is untouched (\\SI is not in _ARG_BLANK_CMDS and has no `[...]`)."""
    chars = list(line)
    for regex in (_OPTIONAL_ARG_RE, _ARG_BLANK_RE):
        for m in regex.finditer(line):
            for i in range(m.start(1), m.end(1)):
                chars[i] = " "
    return "".join(chars)


_NUM = r"\d+(?:\.\d+)?"
# Alternation order is significant: SI/percent forms are tried before the
# generic "plain" form so e.g. "87.3\%"'s digits are claimed by the percent
# branch, not partially matched as a bare "87.3" with a stray "\%" left over.
_CLAIM_RE = re.compile(
    rf"\\SI\{{(?P<si>{_NUM})\}}\{{\\percent\}}"
    rf"|(?P<pct>{_NUM})\\%"
    rf"|(?<!\\)\$\s*(?P<math>\d+\.\d+)\s*\$"
    rf"|(?<![\w.\\])(?P<plain>\d+\.\d+)(?!\.\d)(?!\w)"
)
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")

# Both extractors already writing experiment nodes (mlflow/wandb) put their
# numeric metrics under one of these attrs keys -- see
# rce.ingest.mlflow.ingest_mlflow_dir / rce.ingest.wandb.transform_runs.
_METRIC_ATTR_KEYS = ("metrics", "summary_metrics")


def _blank_skip_regions(lines: list[str]) -> list[str]:
    """Replace non-prose environment bodies and display-math spans with
    spaces, char-for-char, so positions (and sentence extraction) stay
    correct. `depth` (environments, `\\[...\\]`) and `dollar_open` (`$$`
    pairs) both thread across lines -- a real table/equation/display-math
    span commonly spans several."""
    depth = 0
    dollar_open = False
    blanked: list[str] = []
    for line in lines:
        chars = list(line)
        markers = sorted(
            [(m.start(), m.end(), "begin") for m in _SKIP_BEGIN_RE.finditer(line)]
            + [(m.start(), m.end(), "end") for m in _SKIP_END_RE.finditer(line)]
            + [(m.start(), m.end(), "begin") for m in _DISPLAY_BRACKET_OPEN_RE.finditer(line)]
            + [(m.start(), m.end(), "end") for m in _DISPLAY_BRACKET_CLOSE_RE.finditer(line)]
            + [(m.start(), m.end(), "dollar") for m in _DOLLAR_DOLLAR_RE.finditer(line)]
        )
        cursor = 0
        for start, end, kind in markers:
            if depth > 0 or dollar_open:
                for i in range(cursor, start):
                    chars[i] = " "
            if kind == "begin":
                depth += 1
            elif kind == "end":
                depth = max(0, depth - 1)
            else:  # "dollar" -- $$ has no distinct open/close spelling, so toggle
                dollar_open = not dollar_open
            cursor = end
        if depth > 0 or dollar_open:
            for i in range(cursor, len(chars)):
                chars[i] = " "
        blanked.append("".join(chars))
    return blanked


def _sentence_bounds(line: str, start: int, end: int) -> tuple[int, int]:
    """Start/end offsets of the sentence containing line[start:end], scoped
    to this one source line (LaTeX line breaks don't track prose sentences;
    Occam rule 5). Factored out of `_extract_sentence` so the parse loop can
    also use it to group same-sentence matches for `seq_in_sentence` (see
    `_content_id`) without deriving sentence identity a second, possibly
    inconsistent way."""
    left = 0
    for m in _SENTENCE_END_RE.finditer(line, 0, start):
        left = m.end()
    right_match = _SENTENCE_END_RE.search(line, end)
    right = right_match.end() if right_match else len(line)
    return left, right


def _extract_sentence(line: str, start: int, end: int) -> str:
    """Sentence containing line[start:end]. Truncated to _MAX_SENTENCE_LEN
    for storage only."""
    left, right = _sentence_bounds(line, start, end)
    sentence = line[left:right].strip()
    if len(sentence) > _MAX_SENTENCE_LEN:
        sentence = sentence[: _MAX_SENTENCE_LEN - 1].rstrip() + "…"
    return sentence


def _decimal_places(raw: str) -> int:
    return len(raw.split(".", 1)[1]) if "." in raw else 0


def _normalize(raw: str, unit_form: str) -> tuple[Decimal, int]:
    """(normalized value, decimal places to round to for comparison).

    Percent divides by 100 and the precision shifts by 2 places to match --
    the precision is entirely derived from how many digits the claim itself
    was printed with, never a configured tolerance (DESIGN.md section 5
    connector 7 addendum)."""
    places = _decimal_places(raw)
    value = Decimal(raw)
    if unit_form == "percent":
        value /= Decimal(100)
        places += 2
    return value, places


def _round_half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


# Hex digits of the sha256 digest kept in a claim id (F2). An id-length
# convention, not a match/scoring threshold -- DESIGN.md section 0's "never
# guess" governs match *decisions* (the rounding rule above), not how many
# hash characters make an id string practically collision-free.
_ID_HASH_HEX_LEN = 16

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_for_id(sentence: str) -> str:
    """Collapse whitespace and case before hashing, so cosmetic
    re-formatting (rewrapped lines, trailing spaces, a stray capital) never
    changes a claim's id. Hashing input only -- attrs still store `sentence`
    exactly as printed."""
    return _WHITESPACE_RE.sub(" ", sentence).strip().lower()


def _content_id(
    tex_rel_path: str, section_id: str | None, sentence: str, raw: str, seq_in_sentence: int
) -> str:
    """Content-addressed claim id (F2, DESIGN.md section 4):
    `claim:<tex path>#<sha256 prefix>` over (owning section's slug,
    whitespace/case-normalized sentence, the number's own literal form as
    printed, its position among same-sentence numbers).

    Inserting, deleting, or reordering unrelated lines never changes this id
    -- only the claim's own sentence, its printed number, or its section
    does. That is the point: a line-number-based id let an unrelated edit
    shift a claim onto another claim's former id, silently inheriting that
    other claim's human confirm/reject verdict (the bug this replaces).
    `seq_in_sentence` (not the raw literal alone) is what separates two
    claims that both happen to print, say, "0.5" within the same sentence.
    """
    key = "\x1f".join([section_id or "", _normalize_for_id(sentence), raw, str(seq_in_sentence)])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_ID_HASH_HEX_LEN]
    return f"claim:{tex_rel_path}#{digest}"


@dataclass(frozen=True)
class ParsedClaim:
    id: str
    tex_path: str
    line: int
    section_id: str | None
    sentence: str
    raw: str  # as printed in the source, e.g. "87.3\%" or "$92.1$"
    printed_number: str  # bare numeral as printed, e.g. "87.3"
    unit_form: str  # "percent" | "fraction" | "plain"
    value: float  # normalized, comparable across percent/fraction
    precision_decimals: int  # rounding precision derived from `raw`


def parse_tex_claims(repo_root: str | Path, tex_rel_path: str) -> list[ParsedClaim]:
    """Scan one .tex file for quantitative prose claims. Reuses
    `latex.parse_tex_file`'s already-computed section list purely to look up
    which section a claim's line falls under -- no section-parsing logic is
    duplicated here."""
    repo_root = Path(repo_root)
    text = (repo_root / tex_rel_path).read_text(errors="replace")
    raw_lines = text.splitlines()
    stripped = [_strip_comment(line) for line in raw_lines]
    no_command_args = [_blank_command_args(line) for line in stripped]
    blanked = _blank_skip_regions(no_command_args)

    sections = parse_tex_file(repo_root, tex_rel_path).sections  # ascending by line

    claims: list[ParsedClaim] = []
    sec_idx = 0
    current_section_id: str | None = None
    for lineno, line in enumerate(blanked, start=1):
        while sec_idx < len(sections) and sections[sec_idx].line <= lineno:
            current_section_id = sections[sec_idx].id
            sec_idx += 1

        sentence_seq: dict[tuple[int, int], int] = {}  # per-sentence-bounds counter, not per-line
        for m in _CLAIM_RE.finditer(line):
            if m.group("si") is not None:
                unit_form, printed_number = "percent", m.group("si")
            elif m.group("pct") is not None:
                unit_form, printed_number = "percent", m.group("pct")
            else:
                printed_number = m.group("math") if m.group("math") is not None else m.group("plain")
                unit_form = "fraction" if 0 <= float(printed_number) <= 1 else "plain"

            value, places = _normalize(printed_number, unit_form)
            bounds = _sentence_bounds(line, m.start(), m.end())
            seq_in_sentence = sentence_seq.get(bounds, 0) + 1
            sentence_seq[bounds] = seq_in_sentence
            sentence = _extract_sentence(line, m.start(), m.end())
            claims.append(
                ParsedClaim(
                    id=_content_id(tex_rel_path, current_section_id, sentence, m.group(0), seq_in_sentence),
                    tex_path=tex_rel_path,
                    line=lineno,
                    section_id=current_section_id,
                    sentence=sentence,
                    raw=m.group(0),
                    printed_number=printed_number,
                    unit_form=unit_form,
                    value=float(value),
                    precision_decimals=places,
                )
            )
    return claims


def _collect_experiment_metrics(conn: Connection) -> list[tuple[str, str, float]]:
    """(experiment_id, metric_name, metric_value) for every numeric metric
    already in the graph. Built once per ingest run, not once per claim."""
    out: list[tuple[str, str, float]] = []
    for node in db.get_nodes_by_type(conn, "experiment"):
        for key in _METRIC_ATTR_KEYS:
            metrics = node["attrs"].get(key)
            if not isinstance(metrics, dict):
                continue
            for name, val in metrics.items():
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    continue  # non-numeric metric value -- not comparable, skip
                out.append((node["id"], name, float(val)))
    return out


def _match_candidates(
    claim: ParsedClaim, metrics: list[tuple[str, str, float]]
) -> list[tuple[str, str, float]]:
    target = _round_half_up(Decimal(str(claim.value)), claim.precision_decimals)
    return [
        (exp_id, name, val)
        for exp_id, name, val in metrics
        if _round_half_up(Decimal(str(val)), claim.precision_decimals) == target
    ]


def _cleanup_orphaned_claims(
    conn: Connection, scanned_tex_paths: set[str], seen_ids: set[str]
) -> dict[str, int]:
    """Remove claim nodes (and their backed_by edges) this extractor produced
    from one of `scanned_tex_paths` on some earlier run but did not produce
    again this run (F2) -- e.g. the claim's line was deleted, or its section,
    sentence, or printed number changed enough to hash to a different id.
    Without this, every edit accumulates orphaned claim nodes forever.

    Scoped to `scanned_tex_paths`: a claim node whose recorded tex_path is
    not one of the files *successfully* re-parsed this run is left
    untouched. This deliberately excludes a path that was requested but
    failed to read (see `ingest_claims_repo`'s OSError handling) -- a
    transient read failure is not evidence the claims in that file are gone,
    and must never be treated as though it were (DESIGN.md section 0,
    "never guess").

    Conservative by construction (DESIGN.md section 0, "humans own
    judgement" / section 2, "re-ingestion leaves confirmed and rejected
    edges alone"): a claim with a confirmed/rejected backed_by edge is never
    deleted even once orphaned, only logged for manual resolution. Only
    auto/pending orphans -- no recorded human judgement -- are deleted.
    """
    removed_claims = 0
    removed_edges = 0
    preserved = 0
    for node in db.get_nodes_by_type(conn, "claim"):
        if node["id"] in seen_ids or node["attrs"].get("tex_path") not in scanned_tex_paths:
            continue
        claim_edges = [e for e in db.query_edges(conn, src=node["id"]) if e["extractor"] == "claims"]
        human_judged = [e for e in claim_edges if e["status"] in ("confirmed", "rejected")]
        if human_judged:
            preserved += 1
            logger.info(
                "claim %s no longer produced re-ingesting %s, but has %d human-judged "
                "backed_by edge(s); preserving for manual review, not deleting",
                node["id"], node["attrs"].get("tex_path"), len(human_judged),
            )
            continue
        removed_edges += db.delete_edges_for_node(conn, node["id"], extractor="claims")
        db.delete_node(conn, node["id"])
        removed_claims += 1
    return {
        "claims_removed": removed_claims,
        "backed_by_edges_removed": removed_edges,
        "claims_preserved_with_human_judgement": preserved,
    }


def ingest_claims_repo(conn: Connection, repo_root: str | Path, tex_paths: list[str]) -> dict[str, int]:
    """Ingest claim nodes and candidate (pending) backed_by edges.

    Must run after experiment nodes exist (mlflow/wandb) -- with none yet in
    the graph, every claim would trivially get zero candidates. Idempotent
    via db.upsert_node/upsert_edge; confidence is 1.0 for a unique hit or
    1/N across N candidates for the same claim (Owner-confirmed rule, see
    task report) -- never a tuned/guessed number.

    After (re-)ingesting every successfully-read tex path, orphaned claim
    nodes from those same paths -- ones this extractor produced before but
    did not produce again this run -- are cleaned up; see
    `_cleanup_orphaned_claims` (F2). A path that fails to read (below) is
    excluded from that cleanup scope, not treated as evidence its claims are
    gone.
    """
    counts = {"claims": 0, "candidates": 0}
    metrics = _collect_experiment_metrics(conn)
    seen_ids: set[str] = set()
    scanned_paths: set[str] = set()

    for tex_rel_path in tex_paths:
        try:
            claims = parse_tex_claims(repo_root, tex_rel_path)
        except OSError as exc:
            logger.warning("cannot read tex file %s: %s", tex_rel_path, exc)
            continue
        scanned_paths.add(tex_rel_path)

        for claim in claims:
            seen_ids.add(claim.id)
            attrs: dict[str, Any] = {
                "sentence": claim.sentence,
                "value": claim.value,
                "raw": claim.raw,
                "printed_number": claim.printed_number,
                "unit_form": claim.unit_form,
                "precision_decimals": claim.precision_decimals,
                "section": claim.section_id,
                "tex_path": claim.tex_path,
                "line": claim.line,
            }
            db.upsert_node(conn, claim.id, "claim", title=claim.sentence, attrs=attrs)
            counts["claims"] += 1

            matches = _match_candidates(claim, metrics)
            if not matches:
                continue  # a claim with no backing candidate is itself information -- node only, no edge
            if len(matches) > 1:
                logger.info(
                    "%s:%d: claim %r has %d backed_by candidates: %s",
                    tex_rel_path, claim.line, claim.raw, len(matches),
                    ", ".join(f"{eid}:{name}" for eid, name, _ in matches),
                )
            confidence = 1.0 / len(matches)
            for exp_id, metric_name, metric_value in matches:
                db.upsert_edge(
                    conn, claim.id, exp_id, "backed_by", extractor="claims",
                    evidence={
                        "file": claim.tex_path,
                        "line": claim.line,
                        "metric": metric_name,
                        "metric_value": metric_value,
                        "claim_raw": claim.raw,
                        "claim_value": claim.value,
                    },
                    confidence=confidence, status="pending",
                )
                counts["candidates"] += 1

    counts.update(_cleanup_orphaned_claims(conn, scanned_paths, seen_ids))
    return counts
