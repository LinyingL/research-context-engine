"""Tests for rce.ingest.latex. Builds a small fake repo tree under tmp_path
(no real git needed -- this module only needs a root dir + relative paths).
"""

from pathlib import Path

from rce import db
from rce.ingest import latex

TEX_CONTENT = r"""% top-level comment line -- must be fully ignored
\documentclass{article}
\graphicspath{{figs/}{alt/}}

\begin{document}

\section{Introduction}
\label{sec:intro}
Prior work \cite{smith2020, jones2019} and see \ref{fig:overview}.
\includegraphics[width=0.8\linewidth]{overview.png}

\subsection{Motivation}
\includegraphics{overview.png}
\cite{unknown2099}

\section{Introduction}
Duplicate title on purpose -- must get a numbered slug.

\end{document}
"""

BIB_CONTENT = r"""% comment line in bib -- must be ignored
@article{smith2020,
  title = {A Study of Things},
  author = {Smith, John},
  year = {2020},
}

@inproceedings{jones2019,
  title = "Another Paper",
  author = "Jones, Jane",
  year = 2019
}
"""


def _line_containing(text: str, needle: str) -> int:
    return next(i for i, l in enumerate(text.splitlines(), start=1) if needle in l)


def _build_repo(tmp_path: Path) -> Path:
    (tmp_path / "figs").mkdir()
    (tmp_path / "paper.tex").write_text(TEX_CONTENT)
    (tmp_path / "refs.bib").write_text(BIB_CONTENT)
    return tmp_path


def test_parse_tex_file_sections_figures_cites_labels_refs(tmp_path):
    repo = _build_repo(tmp_path)
    result = latex.parse_tex_file(repo, "paper.tex")

    intro, motivation, intro2 = result.sections
    assert intro.id == "section:paper.tex#introduction"
    assert motivation.id == "section:paper.tex#motivation"
    assert intro2.id == "section:paper.tex#introduction-2"  # slug collision numbered
    assert motivation.level == "subsection"

    fig_line = _line_containing(TEX_CONTENT, "includegraphics[width")
    assert result.figure_links[0] == latex.Link(intro.id, "figure:figs/overview.png", fig_line)
    assert result.figure_links[1].section_id == motivation.id
    assert result.figure_links[1].target == "figure:figs/overview.png"  # same figure, other section

    cite_line = _line_containing(TEX_CONTENT, "\\cite{smith2020")
    assert {(l.target, l.line) for l in result.cite_links if l.section_id == intro.id} == {
        ("smith2020", cite_line), ("jones2019", cite_line),
    }

    assert result.section_attrs[intro.id]["labels"] == [{"name": "sec:intro", "line": cite_line - 1}]
    assert result.section_attrs[intro.id]["refs"] == [{"name": "fig:overview", "line": cite_line}]


def test_parse_bib_entries_handles_braced_quoted_and_bare_values():
    entries = {e.key: e for e in latex.parse_bib_entries(BIB_CONTENT)}
    assert entries["smith2020"].fields == {"title": "A Study of Things", "author": "Smith, John", "year": "2020"}
    assert entries["jones2019"].fields == {"title": "Another Paper", "author": "Jones, Jane", "year": "2019"}
    assert entries["jones2019"].entry_type == "inproceedings"


def test_parse_bib_entries_percent_in_field_value_is_not_a_comment():
    # '%' has no comment meaning in BibTeX -- unlike .tex -- so a bare '%'
    # inside a field value must survive, and must not truncate the entry
    # (which would desync the brace-depth scan and swallow later entries).
    bib = r"""
@article{pct2021,
  title = {Achieving 50% accuracy on the benchmark},
  author = {Lee, Kim},
  year = {2021},
}

@article{after2022,
  title = {A Later Paper},
  author = {Doe, Pat},
  year = {2022},
}
"""
    entries = {e.key: e for e in latex.parse_bib_entries(bib)}
    assert entries["pct2021"].fields["title"] == "Achieving 50% accuracy on the benchmark"
    assert entries["after2022"].fields == {
        "title": "A Later Paper", "author": "Doe, Pat", "year": "2022",
    }


