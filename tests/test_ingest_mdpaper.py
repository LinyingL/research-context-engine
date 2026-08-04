"""Tests for rce.ingest.mdpaper (task W3): Markdown paper support.

Builds small fake repo trees under tmp_path (no real git needed -- this
module only needs a root dir + relative paths, same convention as
test_ingest_latex.py / test_ingest_claims.py).
"""

import logging
from pathlib import Path

from rce import db
from rce.ingest import latex, mdpaper

_NO_CLEANUP = {
    "claims_removed": 0,
    "backed_by_edges_removed": 0,
    "claims_preserved_with_human_judgement": 0,
}


def _repo(tmp_path: Path, md: str, path: str = "paper.md") -> Path:
    (tmp_path / path).write_text(md)
    return tmp_path


def _seeded_conn(**metrics_by_experiment):
    conn = db.connect(":memory:")
    db.migrate(conn)
    for run_id, metrics in metrics_by_experiment.items():
        db.upsert_node(conn, f"experiment:{run_id}", "experiment", attrs={"metrics": metrics})
    return conn


# --- Section parsing / slug convention -------------------------------------


def test_parse_md_file_sections_use_same_slug_convention_as_latex(tmp_path):
    repo = _repo(
        tmp_path,
        "# Introduction\n\nSome text.\n\n## Motivation\n\nMore text.\n\n"
        "# Introduction\n\nDuplicate title on purpose.\n",
    )
    result = mdpaper.parse_md_file(repo, "paper.md")
    intro, motivation, intro2 = result.sections
    assert intro.id == "section:paper.md#introduction"
    assert motivation.id == "section:paper.md#motivation"
    assert intro2.id == "section:paper.md#introduction-2"  # slug collision numbered, like latex.py
    assert intro.level == "h1"
    assert motivation.level == "h2"


def test_chinese_heading_slug_matches_latex_convention(tmp_path):
    # rce.ingest.latex._slugify collapses an all-non-ASCII title to a
    # content-derived `section-<hash>` fallback (see that module) -- reused
    # unchanged here (task W3: "slug 化中文标题用与 latex.py 相同的规则"), so a
    # Chinese-only heading gets identical, not merely similar, behavior
    # across formats.
    repo = _repo(tmp_path, "# 结果与讨论\n\n正文。\n\n## 数据\n\n更多正文。\n")
    result = mdpaper.parse_md_file(repo, "paper.md")
    results_section, data_section = result.sections
    expected_results_slug = latex._slugify("结果与讨论")
    expected_data_slug = latex._slugify("数据")
    assert expected_results_slug.startswith("section-") and expected_results_slug != "section"
    assert expected_results_slug != expected_data_slug  # two different titles never collide
    assert results_section.id == f"section:paper.md#{expected_results_slug}"  # matches latex.py's own fallback exactly
    assert results_section.title == "结果与讨论"  # original title preserved, only the slug collapses
    assert data_section.id == f"section:paper.md#{expected_data_slug}"  # different title, different slug, no numbering needed


def test_unrelated_earlier_heading_insertion_does_not_change_a_later_chinese_sections_slug(tmp_path):
    # Regression (evidence-based review, W3): the fallback used to be the
    # bare literal "section", disambiguated only by a per-file
    # encounter-order counter -- so inserting one unrelated non-ASCII
    # heading earlier in the file renumbered every later fallback slug, and
    # since claims._content_id folds the owning section's slug into every
    # claim's content-addressed id, that silently changed claim ids whose
    # own text/section-title never changed (DESIGN.md section 4). The fix
    # makes the fallback slug a hash of the heading's own title text, so it
    # must survive an unrelated heading being inserted earlier in the file.
    before = _repo(tmp_path, "# 结果与讨论\n\n正文。\n", path="before.md")
    before_id = mdpaper.parse_md_file(before, "before.md").sections[0].id

    after = _repo(
        tmp_path,
        "# 二、尝试新方向\n\n不相关的新段落。\n\n# 结果与讨论\n\n正文。\n",
        path="after.md",
    )
    after_sections = mdpaper.parse_md_file(after, "after.md").sections
    after_id = next(s.id for s in after_sections if s.title == "结果与讨论")

    assert after_id.split("#", 1)[1] == before_id.split("#", 1)[1]  # same slug despite the earlier insertion


def test_only_h1_h2_h3_are_recognised_as_sections(tmp_path):
    repo = _repo(
        tmp_path,
        "# Title\n\n#### Not a section (h4)\n\nBody prose here.\n\n##### Also not a section\n",
    )
    result = mdpaper.parse_md_file(repo, "paper.md")
    assert [s.title for s in result.sections] == ["Title"]


