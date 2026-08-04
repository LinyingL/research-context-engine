"""Tests for rce.ingest.latex. Builds a small fake repo tree under tmp_path
(no real git needed -- this module only needs a root dir + relative paths).
"""

from pathlib import Path

from rce import db
from rce.ingest import claims as claims_ingest
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


def test_slugify_fallback_for_non_ascii_title_is_content_derived_not_a_bare_literal():
    # A title with no ASCII letters/digits (e.g. an all-Chinese heading --
    # the common case for a real Chinese-language paper) used to collapse to
    # the bare literal "section", disambiguated only by _dedupe_slug's
    # per-file encounter-order counter. Two different such titles must now
    # get two different slugs on _slugify alone, with no counter involved.
    results_slug = latex._slugify("结果与讨论")
    data_slug = latex._slugify("数据")
    assert results_slug.startswith("section-") and results_slug != "section"
    assert data_slug.startswith("section-") and data_slug != "section"
    assert results_slug != data_slug
    # Deterministic: the same title always slugifies to the same fallback.
    assert latex._slugify("结果与讨论") == results_slug


def test_unrelated_earlier_section_insertion_does_not_change_a_later_chinese_sections_id(tmp_path):
    # Regression (evidence-based review, W3): with the old bare-literal
    # "section" fallback, inserting one unrelated all-Chinese \section
    # earlier in the file renumbered every later fallback slug -- and since
    # claims._content_id folds the owning section's slug into every claim's
    # content-addressed id (DESIGN.md section 4), that silently changed
    # claim ids whose own sentence/number/section-title never changed,
    # orphaning any human confirm/reject verdict recorded against them. The
    # fallback slug must instead survive an unrelated heading inserted
    # anywhere earlier in the same file.
    (tmp_path / "before.tex").write_text("\\section{结果与讨论}\n正文。\n")
    before_id = latex.parse_tex_file(tmp_path, "before.tex").sections[0].id

    (tmp_path / "after.tex").write_text(
        "\\section{二、尝试新方向}\n不相关的新段落。\n\n\\section{结果与讨论}\n正文。\n"
    )
    after_sections = latex.parse_tex_file(tmp_path, "after.tex").sections
    after_id = next(s.id for s in after_sections if s.title == "结果与讨论")

    assert after_id.split("#", 1)[1] == before_id.split("#", 1)[1]  # same slug despite the earlier insertion


def test_compute_ordered_slugs_covers_all_three_disambiguation_rules():
    # (a) no collision at all -- bare base slugs, exactly _dedupe_slug's old
    # behavior. This is the hard regression requirement: an ordinary,
    # non-colliding project's ids must not move because of this fix.
    assert latex._compute_ordered_slugs([("Intro", "Intro"), ("Data", "Data")]) == ["intro", "data"]

    # (b) the blocker itself -- two DIFFERENT titles land on the same base
    # slug ("h3", from titles with few ASCII tokens) -- both hash-
    # disambiguated, and the assignment does not depend on which one is
    # listed (== encountered) first.
    forward = latex._compute_ordered_slugs([("H3 检验", "H3 检验"), ("H3：检验结果", "H3：检验结果")])
    backward = latex._compute_ordered_slugs([("H3：检验结果", "H3：检验结果"), ("H3 检验", "H3 检验")])
    assert forward[0] != forward[1]  # distinct titles never collapse onto the same slug
    assert forward[0].startswith("h3-") and forward[1].startswith("h3-")
    assert forward[0] == backward[1] and forward[1] == backward[0]  # order-independent

    # (c) genuinely identical title text repeated -- the one remaining
    # position-dependent case, numbered in encounter order (same convention
    # _dedupe_slug used).
    assert latex._compute_ordered_slugs([("Intro", "Intro"), ("Intro", "Intro")]) == ["intro", "intro-2"]


# -- Blocker (two review rounds): a base slug collision between two
# DIFFERENT titles used to be numbered by encounter order (see
# _compute_ordered_slugs). Property tests below are the acceptance test. --


def _section_and_claim_ids_by_key(repo: Path, tex_rel_path: str) -> tuple[dict[str, str], dict[str, str]]:
    sections = latex.parse_tex_file(repo, tex_rel_path).sections
    parsed_claims = claims_ingest.parse_tex_claims(repo, tex_rel_path)
    return (
        {s.title: s.id for s in sections},
        {c.sentence: c.id for c in parsed_claims},
    )