def test_parse_bib_entries_unwanted_field_value_does_not_leak_into_wanted_fields():
    # A `name =` substring inside an unwanted field's value (note/url/...)
    # must never be mistaken for a real field -- the scan must consume the
    # whole unwanted value before looking for the next field name.
    bib = r"""
@article{noise2022,
  note = {see author = Smith and year = 1999 for background},
  title = {Correct Title},
  author = {Real Author},
  year = {2022},
}
"""
    entries = {e.key: e for e in latex.parse_bib_entries(bib)}
    assert entries["noise2022"].fields == {
        "title": "Correct Title", "author": "Real Author", "year": "2022",
    }


def test_ingest_latex_repo_writes_nodes_edges_with_evidence_and_is_idempotent(tmp_path):
    repo = _build_repo(tmp_path)
    conn = db.connect(":memory:")
    db.migrate(conn)

    reference_counts_per_run = []
    for _ in range(2):  # second run proves idempotency
        counts = latex.ingest_latex_repo(conn, repo, ["paper.tex"], ["refs.bib"])
        assert counts == {"sections": 3, "figures": 2, "cites": 3}

        fig_node = db.get_node(conn, "figure:figs/overview.png")
        assert fig_node["type"] == "figure"

        includes = db.query_edges(conn, dst="figure:figs/overview.png", type="includes")
        assert len(includes) == 2
        assert {e["src"] for e in includes} == {
            "section:paper.tex#introduction", "section:paper.tex#motivation",
        }
        fig_line = _line_containing(TEX_CONTENT, "includegraphics[width")
        assert [e for e in includes if e["src"] == "section:paper.tex#introduction"][0]["evidence"] == {
            "file": "paper.tex", "line": fig_line,
        }
        for edge in includes:
            assert edge["extractor"] == "latex" and edge["confidence"] == 1.0 and edge["status"] == "auto"

        # resolved reference: real bib fields, not a placeholder.
        smith = db.get_node(conn, "ref:smith2020")
        assert smith["title"] == "A Study of Things"
        assert smith["attrs"]["year"] == "2020"
        assert smith["attrs"]["key"] == "smith2020"  # original as-written bib key
        assert "unresolved" not in smith["attrs"]

        # unresolved reference: cited but absent from refs.bib.
        placeholder = db.get_node(conn, "ref:unknown2099")
        assert placeholder["attrs"] == {"unresolved": True, "key": "unknown2099"}

        cites = db.query_edges(conn, src="section:paper.tex#introduction", type="cites")
        assert {e["dst"] for e in cites} == {"ref:smith2020", "ref:jones2019"}

        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='section'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='figure'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE type='cites'").fetchone()[0] == 3

        reference_counts_per_run.append(
            conn.execute("SELECT COUNT(*) FROM nodes WHERE type='reference'").fetchone()[0]
        )

    # Idempotency gap flagged in the prior review: re-ingesting the same
    # repo must not keep minting new reference nodes (e.g. a placeholder
    # duplicating an already-resolved bib entry due to a casing mismatch).
    assert reference_counts_per_run[0] == reference_counts_per_run[1] == 3

    conn.close()


def test_unresolvable_paths_never_guessed(tmp_path):
    # No \section yet -- there is no Section to attach an edge to.
    (tmp_path / "orphan.tex").write_text("\\includegraphics{nowhere.png}\n\\section{Only}\n")
    result = latex.parse_tex_file(tmp_path, "orphan.tex")
    assert result.figure_links == [] and len(result.sections) == 1
    # Escapes the repo root, and an empty path -- both unresolvable.
    assert latex._resolve_figure_path("paper.tex", None, "../../etc/passwd") is None
    assert latex._resolve_figure_path("paper.tex", None, "") is None


# -- extended cite command coverage (HANDOFF-SPEC.md section 5, 2026-07-22) --


def test_cite_regex_covers_natbib_and_biblatex_variants(tmp_path):
    tex = "\n".join(
        [
            "\\section{Related Work}",
            "\\citep{a2020}",
            "\\citet{b2020}",
            "\\citealp{c2020}",
            "\\parencite{d2020}",
            "\\textcite{e2020}",
            "\\autocite{f2020}",
            "\\Citep{g2020}",
            "\\Citet{h2020}",
            "\\citep[see][p. 5]{i2020}",  # optional-argument form, two brackets
            "\\citet*{j2020}",  # starred form
        ]
    )
    (tmp_path / "related.tex").write_text(tex)
    result = latex.parse_tex_file(tmp_path, "related.tex")
    keys = {link.target for link in result.cite_links}
    assert keys == {
        "a2020", "b2020", "c2020", "d2020", "e2020",
        "f2020", "g2020", "h2020", "i2020", "j2020",
    }


