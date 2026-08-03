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


def test_parse_r_file_skips_file_path_call_and_variable_argument(tmp_path, caplog):
    (tmp_path / "analysis.R").write_text(
        "write.csv(d, file.path('data', 'out.csv'))\n"  # file.path(...) -- skip
        "p <- get_path()\n"
        "read.csv(p)\n"  # variable -- skip
    )
    with caplog.at_level("WARNING", logger="rce.ingest.dataflow"):
        calls = dataflow.parse_r_file(tmp_path, "analysis.R")
    assert calls == []
    assert sum("not guessing" in r.message for r in caplog.records) == 2


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