# --- Figure references -----------------------------------------------------


def test_image_reference_existing_file_creates_includes_edge(tmp_path):
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "overview.png").write_bytes(b"")
    repo = _repo(tmp_path, "# Overview\n\n![overview](figs/overview.png)\n")

    conn = db.connect(":memory:")
    db.migrate(conn)
    counts = mdpaper.ingest_md_repo(conn, repo, ["paper.md"], image_paths=["figs/overview.png"])
    assert counts["figures"] == 1

    fig_node = db.get_node(conn, "figure:figs/overview.png")
    assert fig_node["type"] == "figure"
    edge = db.query_edges(conn, type="includes")[0]
    assert edge["src"] == "section:paper.md#overview"
    assert edge["dst"] == "figure:figs/overview.png"
    assert edge["extractor"] == "mdpaper" and edge["confidence"] == 1.0 and edge["status"] == "auto"
    assert edge["evidence"]["occurrences"][0] == {"file": "paper.md", "line": 3}


def test_image_reference_missing_file_is_skipped_as_ghost_figure(tmp_path, caplog):
    # No such file on disk, and not in the tracked image inventory either --
    # DESIGN.md's ghost-figure discipline: skip + log, never a dangling node.
    repo = _repo(tmp_path, "# Overview\n\n![overview](figs/missing.png)\n")

    conn = db.connect(":memory:")
    db.migrate(conn)
    with caplog.at_level(logging.WARNING, logger="rce.ingest.mdpaper"):
        counts = mdpaper.ingest_md_repo(conn, repo, ["paper.md"], image_paths=[])
    assert counts["figures"] == 0
    assert db.get_node(conn, "figure:figs/missing.png") is None
    assert db.query_edges(conn, type="includes") == []
    assert any("ghost figure" in r.message for r in caplog.records)


def test_image_reference_before_any_heading_is_skipped_and_logged(tmp_path, caplog):
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "overview.png").write_bytes(b"")
    repo = _repo(tmp_path, "![overview](figs/overview.png)\n\n# Overview\n")

    with caplog.at_level(logging.WARNING, logger="rce.ingest.mdpaper"):
        result = mdpaper.parse_md_file(repo, "paper.md")
    assert result.figure_links == []
    assert any("before any heading" in r.message for r in caplog.records)


def test_image_inventory_none_disables_ghost_figure_validation(tmp_path):
    # Default (image_paths=None) keeps this function usable standalone --
    # e.g. in a test that doesn't build a full repo file inventory --
    # unresolved image paths are accepted exactly as-is, matching
    # rce.ingest.latex.ingest_latex_repo's own documented default.
    repo = _repo(tmp_path, "# Overview\n\n![overview](anything.png)\n")
    conn = db.connect(":memory:")
    db.migrate(conn)
    counts = mdpaper.ingest_md_repo(conn, repo, ["paper.md"])  # image_paths defaults to None
    assert counts["figures"] == 1
    assert db.get_node(conn, "figure:anything.png") is not None


def test_remote_image_url_is_skipped_not_a_repo_file(tmp_path):
    repo = _repo(tmp_path, "# Overview\n\n![badge](https://example.com/badge.svg)\n")
    result = mdpaper.parse_md_file(repo, "paper.md")
    assert result.figure_links == []


def test_image_reference_inside_table_cell_is_still_captured(tmp_path):
    # A thumbnail image inside a results table is a legitimate use -- only
    # numbers inside a table row are skipped (task W3), not image links.
    (tmp_path / "x.png").write_bytes(b"")
    repo = _repo(
        tmp_path,
        "# Overview\n\n| Thumb | Value |\n| --- | --- |\n| ![t](x.png) | 92.1 |\n",
    )
    result = mdpaper.parse_md_file(repo, "paper.md")
    assert len(result.figure_links) == 1
    assert result.figure_links[0].target == "figure:x.png"


# --- Fenced code blocks + table rows: number-scan skip (gatekeeping reuse) -


def test_skips_numbers_inside_fenced_code_blocks(tmp_path):
    repo = _repo(
        tmp_path,
        "# Results\n\nWe report 87.3% overall.\n\n"
        "```python\n# 0.123 shown as example code, not a claim\nx = 0.5\n```\n\n"
        "Final figure stays 11.1%.\n",
    )
    printed = {c.printed_number for c in mdpaper.parse_md_claims(repo, "paper.md")}
    assert printed == {"87.3", "11.1"}  # code-fence contents excluded