def test_bib_key_matching_is_case_insensitive(tmp_path):
    # \cite{smith2020} must resolve against @article{Smith2020} -- matching
    # is case-insensitive, node IDs are lowercase-normalized, and no
    # spurious unresolved placeholder is created alongside the real node.
    (tmp_path / "paper.tex").write_text(
        "\\section{Intro}\n\\cite{smith2020}\n\\citep{SMITH2020}\n"
    )
    (tmp_path / "refs.bib").write_text(
        "@article{Smith2020,\n  title = {A Study of Things},\n  year = {2020},\n}\n"
    )
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        latex.ingest_latex_repo(conn, tmp_path, ["paper.tex"], ["refs.bib"])

        # Exactly one reference node, at the lowercase-normalized ID -- not
        # split across ref:smith2020 / ref:Smith2020 / ref:SMITH2020, and no
        # unresolved placeholder alongside it.
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='reference'").fetchone()[0] == 1
        node = db.get_node(conn, "ref:smith2020")
        assert node is not None
        assert node["title"] == "A Study of Things"
        assert node["attrs"]["key"] == "Smith2020"  # original as-written casing preserved
        assert "unresolved" not in node["attrs"]

        # Both differently-cased \cite/\citep calls resolve to the same node.
        cites = db.query_edges(conn, src="section:paper.tex#intro", type="cites")
        assert {e["dst"] for e in cites} == {"ref:smith2020"}
        assert len(cites) == 1  # same (src,dst,type,extractor) key -> upsert, not duplicate
    finally:
        conn.close()


# -- T5.5 review item 1: bib key collision is now logged, not silent --


def test_bib_key_collision_after_normalization_is_logged(tmp_path, caplog):
    # Two distinctly-cased keys collide onto the same ref: node id. Behavior
    # is unchanged (last one written wins), but the overwrite must now be
    # visible via logger.warning instead of happening silently.
    (tmp_path / "refs.bib").write_text(
        "@article{Smith2020,\n  title = {First},\n  year = {2020},\n}\n"
        "@article{smith2020,\n  title = {Second},\n  year = {2021},\n}\n"
    )
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        with caplog.at_level("WARNING", logger="rce.ingest.latex"):
            latex.ingest_latex_repo(conn, tmp_path, [], ["refs.bib"])

        assert any(
            "collides" in r.message and "Smith2020" in r.message and "smith2020" in r.message
            for r in caplog.records
        )
        # Only one node exists at the shared normalized id; last write wins.
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='reference'").fetchone()[0] == 1
        node = db.get_node(conn, "ref:smith2020")
        assert node["title"] == "Second"
    finally:
        conn.close()


# -- T5.5 review item 2: ghost figure nodes must not be created --


def test_includegraphics_path_not_in_image_inventory_is_skipped(tmp_path, caplog):
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "overview.png").write_bytes(b"\x89PNG")
    (tmp_path / "paper.tex").write_text(
        "\\section{Intro}\n"
        "\\includegraphics{figs/overview.png}\n"  # tracked -- kept
        "\\includegraphics{figs/ghost.png}\n"  # not tracked -- ghost, must be skipped
    )
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        with caplog.at_level("WARNING", logger="rce.ingest.latex"):
            counts = latex.ingest_latex_repo(
                conn, tmp_path, ["paper.tex"], [], image_paths=["figs/overview.png"]
            )

        assert counts["figures"] == 1  # only the tracked one counted
        assert db.get_node(conn, "figure:figs/overview.png") is not None
        assert db.get_node(conn, "figure:figs/ghost.png") is None  # no ghost node
        assert db.query_edges(conn, dst="figure:figs/ghost.png") == []  # no ghost edge
        assert any("ghost figure" in r.message and "figs/ghost.png" in r.message for r in caplog.records)
    finally:
        conn.close()


def test_includegraphics_image_inventory_none_disables_validation(tmp_path):
    # Default (image_paths=None) keeps this function usable standalone, e.g.
    # in tests that don't build a full repo file inventory -- unresolved
    # image paths are accepted exactly as before this change.
    (tmp_path / "paper.tex").write_text("\\section{Intro}\n\\includegraphics{anything.png}\n")
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        counts = latex.ingest_latex_repo(conn, tmp_path, ["paper.tex"], [])
        assert counts["figures"] == 1
        assert db.get_node(conn, "figure:anything.png") is not None
    finally:
        conn.close()
