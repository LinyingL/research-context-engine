"""Tests for rce.ingest.mappings (DESIGN.md sections 8.1 and 8.5, task V4
phase 1a): the human mappings file, its ingest, and its writer."""

import tomllib
from pathlib import Path

import pytest

from rce import db
from rce.ingest import mappings

RMD = "复现包_分步/17-叙事更替与汇率波动.Rmd"
PDF = "复现包_分步/17-叙事更替与汇率波动.pdf"


def _write(root: Path, text: str) -> Path:
    path = root / ".rce" / "mappings.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _entry(frm: str, to: str, typ: str, extra: str = "") -> str:
    return f'[[mapping]]\nfrom = "{frm}"\nto   = "{to}"\ntype = "{typ}"\n{extra}\n'


def _mapping_edges(conn) -> list[dict]:
    return [e for e in db.query_edges(conn) if e["extractor"] == "mapping"]


# -- grammar ----------------------------------------------------------------------


def test_the_8_5_example_is_stored_as_written_and_confirmed(conn, tmp_path):
    _write(tmp_path, mappings.FILE_HEADER + _entry(RMD, PDF, "generates", 'note = "knitr 渲染产出"\ndate = "2026-09-06"'))
    report = mappings.ingest_mappings(conn, tmp_path)
    assert report.problems == []
    assert report.counts["mappings"] == 1 and report.counts["nodes_created"] == 2
    (edge,) = _mapping_edges(conn)
    assert (edge["src"], edge["dst"], edge["type"]) == (f"script:{RMD}", f"figure:{PDF}", "generates")
    assert edge["status"] == "confirmed"
    assert edge["evidence"]["mapping"]["note"] == "knitr 渲染产出"
    assert edge["evidence"]["mapping"]["date"] == "2026-09-06"
    node = db.get_node(conn, f"figure:{PDF}")
    assert node["type"] == "figure" and node["title"] == PDF
    assert node["attrs"] == {"ghost_origin": "mapping"}


def test_reads_is_stored_script_reads_dataset_like_dataflow(conn, tmp_path):
    _write(tmp_path, _entry("data/panel.csv", "analysis.py", "reads"))
    mappings.ingest_mappings(conn, tmp_path)
    (edge,) = _mapping_edges(conn)
    assert (edge["src"], edge["dst"], edge["type"]) == ("script:analysis.py", "dataset:data/panel.csv", "reads")


def test_writes_is_script_to_dataset(conn, tmp_path):
    _write(tmp_path, _entry("clean.R", "out/clean.rds", "writes"))
    mappings.ingest_mappings(conn, tmp_path)
    (edge,) = _mapping_edges(conn)
    assert (edge["src"], edge["dst"]) == ("script:clean.R", "dataset:out/clean.rds")


def test_julia_script_is_a_script(conn, tmp_path):
    _write(tmp_path, _entry("sim.jl", "fig/sim.png", "generates"))
    report = mappings.ingest_mappings(conn, tmp_path)
    assert report.problems == [] and len(_mapping_edges(conn)) == 1


@pytest.mark.parametrize(
    ("frm", "to", "typ", "code"),
    [
        ("a.py", "data.csv", "reads", "grammar"),          # reversed read
        ("a.py", "fig.png", "writes", "grammar"),          # writes needs a dataset
        ("a.py", "data.csv", "generates", "grammar"),      # generates needs a figure
        ("data.csv", "fig.png", "generates", "grammar"),   # from must be a script
        ("a.py", "report.docx", "generates", "unknown_extension"),
        ("a.py", "fig.png", "produces", "bad_type"),
        ("/etc/passwd.csv", "a.py", "reads", "absolute_path"),
        ("../outside.csv", "a.py", "reads", "escapes_root"),
        ("sub/../../outside.csv", "a.py", "reads", "escapes_root"),
        ("", "a.py", "reads", "empty_path"),
    ],
)
def test_each_refusal_is_per_entry_and_keeps_the_good_ones(conn, tmp_path, frm, to, typ, code):
    _write(tmp_path, "# header\n" + _entry(frm, to, typ) + "\n" + _entry("x.py", "x.png", "generates"))
    report = mappings.ingest_mappings(conn, tmp_path)
    assert [(p.index, p.line, p.code) for p in report.problems] == [(1, 2, code)]
    assert report.counts == {**report.counts, "mappings": 1, "refused": 1}
    assert [(e["src"], e["dst"]) for e in _mapping_edges(conn)] == [("script:x.py", "figure:x.png")]