def test_skips_numbers_inside_tilde_fenced_code_blocks(tmp_path):
    repo = _repo(
        tmp_path,
        "# Results\n\n~~~\n0.999 inside a tilde fence\n~~~\n\nFinal figure stays 11.1%.\n",
    )
    printed = {c.printed_number for c in mdpaper.parse_md_claims(repo, "paper.md")}
    assert printed == {"11.1"}


def test_skips_numbers_inside_markdown_table_rows(tmp_path):
    repo = _repo(
        tmp_path,
        "# Results\n\nWe report 87.3% overall.\n\n"
        "| Metric | Value |\n| --- | --- |\n| acc | 92.1 |\n| f1 | 0.5 |\n\n"
        "Final figure stays 11.1%.\n",
    )
    printed = {c.printed_number for c in mdpaper.parse_md_claims(repo, "paper.md")}
    assert printed == {"87.3", "11.1"}  # table cell contents (92.1, 0.5) excluded


def test_bare_horizontal_rule_is_not_mistaken_for_a_table(tmp_path):
    # A plain "---" divider (common in hand-written notes) must not trigger
    # the table-row skip just because some earlier, unrelated line has '|'.
    repo = _repo(
        tmp_path,
        "# Results\n\nSee a | b for context.\n\n---\n\nFinal figure stays 11.1%.\n",
    )
    printed = {c.printed_number for c in mdpaper.parse_md_claims(repo, "paper.md")}
    assert printed == {"11.1"}


def test_recognizes_percent_and_fraction_forms_without_latex_escaping(tmp_path):
    # Markdown never backslash-escapes '%' the way LaTeX must -- this is the
    # gap the shared _CLAIM_RE fix (task W3) closes; see rce.ingest.claims.
    repo = _repo(
        tmp_path,
        "# Results\n\nWe reach 87.3% accuracy, matching a raw ratio of 0.873, "
        "with error below 3%.\n",
    )
    forms = {(c.printed_number, c.unit_form, c.value) for c in mdpaper.parse_md_claims(repo, "paper.md")}
    assert forms == {
        ("87.3", "percent", 0.873),
        ("0.873", "fraction", 0.873),
        ("3", "percent", 0.03),
    }


def test_skips_hyphenated_compound_modifier_numbers(tmp_path):
    # Same deterministic syntax rule as rce.ingest.claims (inherited, not
    # reimplemented) -- "1.58-bit precision" is a compound modifier, not a
    # quantitative claim.
    repo = _repo(
        tmp_path,
        "# Method\n\nWe call this quantization scheme 1.58-bit precision.\n\n"
        "Final figure stays 33.3%.\n",
    )
    printed = {c.printed_number for c in mdpaper.parse_md_claims(repo, "paper.md")}
    assert printed == {"33.3"}


def test_plain_integers_years_and_page_numbers_are_not_claims(tmp_path):
    # No decimal point, no percent sign -- never a claim (same rule as LaTeX).
    repo = _repo(tmp_path, "# Results\n\nSee the 2024 results on page 12.\n")
    assert mdpaper.parse_md_claims(repo, "paper.md") == []


# --- Non-paper filename filtering ------------------------------------------


def test_readme_changelog_license_are_skipped_as_non_paper(tmp_path):
    for name in ("README.md", "CHANGELOG.md", "LICENSE.md"):
        (tmp_path / name).write_text("# Not a paper\n\nSome prose with 87.3% in it.\n")
    (tmp_path / "readme_notes.md").write_text("# Real note\n\nLowercase prefix -- still ingested.\n")

    conn = db.connect(":memory:")
    db.migrate(conn)
    counts = mdpaper.ingest_md_repo(
        conn, tmp_path, ["README.md", "CHANGELOG.md", "LICENSE.md", "readme_notes.md"],
    )
    assert counts["md_skipped_non_paper"] == 3
    assert counts["sections"] == 1  # only readme_notes.md's heading is ingested
    section = db.get_nodes_by_type(conn, "section")[0]
    assert section["id"] == "section:readme_notes.md#real-note"


def test_other_md_files_are_ingested_by_default(tmp_path):
    _repo(tmp_path, "# A Draft\n\nSome text.\n", path="00-project-map.md")
    conn = db.connect(":memory:")
    db.migrate(conn)
    counts = mdpaper.ingest_md_repo(conn, tmp_path, ["00-project-map.md"])
    assert counts["sections"] == 1
    assert counts["md_skipped_non_paper"] == 0


# --- Claim nodes + backed_by candidates (reusing claims' core write path) --


