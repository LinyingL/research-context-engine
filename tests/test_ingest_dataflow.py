"""Tests for rce.ingest.dataflow (task W2, data lineage). Needs no git repo
at all -- unlike rce.ingest.pyfig (whose generates edge's src is resolved via
git blame), a reads/writes edge's src is the script file itself, so these
tests build a plain fake repo tree under tmp_path, mirroring
tests/test_ingest_latex.py's style.
"""

from rce import db
from rce.ingest import dataflow


# ---------------------------------------------------------------------------
# Python: parse_py_file
# ---------------------------------------------------------------------------


def test_parse_py_file_recognizes_bare_and_attribute_call_forms(tmp_path):
    (tmp_path / "gen.py").write_text(
        "import pandas as pd\n"
        "df = pd.read_csv('data/a.csv')\n"  # attribute form
        "df2 = read_csv('data/b.csv')\n"  # bare form
        "df.to_csv('data/out.csv')\n"  # attribute write form
    )
    calls = dataflow.parse_py_file(tmp_path, "gen.py")
    assert [(c.callee, c.kind, c.literal, c.line) for c in calls] == [
        ("pd.read_csv", "read", "data/a.csv", 2),
        ("read_csv", "read", "data/b.csv", 3),
        ("df.to_csv", "write", "data/out.csv", 4),
    ]


