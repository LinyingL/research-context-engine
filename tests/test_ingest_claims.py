"""Tests for rce.ingest.claims (Phase B, task B1): claim extraction +
deterministic backed_by candidate generation. No real git needed -- only a
tmp_path .tex file plus an in-memory graph with pre-seeded experiment nodes.
"""

import logging
import re
from pathlib import Path

from rce import db
from rce.ingest import claims

TEX_87_3_PCT = "\\section{Results}\nWe reach 87.3\\% accuracy.\n"

# ingest_claims_repo's counts dict always carries the F2 orphan-cleanup keys
# alongside "claims"/"candidates" -- most tests below expect a no-op cleanup
# (nothing orphaned yet), so spread this in rather than repeating all three
# zeroes at every call site.
_NO_CLEANUP = {
    "claims_removed": 0,
    "backed_by_edges_removed": 0,
    "claims_preserved_with_human_judgement": 0,
}

_CLAIM_ID_RE = r"^claim:paper\.tex#[0-9a-f]{16}$"


def _repo(tmp_path: Path, tex: str) -> Path:
    (tmp_path / "paper.tex").write_text(tex)
    return tmp_path


def _seeded_conn(**metrics_by_experiment):
    """An in-memory, migrated db with one experiment node per kwarg, e.g.
    _seeded_conn(run_a={"accuracy": 0.87312})."""
    conn = db.connect(":memory:")
    db.migrate(conn)
    for run_id, metrics in metrics_by_experiment.items():
        db.upsert_node(conn, f"experiment:{run_id}", "experiment", attrs={"metrics": metrics})
    return conn


def test_recognizes_all_number_forms(tmp_path):
    repo = _repo(
        tmp_path,
        r"""\section{Results}
We reach 87.3\% accuracy, or \SI{4.2}{\percent} better, i.e. $92.1$ under
the strict metric, matching a raw ratio of 0.873 as well.
""",
    )
    result = claims.parse_tex_claims(repo, "paper.tex")
    forms = {(c.printed_number, c.unit_form, c.value) for c in result}
    assert forms == {
        ("87.3", "percent", 0.873),
        ("4.2", "percent", 0.042),
        ("92.1", "plain", 92.1),
        ("0.873", "fraction", 0.873),
    }
    assert all(c.section_id == "section:paper.tex#results" for c in result)


def test_skips_table_and_equation_environments(tmp_path):
    repo = _repo(
        tmp_path,
        r"""\section{Results}
We report 87.3\% overall.

\begin{tabular}{lcc}
87.3 & 92.1 & 0.5 \\
\end{tabular}

\begin{equation}
x = 0.873
\end{equation}

\begin{align}
y &= 0.5
\end{align}

Final figure stays 11.1\%.
""",
    )
    result = claims.parse_tex_claims(repo, "paper.tex")
    printed = {c.printed_number for c in result}
    assert printed == {"87.3", "11.1"}  # table/equation/align contents excluded


def test_skips_tabularx_and_longtable_environments(tmp_path):
    # Regression: the old regex required `tabular` immediately followed by
    # `}`, so `tabularx}`/`longtable}` never matched at all -- table cells
    # are exactly where a metric-shaped number lives, the worst
    # contamination path.
    repo = _repo(
        tmp_path,
        r"""\section{Results}
\begin{tabularx}{\linewidth}{lcc}
87.3 & 92.1 & 0.5 \\
\end{tabularx}

\begin{longtable}{lcc}
11.1 & 22.2 \\
\end{longtable}

Final figure stays 33.3\%.
""",
    )
    printed = {c.printed_number for c in claims.parse_tex_claims(repo, "paper.tex")}
    assert printed == {"33.3"}


def test_skips_gather_multline_and_eqnarray_environments(tmp_path):
    repo = _repo(
        tmp_path,
        r"""\section{Results}
\begin{gather}
y = 0.5
\end{gather}

\begin{multline}
z = 0.6
\end{multline}

\begin{eqnarray}
w = 0.7
\end{eqnarray}

Final figure stays 33.3\%.
""",
    )
    printed = {c.printed_number for c in claims.parse_tex_claims(repo, "paper.tex")}
    assert printed == {"33.3"}


