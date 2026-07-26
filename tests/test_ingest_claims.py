"""Tests for rce.ingest.claims (Phase B, task B1): claim extraction +
deterministic backed_by candidate generation. No real git needed -- only a
tmp_path .tex file plus an in-memory graph with pre-seeded experiment nodes.
"""

from pathlib import Path

from rce import db
from rce.ingest import claims

TEX_87_3_PCT = "\\section{Results}\nWe reach 87.3\\% accuracy.\n"


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
    assert counts == {"claims": 1, "candidates": 1}

    edge = db.query_edges(conn, type="backed_by")[0]
    assert edge["dst"] == "experiment:run_a"
    assert edge["status"] == "pending"  # machine path never writes "confirmed"/"auto" here
    assert edge["confidence"] == 1.0
    assert edge["evidence"]["occurrences"][0]["metric"] == "accuracy"


def test_claim_does_not_match_metric_that_rounds_differently(tmp_path):
    # 0.8735 rounds half-up to 0.874 at 3dp, not 0.873 -- must not match.
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.8735})

    assert claims.ingest_claims_repo(conn, repo, ["paper.tex"]) == {"claims": 1, "candidates": 0}
    assert db.query_edges(conn, type="backed_by") == []


def test_multiple_experiments_each_become_a_pending_candidate_with_split_confidence(tmp_path):
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.87312}, run_b={"acc": 0.8731})

    assert claims.ingest_claims_repo(conn, repo, ["paper.tex"]) == {"claims": 1, "candidates": 2}
    edges = db.query_edges(conn, type="backed_by")
    assert {e["dst"] for e in edges} == {"experiment:run_a", "experiment:run_b"}
    assert all(e["status"] == "pending" and e["confidence"] == 0.5 for e in edges)


def test_zero_hit_claim_creates_node_but_no_edge(tmp_path):
    repo = _repo(tmp_path, TEX_87_3_PCT)
    conn = _seeded_conn(run_a={"accuracy": 0.5})

    assert claims.ingest_claims_repo(conn, repo, ["paper.tex"]) == {"claims": 1, "candidates": 0}
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
    assert first == second == {"claims": 1, "candidates": 1}
    assert len(db.get_nodes_by_type(conn, "claim")) == 1
    assert len(db.query_edges(conn, type="backed_by")) == 1
    assert db.get_nodes_by_type(conn, "claim")[0]["id"] == "claim:paper.tex#2-1"