def test_claim_matches_experiment_metric_and_creates_pending_backed_by_edge(tmp_path):
    repo = _repo(tmp_path, "# Results\n\nWe reach 87.3% accuracy.\n")
    conn = _seeded_conn(run_a={"accuracy": 0.87312})  # rounds to 0.873 at 3dp -> matches

    counts = mdpaper.ingest_md_repo(conn, repo, ["paper.md"])
    assert counts == {
        **_NO_CLEANUP, "sections": 1, "figures": 0, "md_skipped_non_paper": 0,
        "claims": 1, "candidates": 1,
    }

    edge = db.query_edges(conn, type="backed_by")[0]
    assert edge["dst"] == "experiment:run_a"
    assert edge["status"] == "pending"  # machine path never writes "confirmed"/"auto"
    assert edge["extractor"] == "claims"  # names the shared matching algorithm, not the file format
    assert edge["confidence"] == 1.0
    assert edge["evidence"]["occurrences"][0]["metric"] == "accuracy"

    claim_node = db.get_nodes_by_type(conn, "claim")[0]
    assert claim_node["attrs"]["tex_path"] == "paper.md"  # shared key, see rce.ingest.claims docstring


def test_claim_with_no_matching_experiment_creates_node_but_no_edge(tmp_path):
    repo = _repo(tmp_path, "# Results\n\nWe reach 87.3% accuracy.\n")
    conn = _seeded_conn(run_a={"accuracy": 0.5})

    counts = mdpaper.ingest_md_repo(conn, repo, ["paper.md"])
    assert counts["claims"] == 1 and counts["candidates"] == 0
    assert db.query_edges(conn, type="backed_by") == []


def test_no_experiment_nodes_means_every_claim_has_zero_candidates(tmp_path):
    repo = _repo(tmp_path, "# Results\n\nWe reach 87.3% accuracy.\n")
    conn = db.connect(":memory:")
    db.migrate(conn)

    counts = mdpaper.ingest_md_repo(conn, repo, ["paper.md"])
    assert counts["claims"] == 1 and counts["candidates"] == 0
    assert db.query_edges(conn, type="backed_by") == []


# --- Idempotency + orphan cleanup ------------------------------------------


def test_ingest_md_repo_is_idempotent(tmp_path):
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "overview.png").write_bytes(b"")
    repo = _repo(
        tmp_path,
        "# Overview\n\n![overview](figs/overview.png)\n\nWe reach 87.3% accuracy.\n",
    )
    conn = _seeded_conn(run_a={"accuracy": 0.87312})

    first = mdpaper.ingest_md_repo(conn, repo, ["paper.md"], image_paths=["figs/overview.png"])
    second = mdpaper.ingest_md_repo(conn, repo, ["paper.md"], image_paths=["figs/overview.png"])
    assert first == second
    assert len(db.get_nodes_by_type(conn, "section")) == 1
    assert len(db.get_nodes_by_type(conn, "figure")) == 1
    assert len(db.get_nodes_by_type(conn, "claim")) == 1
    assert len(db.query_edges(conn, type="includes")) == 1
    assert len(db.query_edges(conn, type="backed_by")) == 1


def test_orphaned_md_claim_is_removed_once_its_sentence_disappears(tmp_path):
    repo = _repo(tmp_path, "# Results\n\nWe reach 87.3% accuracy.\n")
    conn = _seeded_conn(run_a={"accuracy": 0.87312})

    first = mdpaper.ingest_md_repo(conn, repo, ["paper.md"])
    assert first["claims"] == 1 and len(db.get_nodes_by_type(conn, "claim")) == 1

    (tmp_path / "paper.md").write_text("# Results\n\nNothing quantitative here.\n")
    second = mdpaper.ingest_md_repo(conn, repo, ["paper.md"])

    assert second["claims"] == 0
    assert second["claims_removed"] == 1
    assert second["backed_by_edges_removed"] == 1
    assert db.get_nodes_by_type(conn, "claim") == []
    assert db.query_edges(conn, type="backed_by") == []


def test_unreadable_md_path_is_not_treated_as_evidence_its_content_is_gone(tmp_path):
    repo = _repo(tmp_path, "# Results\n\nWe reach 87.3% accuracy.\n")
    conn = _seeded_conn(run_a={"accuracy": 0.87312})

    first = mdpaper.ingest_md_repo(conn, repo, ["paper.md"])
    assert first["claims"] == 1

    (tmp_path / "paper.md").unlink()
    second = mdpaper.ingest_md_repo(conn, repo, ["paper.md"])

    assert second["claims"] == 0
    assert second["claims_removed"] == 0  # not wiped out just because the read failed
    assert len(db.get_nodes_by_type(conn, "claim")) == 1
