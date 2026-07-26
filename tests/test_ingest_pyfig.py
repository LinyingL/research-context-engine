"""Tests for rce.ingest.pyfig (T6). parse_py_file needs only a directory +
relative path (no git), mirroring tests/test_ingest_latex.py. ingest_pyfig_repo
now resolves each edge's src via `git blame` (batch3-fix), so those tests use
real throwaway repos via subprocess `git`, following tests/test_ingest_git.py's
pattern.
"""

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from rce import db
from rce.ingest import git as git_ingest
from rce.ingest import pyfig


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit_all(repo: Path, message: str) -> str:
    """Stage everything and commit; returns the new commit's SHA."""
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", message)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_parse_py_file_finds_literal_calls_and_skips_fstring(tmp_path, caplog):
    (tmp_path / "plot.py").write_text(
        "import matplotlib.pyplot as plt\n"
        "i = 3\n"
        "plt.savefig('figs/plot.png')\n"
        "fig.savefig(f'figs/out_{i}.png')\n"  # f-string -- must be skipped
        "savefig('bare.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "plot.py")

    assert [(c.callee, c.literal, c.line) for c in calls] == [
        ("plt.savefig", "figs/plot.png", 3),
        ("savefig", "bare.png", 5),
    ]
    assert any("not guessing" in r.message for r in caplog.records)


def test_ingest_pyfig_repo_literal_fstring_missing_file_and_idempotent(tmp_path, caplog):
    # One file exercising every required case: a legit literal call
    # (resolves at repo root), a second legit call resolved only via the
    # script-directory fallback, an f-string call (must be skipped), and a
    # literal pointing at a file the repo doesn't actually have (must be
    # skipped, never guessed into a node).
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "figs").mkdir()
    (repo / "figs" / "plot.png").write_bytes(b"\x89PNG")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "local.png").write_bytes(b"\x89PNG")
    (repo / "scripts" / "gen.py").write_text(
        "import matplotlib.pyplot as plt\n"
        "name = str('plot')\n"  # RHS not a plain string literal -- not T9-foldable
        "plt.savefig('figs/plot.png')\n"  # resolves relative to repo root
        "plt.savefig('local.png')\n"  # only resolves relative to scripts/
        "plt.savefig(f'{name}.png')\n"  # f-string over a non-foldable name -- skip
        "plt.savefig('no_such_file.png')\n"  # not a tracked image -- skip
    )
    gen_sha = _commit_all(repo, "add gen.py and figures")
    known_images = ["figs/plot.png", "scripts/local.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        git_ingest.ingest_git_repo(conn, repo)  # creates the Commit node the FK needs

        for _ in range(2):  # second run proves idempotency
            with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
                counts = pyfig.ingest_pyfig_repo(conn, repo, ["scripts/gen.py"], known_images)
            assert counts == {"generates": 2}

        edges = db.query_edges(conn, src=f"commit:{gen_sha}", type="generates")
        assert {e["dst"] for e in edges} == {"figure:figs/plot.png", "figure:scripts/local.png"}
        root_edge = next(e for e in edges if e["dst"] == "figure:figs/plot.png")
        assert root_edge["extractor"] == "pyfig" and root_edge["confidence"] == 1.0
        assert root_edge["status"] == "auto"
        # (T10) db.upsert_edge now wraps evidence as {"occurrences": [...]};
        # re-running with identical evidence (idempotency) dedupes to one.
        assert root_edge["evidence"] == {
            "occurrences": [{"file": "scripts/gen.py", "line": 3, "callee": "plt.savefig"}]
        }
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE type='generates'").fetchone()[0] == 2

        assert db.get_node(conn, "figure:no_such_file.png") is None
        assert any("not guessing" in r.message for r in caplog.records)
        assert any(
            "does not resolve to a tracked repo image" in r.message and "no_such_file.png" in r.message
            for r in caplog.records
        )
    finally:
        conn.close()


def test_generates_edge_pinned_to_introducing_commit_not_head(tmp_path):
    """Regression test for batch3-fix: src used to be the repo's HEAD at
    ingestion time (a stable dst `figure:<path>` paired with a src that
    drifts every time HEAD moves), so ingest -> unrelated commit -> re-ingest
    produced TWO generates edges for the same unchanged figure/script. src
    must stay pinned to the commit that introduced the savefig(...) line, so
    a re-ingest after unrelated history never accumulates a second edge.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "figs").mkdir()
    (repo / "figs" / "plot.png").write_bytes(b"\x89PNG")
    (repo / "gen.py").write_text(
        "import matplotlib.pyplot as plt\nplt.savefig('figs/plot.png')\n"
    )
    gen_sha = _commit_all(repo, "add gen.py and figure")
    known_images = ["figs/plot.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        git_ingest.ingest_git_repo(conn, repo)
        first = pyfig.ingest_pyfig_repo(conn, repo, ["gen.py"], known_images)
        assert first == {"generates": 1}

        # An unrelated commit moves HEAD without touching gen.py at all.
        (repo / "README.md").write_text("unrelated change\n")
        _commit_all(repo, "unrelated commit")
        git_ingest.ingest_git_repo(conn, repo)  # picks up the new HEAD commit node
        second = pyfig.ingest_pyfig_repo(conn, repo, ["gen.py"], known_images)
        assert second == {"generates": 1}

        all_generates = db.query_edges(conn, type="generates")
        assert len(all_generates) == 1  # not 2 -- no stale edge left behind
        assert all_generates[0]["src"] == f"commit:{gen_sha}"
        assert all_generates[0]["dst"] == "figure:figs/plot.png"
    finally:
        conn.close()


def test_uncommitted_savefig_line_is_skipped_not_guessed(tmp_path, caplog):
    """A savefig(...) line with only a local, uncommitted edit has no real
    commit to attribute it to yet -- git blame reports the all-zero
    pseudo-sha for it, which must be skipped and logged, never guessed at.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "figs").mkdir()
    (repo / "figs" / "plot.png").write_bytes(b"\x89PNG")
    (repo / "figs" / "other.png").write_bytes(b"\x89PNG")
    (repo / "gen.py").write_text("import matplotlib.pyplot as plt\n")
    _commit_all(repo, "add empty gen.py and figures")
    # Append a new, never-committed savefig line.
    with (repo / "gen.py").open("a") as f:
        f.write("plt.savefig('figs/other.png')\n")
    known_images = ["figs/plot.png", "figs/other.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        git_ingest.ingest_git_repo(conn, repo)
        with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
            counts = pyfig.ingest_pyfig_repo(conn, repo, ["gen.py"], known_images)
        assert counts == {"generates": 0}
        assert db.query_edges(conn, type="generates") == []
        assert any("cannot be attributed to a commit" in r.message for r in caplog.records)
    finally:
        conn.close()


def test_unborn_repo_skips_entire_scan(tmp_path, caplog):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")  # no commits yet
    (repo / "plot.png").write_bytes(b"\x89PNG")
    (repo / "plot.py").write_text("plt.savefig('plot.png')\n")
    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
            counts = pyfig.ingest_pyfig_repo(conn, repo, ["plot.py"], ["plot.png"])
        assert counts == {"generates": 0}
        assert db.query_edges(conn, type="generates") == []
        assert any("unborn repo" in r.message for r in caplog.records)
    finally:
        conn.close()


# --- T9: same-file module constant folding -----------------------------------


def test_module_constant_fstring_folds_successfully(tmp_path):
    """T9: a module-level string constant inside an f-string folds to a real
    path; the generates edge's evidence records `folded_from`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "figs").mkdir()
    (repo / "figs" / "loss.png").write_bytes(b"\x89PNG")
    (repo / "gen.py").write_text(
        "import matplotlib.pyplot as plt\n"
        "SAVE_DIR = 'figs'\n"
        "plt.savefig(f'{SAVE_DIR}/loss.png')\n"
    )
    gen_sha = _commit_all(repo, "add gen.py with module constant f-string")
    known_images = ["figs/loss.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        git_ingest.ingest_git_repo(conn, repo)
        counts = pyfig.ingest_pyfig_repo(conn, repo, ["gen.py"], known_images)
        assert counts == {"generates": 1}

        edges = db.query_edges(conn, src=f"commit:{gen_sha}", type="generates")
        assert len(edges) == 1
        assert edges[0]["dst"] == "figure:figs/loss.png"
        # (T10) evidence lives under the single occurrence's dict now.
        assert edges[0]["evidence"]["occurrences"][0]["folded_from"] == "f'{SAVE_DIR}/loss.png'"
    finally:
        conn.close()


def test_parse_py_file_skips_fstring_with_loop_variable(tmp_path, caplog):
    """A for-loop-bound name is not a top-level Assign target -- never
    folded; the f-string call site is skipped and logged as pre-T9."""
    (tmp_path / "gen.py").write_text(
        "for i in range(1):\n"
        "    plt.savefig(f'figs/{i}.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


def test_parse_py_file_skips_reassigned_module_constant(tmp_path, caplog):
    """A name assigned twice at module level is ambiguous and must not
    fold (HANDOFF-SPEC.md T9: "重复赋值的名字不折叠")."""
    (tmp_path / "gen.py").write_text(
        "SAVE_DIR = 'figs'\n"
        "SAVE_DIR = 'figs2'\n"
        "plt.savefig(f'{SAVE_DIR}/loss.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


def test_parse_py_file_folds_os_path_join_mixed_forms(tmp_path):
    """os.path.join(...) folds when every argument is either a plain
    string literal or a foldable module-level constant (T9)."""
    (tmp_path / "gen.py").write_text(
        "import os\n"
        "SAVE_DIR = 'out'\n"
        "plt.savefig(os.path.join(SAVE_DIR, 'loss.png'))\n"
    )
    calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert [(c.literal, c.folded_from) for c in calls] == [
        ("out/loss.png", "os.path.join(SAVE_DIR, 'loss.png')"),
    ]


# -- T-blocker fix: touch-count scan must cover the whole file, not just
# tree.body, and must respect function scope -- otherwise a name that is
# conditionally, nestedly, or locally reassigned still folds to its stale
# top-level literal. Each case below gives the name a real top-level
# `NAME = "..."` assignment (so pre-fix's tree.body-only scan saw exactly
# one touch and folded it) plus a second, differently-shaped touch that
# pre-fix code couldn't see -- reproducing the report's over-eager fold.


def test_parse_py_file_skips_conditional_if_and_module_level_for_reassignment(tmp_path, caplog):
    (tmp_path / "gen.py").write_text(
        "import argparse\n"
        "OUT = 'figures'\n"
        "args = argparse.Namespace(out=None)\n"
        "if args.out:\n"
        "    OUT = args.out\n"
        "plt.savefig(OUT + '/f1.png')\n"
        "\n"
        "D = 'other_figures'\n"
        "for D in ['a', 'b']:\n"
        "    pass\n"
        "plt.savefig(D + '/f2.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert sum("not guessing" in r.message for r in caplog.records) == 2


def test_parse_py_file_skips_try_except_with_and_walrus_reassignment(tmp_path, caplog):
    (tmp_path / "gen.py").write_text(
        "import os\n"
        "TDIR = 'stale_figs'\n"
        "try:\n"
        "    TDIR = os.environ['FIGDIR']\n"
        "except Exception:\n"
        "    pass\n"
        "plt.savefig(TDIR + '/f1.png')\n"
        "\n"
        "WDIR = 'stale_figs'\n"
        "with open('x.txt') as WDIR:\n"
        "    pass\n"
        "plt.savefig(WDIR + '/f2.png')\n"
        "\n"
        "EDIR = 'stale_figs'\n"
        "try:\n"
        "    pass\n"
        "except Exception as EDIR:\n"
        "    pass\n"
        "plt.savefig(EDIR + '/f3.png')\n"
        "\n"
        "WALRUS = 'stale_figs'\n"
        "if (WALRUS := 'other'):\n"
        "    pass\n"
        "plt.savefig(WALRUS + '/f4.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert sum("not guessing" in r.message for r in caplog.records) == 4


def test_parse_py_file_skips_name_shadowed_inside_function_scope(tmp_path, caplog):
    """A module-level constant whose name is reused as a function parameter,
    a function-local variable, or a `global`-declared name inside a
    function must never be folded into a savefig() call in that function --
    the call site actually receives that function's own local/global-
    written value at runtime, not the module constant (T-blocker fix: the
    old scan had zero function-scope awareness)."""
    (tmp_path / "gen.py").write_text(
        "D = 'module_figs'\n"
        "\n"
        "def plot_param(D):\n"
        "    plt.savefig(D + '/x.png')\n"
        "\n"
        "def plot_local():\n"
        "    D = 'local_figs'\n"
        "    plt.savefig(D + '/y.png')\n"
        "\n"
        "def plot_global():\n"
        "    global D\n"
        "    D = 'overridden'\n"
        "    plt.savefig(D + '/z.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert sum("not guessing" in r.message for r in caplog.records) == 3


def test_parse_py_file_skips_name_touched_by_delete_after_assignment(tmp_path, caplog):
    (tmp_path / "gen.py").write_text(
        "D = 'module_figs'\n"
        "del D\n"
        "plt.savefig(D + '/after_del.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


def test_parse_py_file_still_skips_imported_name_never_folds(tmp_path, caplog):
    """Guard test (no reassignment involved): an imported name was already
    correctly excluded pre-fix (an import produces no Assign node at all) --
    confirm the whole-file touch-count rewrite didn't regress this."""
    (tmp_path / "gen.py").write_text(
        "from config import OUT\n"
        "plt.savefig(OUT + '/f1.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


def test_parse_py_file_still_skips_pathlib_slash_operator(tmp_path, caplog):
    """Guard test: pathlib's `/` path-join operator is deliberately excluded
    from T9 folding (dispatches on the left operand's runtime type -- see
    module docstring); confirm the touch-count rewrite didn't accidentally
    start folding it."""
    (tmp_path / "gen.py").write_text(
        "from pathlib import Path\n"
        "SAVE_DIR = Path('figs')\n"
        "plt.savefig(SAVE_DIR / 'loss.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


# -- Opus re-review blocker fix: three more rebinding forms
# `_count_all_name_bindings` never counted at all -- Lambda parameters,
# `match` capture patterns, and PEP 695 `type` aliases -- each letting a
# reused name still fold to its stale module-level literal. Confirmed via
# `git show HEAD` against the pre-fix module (see completion report) that
# every case below mis-folded to `'figures/f1.png'` before this patch.


def test_parse_py_file_skips_lambda_parameter_shadowing_module_constant(tmp_path, caplog):
    """A lambda parameter reuses a module constant's name and shadows it
    inside the lambda body at runtime, exactly like a `def`'s parameter --
    must not fold to the stale top-level literal."""
    (tmp_path / "gen.py").write_text(
        "OUT = 'figures'\n"
        "save = lambda OUT: plt.savefig(OUT + '/f1.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


def test_parse_py_file_skips_match_case_capture_shadowing_module_constant(tmp_path, caplog):
    """A `match` `case` capture pattern (`case ['save', OUT]:`) rebinds the
    name for the rest of the match body, shadowing a same-named module
    constant -- must not fold."""
    (tmp_path / "gen.py").write_text(
        "OUT = 'figures'\n"
        "cmd = ['save', 'runtime_dir']\n"
        "match cmd:\n"
        "    case ['save', OUT]:\n"
        "        plt.savefig(OUT + '/f1.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="PEP 695 `type` alias statement requires Python 3.12+",
)
def test_parse_py_file_skips_type_alias_shadowing_module_constant(tmp_path, caplog):
    """A PEP 695 `type OUT = ...` alias rebinds the name at module level, so
    it must count as a second touch and block the fold. Skipped (not
    failed) below Python 3.12, where this syntax does not parse at all."""
    (tmp_path / "gen.py").write_text(
        "OUT = 'figures'\n"
        "type OUT = str\n"
        "plt.savefig(OUT + '/f1.png')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


# --- T12: exhaustive name-binding-form table (ends the whack-a-mole) -------
# Three consecutive review passes each caught a different Python name-
# binding grammar form `_count_all_name_bindings` didn't yet count (Lambda
# parameters, `match` captures, PEP 695 `type` aliases -- see the tests
# above -- and, this round, PEP 695 type parameters -- see the pyfig.py fix
# alongside this table). Patching one shape per review doesn't converge, so
# this table enumerates every Python name-binding form the authors are
# aware of and asserts none of them ever lets `OUT` fold into the trailing
# savefig(...) call. The next missed form gets caught by construction, not
# by a fourth review pass.
#
# None of these sources are executed, only ast.parse'd, so a source only
# needs to be syntactically valid, not runtime-correct -- e.g.
# `def f(**OUT): plt.savefig(OUT + '/f1.png')` is nonsense if run, but is
# exactly the AST shape under test (does `**OUT`'s binding get counted?).
#
# Run against the code before this change's pyfig.py fix, this table failed
# on exactly 5 of the PEP 695 rows below -- type_param_typevar,
# type_param_paramspec, type_param_typevartuple, class_type_param, and
# type_alias_type_param (type_alias_name alone was already handled) -- all
# five fold clean after the fix (see completion report for the raw before/
# after run).
_SAVE = "plt.savefig(OUT + '/f1.png')"

_NAME_BINDING_FORMS: list[tuple[str, str]] = [
    ("plain_reassignment", f"OUT = 'figures'\nOUT = 'figures2'\n{_SAVE}\n"),
    ("aug_assign", f"OUT = 'figures'\nOUT += '_sub'\n{_SAVE}\n"),
    ("annotated_assign", f"OUT = 'figures'\nOUT: str = 'other'\n{_SAVE}\n"),
    ("tuple_unpack", f"OUT = 'figures'\nOUT, other = 'a', 'b'\n{_SAVE}\n"),
    ("list_unpack", f"OUT = 'figures'\n[OUT, other] = ['a', 'b']\n{_SAVE}\n"),
    ("starred_unpack", f"OUT = 'figures'\n*rest, OUT = ['a', 'b']\n{_SAVE}\n"),
    ("chained_assign", f"OUT = 'figures'\nother = OUT = 'stale'\n{_SAVE}\n"),
    ("walrus", f"OUT = 'figures'\n_ = (OUT := 'other')\n{_SAVE}\n"),
    ("for_target", f"OUT = 'figures'\nfor OUT in ['a']:\n    pass\n{_SAVE}\n"),
    ("async_for_target",
     f"OUT = 'figures'\n\nasync def f():\n    async for OUT in agen():\n        pass\n    {_SAVE}\n"),
    ("with_as", f"OUT = 'figures'\nwith open('x') as OUT:\n    pass\n{_SAVE}\n"),
    ("async_with_as",
     f"OUT = 'figures'\n\nasync def f():\n    async with actx() as OUT:\n        pass\n    {_SAVE}\n"),
    ("except_as", f"OUT = 'figures'\ntry:\n    pass\nexcept Exception as OUT:\n    pass\n{_SAVE}\n"),
    ("listcomp_target", f"OUT = 'figures'\n_ = [x for OUT in range(3)]\n{_SAVE}\n"),
    ("setcomp_target", f"OUT = 'figures'\n_ = {{x for OUT in range(3)}}\n{_SAVE}\n"),
    ("dictcomp_target", f"OUT = 'figures'\n_ = {{OUT: 1 for OUT in range(3)}}\n{_SAVE}\n"),
    ("genexp_target", f"OUT = 'figures'\n_ = (x for OUT in range(3))\n{_SAVE}\n"),
    ("nested_comp_target", f"OUT = 'figures'\n_ = [y for x in range(3) for OUT in range(3)]\n{_SAVE}\n"),
    ("async_comp_target",
     f"OUT = 'figures'\n\nasync def f():\n    _ = [x async for OUT in agen()]\n    {_SAVE}\n"),
    ("funcdef_name", f"OUT = 'figures'\ndef OUT():\n    pass\n{_SAVE}\n"),
    ("asyncfuncdef_name", f"OUT = 'figures'\nasync def OUT():\n    pass\n{_SAVE}\n"),
    ("classdef_name", f"OUT = 'figures'\nclass OUT:\n    pass\n{_SAVE}\n"),
    ("param_posonly", f"OUT = 'figures'\ndef f(OUT, /):\n    {_SAVE}\n"),
    ("param_normal", f"OUT = 'figures'\ndef f(OUT):\n    {_SAVE}\n"),
    ("param_normal_default", f"OUT = 'figures'\ndef f(OUT='d'):\n    {_SAVE}\n"),
    ("param_kwonly", f"OUT = 'figures'\ndef f(*, OUT):\n    {_SAVE}\n"),
    ("param_kwonly_default", f"OUT = 'figures'\ndef f(*, OUT='d'):\n    {_SAVE}\n"),
    ("param_vararg", f"OUT = 'figures'\ndef f(*OUT):\n    {_SAVE}\n"),
    ("param_kwarg", f"OUT = 'figures'\ndef f(**OUT):\n    {_SAVE}\n"),
    ("lambda_posonly", f"OUT = 'figures'\nsave = lambda OUT, /: OUT\n{_SAVE}\n"),
    ("lambda_normal", f"OUT = 'figures'\nsave = lambda OUT: OUT\n{_SAVE}\n"),
    ("lambda_normal_default", f"OUT = 'figures'\nsave = lambda OUT='d': OUT\n{_SAVE}\n"),
    ("lambda_kwonly", f"OUT = 'figures'\nsave = lambda *, OUT: OUT\n{_SAVE}\n"),
    ("lambda_kwonly_default", f"OUT = 'figures'\nsave = lambda *, OUT='d': OUT\n{_SAVE}\n"),
    ("lambda_vararg", f"OUT = 'figures'\nsave = lambda *OUT: OUT\n{_SAVE}\n"),
    ("lambda_kwarg", f"OUT = 'figures'\nsave = lambda **OUT: OUT\n{_SAVE}\n"),
    ("global_decl", f"OUT = 'figures'\ndef f():\n    global OUT\n    OUT = 'other'\n    {_SAVE}\n"),
    ("nonlocal_decl",
     f"OUT = 'figures'\ndef outer():\n    OUT = 'mid'\n    def inner():\n"
     f"        nonlocal OUT\n        OUT = 'other'\n        {_SAVE}\n"),
    ("import_stmt", f"OUT = 'figures'\nimport os as OUT\n{_SAVE}\n"),
    ("from_import_stmt", f"OUT = 'figures'\nfrom os import path as OUT\n{_SAVE}\n"),
    ("del_stmt", f"OUT = 'figures'\ndel OUT\n{_SAVE}\n"),
    ("match_as", f"OUT = 'figures'\nmatch [1]:\n    case [OUT]:\n        {_SAVE}\n"),
    ("match_star", f"OUT = 'figures'\nmatch [1, 2]:\n    case [*OUT]:\n        {_SAVE}\n"),
    ("match_mapping_rest", f"OUT = 'figures'\nmatch {{'a': 1}}:\n    case {{**OUT}}:\n        {_SAVE}\n"),
]

_PEP695_FORMS: list[tuple[str, str]] = [
    ("type_param_typevar", f"OUT = 'figures'\ndef plot[OUT](x):\n    {_SAVE}\n"),
    ("type_param_paramspec", f"OUT = 'figures'\ndef plot[**OUT](x):\n    {_SAVE}\n"),
    ("type_param_typevartuple", f"OUT = 'figures'\ndef plot[*OUT](x):\n    {_SAVE}\n"),
    ("class_type_param", f"OUT = 'figures'\nclass P[OUT]:\n    pass\n{_SAVE}\n"),
    ("type_alias_name", f"OUT = 'figures'\ntype OUT = str\n{_SAVE}\n"),
    ("type_alias_type_param", f"OUT = 'figures'\ntype Alias[OUT] = list\n{_SAVE}\n"),
]

# Positive controls: a name that is genuinely foldable, or whose only extra
# appearance is a *read* (never a binding target), must still fold --
# otherwise the fixes above could over-correct into "nothing ever folds",
# which is a functional regression, not a safe default.
_POSITIVE_CONTROLS: list[tuple[str, str]] = [
    ("plain_single_assign", f"OUT = 'figures'\n{_SAVE}\n"),
    ("os_path_join", "import os\nOUT = 'figures'\nplt.savefig(os.path.join(OUT, 'f1.png'))\n"),
    ("decorator_reads_not_binds",
     f"OUT = 'figures'\n\n@OUT\ndef f():\n    {_SAVE}\n"),
    # The shape every edge in the real testbed uses -- guards against a change
    # that silently breaks f-string folding while the negative table stays green.
    ("fstring_interpolation", "OUT = 'figures'\nplt.savefig(f'{OUT}/f1.png')\n"),
]


def test_star_import_disables_folding_for_the_whole_file(tmp_path, caplog):
    """`from x import *` can bind any name, so no touch count is trustworthy;
    the whole file gives up folding rather than guessing (HANDOFF-SPEC section 5)."""
    (tmp_path / "gen.py").write_text(
        f"OUT = 'figures'\nfrom cfg import *\n{_SAVE}\n"
    )
    with caplog.at_level(logging.WARNING):
        calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == [], "star import must disable folding, not fold a possibly-rebound name"
    assert any("star import" in r.message for r in caplog.records)


def test_star_import_before_the_constant_also_disables_folding(tmp_path):
    """Order does not matter: the import may rebind the name after assignment
    at runtime regardless of where it appears."""
    (tmp_path / "gen.py").write_text(
        f"from cfg import *\nOUT = 'figures'\n{_SAVE}\n"
    )
    assert pyfig.parse_py_file(tmp_path, "gen.py") == []


def test_plain_from_import_still_allows_unrelated_constants_to_fold(tmp_path):
    """Only star imports are blanket-disabling -- a named import binds a
    knowable name and must not disable folding of other constants."""
    (tmp_path / "gen.py").write_text(
        f"from cfg import something\nOUT = 'figures'\n{_SAVE}\n"
    )
    assert pyfig.parse_py_file(tmp_path, "gen.py") != []


@pytest.mark.parametrize("name,source", _NAME_BINDING_FORMS, ids=[f[0] for f in _NAME_BINDING_FORMS])
def test_name_binding_form_never_folds(tmp_path, name, source):
    (tmp_path / "gen.py").write_text(source)
    calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == [], f"{name} unexpectedly folded: {calls}"


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 syntax requires Python 3.12+")
@pytest.mark.parametrize("name,source", _PEP695_FORMS, ids=[f[0] for f in _PEP695_FORMS])
def test_pep695_binding_form_never_folds(tmp_path, name, source):
    (tmp_path / "gen.py").write_text(source)
    calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls == [], f"{name} unexpectedly folded: {calls}"


@pytest.mark.parametrize("name,source", _POSITIVE_CONTROLS, ids=[f[0] for f in _POSITIVE_CONTROLS])
def test_positive_control_still_folds(tmp_path, name, source):
    (tmp_path / "gen.py").write_text(source)
    calls = pyfig.parse_py_file(tmp_path, "gen.py")
    assert calls != [], f"{name} expected to fold but did not"


def test_folded_path_not_a_tracked_image_is_still_caught(tmp_path, caplog):
    """A folded path must still pass the tracked-image verification --
    folding is not a free pass around the ghost-figure guard."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "figs").mkdir()
    (repo / "figs" / "other.png").write_bytes(b"\x89PNG")  # exists, but not the folded target
    (repo / "gen.py").write_text(
        "import matplotlib.pyplot as plt\n"
        "SAVE_DIR = 'figs'\n"
        "plt.savefig(f'{SAVE_DIR}/missing.png')\n"
    )
    _commit_all(repo, "add gen.py with folded path to a missing file")
    known_images = ["figs/other.png"]

    conn = db.connect(":memory:")
    db.migrate(conn)
    try:
        git_ingest.ingest_git_repo(conn, repo)
        with caplog.at_level("WARNING", logger="rce.ingest.pyfig"):
            counts = pyfig.ingest_pyfig_repo(conn, repo, ["gen.py"], known_images)
        assert counts == {"generates": 0}
        assert db.query_edges(conn, type="generates") == []
        assert any(
            "does not resolve to a tracked repo image" in r.message and "missing.png" in r.message
            for r in caplog.records
        )
    finally:
        conn.close()