def test_reversed_read_message_says_to_swap(tmp_path):
    _write(tmp_path, _entry("a.py", "d.csv", "reads"))
    (problem,) = mappings.load_mappings(tmp_path).problems
    assert "swap" in problem.message


def test_missing_key_and_wrong_types_are_refused(tmp_path):
    _write(tmp_path, '[[mapping]]\nfrom = "a.py"\ntype = "generates"\n\n[[mapping]]\nfrom = 3\nto = "f.png"\ntype = "generates"\n')
    codes = [p.code for p in mappings.load_mappings(tmp_path).problems]
    assert codes == ["missing_field", "not_string"]


def test_bad_note_is_dropped_but_entry_kept_and_unquoted_date_accepted(conn, tmp_path):
    _write(tmp_path, _entry("a.py", "f.png", "generates", "note = 5\ndate = 2026-09-06"))
    report = mappings.ingest_mappings(conn, tmp_path)
    assert [p.code for p in report.problems] == ["bad_note"]
    assert report.counts["refused"] == 0
    (edge,) = _mapping_edges(conn)
    assert edge["evidence"]["mapping"]["date"] == "2026-09-06"
    assert "note" not in edge["evidence"]["mapping"]


def test_duplicate_entry_in_file_is_reported_and_edge_asserted_once(conn, tmp_path):
    _write(tmp_path, _entry("a.py", "f.png", "generates") + _entry("./a.py", "f.png", "generates"))
    report = mappings.ingest_mappings(conn, tmp_path)
    assert [p.code for p in report.problems] == ["duplicate"]
    assert len(_mapping_edges(conn)) == 1


