"""Tests for rce.ingest.files (W1): the filesystem-walk fallback inventory
used when a project root is not a git repository. No subprocess `git`
involved here -- plain directories/files built via tmp_path, mirroring the
category assertions tests/test_ingest_git.py already makes for its git
counterpart.
"""

import os
from pathlib import Path

import pytest

from rce.ingest import files as files_ingest


def test_categorizes_every_tracked_extension(tmp_path: Path):
    (tmp_path / "paper.tex").write_text(r"\section{Intro}")
    (tmp_path / "refs.bib").write_text("@article{a,}")
    (tmp_path / "fig1.png").write_bytes(b"\x89PNG")
    (tmp_path / "plot.py").write_text("print('hi')\n")
    (tmp_path / "notes.md").write_text("# Notes\n")
    (tmp_path / "analysis.R").write_text("x <- 1\n")
    (tmp_path / "report.Rmd").write_text("---\ntitle: x\n---\n")
    (tmp_path / "data1.csv").write_text("a,b\n1,2\n")
    (tmp_path / "data2.xlsx").write_bytes(b"PK\x03\x04")
    (tmp_path / "data3.parquet").write_bytes(b"PAR1")
    (tmp_path / "data4.rds").write_bytes(b"\x1f\x8b")
    (tmp_path / "data5.dta").write_bytes(b"<stata_dta>")
    (tmp_path / "data6.json").write_text("{}")
    (tmp_path / "readme.txt").write_text("not a tracked category")

    inventory = files_ingest.list_source_files(tmp_path)

    assert inventory["tex"] == ["paper.tex"]
    assert inventory["bib"] == ["refs.bib"]
    assert inventory["image"] == ["fig1.png"]
    assert inventory["py"] == ["plot.py"]
    assert inventory["md"] == ["notes.md"]
    assert inventory["r"] == ["analysis.R"]
    assert inventory["rmd"] == ["report.Rmd"]
    assert sorted(inventory["data"]) == sorted(
        ["data1.csv", "data2.xlsx", "data3.parquet", "data4.rds", "data5.dta", "data6.json"]
    )
    all_listed = [p for paths in inventory.values() for p in paths]
    assert "readme.txt" not in all_listed


def test_recurses_into_ordinary_subdirectories(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "gen.py").write_text("print('x')\n")
    (tmp_path / "figs" / "sub").mkdir(parents=True)
    (tmp_path / "figs" / "sub" / "deep.png").write_bytes(b"\x89PNG")

    inventory = files_ingest.list_source_files(tmp_path)
    assert inventory["py"] == ["scripts/gen.py"]
    assert inventory["image"] == ["figs/sub/deep.png"]


@pytest.mark.parametrize(
    "noise_dir",
    [".git", ".rce", "__pycache__", ".venv", "node_modules", ".Rproj.user", "_cache", ".ipynb_checkpoints"],
)
def test_skips_known_noise_directories(tmp_path: Path, noise_dir: str):
    (tmp_path / noise_dir).mkdir()
    (tmp_path / noise_dir / "hidden.py").write_text("print('should not be seen')\n")
    (tmp_path / "visible.py").write_text("print('ok')\n")

    inventory = files_ingest.list_source_files(tmp_path)
    assert inventory["py"] == ["visible.py"]


def test_skips_arbitrary_dot_prefixed_directories(tmp_path: Path):
    """Not every tool-cache directory can be enumerated in a fixed list
    (.mypy_cache, .idea, .pytest_cache, ...) -- any dot-prefixed directory
    name is skipped outright, not just the ones in the known-noise list."""
    (tmp_path / ".mypy_cache").mkdir()
    (tmp_path / ".mypy_cache" / "hidden.py").write_text("print('should not be seen')\n")
    (tmp_path / "visible.py").write_text("print('ok')\n")

    inventory = files_ingest.list_source_files(tmp_path)
    assert inventory["py"] == ["visible.py"]


def test_skips_hidden_files(tmp_path: Path):
    (tmp_path / ".hidden.py").write_text("print('should not be seen')\n")
    (tmp_path / "visible.py").write_text("print('ok')\n")

    inventory = files_ingest.list_source_files(tmp_path)
    assert inventory["py"] == ["visible.py"]


def test_does_not_follow_symlinked_directory(tmp_path: Path):
    real_dir = tmp_path / "real_figs"
    real_dir.mkdir()
    (real_dir / "linked.png").write_bytes(b"\x89PNG")
    link_dir = tmp_path / "linked_dir"
    try:
        os.symlink(real_dir, link_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this filesystem")

    inventory = files_ingest.list_source_files(tmp_path)
    # The real directory's own file is found via its real path...
    assert inventory["image"] == ["real_figs/linked.png"]
    # ...but never a second time by walking through the symlink to it.
    assert "linked_dir/linked.png" not in inventory["image"]


def test_does_not_follow_symlinked_file(tmp_path: Path):
    real_file = tmp_path / "real.png"
    real_file.write_bytes(b"\x89PNG")
    link_file = tmp_path / "linked.png"
    try:
        os.symlink(real_file, link_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this filesystem")

    inventory = files_ingest.list_source_files(tmp_path)
    assert inventory["image"] == ["real.png"]


def test_empty_directory_returns_all_empty_categories(tmp_path: Path):
    inventory = files_ingest.list_source_files(tmp_path)
    assert inventory == {
        "tex": [], "bib": [], "image": [], "py": [], "md": [], "r": [], "rmd": [], "data": [],
    }