def test_parse_py_file_folds_module_constant_and_skips_fstring(tmp_path, caplog):
    (tmp_path / "gen.py").write_text(
        "import pandas as pd\n"
        "RAW = 'data/raw.csv'\n"
        "i = 3\n"
        "df = pd.read_csv(RAW)\n"  # T9 constant fold
        "df2 = pd.read_csv(f'data/{i}.csv')\n"  # f-string over non-foldable -- skip
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_py_file(tmp_path, "gen.py")
    assert len(calls) == 1
    assert calls[0].literal == "data/raw.csv"
    assert calls[0].folded_from == "RAW"
    assert any("not guessing" in r.message for r in caplog.records)


def test_parse_py_file_open_default_and_w_mode_classified_other_modes_skipped(tmp_path, caplog):
    (tmp_path / "io.py").write_text(
        "f1 = open('data/in.csv')\n"  # default mode -- read
        "f2 = open('data/in2.csv', 'r')\n"  # explicit 'r' -- read
        "f3 = open('data/out.csv', 'w')\n"  # 'w' -- write
        "f4 = open('data/app.csv', 'a')\n"  # unsupported mode -- skip
        "f5 = open('data/bin.csv', mode='rb')\n"  # unsupported mode (keyword) -- skip
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_py_file(tmp_path, "io.py")
    assert [(c.kind, c.literal) for c in calls] == [
        ("read", "data/in.csv"),
        ("read", "data/in2.csv"),
        ("write", "data/out.csv"),
    ]
    skip_msgs = [r.message for r in caplog.records if "mode is neither" in r.message]
    assert len(skip_msgs) == 2


def test_parse_py_file_variable_argument_skipped_and_logged(tmp_path, caplog):
    (tmp_path / "gen.py").write_text(
        "import pandas as pd\n"
        "path = compute_path()\n"
        "df = pd.read_csv(path)\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("not a string literal" in r.message for r in caplog.records)


def test_parse_py_file_resolves_path_via_keyword_when_no_positional_arg(tmp_path):
    (tmp_path / "gen.py").write_text(
        "import pandas as pd\n"
        "df = pd.read_csv(filepath='data/kw.csv')\n"
        "df.to_excel(fname='data/kw_out.xlsx')\n"
    )
    calls = dataflow.parse_py_file(tmp_path, "gen.py")
    assert [(c.kind, c.literal) for c in calls] == [
        ("read", "data/kw.csv"),
        ("write", "data/kw_out.xlsx"),
    ]


def test_parse_py_file_unparseable_source_skipped_not_fatal(tmp_path, caplog):
    (tmp_path / "broken.py").write_text("def(:\n")
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_py_file(tmp_path, "broken.py")
    assert calls == []
    assert any("cannot parse as Python" in r.message for r in caplog.records)


def test_parse_py_file_module_constant_chain_folds_in_program_order(tmp_path):
    """`pyconst.collect_module_string_constants` folds a name whose own RHS
    is itself a foldable expression (not only a bare literal), resolved in
    top-to-bottom program order -- `BASE` -> `DATA` -> `RAW`, each using only
    names already resolved by the time its own statement is reached, exactly
    mirroring real Python execution order. This is the piece that makes the
    real project's `BASE = ".../root/"; DATA = BASE + "sub/Data/"` chain (see
    the default-parameter tests below) resolve at all -- without it, `DATA`
    is never in `module_constants` and every call downstream of it silently
    fails to fold."""
    (tmp_path / "gen.py").write_text(
        "import pandas as pd\n"
        "BASE = 'root/'\n"
        "DATA = BASE + 'data/'\n"
        "RAW = DATA + '_raw/'\n"
        "df = pd.read_csv(RAW)\n"
    )
    calls = dataflow.parse_py_file(tmp_path, "gen.py")
    assert [(c.literal, c.folded_from) for c in calls] == [
        ("root/data/_raw/", "RAW"),
    ]


# ---------------------------------------------------------------------------
# Python: default-parameter folding
# ---------------------------------------------------------------------------


def test_parse_py_file_default_param_folds_when_every_call_omits_it(tmp_path):
    """Success case, modeled byte-for-byte on the shape of the user's real
    .../复现包_分步/1-下载与整理数据.py: `BASE = "..."; DATA = BASE + "...";
    def build_foreign_flows(out=DATA + "foreign_flows_ccdc.csv"): ...
    open(out, "w")`, called elsewhere in the same file as a bare
    `build_foreign_flows()`. `out` is a parameter, not a module constant, so
    T9 alone never resolves it -- this is exactly the class of call this
    feature exists for."""
    (tmp_path / "gen.py").write_text(
        "BASE = 'root/'\n"
        "DATA = BASE + 'data/'\n"
        "\n"
        "def build_foreign_flows(out=DATA + 'foreign_flows_ccdc.csv'):\n"
        "    with open(out, 'w') as f:\n"
        "        f.write('x')\n"
        "\n"
        "build_foreign_flows()\n"
    )
    calls = dataflow.parse_py_file(tmp_path, "gen.py")
    assert [(c.kind, c.literal, c.folded_from) for c in calls] == [
        ("write", "root/data/foreign_flows_ccdc.csv", "out"),
    ]


def test_parse_py_file_default_param_reassigned_in_body_skips(tmp_path, caplog):
    """Ambiguity 1: `out` is reassigned inside `build_foreign_flows`'s own
    body, so the default value no longer necessarily describes what `out`
    holds by the time `open(out, ...)` runs -- skip, not guess."""
    (tmp_path / "gen.py").write_text(
        "DATA = 'data/'\n"
        "\n"
        "def build_foreign_flows(out=DATA + 'foreign_flows_ccdc.csv'):\n"
        "    out = out + '.tmp'\n"
        "    open(out, 'w')\n"
        "\n"
        "build_foreign_flows()\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("reassigned inside the function body" in r.message for r in caplog.records)


def test_parse_py_file_default_param_provided_by_call_site_skips(tmp_path, caplog):
    """Ambiguity 2: the one call site in this file supplies `out` explicitly,
    so the default value was never actually used at that call -- folding it
    in anyway would misattribute a path the call never asked for."""
    (tmp_path / "gen.py").write_text(
        "DATA = 'data/'\n"
        "\n"
        "def build_foreign_flows(out=DATA + 'foreign_flows_ccdc.csv'):\n"
        "    open(out, 'w')\n"
        "\n"
        "build_foreign_flows(out='custom.csv')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("do not consistently omit" in r.message for r in caplog.records)


def test_parse_py_file_default_param_calls_disagree_skips(tmp_path, caplog):
    """Ambiguity 3: two call sites in this file disagree -- one omits `out`,
    one provides it -- so there is no single answer for "what does the
    default stand in for", not even a majority-rules guess."""
    (tmp_path / "gen.py").write_text(
        "DATA = 'data/'\n"
        "\n"
        "def build_foreign_flows(out=DATA + 'foreign_flows_ccdc.csv'):\n"
        "    open(out, 'w')\n"
        "\n"
        "build_foreign_flows()\n"
        "build_foreign_flows(out='custom.csv')\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_py_file(tmp_path, "gen.py")
    assert calls == []
    assert any("do not consistently omit" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# R: parse_r_file
# ---------------------------------------------------------------------------


def test_parse_r_file_read_and_write_names_with_correct_path_argument_index(tmp_path):
    (tmp_path / "analysis.R").write_text(
        "d <- read.csv('data/in.csv')\n"  # path is 1st positional
        "write.csv(d, file = 'data/out.csv')\n"  # path is 'file' keyword
        "saveRDS(d, 'data/out.rds')\n"  # path is 2nd positional (object comes first)
        "ggsave('figs/plot.png', plot = p)\n"  # path is 1st positional
        "pdf('figs/report.pdf')\n"
    )
    calls = dataflow.parse_r_file(tmp_path, "analysis.R")
    assert [(c.callee, c.kind, c.literal) for c in calls] == [
        ("read.csv", "read", "data/in.csv"),
        ("write.csv", "write", "data/out.csv"),
        ("saveRDS", "write", "data/out.rds"),
        ("ggsave", "write", "figs/plot.png"),
        ("pdf", "write", "figs/report.pdf"),
    ]


def test_parse_r_file_haven_prefixed_read_dta(tmp_path):
    (tmp_path / "analysis.R").write_text('d <- haven::read_dta("data/in.dta")\n')
    calls = dataflow.parse_r_file(tmp_path, "analysis.R")
    assert [(c.callee, c.kind, c.literal) for c in calls] == [
        ("haven::read_dta", "read", "data/in.dta"),
    ]


def test_parse_r_file_file_path_of_literals_folds_but_variable_still_skipped(tmp_path, caplog):
    """`file.path(...)` of plain literals is now a supported call-argument
    fold shape (R constant folding, this module's T9 counterpart) -- a real
    behavior change from the pre-folding version of this test, which
    expected file.path(...) to be an unconditional skip. A genuine variable
    (`p <- get_path()`, not a literal) is still not foldable and still
    skipped."""
    (tmp_path / "analysis.R").write_text(
        "write.csv(d, file.path('data', 'out.csv'))\n"  # both args literal -- folds
        "p <- get_path()\n"
        "read.csv(p)\n"  # p's own RHS isn't a literal -- still skip
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_r_file(tmp_path, "analysis.R")
    assert [(c.kind, c.literal, c.folded_from) for c in calls] == [
        ("write", "data/out.csv", "file.path('data', 'out.csv')"),
    ]
    assert sum("not guessing" in r.message for r in caplog.records) == 1


def test_parse_r_file_ignores_commented_out_call(tmp_path):
    (tmp_path / "analysis.R").write_text(
        "# read.csv('should_not_count.csv')\n"
        "d <- read.csv('data/real.csv')  # trailing comment\n"
    )
    calls = dataflow.parse_r_file(tmp_path, "analysis.R")
    assert [(c.literal, c.line) for c in calls] == [("data/real.csv", 2)]


def test_parse_r_file_supports_multiline_call(tmp_path):
    (tmp_path / "analysis.R").write_text(
        "write.csv(d, file = 'data/out.csv',\n"
        "          row.names = FALSE)\n"
    )
    calls = dataflow.parse_r_file(tmp_path, "analysis.R")
    assert [(c.kind, c.literal) for c in calls] == [("write", "data/out.csv")]


def test_parse_r_file_second_binding_disqualifies_fold(tmp_path, caplog):
    """A name bound twice (`<-` twice anywhere in the file) is never folded,
    mirroring pyconst's identical rule for Python module constants -- `base`'s
    second binding makes the paste0(base, ...) call site fall back to
    skip+log rather than silently picking either binding."""
    (tmp_path / "analysis.R").write_text(
        'base <- "root/"\n'
        'base <- "other/"\n'
        'df <- read.csv(paste0(base, "x.csv"))\n'
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_r_file(tmp_path, "analysis.R")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


def test_parse_r_file_paste0_folds_multiple_bound_names(tmp_path):
    """paste0(...) folding a chain of two separately-bound names used as
    different arguments in the same call -- not only a single name plus
    literal segments."""
    (tmp_path / "analysis.R").write_text(
        'dir <- "data"\n'
        'sub <- "raw"\n'
        'df <- read.csv(paste0(dir, "/", sub, "/in.csv"))\n'
    )
    calls = dataflow.parse_r_file(tmp_path, "analysis.R")
    assert [(c.literal, c.folded_from) for c in calls] == [
        ("data/raw/in.csv", 'paste0(dir, "/", sub, "/in.csv")'),
    ]


# ---------------------------------------------------------------------------
# R Markdown: parse_rmd_file
# ---------------------------------------------------------------------------


def test_parse_rmd_file_only_scans_r_code_chunks(tmp_path):
    (tmp_path / "report.Rmd").write_text(
        "---\ntitle: x\n---\n"
        "\n"
        "Prose mentioning read.csv(\"should_not_count.csv\") is not code.\n"
        "\n"
        "```{r setup}\n"
        "x <- read.csv(\"data/rmd_in.csv\")\n"
        "```\n"
        "\n"
        "```{python}\n"
        "import pandas as pd\n"
        "pd.read_csv(\"should_not_count_python.csv\")\n"
        "```\n"
        "\n"
        "```{r}\n"
        "write.csv(x, file = \"data/rmd_out.csv\")\n"
        "```\n"
    )
    calls = dataflow.parse_rmd_file(tmp_path, "report.Rmd")
    assert [(c.kind, c.literal, c.line) for c in calls] == [
        ("read", "data/rmd_in.csv", 8),
        ("write", "data/rmd_out.csv", 17),
    ]


def test_parse_rmd_file_constant_bound_in_one_chunk_folds_in_another(tmp_path):
    """A name's binding-count scan spans every R chunk concatenated (module
    docstring, "R constant folding"): bound in the setup chunk, used two
    chunks later, still folds as one document-wide scope rather than being
    treated as unbound in the chunk that reads it."""
    (tmp_path / "report.Rmd").write_text(
        "---\ntitle: x\n---\n"
        "\n"
        "```{r setup}\n"
        'base <- "data/"\n'
        "```\n"
        "\n"
        "```{r}\n"
        'df <- read.csv(paste0(base, "in.csv"))\n'
        "```\n"
    )
    calls = dataflow.parse_rmd_file(tmp_path, "report.Rmd")
    assert [(c.literal, c.folded_from) for c in calls] == [
        ("data/in.csv", 'paste0(base, "in.csv")'),
    ]


def test_parse_rmd_file_rebinding_in_later_chunk_disqualifies_fold(tmp_path, caplog):
    """The same cross-chunk scope cuts both ways: a name rebound in a later
    chunk is touched twice document-wide even though each individual chunk
    only binds it once, so folding is correctly refused."""
    (tmp_path / "report.Rmd").write_text(
        "---\ntitle: x\n---\n"
        "\n"
        "```{r setup}\n"
        'base <- "data/"\n'
        "```\n"
        "\n"
        "```{r}\n"
        'base <- "other/"\n'
        'df <- read.csv(paste0(base, "in.csv"))\n'
        "```\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_rmd_file(tmp_path, "report.Rmd")
    assert calls == []
    assert any("not guessing" in r.message for r in caplog.records)


def test_parse_rmd_file_real_13_sample_folds_paste0_base(tmp_path):
    """Sampled verbatim (setup chunk) from the user's real project,
    .../复现包_分步/13-地缘风险_价格与数量.Rmd: a `base <- "..."` binding
    immediately followed by `read.csv(paste0(base, ...))`. This exact shape
    -- a same-file name folded into paste0 -- was previously an unconditional
    skip (no name-folding inside paste0 at all) and accounted for roughly 50
    of the project's ~101 real skipped calls."""
    (tmp_path / "13-real.Rmd").write_text(
        "---\ntitle: x\n---\n"
        "\n"
        "```{r setup, include=FALSE}\n"
        'base <- "/Users/lilinying/Documents/默认安全锚_论文流水线/"\n'
        'df <- read.csv(paste0(base, "复现包_分步/Data/panel_pricing.csv"))\n'
        "```\n"
    )
    calls = dataflow.parse_rmd_file(tmp_path, "13-real.Rmd")
    assert [(c.kind, c.literal, c.folded_from) for c in calls] == [
        (
            "read",
            "/Users/lilinying/Documents/默认安全锚_论文流水线/复现包_分步/Data/panel_pricing.csv",
            'paste0(base, "复现包_分步/Data/panel_pricing.csv")',
        ),
    ]


# ---------------------------------------------------------------------------
# ingest_dataflow_repo: node/edge creation, missing flag, idempotency
# ---------------------------------------------------------------------------


def test_ingest_writes_reads_and_writes_edges_dataset_vs_figure_and_is_idempotent(tmp_path, conn):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "raw.csv").write_text("a,b\n1,2\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "gen.py").write_text(
        "import pandas as pd\n"
        "import matplotlib.pyplot as plt\n"
        "df = pd.read_csv('data/raw.csv')\n"  # exists -- missing=False
        "df.to_csv('data/out.csv')\n"  # does not exist yet -- missing=True
        "plt.savefig('figs/plot.png')\n"  # does not exist yet -- missing=True, figure node
    )

    for _ in range(2):  # second run proves idempotency
        counts = dataflow.ingest_dataflow_repo(
            conn, tmp_path, ["scripts/gen.py"], [], [],
        )
        assert counts == {"reads": 1, "writes": 2}

    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 3
    assert db.get_node(conn, "script:scripts/gen.py")["type"] == "script"

    read_edges = db.query_edges(conn, type="reads")
    assert len(read_edges) == 1
    assert read_edges[0]["dst"] == "dataset:data/raw.csv"
    assert read_edges[0]["evidence"] == {
        "occurrences": [{"file": "scripts/gen.py", "line": 3, "callee": "pd.read_csv"}]
    }

    write_edges = {e["dst"]: e for e in db.query_edges(conn, type="writes")}
    assert set(write_edges) == {"dataset:data/out.csv", "figure:figs/plot.png"}
    assert write_edges["dataset:data/out.csv"]["evidence"]["occurrences"][0]["missing"] is True
    assert write_edges["figure:figs/plot.png"]["evidence"]["occurrences"][0]["missing"] is True
    assert db.get_node(conn, "dataset:data/out.csv")["type"] == "dataset"
    assert db.get_node(conn, "figure:figs/plot.png")["type"] == "figure"


def test_ingest_skips_absolute_and_escaping_paths_and_unrecognized_extensions(tmp_path, conn, caplog):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "gen.py").write_text(
        "import pandas as pd\n"
        "pd.read_csv('/etc/passwd')\n"  # absolute -- skip
        "pd.read_csv('../../outside.csv')\n"  # escapes repo root from either anchor -- skip
        "open('notes.txt', 'w')\n"  # neither data nor image extension -- skip
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        counts = dataflow.ingest_dataflow_repo(conn, tmp_path, ["scripts/gen.py"], [], [])
    assert counts == {"reads": 0, "writes": 0}
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='dataset'").fetchone()[0] == 0
    assert any("cannot be safely mapped into the repo" in r.message for r in caplog.records)
    assert any("neither a tracked" in r.message for r in caplog.records)


def test_ingest_needs_no_git_repository(tmp_path, conn):
    """W2's whole point: unlike rce.ingest.pyfig, this extractor's edges
    never reference a Commit node, so it must work in a plain, never-`git
    init`ed directory -- no NotAGitRepositoryError, no skip."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "in.csv").write_text("x\n1\n")
    (tmp_path / "analysis.R").write_text('read.csv("data/in.csv")\n')
    counts = dataflow.ingest_dataflow_repo(conn, tmp_path, [], ["analysis.R"], [])
    assert counts == {"reads": 1, "writes": 0}
    assert db.get_node(conn, "script:analysis.R")["type"] == "script"


# ---------------------------------------------------------------------------
# Absolute-path remapping under the project root (Python and R alike)
# ---------------------------------------------------------------------------


def test_ingest_remaps_absolute_python_literal_under_project_root_and_is_idempotent(tmp_path, conn):
    """A folded absolute literal that resolves under the fixture's own
    project root is remapped to a project-relative path and ingested
    normally -- the deterministic, in-project "hit" case -- rather than
    skipped, and stays a single edge across re-ingestion."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "in.csv").write_text("x\n1\n")
    root = tmp_path.resolve().as_posix()
    (tmp_path / "gen.py").write_text(
        "import pandas as pd\n"
        f'BASE = "{root}/"\n'
        'DATA = BASE + "data/"\n'
        'df = pd.read_csv(DATA + "in.csv")\n'
    )
    for _ in range(2):  # second run proves idempotency
        counts = dataflow.ingest_dataflow_repo(conn, tmp_path, ["gen.py"], [], [])
        assert counts == {"reads": 1, "writes": 0}
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
    edge = db.query_edges(conn, type="reads")[0]
    assert edge["dst"] == "dataset:data/in.csv"
    occurrence = edge["evidence"]["occurrences"][0]
    assert occurrence["remapped_from_absolute"] is True
    assert occurrence.get("missing") is not True


def test_ingest_python_absolute_literal_outside_project_root_stays_skipped(tmp_path, conn, caplog):
    """Sampled verbatim from the user's real project,
    .../复现包_分步/1-下载与整理数据.py: `BASE`/`DATA` fold to an absolute
    path that is NOT under this fixture's own project root, so remapping
    correctly declines and the call is skipped exactly as before this
    feature existed -- the "miss" case, using the real literal that
    motivated the feature (12 of the project's ~101 real skipped calls)."""
    (tmp_path / "gen.py").write_text(
        "import pandas as pd\n"
        'BASE = "/Users/lilinying/Documents/默认安全锚_论文流水线/"\n'
        'DATA = BASE + "复现包_分步/Data/"\n'
        'df = pd.read_csv(DATA + "panel_pricing.csv")\n'
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        counts = dataflow.ingest_dataflow_repo(conn, tmp_path, ["gen.py"], [], [])
    assert counts == {"reads": 0, "writes": 0}
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
    assert any("cannot be safely mapped into the repo" in r.message for r in caplog.records)


def test_ingest_remaps_absolute_r_literal_under_project_root_and_is_idempotent(tmp_path, conn):
    """Same "hit" shape as the Python remap test above, for an R script's
    paste0(base, ...) call."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "in.csv").write_text("x\n1\n")
    root = tmp_path.resolve().as_posix()
    (tmp_path / "analysis.R").write_text(
        f'base <- "{root}/"\n'
        'df <- read.csv(paste0(base, "data/in.csv"))\n'
    )
    for _ in range(2):  # second run proves idempotency
        counts = dataflow.ingest_dataflow_repo(conn, tmp_path, [], ["analysis.R"], [])
        assert counts == {"reads": 1, "writes": 0}
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
    edge = db.query_edges(conn, type="reads")[0]
    assert edge["dst"] == "dataset:data/in.csv"
    assert edge["evidence"]["occurrences"][0]["remapped_from_absolute"] is True


def test_ingest_r_absolute_literal_outside_project_root_stays_skipped_real_sample(tmp_path, conn, caplog):
    """Sampled verbatim from the user's real project,
    .../复现包_分步/13-地缘风险_价格与数量.Rmd -- the same real absolute-path
    sample as test_parse_rmd_file_real_13_sample_folds_paste0_base above, now
    exercised through the full ingest path against a fixture root it does
    not resolve under: correctly skipped, not remapped."""
    (tmp_path / "13-real.Rmd").write_text(
        "---\ntitle: x\n---\n"
        "\n"
        "```{r setup, include=FALSE}\n"
        'base <- "/Users/lilinying/Documents/默认安全锚_论文流水线/"\n'
        'df <- read.csv(paste0(base, "复现包_分步/Data/panel_pricing.csv"))\n'
        "```\n"
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        counts = dataflow.ingest_dataflow_repo(conn, tmp_path, [], [], ["13-real.Rmd"])
    assert counts == {"reads": 0, "writes": 0}
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
    assert any("cannot be safely mapped into the repo" in r.message for r in caplog.records)


def test_ingest_default_param_fold_creates_edge_and_is_idempotent(tmp_path, conn):
    """Python default-parameter folding (build_foreign_flows-shaped code, see
    the parse_py_file-level tests above), exercised through the full ingest
    path rather than parse_py_file alone -- proves the folded edge is
    actually created, node-typed correctly, and stable across
    re-ingestion."""
    (tmp_path / "gen.py").write_text(
        "DATA = 'data/'\n"
        "\n"
        "def build_foreign_flows(out=DATA + 'foreign_flows_ccdc.csv'):\n"
        "    with open(out, 'w') as f:\n"
        "        f.write('x')\n"
        "\n"
        "build_foreign_flows()\n"
    )
    for _ in range(2):  # second run proves idempotency
        counts = dataflow.ingest_dataflow_repo(conn, tmp_path, ["gen.py"], [], [])
        assert counts == {"reads": 0, "writes": 1}
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
    edge = db.query_edges(conn, type="writes")[0]
    assert edge["dst"] == "dataset:data/foreign_flows_ccdc.csv"
    assert edge["evidence"]["occurrences"][0]["folded_from"] == "out"
