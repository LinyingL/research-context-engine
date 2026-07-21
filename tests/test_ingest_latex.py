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
        assert "unresolved" not in smith["attrs"]

        # unresolved reference: cited but absent from refs.bib.
        placeholder = db.get_node(conn, "ref:unknown2099")
        assert placeholder["attrs"] == {"unresolved": True}

        cites = db.query_edges(conn, src="section:paper.tex#introduction", type="cites")
        assert {e["dst"] for e in cites} == {"ref:smith2020", "ref:jones2019"}

        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='section'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='figure'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE type='cites'").fetchone()[0] == 3

    conn.close()


def test_unresolvable_paths_never_guessed(tmp_path):
    # No \section yet -- there is no Section to attach an edge to.
    (tmp_path / "orphan.tex").write_text("\\includegraphics{nowhere.png}\n\\section{Only}\n")
    result = latex.parse_tex_file(tmp_path, "orphan.tex")
    assert result.figure_links == [] and len(result.sections) == 1
    # Escapes the repo root, and an empty path -- both unresolvable.
    assert latex._resolve_figure_path("paper.tex", None, "../../etc/passwd") is None
    assert latex._resolve_figure_path("paper.tex", None, "") is None
