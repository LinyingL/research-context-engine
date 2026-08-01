"""Integration regression tests for the non-ASCII git path bug (real repro:
a Chinese-filename research repo -- see rce.ingest.git's module docstring).

git's default core.quotepath=true octal-escapes and double-quotes any path
containing non-ASCII bytes in `git ls-files` / `git log --name-only`
output; before the fix, rce.ingest.git used that escaped string verbatim
for extension matching and path joins, so every non-ASCII-named file was
invisible to every downstream extractor with zero warning. These tests
build one real throwaway git repo (subprocess `git`, no mocking, mirroring
tests/test_ingest_git.py's pattern) with Chinese filenames/dirnames and a
filename mixing a space with non-ASCII text, run the git + latex + pyfig
ingesters together end to end, and assert the whole pipeline resolves real
paths rather than escaped ones. core.quotepath is set to true explicitly
(not relying on git's default) so this stays a real regression guard even
if the default, or a machine's global gitconfig, ever changes.
"""

import subprocess
from pathlib import Path

import pytest

from rce import db
from rce.ingest import git as git_ingest
from rce.ingest import latex as latex_ingest
from rce.ingest import pyfig as pyfig_ingest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def unicode_repo(tmp_path: Path) -> Path:
    """A real git repo with: a Chinese-named directory holding a Chinese-
    named image, a Chinese-named .py with a savefig() call targeting that
    image, a Chinese-named .tex that \\includegraphics-es both that image
    and a second image whose name mixes a space with non-ASCII text, and
    core.quotepath explicitly set to true.
    """
    repo = tmp_path / "论文项目"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.quotepath", "true")

    (repo / "图表").mkdir()
    (repo / "图表" / "结果图.png").write_bytes(b"\x89PNG")
    (repo / "another space 图.png").write_bytes(b"\x89PNG")
    (repo / "2-信息熵_标签级.py").write_text(
        "import matplotlib.pyplot as plt\nplt.savefig('图表/结果图.png')\n"
    )
    (repo / "论文.tex").write_text(
        "\\section{Intro}\n"
        "\\includegraphics{图表/结果图.png}\n"
        "\\includegraphics{another space 图.png}\n"
    )
    _git(repo, "add", "-A")
    _git(
        repo, "-c", "user.name=T", "-c", "user.email=t@example.com",
        "commit", "-m", "中文提交信息: add sources",
    )
    return repo


def test_quotepath_is_explicitly_true_on_this_fixture(unicode_repo):
    """Sanity check on the fixture itself, not the fix -- guards against this
    whole test module silently passing for the wrong reason (a machine/CI
    image whose global gitconfig already disabled quotepath)."""
    result = subprocess.run(
        ["git", "-C", str(unicode_repo), "config", "--get", "core.quotepath"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "true"


def test_list_source_files_recognizes_non_ascii_and_space_combo(unicode_repo):
    inventory = git_ingest.list_source_files(unicode_repo)
    assert inventory["tex"] == ["论文.tex"]
    assert inventory["bib"] == []
    assert sorted(inventory["image"]) == sorted(
        ["图表/结果图.png", "another space 图.png"]
    )
    assert inventory["py"] == ["2-信息熵_标签级.py"]


def test_read_commits_preserves_real_non_ascii_filenames(unicode_repo):
    [commit] = git_ingest.read_commits(unicode_repo)
    assert set(commit.files) == {
        "图表/结果图.png", "another space 图.png", "2-信息熵_标签级.py", "论文.tex",
    }
    # never the octal-escaped, double-quoted string quotepath=true would
    # otherwise produce for a non-ASCII path.
    assert not any(f.startswith('"') for f in commit.files)


def test_full_ingest_pipeline_resolves_non_ascii_paths_end_to_end(unicode_repo, conn):
    inventory = git_ingest.list_source_files(unicode_repo)
    git_ingest.ingest_git_repo(conn, unicode_repo)
    latex_counts = latex_ingest.ingest_latex_repo(
        conn, unicode_repo, inventory["tex"], inventory["bib"],
        image_paths=inventory["image"],
    )
    pyfig_counts = pyfig_ingest.ingest_pyfig_repo(
        conn, unicode_repo, inventory["py"], inventory["image"],
    )

    # latex: the section is created and both \includegraphics targets
    # resolve -- the ghost-figure guard passes because image_paths now
    # actually contains the real, non-escaped paths. Proves "latex 的
    # section/includes 正常" on non-ASCII paths.
    assert latex_counts["sections"] == 1
    assert latex_counts["figures"] == 2

    fig_id = "figure:图表/结果图.png"
    fig_node = db.get_node(conn, fig_id)
    assert fig_node is not None
    assert fig_node["id"] == fig_id  # the real Chinese path, not an escaped/quoted string

    space_fig_id = "figure:another space 图.png"
    assert db.get_node(conn, space_fig_id) is not None

    includes_edges = db.query_edges(conn, dst=fig_id, type="includes")
    assert len(includes_edges) == 1
    assert includes_edges[0]["src"] == "section:论文.tex#intro"

    # pyfig: the generates edge's src commit is resolved via `git blame` on
    # the real file path (rce.ingest.git.blame_line) -- if blame had been
    # given an escaped/quoted path instead it would fail to resolve and
    # log+skip, and this edge would never exist. Its presence is the
    # end-to-end proof that blame works correctly on a non-ASCII path too.
    assert pyfig_counts["generates"] == 1
    generates_edges = db.query_edges(conn, dst=fig_id, type="generates")
    assert len(generates_edges) == 1
    src = generates_edges[0]["src"]
    assert src.startswith("commit:")
    assert len(src) == len("commit:") + 40  # a real 40-hex sha, not an escaped string