def test_symlink_escape_is_refused(conn, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("x")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    _write(tmp_path, _entry("link/secret.csv", "a.py", "reads"))
    report = mappings.ingest_mappings(conn, tmp_path)
    assert [p.code for p in report.problems] == ["escapes_root"]
    assert _mapping_edges(conn) == []


def test_line_numbers_fall_back_to_entry_index_when_headers_do_not_match(tmp_path):
    # An inline-array spelling has no [[mapping]] header lines at all.
    _write(tmp_path, 'mapping = [{from = "a.py", to = "x.docx", type = "generates"}]\n')
    (problem,) = mappings.load_mappings(tmp_path).problems
    assert problem.line is None and problem.index == 1 and problem.location() == "entry #1"


# -- resync ----------------------------------------------------------------------


def test_removed_entry_removes_its_edge_and_its_ghost_nodes(conn, tmp_path):
    _write(tmp_path, _entry("a.py", "f.png", "generates") + _entry("b.py", "g.png", "generates"))
    mappings.ingest_mappings(conn, tmp_path)
    _write(tmp_path, _entry("b.py", "g.png", "generates"))
    report = mappings.ingest_mappings(conn, tmp_path)
    assert report.counts["edges_removed"] == 1 and report.counts["nodes_removed"] == 2
    assert db.get_node(conn, "script:a.py") is None and db.get_node(conn, "figure:f.png") is None
    assert [e["src"] for e in _mapping_edges(conn)] == ["script:b.py"]


def test_shared_endpoint_survives_while_another_mapping_uses_it(conn, tmp_path):
    _write(tmp_path, _entry("a.py", "f.png", "generates") + _entry("a.py", "d.csv", "writes"))
    mappings.ingest_mappings(conn, tmp_path)
    _write(tmp_path, _entry("a.py", "d.csv", "writes"))
    mappings.ingest_mappings(conn, tmp_path)
    assert db.get_node(conn, "script:a.py") is not None
    assert db.get_node(conn, "figure:f.png") is None


def test_resync_never_removes_a_node_a_machine_extractor_owns(conn, tmp_path):
    db.upsert_node(conn, "script:a.py", "script", title="a.py")  # machine-known before the mapping
    _write(tmp_path, _entry("a.py", "f.png", "generates"))
    mappings.ingest_mappings(conn, tmp_path)
    # A machine extractor later upserts the ghost too: it now owns it.
    db.upsert_node(conn, "figure:f.png", "figure", title="f.png")
    _write(tmp_path, mappings.FILE_HEADER)
    report = mappings.ingest_mappings(conn, tmp_path)
    assert report.counts["edges_removed"] == 1 and report.counts["nodes_removed"] == 0
    assert db.get_node(conn, "script:a.py") is not None
    assert db.get_node(conn, "figure:f.png") is not None


def test_resync_keeps_a_ghost_node_another_extractor_has_an_edge_on(conn, tmp_path):
    _write(tmp_path, _entry("d.csv", "a.py", "reads"))
    mappings.ingest_mappings(conn, tmp_path)
    db.upsert_edge(conn, "script:a.py", "dataset:d.csv", "reads", "dataflow", {"line": 1}, 1.0)
    _write(tmp_path, "")
    mappings.ingest_mappings(conn, tmp_path)
    assert db.get_node(conn, "dataset:d.csv") is not None
    assert [e["extractor"] for e in db.query_edges(conn, dst="dataset:d.csv")] == ["dataflow"]


def test_resync_never_touches_another_extractors_edge_on_the_same_pair(conn, tmp_path):
    db.upsert_node(conn, "script:a.py", "script")
    db.upsert_node(conn, "dataset:d.csv", "dataset")
    db.upsert_edge(conn, "script:a.py", "dataset:d.csv", "reads", "dataflow", {"line": 3}, 1.0)
    _write(tmp_path, _entry("d.csv", "a.py", "reads"))
    mappings.ingest_mappings(conn, tmp_path)
    assert {e["extractor"] for e in db.query_edges(conn, src="script:a.py")} == {"dataflow", "mapping"}
    _write(tmp_path, "")
    mappings.ingest_mappings(conn, tmp_path)
    (edge,) = db.query_edges(conn, src="script:a.py")
    assert edge["extractor"] == "dataflow" and edge["status"] == "auto"


def test_missing_file_is_zero_mappings_and_not_deletion(conn, tmp_path):
    path = _write(tmp_path, _entry("a.py", "f.png", "generates"))
    mappings.ingest_mappings(conn, tmp_path)
    path.unlink()
    report = mappings.ingest_mappings(conn, tmp_path)
    assert report.file_present is False and report.counts["mappings"] == 0
    assert len(_mapping_edges(conn)) == 1


def test_missing_file_with_empty_graph_is_not_an_error(conn, tmp_path):
    report = mappings.ingest_mappings(conn, tmp_path)
    assert report.file_present is False and report.problems == []


@pytest.mark.parametrize(
    "content",
    [b'[[mapping]\nfrom = "a.py"\n', b"\xff\xfe not utf-8", b'mapping = "not an array"\n'],
    ids=["toml-syntax", "not-utf8", "wrong-shape"],
)
def test_unreadable_file_raises_and_leaves_the_graph_untouched(conn, tmp_path, content):
    path = _write(tmp_path, _entry("a.py", "f.png", "generates"))
    mappings.ingest_mappings(conn, tmp_path)
    path.write_bytes(content)
    with pytest.raises(mappings.MappingsFileError):
        mappings.ingest_mappings(conn, tmp_path)
    assert len(_mapping_edges(conn)) == 1
    assert db.get_node(conn, "figure:f.png") is not None


def test_reingest_is_idempotent(conn, tmp_path):
    _write(tmp_path, _entry("a.py", "f.png", "generates", 'note = "n"'))
    mappings.ingest_mappings(conn, tmp_path)
    report = mappings.ingest_mappings(conn, tmp_path)
    assert report.counts["nodes_created"] == 0 and report.counts["edges_confirmed"] == 0
    (edge,) = _mapping_edges(conn)
    assert len(edge["evidence"]["occurrences"]) == 1


def test_edited_note_replaces_the_old_one(conn, tmp_path):
    _write(tmp_path, _entry("a.py", "f.png", "generates", 'note = "old"'))
    mappings.ingest_mappings(conn, tmp_path)
    _write(tmp_path, _entry("a.py", "f.png", "generates"))
    mappings.ingest_mappings(conn, tmp_path)
    (edge,) = _mapping_edges(conn)
    assert "note" not in edge["evidence"]["mapping"]


def test_the_file_wins_over_a_later_reject(conn, tmp_path):
    _write(tmp_path, _entry("a.py", "f.png", "generates"))
    mappings.ingest_mappings(conn, tmp_path)
    db.set_edge_status(conn, "script:a.py", "figure:f.png", "generates", "mapping", "rejected")
    mappings.ingest_mappings(conn, tmp_path)
    assert _mapping_edges(conn)[0]["status"] == "confirmed"


# -- the machine boundary ----------------------------------------------------------


def test_machine_extractor_cannot_write_the_mapping_extractor(conn):
    db.upsert_node(conn, "script:a.py", "script")
    db.upsert_node(conn, "figure:f.png", "figure")
    with pytest.raises(ValueError, match="reserved"):
        db.upsert_edge(conn, "script:a.py", "figure:f.png", "generates", "mapping", {"x": 1}, 1.0)
    assert db.query_edges(conn) == []


def test_machine_reingest_never_removes_or_downgrades_a_mapping_edge(conn, tmp_path):
    _write(tmp_path, _entry("d.csv", "a.py", "reads"))
    mappings.ingest_mappings(conn, tmp_path)
    # A machine extractor writes the same pair under its own name, with a
    # machine status, and a machine orphan cleanup clears "every edge".
    db.upsert_node(conn, "script:a.py", "script", title="a.py")
    db.upsert_edge(conn, "script:a.py", "dataset:d.csv", "reads", "dataflow", {"line": 1}, 1.0, "pending")
    db.delete_edges_for_node(conn, "script:a.py")
    (edge,) = db.query_edges(conn, src="script:a.py")
    assert edge["extractor"] == "mapping" and edge["status"] == "confirmed"
    # ...and the node cannot be deleted out from under it.
    with pytest.raises(Exception):
        db.delete_node(conn, "script:a.py")


def test_delete_edges_for_node_still_deletes_mapping_edges_by_name(conn, tmp_path):
    _write(tmp_path, _entry("a.py", "f.png", "generates"))
    mappings.ingest_mappings(conn, tmp_path)
    assert db.delete_edges_for_node(conn, "script:a.py", extractor="mapping") == 1


def test_cjk_paths_round_trip_into_node_ids(conn, tmp_path):
    (tmp_path / "复现包_分步").mkdir()
    (tmp_path / RMD).write_text("x", encoding="utf-8")
    _write(tmp_path, _entry(RMD, PDF, "generates"))
    mappings.ingest_mappings(conn, tmp_path)
    assert db.get_node(conn, f"script:{RMD}")["title"] == RMD


# -- writer ----------------------------------------------------------------------


def _parsed(root: Path) -> list[dict]:
    return tomllib.loads((root / ".rce" / "mappings.toml").read_text(encoding="utf-8"))["mapping"]


def test_add_creates_the_file_with_the_chinese_header(tmp_path):
    result = mappings.add_mapping(tmp_path, RMD, PDF, "generates", note="knitr 渲染产出", date="2026-09-06")
    text = (tmp_path / ".rce" / "mappings.toml").read_text(encoding="utf-8")
    assert text.startswith(mappings.FILE_HEADER)
    assert result["backup"] is None
    assert _parsed(tmp_path) == [
        {"from": RMD, "to": PDF, "type": "generates", "note": "knitr 渲染产出", "date": "2026-09-06"}
    ]
    assert mappings.load_mappings(tmp_path).problems == []


def test_add_defaults_date_to_today_local(tmp_path):
    import datetime

    mappings.add_mapping(tmp_path, "a.py", "f.png", "generates")
    assert _parsed(tmp_path)[0]["date"] == datetime.date.today().isoformat()


@pytest.mark.parametrize(
    "note",
    ['he said "hi"', "C:\\data\\raw", 'tricky \\" end', "trailing backslash \\", "tab\there", "bell\x07", "引号「」与 \\n 字面"],
)
def test_quotes_backslashes_and_controls_round_trip_through_tomllib(tmp_path, note):
    mappings.add_mapping(tmp_path, "a.py", "f.png", "generates", note=note, date="2026-10-04")
    assert _parsed(tmp_path)[0]["note"] == note


def test_paths_with_quotes_round_trip(tmp_path):
    mappings.add_mapping(tmp_path, 'odd "name".py', "f.png", "generates", date="d")
    assert _parsed(tmp_path)[0]["from"] == 'odd "name".py'


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\u2028b", "a\x0bb", "a\x85b"])
def test_add_rejects_invisible_line_separators(tmp_path, bad):
    with pytest.raises(mappings.MappingsWriteError) as exc:
        mappings.add_mapping(tmp_path, "a.py", "f.png", "generates", note=bad)
    assert exc.value.code == "line_break"
    assert not (tmp_path / ".rce" / "mappings.toml").exists()


def test_add_preserves_header_comments_and_order_and_appends_at_end(tmp_path):
    original = (
        "# my own header\n# second line\n\n"
        + _entry("b.py", "g.png", "generates", "# a comment inside the entry")
        + "\n# a trailing remark\n"
        + _entry("d.csv", "b.py", "reads")
    )
    _write(tmp_path, original)
    mappings.add_mapping(tmp_path, "a.py", "f.png", "generates", date="2026-10-04")
    text = (tmp_path / ".rce" / "mappings.toml").read_text(encoding="utf-8")
    assert text.startswith(original)
    assert [e["from"] for e in _parsed(tmp_path)] == ["b.py", "d.csv", "a.py"]


def test_add_refuses_a_duplicate_even_spelled_differently(tmp_path):
    mappings.add_mapping(tmp_path, "a.py", "f.png", "generates")
    before = (tmp_path / ".rce" / "mappings.toml").read_bytes()
    with pytest.raises(mappings.MappingsWriteError) as exc:
        mappings.add_mapping(tmp_path, "./a.py", "f.png", "generates")
    assert exc.value.code == "duplicate"
    assert (tmp_path / ".rce" / "mappings.toml").read_bytes() == before


@pytest.mark.parametrize(
    ("frm", "to", "typ", "code"),
    [
        ("/abs/a.py", "f.png", "generates", "absolute_path"),
        ("../a.py", "f.png", "generates", "escapes_root"),
        ("a.py", "d.csv", "reads", "grammar"),
        ("a.py", "x.docx", "generates", "unknown_extension"),
    ],
)
def test_add_is_never_less_confined_than_ingest(tmp_path, frm, to, typ, code):
    with pytest.raises(mappings.MappingsWriteError) as exc:
        mappings.add_mapping(tmp_path, frm, to, typ)
    assert exc.value.code == code


def test_add_symlink_escape_refused(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-out"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(mappings.MappingsWriteError) as exc:
        mappings.add_mapping(tmp_path, "link/x.csv", "a.py", "reads")
    assert exc.value.code == "escapes_root"


def test_add_creates_a_backup_of_the_previous_file(tmp_path):
    mappings.add_mapping(tmp_path, "a.py", "f.png", "generates")
    before = (tmp_path / ".rce" / "mappings.toml").read_bytes()
    result = mappings.add_mapping(tmp_path, "b.py", "g.png", "generates")
    backup = tmp_path / result["backup"]
    assert backup.parent == tmp_path / ".rce" / "backups"
    assert backup.name.startswith("mappings.toml.") and backup.suffix == ".toml"
    assert backup.read_bytes() == before


def test_add_refuses_to_edit_an_unparseable_file(tmp_path):
    _write(tmp_path, "[[mapping]\n")
    with pytest.raises(mappings.MappingsWriteError) as exc:
        mappings.add_mapping(tmp_path, "a.py", "f.png", "generates")
    assert exc.value.code == "file_unreadable"


def test_add_keeps_crlf_files_crlf(tmp_path):
    path = tmp_path / ".rce" / "mappings.toml"
    path.parent.mkdir()
    path.write_bytes(b"# h\r\n[[mapping]]\r\nfrom = \"b.py\"\r\nto = \"g.png\"\r\ntype = \"generates\"\r\n")
    mappings.add_mapping(tmp_path, "a.py", "f.png", "generates", date="d")
    raw = path.read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert len(_parsed(tmp_path)) == 2


def test_delete_removes_only_that_entry_and_keeps_everything_else(tmp_path):
    original = (
        mappings.FILE_HEADER
        + _entry("a.py", "f.png", "generates", "# about a")
        + "\n"
        + _entry("b.py", "g.png", "generates")
        + "\n# remark before c\n"
        + _entry("c.py", "h.png", "generates")
    )
    _write(tmp_path, original)
    result = mappings.delete_mapping(tmp_path, "b.py", "g.png", "generates")
    assert result["removed"] == 1 and (tmp_path / result["backup"]).read_text(encoding="utf-8") == original
    text = (tmp_path / ".rce" / "mappings.toml").read_text(encoding="utf-8")
    assert text.startswith(mappings.FILE_HEADER) and "# about a" in text and "# remark before c" in text
    assert [e["from"] for e in _parsed(tmp_path)] == ["a.py", "c.py"]


def test_delete_last_entry_leaves_the_header_and_ingest_resyncs_to_zero(conn, tmp_path):
    mappings.add_mapping(tmp_path, "a.py", "f.png", "generates")
    mappings.ingest_mappings(conn, tmp_path)
    mappings.delete_mapping(tmp_path, "a.py", "f.png", "generates")
    text = (tmp_path / ".rce" / "mappings.toml").read_text(encoding="utf-8")
    assert text == mappings.FILE_HEADER
    report = mappings.ingest_mappings(conn, tmp_path)
    assert report.counts["edges_removed"] == 1 and _mapping_edges(conn) == []


def test_delete_unknown_entry_is_not_found(tmp_path):
    mappings.add_mapping(tmp_path, "a.py", "f.png", "generates")
    with pytest.raises(mappings.MappingsWriteError) as exc:
        mappings.delete_mapping(tmp_path, "b.py", "f.png", "generates")
    assert exc.value.code == "not_found"
    # A folder that does not exist is refused before its file is even
    # looked for (DESIGN.md 9.4: a writer never touches -- or re-creates --
    # a project folder that is not there).
    from rce.records import situation

    with pytest.raises(situation.ProjectMovedError):
        mappings.delete_mapping(tmp_path / "nowhere-else", "a.py", "f.png", "generates")
    assert not (tmp_path / "nowhere-else").exists()


def test_delete_refuses_a_shape_it_cannot_edit_faithfully(tmp_path):
    # An inline array holds the entry; the line editor has no block for it.
    _write(tmp_path, 'mapping = [{from = "a.py", to = "f.png", type = "generates"}]\n')
    with pytest.raises(mappings.MappingsWriteError) as exc:
        mappings.delete_mapping(tmp_path, "a.py", "f.png", "generates")
    assert exc.value.code == "not_found"


def test_writer_output_ingests_cleanly(conn, tmp_path):
    mappings.add_mapping(tmp_path, "d.csv", "a.py", "reads", note='q "x" \\ y')
    mappings.add_mapping(tmp_path, "a.py", "o.parquet", "writes")
    report = mappings.ingest_mappings(conn, tmp_path)
    assert report.problems == [] and report.counts["mappings"] == 2
    assert {e["status"] for e in _mapping_edges(conn)} == {"confirmed"}


def test_add_refuses_when_the_planned_text_would_not_round_trip(tmp_path):
    # Appending [[mapping]] after a static inline array is invalid TOML; the
    # re-parse check catches it and the file is left exactly as it was.
    path = _write(tmp_path, 'mapping = [{from = "b.py", to = "g.png", type = "generates"}]\n')
    before = path.read_bytes()
    with pytest.raises(mappings.MappingsWriteError) as exc:
        mappings.add_mapping(tmp_path, "a.py", "f.png", "generates")
    assert exc.value.code == "unsafe_edit"
    assert path.read_bytes() == before
    assert not (tmp_path / ".rce" / "backups").exists()


# -- the file itself is confined too (adversarial review of the V4 work) ----------


def _symlinked_rce_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "data" / "a.csv").write_text("x\n", encoding="utf-8")
    (root / "s.py").write_text("print(1)\n", encoding="utf-8")
    outside = tmp_path / "outside" / "victim"
    outside.mkdir(parents=True)
    (root / ".rce").symlink_to(outside, target_is_directory=True)
    return root, outside


def test_add_refuses_a_symlinked_rce_dir_that_leaves_the_project(tmp_path):
    """Section 8.5: the write path is never less confined than the read
    path. A `.rce` that is a symlink out of the project must not let 确认标注
    create (or replace) a mappings.toml somewhere else."""
    root, outside = _symlinked_rce_project(tmp_path)
    with pytest.raises(mappings.MappingsWriteError) as excinfo:
        mappings.add_mapping(root, "data/a.csv", "s.py", "reads")
    assert excinfo.value.code == "escapes_root"
    assert list(outside.iterdir()) == []


def test_add_refuses_to_replace_an_outside_mappings_file_and_backs_up_nothing(tmp_path):
    root, outside = _symlinked_rce_project(tmp_path)
    (outside / "mappings.toml").write_text(mappings.FILE_HEADER, encoding="utf-8")
    with pytest.raises(mappings.MappingsWriteError):
        mappings.add_mapping(root, "data/a.csv", "s.py", "reads")
    assert (outside / "mappings.toml").read_text(encoding="utf-8") == mappings.FILE_HEADER
    assert not (outside / "backups").exists()


def test_add_and_delete_refuse_a_symlinked_mappings_file(tmp_path):
    """Replacing the link would silently turn it into a regular file, and
    the backup would carry the link target's bytes under this name."""
    root = tmp_path / "proj"
    (root / ".rce").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "data" / "a.csv").write_text("x\n", encoding="utf-8")
    (root / "s.py").write_text("print(1)\n", encoding="utf-8")
    secret = tmp_path / "secret.toml"
    secret.write_text(mappings.FILE_HEADER + _entry("data/a.csv", "s.py", "reads"), encoding="utf-8")
    (root / ".rce" / "mappings.toml").symlink_to(secret)
    for call in (
        lambda: mappings.add_mapping(root, "data/a.csv", "s.py", "reads", note="n"),
        lambda: mappings.delete_mapping(root, "data/a.csv", "s.py", "reads"),
    ):
        with pytest.raises(mappings.MappingsWriteError) as excinfo:
            call()
        assert excinfo.value.code == "escapes_root"
    assert (root / ".rce" / "mappings.toml").is_symlink()
    assert not (root / ".rce" / "backups").exists()


def test_ingest_refuses_a_mappings_file_that_resolves_outside_the_project(conn, tmp_path):
    root, outside = _symlinked_rce_project(tmp_path)
    (outside / "mappings.toml").write_text(_entry("data/a.csv", "s.py", "reads"), encoding="utf-8")
    with pytest.raises(mappings.MappingsFileError):
        mappings.ingest_mappings(conn, root)
    assert _mapping_edges(conn) == []