def test_skips_display_bracket_and_dollar_dollar_math(tmp_path):
    repo = _repo(
        tmp_path,
        r"""\section{Results}
\[
q = 0.8
\]

$$
r = 0.9
$$

Final figure stays 33.3\%.
""",
    )
    printed = {c.printed_number for c in claims.parse_tex_claims(repo, "paper.tex")}
    assert printed == {"33.3"}


def test_skips_verbatim_environment_digits(tmp_path):
    repo = _repo(
        tmp_path,
        r"""\section{Results}
\begin{verbatim}
0.123
\end{verbatim}

Final figure stays 33.3\%.
""",
    )
    printed = {c.printed_number for c in claims.parse_tex_claims(repo, "paper.tex")}
    assert printed == {"33.3"}


def test_skips_includegraphics_optional_width_arg(tmp_path):
    # Regression: every figure in a real paper carries `width=0.NN`, which
    # used to be scanned as a bare plain-form claim (and, with a metric
    # that happened to round the same way, matched it).
    repo = _repo(
        tmp_path,
        r"""\section{Overview}
\includegraphics[width=0.8\textwidth]{overview.png}

Final figure stays 33.3\%.
""",
    )
    printed = {c.printed_number for c in claims.parse_tex_claims(repo, "paper.tex")}
    assert printed == {"33.3"}


def test_skips_ref_and_label_with_decimal_looking_targets(tmp_path):
    # Regression: `\ref{fig:2.1}`/`\label{tab:3.2}` were scanned as plain
    # claims "2.1"/"3.2" -- unlike a `\cite` key, a label target routinely
    # contains a literal decimal-point-shaped substring pre-compile.
    repo = _repo(
        tmp_path,
        r"""\section{Overview}
See~\ref{fig:2.1} and \ref{sec:4.2}.
\label{tab:3.2}

Final figure stays 33.3\%.
""",
    )
    printed = {c.printed_number for c in claims.parse_tex_claims(repo, "paper.tex")}
    assert printed == {"33.3"}


def test_command_arg_blanking_does_not_touch_textbf_or_emph_prose(tmp_path):
    # Guard against over-correction: \textbf/\emph are not in the
    # arg-blanking whitelist because their argument can carry a real claim.
    repo = _repo(
        tmp_path,
        r"""\section{Results}
\textbf{We highlight 55.5\% in bold} and \emph{a raw ratio of 0.873}.
""",
    )
    forms = {(c.printed_number, c.unit_form) for c in claims.parse_tex_claims(repo, "paper.tex")}
    assert forms == {("55.5", "percent"), ("0.873", "fraction")}


def test_skips_ref_cite_years_and_page_numbers(tmp_path):
    repo = _repo(
        tmp_path,
        r"""\section{Results}
See Table~\ref{tab:results} and \citep{smith2020} from 2024 on page 12.
""",
    )
    assert claims.parse_tex_claims(repo, "paper.tex") == []  # no decimal-bearing number on that line


def test_precision_normalization_derived_from_printed_digits():
    # 87.3% -> 0.873 at 3dp (1 printed decimal + 2 for the percent shift).
    assert claims._normalize("87.3", "percent") == (claims.Decimal("0.873"), 3)
    # 87% -> 0.87 at 2dp (0 printed decimals + 2).
    assert claims._normalize("87", "percent") == (claims.Decimal("87") / claims.Decimal(100), 2)
    assert claims._round_half_up(claims.Decimal("0.87312"), 3) == claims.Decimal("0.873")


def test_claim_matches_metric_rounded_to_its_own_printed_precision(tmp_path):
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.87312})  # rounds to 0.873 at 3dp -> matches

    counts = claims.ingest_claims_repo(conn, repo, ["paper.tex"])
    assert counts == {**_NO_CLEANUP, "claims": 1, "candidates": 1}

    edge = db.query_edges(conn, type="backed_by")[0]
    assert edge["dst"] == "experiment:run_a"
    assert edge["status"] == "pending"  # machine path never writes "confirmed"/"auto" here
    assert edge["confidence"] == 1.0
    assert edge["evidence"]["occurrences"][0]["metric"] == "accuracy"


def test_claim_does_not_match_metric_that_rounds_differently(tmp_path):
    # 0.8735 rounds half-up to 0.874 at 3dp, not 0.873 -- must not match.
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.8735})

    assert claims.ingest_claims_repo(conn, repo, ["paper.tex"]) == {**_NO_CLEANUP, "claims": 1, "candidates": 0}
    assert db.query_edges(conn, type="backed_by") == []