def test_ascii_collision_ids_survive_an_unrelated_heading_inserted_earlier(tmp_path):
    # Same file, edited in place (the real-world shape of this bug: a
    # researcher inserts one new heading and re-ingests) -- a claim id
    # embeds its owning tex path (claims._content_id), so comparing across
    # two *different* filenames would conflate "different file" with
    # "different content" and prove nothing about position-independence.
    body = (
        "\\section{H3 检验}\n准确率达到 87.3\\%。\n\n"
        "\\section{H3：检验结果}\n误差低于 3.0\\%。\n"
    )
    tex_path = tmp_path / "paper.tex"
    tex_path.write_text(body)
    before_sections, before_claims = _section_and_claim_ids_by_key(tmp_path, "paper.tex")

    tex_path.write_text("\\section{无关新段落}\n不相关的内容。\n\n" + body)
    after_sections, after_claims = _section_and_claim_ids_by_key(tmp_path, "paper.tex")

    assert before_claims and before_claims == after_claims  # sanity: claims were actually found
    for title in ("H3 检验", "H3：检验结果"):
        assert before_sections[title] == after_sections[title]


def test_ascii_collision_ids_are_stable_when_the_two_colliding_headings_swap_order(tmp_path):
    # Same file (see rationale above): reordering the two colliding
    # headings in place must not change either one's own id.
    first = (
        "\\section{H3 检验}\n准确率达到 87.3\\%。\n\n"
        "\\section{H3：检验结果}\n误差低于 3.0\\%。\n"
    )
    second = (
        "\\section{H3：检验结果}\n误差低于 3.0\\%。\n\n"
        "\\section{H3 检验}\n准确率达到 87.3\\%。\n"
    )
    tex_path = tmp_path / "paper.tex"
    tex_path.write_text(first)
    first_sections, first_claims = _section_and_claim_ids_by_key(tmp_path, "paper.tex")

    tex_path.write_text(second)
    second_sections, second_claims = _section_and_claim_ids_by_key(tmp_path, "paper.tex")

    assert first_claims and first_claims == second_claims  # sanity: claims were actually found
    for title in ("H3 检验", "H3：检验结果"):
        assert first_sections[title] == second_sections[title]


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
        # (T10) db.upsert_edge now wraps evidence as {"occurrences": [...]}.
        assert [e for e in includes if e["src"] == "section:paper.tex#introduction"][0]["evidence"] == {
            "occurrences": [{"file": "paper.tex", "line": fig_line}],
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


# -- T10 (candidate-2 testbed regression): preamble/abstract citations -----
# were silently dropped -- \cite before the first \section (i.e. in the
# abstract or introduction) now attaches to a synthetic
# section:<file>#preamble node instead of being lost.


def test_parse_tex_file_attaches_preamble_cite_to_synthetic_section(tmp_path):
    (tmp_path / "paper.tex").write_text(
        "\\begin{abstract}\nSee \\cite{early2020} for background.\n\\end{abstract}\n"
        "\\section{Intro}\n\\cite{late2021}\n"
    )
    result = latex.parse_tex_file(tmp_path, "paper.tex")

    preamble_id = "section:paper.tex#preamble"
    assert result.cite_links[0] == latex.Link(preamble_id, "early2020", 2)
    assert result.cite_links[1].section_id == "section:paper.tex#intro"

    # Synthetic section is present, first in document order, correctly tagged.
    assert result.sections[0].id == preamble_id
    assert result.sections[0].title == "Preamble/Abstract"
    assert result.section_attrs[preamble_id]["synthetic"] is True
    assert result.section_attrs[preamble_id]["labels"] == []
    assert result.section_attrs[preamble_id]["refs"] == []


def test_parse_tex_file_no_preamble_section_when_no_cite_precedes_first_section(tmp_path):
    # Lazy creation: a file with no preamble citation gets no synthetic node.
    (tmp_path / "paper.tex").write_text("\\section{Intro}\n\\cite{a2020}\n")
    result = latex.parse_tex_file(tmp_path, "paper.tex")
    assert all(s.id != "section:paper.tex#preamble" for s in result.sections)
    assert "section:paper.tex#preamble" not in result.section_attrs


def test_parse_tex_file_real_section_titled_preamble_does_not_collide_with_synthetic(tmp_path):
    """T-blocker regression: a real \\section{Preamble} (any title that
    slugifies to "preamble") used to collide with the synthetic preamble
    node's id, and the lazy synthetic-section block unconditionally
    overwrote the shared section_attrs entry -- discarding the real
    section's already-collected labels and mislabeling it synthetic=True.

    Blocker fix (two review rounds) changed the disambiguation itself: the
    old fix numbered the two by encounter order (`preamble`/`preamble-2`),
    which is exactly the position-dependent numbering the later blocker
    flagged as unstable elsewhere. The synthetic node and the real
    `\\section{Preamble}` are now just two entries in the same
    collision-detection pass (`latex._compute_ordered_slugs`), so *both*
    get a hash-disambiguated slug -- `preamble-<hash>` on both sides,
    computed from each one's own title text, independent of which comes
    first in the file.
    """
    (tmp_path / "paper.tex").write_text(
        "\\documentclass{article}\n"
        "\\citep{early2020}\n"
        "\\section{Preamble}\n"
        "\\label{sec:pre}\n"
    )
    result = latex.parse_tex_file(tmp_path, "paper.tex")

    synthetic_id, real_id = (
        f"section:paper.tex#{slug}"
        for slug in latex._compute_ordered_slugs(
            [(latex._PREAMBLE_SLUG, latex._PREAMBLE_TITLE), ("Preamble", "Preamble")]
        )
    )
    assert synthetic_id != real_id  # still two distinct ids, just not numbered by order
    assert synthetic_id.startswith("section:paper.tex#preamble-")
    assert real_id.startswith("section:paper.tex#preamble-")

    assert [s.id for s in result.sections] == [synthetic_id, real_id]

    assert result.section_attrs[synthetic_id]["synthetic"] is True
    assert result.section_attrs[synthetic_id]["labels"] == []
    assert result.section_attrs[synthetic_id]["refs"] == []

    # The real section's own attrs must survive untouched -- not clobbered
    # by the synthetic block that runs after it in the file.
    assert "synthetic" not in result.section_attrs[real_id]
    assert result.section_attrs[real_id]["labels"] == [{"name": "sec:pre", "line": 4}]

    assert result.cite_links[0].section_id == synthetic_id


def test_ingest_latex_repo_preserves_real_section_when_it_collides_with_preamble_slug(tmp_path):
    # Blocker fix: both ids are now hash-disambiguated (see
    # test_parse_tex_file_real_section_titled_preamble_does_not_collide_
    # with_synthetic above for why "preamble"/"preamble-2" was replaced),
    # so this test looks up the actual ids via the same shared helper
    # rather than hardcoding the old numbered literals.
    (tmp_path / "paper.tex").write_text(
        "\\citep{early2020}\n\\section{Preamble}\n\\label{sec:pre}\n"
    )
    (tmp_path / "refs.bib").write_text(
        "@article{early2020,\n  title = {Early},\n  year = {2020},\n}\n"
    )
    synthetic_slug, real_slug = latex._compute_ordered_slugs(
        [(latex._PREAMBLE_SLUG, latex._PREAMBLE_TITLE), ("Preamble", "Preamble")]
    )
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        counts = latex.ingest_latex_repo(conn, tmp_path, ["paper.tex"], ["refs.bib"])
        # Two distinct section nodes (synthetic preamble + real "Preamble"
        # section) -- counts must match what actually lands in the DB, not
        # over-count a since-deduped id.
        assert counts == {"sections": 2, "figures": 0, "cites": 1}
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='section'").fetchone()[0] == 2

        synthetic = db.get_node(conn, f"section:paper.tex#{synthetic_slug}")
        assert synthetic is not None
        assert synthetic["attrs"]["synthetic"] is True
        assert synthetic["attrs"]["labels"] == []

        real = db.get_node(conn, f"section:paper.tex#{real_slug}")
        assert real is not None
        assert "synthetic" not in real["attrs"]
        assert real["attrs"]["labels"] == [{"name": "sec:pre", "line": 3}]
    finally:
        conn.close()


def test_ingest_latex_repo_creates_preamble_node_and_cites_edge(tmp_path):
    (tmp_path / "paper.tex").write_text(
        "\\cite{foo2020}\n\\section{Intro}\n\\cite{bar2021}\n"
    )
    (tmp_path / "refs.bib").write_text(
        "@article{foo2020,\n  title = {Foo},\n  year = {2020},\n}\n"
        "@article{bar2021,\n  title = {Bar},\n  year = {2021},\n}\n"
    )
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        counts = latex.ingest_latex_repo(conn, tmp_path, ["paper.tex"], ["refs.bib"])
        assert counts == {"sections": 2, "figures": 0, "cites": 2}  # preamble + Intro

        preamble = db.get_node(conn, "section:paper.tex#preamble")
        assert preamble is not None
        assert preamble["type"] == "section"
        assert preamble["title"] == "Preamble/Abstract"
        assert preamble["attrs"]["synthetic"] is True
        assert preamble["attrs"]["tex_path"] == "paper.tex"

        preamble_cites = db.query_edges(conn, src="section:paper.tex#preamble", type="cites")
        assert {e["dst"] for e in preamble_cites} == {"ref:foo2020"}

        intro_cites = db.query_edges(conn, src="section:paper.tex#intro", type="cites")
        assert {e["dst"] for e in intro_cites} == {"ref:bar2021"}
    finally:
        conn.close()


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


# -- regression fix (commit 3085680 introduced the exact-match-only ghost
# figure guard above; it wrongly ghosted the mainstream no-extension
# \includegraphics{name} form) --


def test_includegraphics_without_extension_resolves_to_tracked_image(tmp_path):
    # \includegraphics{overview} (no suffix) is ordinary, mainstream LaTeX
    # usage -- pdflatex itself searches extensions at compile time. Must
    # still create a node + includes edge, with the id carrying the actual
    # tracked extension so it converges with the pyfig ingester's figure ids
    # (HANDOFF-SPEC.md north-star triangle: Section--includes-->Figure).
    (tmp_path / "overview.png").write_bytes(b"\x89PNG")
    (tmp_path / "paper.tex").write_text("\\section{Intro}\n\\includegraphics{overview}\n")
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        counts = latex.ingest_latex_repo(
            conn, tmp_path, ["paper.tex"], [], image_paths=["overview.png"]
        )
        assert counts["figures"] == 1
        assert db.get_node(conn, "figure:overview.png") is not None

        includes = db.query_edges(conn, dst="figure:overview.png", type="includes")
        assert len(includes) == 1
        assert includes[0]["src"] == "section:paper.tex#intro"
    finally:
        conn.close()


def test_includegraphics_without_extension_prefers_pdf_when_both_tracked(tmp_path, caplog):
    # overview.png and overview.pdf both tracked -- deterministic order
    # (mirrors pdflatex's own search order) takes .pdf, and the choice is
    # logged since it was ambiguous.
    (tmp_path / "overview.png").write_bytes(b"\x89PNG")
    (tmp_path / "overview.pdf").write_bytes(b"%PDF")
    (tmp_path / "paper.tex").write_text("\\section{Intro}\n\\includegraphics{overview}\n")
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        with caplog.at_level("INFO", logger="rce.ingest.latex"):
            counts = latex.ingest_latex_repo(
                conn, tmp_path, ["paper.tex"], [], image_paths=["overview.png", "overview.pdf"]
            )
        assert counts["figures"] == 1
        assert db.get_node(conn, "figure:overview.pdf") is not None
        assert db.get_node(conn, "figure:overview.png") is None
        assert any(
            "multiple tracked images" in r.message and "overview.pdf" in r.message
            for r in caplog.records
        )
    finally:
        conn.close()


def test_includegraphics_without_extension_and_no_tracked_candidate_is_still_ghost(tmp_path, caplog):
    # No extension of "nowhere" is tracked at all -- a real ghost figure,
    # still skipped + logged, not silently guessed at.
    (tmp_path / "paper.tex").write_text("\\section{Intro}\n\\includegraphics{nowhere}\n")
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        with caplog.at_level("WARNING", logger="rce.ingest.latex"):
            counts = latex.ingest_latex_repo(
                conn, tmp_path, ["paper.tex"], [], image_paths=["something-else.png"]
            )
        assert counts["figures"] == 0
        assert db.get_node(conn, "figure:nowhere") is None
        assert any("ghost figure" in r.message and "nowhere" in r.message for r in caplog.records)
    finally:
        conn.close()