def test_multiple_experiments_each_become_a_pending_candidate_with_split_confidence(tmp_path):
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.87312}, run_b={"acc": 0.8731})

    assert claims.ingest_claims_repo(conn, repo, ["paper.tex"]) == {**_NO_CLEANUP, "claims": 1, "candidates": 2}
    edges = db.query_edges(conn, type="backed_by")
    assert {e["dst"] for e in edges} == {"experiment:run_a", "experiment:run_b"}
    assert all(e["status"] == "pending" and e["confidence"] == 0.5 for e in edges)


def test_zero_hit_claim_creates_node_but_no_edge(tmp_path):
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.5})

    assert claims.ingest_claims_repo(conn, repo, ["paper.tex"]) == {**_NO_CLEANUP, "claims": 1, "candidates": 0}
    assert db.query_edges(conn, type="backed_by") == []
    claim_nodes = db.get_nodes_by_type(conn, "claim")
    assert len(claim_nodes) == 1
    assert claim_nodes[0]["attrs"]["unit_form"] == "percent"
    assert claim_nodes[0]["attrs"]["value"] == 0.873


def test_ingest_claims_repo_is_idempotent_with_stable_ids(tmp_path):
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.87312})

    first = claims.ingest_claims_repo(conn, repo, ["paper.tex"])
    second = claims.ingest_claims_repo(conn, repo, ["paper.tex"])
    assert first == second == {**_NO_CLEANUP, "claims": 1, "candidates": 1}
    assert len(db.get_nodes_by_type(conn, "claim")) == 1
    assert len(db.query_edges(conn, type="backed_by")) == 1
    claim_id = db.get_nodes_by_type(conn, "claim")[0]["id"]
    assert re.match(_CLAIM_ID_RE, claim_id), claim_id
    # Content-addressed (F2): the id is a pure function of section + sentence
    # + printed number + intra-sentence position -- recomputing it from the
    # same inputs the extractor used must land on the exact same id.
    assert claim_id == claims._content_id("paper.tex", "section:paper.tex#results", "We reach 87.3\\% accuracy.", "87.3\\%", 1)


# --- F2: content-addressed ids + orphan cleanup ----------------------------
#
# Reproduces the review-reported regression directly: with the old
# `claim:<path>#<line>-<seq>` id, inserting one line at the top of the file
# shifted every later claim's line number, so a claim's human confirm/reject
# silently reattached to whatever claim now happened to land on its old
# line -- and the claim's *own*, now-orphaned old id was left behind forever
# (claim nodes growing 31 -> 51, backed_by 8 -> 15 purely from edits, per the
# review's own count).

_TWO_CLAIMS_TEX = (
    "\\section{Results}\n"
    "We reach 87.3\\% accuracy.\n"
    "The baseline gets 50.0\\% accuracy.\n"
)


def test_reingest_after_top_of_file_insertion_keeps_human_judgement_on_correct_claim(tmp_path):
    repo = _repo(tmp_path, _TWO_CLAIMS_TEX)
    conn = _seeded_conn(run_a={"accuracy": 0.873}, run_b={"accuracy": 0.500})

    claims.ingest_claims_repo(conn, repo, ["paper.tex"])
    nodes_before = {n["attrs"]["raw"]: n["id"] for n in db.get_nodes_by_type(conn, "claim")}
    assert nodes_before.keys() == {"87.3\\%", "50.0\\%"}
    claim_a_id, claim_b_id = nodes_before["87.3\\%"], nodes_before["50.0\\%"]

    # A human confirms the real 87.3% claim and rejects the 50.0% one.
    db.set_edge_status(conn, claim_a_id, "experiment:run_a", "backed_by", "claims", "confirmed")
    db.set_edge_status(conn, claim_b_id, "experiment:run_b", "backed_by", "claims", "rejected")

    # Insert an unrelated line at the very top of the file -- both claims'
    # line numbers shift by 1; neither claim's own text changes at all.
    (tmp_path / "paper.tex").write_text("% inserted note\n" + _TWO_CLAIMS_TEX)
    second_counts = claims.ingest_claims_repo(conn, repo, ["paper.tex"])

    nodes_after = {n["attrs"]["raw"]: n["id"] for n in db.get_nodes_by_type(conn, "claim")}
    assert nodes_after == nodes_before  # same ids reappear -- no misattribution, no orphans
    assert len(db.get_nodes_by_type(conn, "claim")) == 2  # no growth from the edit
    assert second_counts["claims_removed"] == 0

    edge_a = db.query_edges(conn, src=claim_a_id, dst="experiment:run_a", type="backed_by")[0]
    edge_b = db.query_edges(conn, src=claim_b_id, dst="experiment:run_b", type="backed_by")[0]
    assert edge_a["status"] == "confirmed"  # the correct claim's confirm survived the edit
    assert edge_b["status"] == "rejected"  # the correct claim's reject survived, not lost


def test_repeated_unrelated_edits_do_not_accumulate_orphaned_claim_nodes(tmp_path):
    # Regression: the review found claim nodes growing 31 -> 51 and
    # backed_by edges 8 -> 15 purely from repeated edits, no new claims.
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.87312})

    claims.ingest_claims_repo(conn, repo, ["paper.tex"])
    for i in range(5):
        (tmp_path / "paper.tex").write_text(f"% unrelated edit {i}\n" + TEX_87_3_PCT)
        claims.ingest_claims_repo(conn, repo, ["paper.tex"])

    assert len(db.get_nodes_by_type(conn, "claim")) == 1
    assert len(db.query_edges(conn, type="backed_by")) == 1


def test_orphaned_pending_claim_is_removed_once_its_sentence_disappears(tmp_path):
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.87312})

    first = claims.ingest_claims_repo(conn, repo, ["paper.tex"])
    assert first["claims"] == 1 and len(db.get_nodes_by_type(conn, "claim")) == 1

    (tmp_path / "paper.tex").write_text("\\section{Results}\nNothing quantitative here.\n")
    second = claims.ingest_claims_repo(conn, repo, ["paper.tex"])

    assert second == {
        "claims": 0, "candidates": 0,
        "claims_removed": 1, "backed_by_edges_removed": 1,
        "claims_preserved_with_human_judgement": 0,
    }
    assert db.get_nodes_by_type(conn, "claim") == []
    assert db.query_edges(conn, type="backed_by") == []


def test_unreadable_tex_path_is_not_treated_as_evidence_its_claims_are_gone(tmp_path):
    # A transient read failure (file deleted/locked between an upstream
    # inventory snapshot and this read) must never be treated as evidence
    # that a file's claims disappeared -- DESIGN.md section 0, "never
    # guess". Simulated by removing the file from disk while still passing
    # its name in tex_paths, exactly as a moments-earlier inventory would.
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.87312})

    first = claims.ingest_claims_repo(conn, repo, ["paper.tex"])
    assert first["claims"] == 1

    (tmp_path / "paper.tex").unlink()
    second = claims.ingest_claims_repo(conn, repo, ["paper.tex"])

    assert second["claims"] == 0
    assert second["claims_removed"] == 0  # not wiped out just because the read failed
    assert len(db.get_nodes_by_type(conn, "claim")) == 1


def test_orphaned_claim_with_human_judgement_is_preserved_and_logged(tmp_path, caplog):
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.87312})

    claims.ingest_claims_repo(conn, repo, ["paper.tex"])
    claim_id = db.get_nodes_by_type(conn, "claim")[0]["id"]
    db.set_edge_status(conn, claim_id, "experiment:run_a", "backed_by", "claims", "rejected")

    # The claim's sentence disappears entirely (not merely shifted/reworded).
    (tmp_path / "paper.tex").write_text("\\section{Results}\nNothing quantitative here.\n")
    with caplog.at_level(logging.INFO, logger="rce.ingest.claims"):
        counts = claims.ingest_claims_repo(conn, repo, ["paper.tex"])

    assert counts["claims_removed"] == 0
    assert counts["claims_preserved_with_human_judgement"] == 1
    assert db.get_node(conn, claim_id) is not None  # node kept, not deleted
    edge = db.query_edges(conn, src=claim_id, dst="experiment:run_a", type="backed_by")[0]
    assert edge["status"] == "rejected"  # human verdict untouched
    assert any("human-judged" in r.message for r in caplog.records)
