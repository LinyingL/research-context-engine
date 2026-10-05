"""Variable definition cards (DESIGN.md 9.11; task V5 phase 8): the files,
the strict version schema, the log as a 9.3 ledger, confirming (snapshot
first, entry last), freezing and its question, references, dead variables,
the index's copy, and the CLI -- with the acceptance scenarios of 9.11
named in each test's docstring (13 life of a card, 14 history is not
overwritten, 15 survival, 16 dead and back)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rce import cli, db, inventory, paths
from rce import project as project_identity
from rce.records import cards
from rce.records import variables as V
from rce.records.situation import index_db_path
from rce.records.trust import Trust

from test_project_identity import _make, _pid


# -- fixtures ---------------------------------------------------------------------------------

SCRIPT = 'import pandas as pd\ndf = pd.read_csv("Data/theme.csv")\ndf.to_csv("Data/ts.csv")\n'


def _project(root: Path) -> str:
    (root / "Data").mkdir(parents=True)
    (root / "Data" / "theme.csv").write_text("month,theme,count\n2024-01,a,3\n")
    (root / "Data" / "ts.csv").write_text("month,topicshift\n2024-01,0.1\n")
    (root / "build.py").write_text(SCRIPT)
    assert cli.main(["init", str(root)]) == 0
    return _pid(root)


def _text(
    *,
    formula: str = "TS_t = 1 − cos(p_t, p_{t−1})",
    comment: str = "# 含义",
    datasets: tuple[str, ...] = ("Data/theme.csv",),
    variable: str | None = None,
    output: str = "Data/ts.csv",
    field: str = "topicshift",
    script: str = "build.py",
    aliases: tuple[str, ...] = ("TopicShift",),
    why: str = "余弦距离对主题总量不敏感",
) -> str:
    inputs = "".join(f'[[input]]\ndataset = "{d}"\nfields = ["month"]\n\n' for d in datasets)
    if variable:
        inputs += f'[[input]]\nvariable = "{variable}"\n\n'
    alias_list = ", ".join(f'"{a}"' for a in aliases)
    return (
        f'name = "TopicShift（叙事更替）"\naliases = [{alias_list}]\n\n{comment}\n'
        f"meaning = '''相邻两月新闻主题分布的差异'''\nunit = \"无量纲，0–1\"\ngranularity = \"月\"\n\n"
        f"{inputs}[construction]\nformula = '''{formula}'''\nmissing = '''缺月不插值'''\n\n"
        f'[implementation]\nscript = "{script}"\noutput = "{output}"\nfield = "{field}"\n\n'
        f"[decision]\nwhy = '''{why}'''\ndecided_by = \"LL\"\nadopted_on = \"2026-07-26\"\n"
    )


def _write(root: Path, card: str, n: int, text: str) -> Path:
    path = V.variables_dir(root) / card / f"v{n}.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _new_confirmed(root: Path, card: str = "topicshift", text: str | None = None, **kw) -> cards.Confirmed:
    cards.new_card(root, card)
    _write(root, card, 1, text or _text())
    kw.setdefault("attested", "unknown")
    return cards.confirm(root, card, **kw)


def _card(root: Path, card: str = "topicshift") -> V.Card:
    return V.open_card(root, card)


def _decision(root: Path, card: str = "topicshift"):
    ident = cards._identity_now(root)
    conn = db.connect(index_db_path(ident.id))
    found = V.find_card_dirs(root, card)
    read = V.read_card(root, found[0] if found else V.variables_dir(root) / card)
    try:
        return cards.assess_card(conn, root, read, ident)
    finally:
        conn.close()


def _no_entry_without_copy(root: Path) -> None:
    """9.11: never an entry whose frozen or code copy is missing."""
    for card in V.read_cards(root):
        for e in card.entries:
            if e.get("frozen") and e.get("act") in ("confirmed", "corrected"):
                assert (card.directory / e.get("frozen")).is_file(), e.data
            copy = ((e.get("checked") or {}).get("script") or {}).get("copy")
            if copy:
                assert (V.variables_dir(root) / copy).is_file(), e.data


def _tree(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


# -- the version file ------------------------------------------------------------------------


def test_template_is_a_valid_incomplete_draft():
    content = V.parse_version(cards.template(1).decode("utf-8"))
    missing = V.missing_for_confirm(content)
    assert "name" in missing and "construction.formula" in missing and "decision.why" in missing


def test_an_unknown_key_is_an_error_naming_its_line():
    """9.11: a line that slid under the wrong heading is caught, with its line."""
    text = _text().replace('field = "topicshift"\n', 'field = "topicshift"\nwhy = "slid"\n')
    with pytest.raises(V.VersionInvalid) as err:
        V.parse_version(text)
    assert err.value.line == text.split("\n").index('why = "slid"') + 1
    assert "implementation" in str(err.value)
    with pytest.raises(V.VersionInvalid, match="unknown key 'colour'"):
        V.parse_version(_text() + 'colour = "x"\n')


@pytest.mark.parametrize("bad, match", [
    ('[input]\ndataset = "a.csv"\n', "each input"),
    ('[[input]]\nvariable = "returns"\n', "pinned version"),
    ('[[input]]\ndataset = "../x.csv"\n', "leaves the project"),
    ('[[input]]\ndataset = "a.csv"\nvariable = "r@v1"\n', "not both"),
    ("meaning = \"tab\\there\"\n", "control character"),
    ('unit = 3\n', "must be text"),
])
def test_strict_schema_refusals(bad, match):
    with pytest.raises(V.VersionInvalid, match=match):
        V.parse_version(bad)


def test_comments_and_layout_do_not_change_the_content_hash():
    a = V.content_hash(V.parse_version(_text()))
    b = V.content_hash(V.parse_version(_text(comment="# 另一种注释\n\n\n")))
    assert a == b
    assert a != V.content_hash(V.parse_version(_text(formula="TS = JS")))


def test_new_refuses_an_id_present_anywhere_up_to_case(tmp_path):
    root = tmp_path / "p"
    _project(root)
    cards.new_card(root, "TopicShift")
    with pytest.raises(cards.CardRefused) as err:
        cards.new_card(root, "topicshift")
    assert err.value.code == "exists"
    (root / ".rce" / "backups" / "variables" / "RV").mkdir(parents=True)
    with pytest.raises(cards.CardRefused, match="snapshots"):
        cards.new_card(root, "rv")
    for bad in ("_code", "a/b", "x@v1", "", "a..b"):
        with pytest.raises(cards.CardRefused):
            cards.new_card(root, bad)
    assert sorted(p.name for p in V.card_dirs(root)) == ["TopicShift"]


def test_new_refuses_an_id_the_index_still_holds(tmp_path):
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root, "rv")
    shutil.rmtree(V.variables_dir(root) / "rv")  # gone, but the index applied its log
    with pytest.raises(cards.CardRefused, match="index still holds"):
        cards.new_card(root, "RV")


def test_new_is_refused_on_a_pre_v5_project(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    paths.legacy_graph_dir(root).mkdir(parents=True)
    conn = db.connect(paths.legacy_index_db_path(root))
    db.migrate(conn)
    conn.close()
    assert cli.main(["variable", "new", "x", str(root)]) == 1
    assert not V.variables_dir(root).exists()


# -- 13: the life of a card --------------------------------------------------------------------


def test_scenario_13_life_of_a_card(tmp_path, capsys):
    """9.11 #13: new -> a draft from the template; edit freely; confirm with
    one input deliberately absent and the output not a CSV: confirmed, those
    checks 未核对 with their reasons, the script copied to _code/, the output
    fingerprinted. Revise and confirm: v2 in use, v1 superseded from the
    entry's time, v1.toml byte-identical. Revise with a draft open: refused."""
    root = tmp_path / "p"
    _project(root)
    (root / "Data" / "ts.parquet").write_bytes(b"PAR1")
    assert cli.main(["variable", "new", "topicshift", "--path", str(root)]) == 0
    assert _card(root).draft == 1
    _write(root, "topicshift", 1, _text(formula="first try"))
    _write(root, "topicshift", 1, _text(datasets=("Data/theme.csv", "Data/absent.csv"), output="Data/ts.parquet"))
    assert cli.main(["variable", "confirm", "topicshift", str(root)]) == 1  # off a terminal the question must be answered
    assert cli.main(["variable", "confirm", "topicshift", "--attest", "unknown", str(root)]) == 0

    card = _card(root)
    assert card.in_use == 1 and card.draft is None
    entry = card.versions[1].entry
    observed, checked = entry.get("observed"), entry.get("checked")
    absent = [i for i in observed["inputs"] if i["dataset"] == "Data/absent.csv"][0]
    assert absent == {"dataset": "Data/absent.csv", "result": "未核对", "reason": cards.R_FILE_ABSENT}
    assert checked["field"] == {"result": "未核对", "reason": cards.R_NOT_CSV}
    assert checked["writes"]["result"] == "不符"  # the script writes ts.csv, not the parquet
    assert [r["result"] for r in checked["reads"]] == ["已核对", "不符"]
    copy = checked["script"]["copy"]
    assert (V.variables_dir(root) / copy).read_bytes() == SCRIPT.encode()
    assert observed["output"]["sha256"] and observed["output"]["size"] == 4
    assert entry.get("attested") == "unknown"
    v1_bytes = (V.variables_dir(root) / "topicshift" / "v1.toml").read_bytes()
    assert (card.directory / entry.get("frozen")).read_bytes() == v1_bytes

    assert cli.main(["variable", "revise", "topicshift", str(root)]) == 0
    assert (card.directory / "v2.toml").read_bytes() == v1_bytes
    assert cli.main(["variable", "revise", "topicshift", str(root)]) == 1
    assert "draft v2 is open" in capsys.readouterr().err
    _write(root, "topicshift", 2, _text(formula="TS = JS divergence"))
    done = cards.confirm(root, "topicshift", attested="no")
    card = _card(root)
    assert card.in_use == 2 and card.versions[1].status == "superseded"
    assert card.versions[1].superseded_by == (2, done.entry.at)
    assert (card.directory / "v1.toml").read_bytes() == v1_bytes
    assert done.entry.get("attested") == "no"
    _no_entry_without_copy(root)
    capsys.readouterr()
    assert cli.main(["variable", "show", "topicshift", str(root)]) == 0
    out = capsys.readouterr().out
    assert "superseded by v2" in out and "in_use" in out


def test_scenario_13_a_matching_output_hash_never_says_built_with(tmp_path):
    """9.11 #13 / Section 0: the attestation is the researcher's answer,
    recorded as theirs; an output on disk whose hash matches the observation
    never makes the view say it was built with the version."""
    root = tmp_path / "p"
    _project(root)
    done = _new_confirmed(root)
    view = cards.card_payload(root, _card(root))["versions"][0]
    output = (root / "Data" / "ts.csv").read_bytes()
    assert view["observed"]["output"]["sha256"] == cards._sha256(output)
    assert view["attested"] == "unknown" and done.entry.get("attested") == "unknown"
    cards.revise(root, "topicshift")
    _write(root, "topicshift", 2, _text(formula="v2"))
    cards.confirm(root, "topicshift", attested="yes")
    assert [v["attested"] for v in cards.card_payload(root, _card(root))["versions"]] == ["unknown", "yes"]
    with pytest.raises(cards.CardRefused):
        cards.confirm(root, "topicshift", attested="probably")


def test_confirm_needs_a_complete_draft_and_refuses_an_invalid_one(tmp_path):
    root = tmp_path / "p"
    _project(root)
    cards.new_card(root, "x")
    with pytest.raises(cards.CardRefused) as err:
        cards.confirm(root, "x", attested="unknown")
    assert err.value.code == "incomplete" and "decision.why" in str(err.value)
    _write(root, "x", 1, _text() + 'stray = "x"\n')
    with pytest.raises(cards.CardRefused, match="unknown key 'stray'"):
        cards.confirm(root, "x", attested="unknown")
    assert not (V.variables_dir(root) / "x" / "log.toml").exists()


def test_checks_name_why_they_could_not_be_made(tmp_path):
    """9.11: 未核对 says why -- the script could not be parsed; the link
    exists only as the researcher's own hand-drawn mapping."""
    root = tmp_path / "p"
    _project(root)
    (root / "broken.py").write_text("def (:\n")
    _new_confirmed(root, "a", _text(script="broken.py"))
    checked = _card(root, "a").versions[1].entry.get("checked")
    assert checked["writes"] == {"result": "未核对", "reason": cards.R_SCRIPT_UNPARSEABLE}
    assert checked["script"]["result"] == "已核对"  # copied all the same

    (root / "quiet.py").write_text("print('no io')\n")
    from rce.ingest import mappings as mappings_ingest

    mappings_ingest.add_mapping(root, "quiet.py", "Data/ts.csv", "writes")
    _new_confirmed(root, "b", _text(script="quiet.py"))
    checked = _card(root, "b").versions[1].entry.get("checked")
    assert checked["writes"] == {"result": "未核对", "reason": cards.R_MAPPING_ONLY}
    assert checked["reads"][0]["result"] == "不符"


_KILLER = r"""
import os, sys
from pathlib import Path
from rce.records import cards
root, card, point = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def fault(name):
    if name == point:
        os._exit(17)
cards.confirm(root, card, attested="unknown", fault=fault)
"""


def _killed(root: Path, card: str, point: str) -> None:
    proc = subprocess.run([sys.executable, "-c", _KILLER, str(root), card, point], capture_output=True)
    assert proc.returncode == 17, proc.stderr.decode()


def test_scenario_13_a_kill_between_each_pair_of_writes(tmp_path, capsys):
    """9.11 #13: kill the process between each pair of writes of a
    confirmation and start again -- never an entry whose frozen or code copy
    is missing; the leftover copies are removed by `rce records --clean`."""
    root = tmp_path / "p"
    _project(root)
    cards.new_card(root, "topicshift")
    _write(root, "topicshift", 1, _text())
    _killed(root, "topicshift", "after_code_copy")
    _no_entry_without_copy(root)
    assert _card(root).draft == 1 and not _card(root).entries
    leftover_code = sorted(p.name for p in V.code_dir(root).iterdir())

    (root / "build.py").write_text(SCRIPT + "# edited\n")
    _killed(root, "topicshift", "after_frozen_copy")
    _no_entry_without_copy(root)
    leftover_frozen = sorted(p.name for p in (V.variables_dir(root) / "topicshift" / "frozen").iterdir())
    assert _card(root).draft == 1  # nothing confirmed: the card is still a draft, and not frozen

    _write(root, "topicshift", 1, _text(formula="settled"))
    cards.confirm(root, "topicshift", attested="unknown")
    cards.revise(root, "topicshift")
    _write(root, "topicshift", 2, _text(formula="second"))
    _killed(root, "topicshift", "after_entry")  # the entry landed; the index not yet
    _no_entry_without_copy(root)
    assert _card(root).in_use == 2
    assert _decision(root).verdict is Trust.OK  # an entry the index never saw is simply applied

    capsys.readouterr()
    assert cli.main(["records", "--clean", "--yes", str(root)]) == 0
    out = capsys.readouterr().out
    assert f"variables/_code/{leftover_code[0]}" in out
    assert f"variables/topicshift/frozen/{leftover_frozen[0]}" in out
    _no_entry_without_copy(root)
    assert cli.main(["records", "--verify", str(root)]) == 1  # the index lags the record until it is applied
    assert cli.main(["review", str(root)]) == 0  # applies the record (as any scan or the watcher does)
    assert cli.main(["records", "--verify", str(root)]) == 0


# -- 14: history is not overwritten ------------------------------------------------------------


def _two_versions(root: Path) -> None:
    _new_confirmed(root)
    cards.revise(root, "topicshift")
    _write(root, "topicshift", 2, _text(formula="TS_t = 1 − cos(p_t, p_{t−2})"))
    cards.confirm(root, "topicshift", attested="unknown")


def test_scenario_14_edit_a_confirmed_version_and_save_as_new(tmp_path, capsys):
    """9.11 #14: edit a confirmed version's formula by hand -- nothing is
    applied and the question is asked; 「另存为新版本」: the edit becomes
    draft v3 and v2.toml is byte-identical to its frozen copy. Refused
    while a draft is open, saying so."""
    root = tmp_path / "p"
    _project(root)
    _two_versions(root)
    v2 = V.variables_dir(root) / "topicshift" / "v2.toml"
    frozen = _card(root).versions[2].frozen_path.read_bytes()
    edited = _text(formula="TS_t = Hellinger")
    v2.write_text(edited, encoding="utf-8")
    card = _card(root)
    assert card.questions == [2]
    shown = cards.card_payload(root, card)["versions"][1]
    assert shown["question"] == "v2 的定义在确认后被改动了"
    assert shown["content"]["construction"]["formula"] == "TS_t = 1 − cos(p_t, p_{t−2})"  # the edit is not obeyed
    capsys.readouterr()
    assert cli.main(["variable", "list", str(root)]) == 0
    assert "was changed after it was confirmed" in capsys.readouterr().out

    (V.variables_dir(root) / "topicshift" / "v3.toml").write_text(_text(formula="draft"), encoding="utf-8")
    with pytest.raises(cards.CardRefused) as err:
        cards.answer_edited(root, "topicshift", "new")
    assert err.value.code == "draft_open" and "已有草稿 v3" in err.value.message_zh
    (V.variables_dir(root) / "topicshift" / "v3.toml").unlink()

    assert cli.main(["variable", "answer", "topicshift", "new", str(root)]) == 0
    assert v2.read_bytes() == frozen
    assert (V.variables_dir(root) / "topicshift" / "v3.toml").read_text(encoding="utf-8") == edited
    card = _card(root)
    assert card.questions == [] and card.draft == 3 and card.in_use == 2


def test_scenario_14_correction_keeps_both_wordings_and_old_references(tmp_path):
    """9.11 #14: 「这是更正」 -- a corrected entry with both hashes and a new
    frozen copy; both wordings can be read; a reference made before the
    correction still shows the wording it was made against."""
    root = tmp_path / "p"
    _project(root)
    _two_versions(root)
    before = V.reference_to(root, "topicshift")
    assert before.label.startswith("topicshift@v2·")
    v2 = V.variables_dir(root) / "topicshift" / "v2.toml"
    v2.write_text(_text(formula="TS_t = 1 − cos(p_t, p_{t−2})  （更正：笔误）"), encoding="utf-8")
    done = cards.answer_edited(root, "topicshift", "correct")
    assert done.entry.get("act") == "corrected"
    assert done.entry.get("previous") == before.content and done.entry.get("corrects") == before.entry
    card = _card(root)
    assert card.questions == [] and card.in_use == 2
    old = V.resolve(root, before)
    assert old is not None and "笔误" not in old.text and "p_{t−2}" in old.text
    after = V.reference_to(root, "topicshift")
    assert after != before and "笔误" in V.resolve(root, after).text
    _no_entry_without_copy(root)


def test_scenario_14_the_question_is_asked_without_an_index(tmp_path, capsys):
    """9.11 #14: with ~/.rce/graphs/<id>/ deleted before the edit is seen,
    the question is still asked -- the check needs no index."""
    root = tmp_path / "p"
    pid = _project(root)
    _two_versions(root)
    shutil.rmtree(paths.index_dir(pid))
    (V.variables_dir(root) / "topicshift" / "v2.toml").write_text(_text(formula="edited"), encoding="utf-8")
    assert _card(root).questions == [2]
    capsys.readouterr()
    assert cli.main(["variable", "show", "topicshift", str(root)]) == 0  # opening rebuilds the index
    assert "was changed after it was confirmed" in capsys.readouterr().out
    assert cli.main(["variable", "answer", "topicshift", "correct", str(root)]) == 0
    assert _card(root).questions == []


def test_scenario_14_a_comment_only_change_raises_nothing(tmp_path):
    root = tmp_path / "p"
    _project(root)
    _two_versions(root)
    v2 = V.variables_dir(root) / "topicshift" / "v2.toml"
    v2.write_text("# 加一行注释\n" + v2.read_text(encoding="utf-8").replace("\n\n", "\n\n\n"), encoding="utf-8")
    assert _card(root).questions == []
    with pytest.raises(cards.CardRefused) as err:
        cards.answer_edited(root, "topicshift", "correct")
    assert err.value.code == "no_question"


def test_scenario_14_restored_card_makes_a_reference_unresolvable_and_a_new_v2_does_not_capture_it(tmp_path):
    """9.11 #14: with the card restored from a backup that predates the
    version, the reference shows 「引用暂不可解析」, and a newly written v2
    does not capture it."""
    root = tmp_path / "p"
    pid = _project(root)
    _new_confirmed(root)
    aside = tmp_path / "card-backup"
    shutil.copytree(V.variables_dir(root) / "topicshift", aside)
    cards.revise(root, "topicshift")
    _write(root, "topicshift", 2, _text(formula="v2 as first written"))
    cards.confirm(root, "topicshift", attested="unknown")
    ref = V.reference_to(root, "topicshift")
    assert V.resolve(root, ref) is not None

    shutil.rmtree(paths.index_dir(pid))  # a machine with no index: nothing can tell
    shutil.rmtree(V.variables_dir(root) / "topicshift")
    shutil.copytree(aside, V.variables_dir(root) / "topicshift")
    assert V.resolve(root, ref) is None
    assert cli.main(["status", "--path", str(root)]) == 0  # rebuilds the index
    cards.revise(root, "topicshift")
    _write(root, "topicshift", 2, _text(formula="v2 as first written"))  # even the same text
    cards.confirm(root, "topicshift", attested="unknown")
    assert V.resolve(root, ref) is None
    assert V.reference_to(root, "topicshift").content == ref.content  # same wording, another entry


# -- references and upstream variables --------------------------------------------------------


def test_variable_inputs_are_pinned_and_a_newer_upstream_is_only_a_note(tmp_path):
    """9.11 "Variables built from variables": an input pinned to returns@v1
    is recorded as the reference it relies on; when returns gains v2 the
    card says 「上游 returns 已有 v2」 and nothing else happens. Only a
    confirmed version can be referred to."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root, "returns")
    cards.new_card(root, "rv")
    _write(root, "rv", 1, _text(variable="returns@v2"))
    with pytest.raises(cards.CardRefused) as err:
        cards.confirm(root, "rv", attested="unknown")
    assert err.value.code == "unresolvable"
    _write(root, "rv", 1, _text(variable="returns@v1"))
    cards.confirm(root, "rv", attested="unknown")
    pinned = _card(root, "rv").versions[1].entry.get("upstream")[0]
    assert pinned["variable"] == "returns" and pinned["version"] == 1 and pinned["entry"].startswith("v-")
    assert cards.card_payload(root, _card(root, "rv"))["versions"][0]["upstream_notes"] == []
    cards.revise(root, "returns")
    _write(root, "returns", 2, _text(formula="log returns"))
    cards.confirm(root, "returns", attested="unknown")
    rv = _card(root, "rv")
    assert rv.in_use == 1
    assert cards.card_payload(root, rv)["versions"][0]["upstream_notes"] == ["上游 returns 已有 v2"]
    hashed = V.resolve_text(root, pinned["ref"])
    assert hashed is not None and hashed.entry == pinned["entry"]


def test_a_draft_cannot_be_referred_to(tmp_path):
    root = tmp_path / "p"
    _project(root)
    cards.new_card(root, "x")
    with pytest.raises(V.VariableError):
        V.reference_to(root, "x", 1)
    assert V.resolve(root, V.Reference("x", 1, "v-nope", "sha256:00")) is None


# -- 15: survival -------------------------------------------------------------------------------


def _with_cards(root: Path) -> str:
    """The 9.9 fixture plus two cards (9.11 #15): `topicshift` with two
    versions, a correction and an `abandoned` entry; `rv` with one version."""
    pid = _make(root)
    text = _text(datasets=("data.csv",), output="out.csv", script="a.py", field="a")
    cards.new_card(root, "topicshift")
    _write(root, "topicshift", 1, text)
    cards.confirm(root, "topicshift", attested="yes")
    cards.revise(root, "topicshift")
    _write(root, "topicshift", 2, text.replace("p_{t−1}", "p_{t−2}"))
    cards.confirm(root, "topicshift", attested="unknown")
    _write(root, "topicshift", 2, text.replace("p_{t−1}", "p_{t−2} （更正）"))
    cards.answer_edited(root, "topicshift", "correct")
    cards.abandon(root, "topicshift", note="与 RV 的关系不稳定")
    cards.new_card(root, "rv")
    _write(root, "rv", 1, text.replace("TopicShift", "RV"))
    cards.confirm(root, "rv", attested="unknown")
    return pid


def _card_files(root: Path) -> dict[str, bytes]:
    return _tree(V.variables_dir(root))


def _cards_ok(root: Path) -> None:
    ts, rv = _card(root, "topicshift"), _card(root, "rv")
    assert ts.readable and rv.readable
    assert [e.get("act") for e in ts.entries] == ["confirmed", "confirmed", "corrected", "abandoned"]
    assert ts.in_use is None and ts.abandoned is not None and rv.in_use == 1
    _no_entry_without_copy(root)
    assert cli.main(["records", "--verify", str(root)]) == 0


def test_scenario_15_move_rename_and_rebuild(tmp_path):
    """9.11 #15 with 9.9 #1, #2 and #5: after a move, a rename (letter case
    only too) and a rebuild -- every version, entry, frozen copy and code
    copy is present and `rce records --verify` passes."""
    old = tmp_path / "a" / "p"
    pid = _with_cards(old)
    files_before = _card_files(old)
    new = tmp_path / "b" / "p"
    new.parent.mkdir()
    shutil.copytree(old, new, symlinks=True)
    shutil.rmtree(old)
    assert cli.main(["status", "--path", str(new)]) == 0
    assert _card_files(new) == files_before
    _cards_ok(new)
    renamed = tmp_path / "b" / "P"
    os.rename(new, renamed)
    assert cli.main(["status", "--path", str(renamed)]) == 0
    _cards_ok(renamed)
    shutil.rmtree(paths.index_dir(pid))
    assert cli.main(["status", "--path", str(renamed)]) == 0
    _cards_ok(renamed)
    assert cli.main(["rebuild", str(renamed)]) == 0
    _cards_ok(renamed)
    assert _card_files(renamed) == files_before


def test_scenario_15_copy_with_each_answer(tmp_path):
    """9.11 #15 with 9.9 #3: fork -- the cards travel, and a later act in the
    copy does not appear in the original; claim -- rebuilt from the claimer,
    verify passes; 「这是另一个项目」 -- the cards are set aside."""
    p = tmp_path / "p"
    _with_cards(p)
    files_before = _card_files(p)
    for answer in ("fork", "claim", "other"):
        q = tmp_path / answer
        shutil.copytree(p, q)
        assert cli.main(["project", answer, str(q)]) == 0
        if answer == "other":
            assert not V.variables_dir(q).exists()
            aside = list((q / ".rce" / "backups").glob("from-another-project-*"))
            assert _tree(aside[0] / "variables") == files_before
            assert cli.main(["variable", "list", str(q)]) == 0
            continue
        assert _card_files(q) == files_before
        _cards_ok(q)
        if answer == "fork":
            cards.revive(q, "topicshift", note="在分支里复活")
            assert _card(q).abandoned is None and _card(p).abandoned is not None
            shutil.rmtree(q)
        else:
            assert cli.main(["status", "--path", str(p)]) == 1  # the original is asked on its next open


def test_scenario_15_restore_asks_and_the_file_wins(tmp_path, capsys):
    """9.11 #15 with 9.9 #7: copy .rce/ aside, act further on a card,
    restore the copy: the card's log has fewer entries than the index
    applied -- it is frozen and asked about, the other card keeps working;
    「以文件为准」 gives exactly the copy's records, and the version a
    restore took away does not give its number to the next revision."""
    root = tmp_path / "p"
    _with_cards(root)
    aside = tmp_path / "aside"
    shutil.copytree(root / ".rce", aside)
    cards.revise(root, "rv")
    _write(root, "rv", 2, _text(formula="rv v2", datasets=("data.csv",), output="out.csv", script="a.py"))
    cards.confirm(root, "rv", attested="unknown")
    shutil.rmtree(root / ".rce")
    shutil.copytree(aside, root / ".rce")

    decision = _decision(root, "rv")
    assert decision.verdict is Trust.SHRUNK and len(decision.missing) == 1
    with pytest.raises(cards.CardRefused) as err:
        cards.abandon(root, "rv", note="x")
    assert err.value.code == "untrusted" and "少了 1 条" in err.value.message_zh
    cards.revive(root, "topicshift", note="另一张卡照常工作")
    capsys.readouterr()
    assert cli.main(["records", str(root)]) == 0
    assert "rv: log.toml lacks 1 entr(y/ies)" in capsys.readouterr().out
    assert cli.main(["rebuild", str(root)]) == 1  # the question is answered first
    ids = ",".join(m["id"] for m in decision.missing)
    assert cli.main(["variable", "answer", "rv", "file", "--missing", ids, str(root)]) == 0
    rv = _card(root, "rv")
    assert [e.get("act") for e in rv.entries] == ["confirmed", "removed"]
    assert rv.next_number == 3
    cards.revise(root, "rv")
    assert (V.variables_dir(root) / "rv" / "v3.toml").exists()
    _no_entry_without_copy(root)
    assert cli.main(["records", "--verify", str(root)]) == 0


@pytest.mark.parametrize("damage", ["unparseable", "removed", "truncated", "lost_abandoned", "zero_bytes"])
def test_scenario_15_a_damaged_log_freezes_that_card_only(tmp_path, damage):
    """9.11 #15: a log made unparseable, removed or truncated -- including one
    that lost only its `abandoned` entry -- freezes that card, refuses writes
    to it and asks; the other cards keep working; repairing it restores
    normal operation."""
    root = tmp_path / "p"
    _with_cards(root)
    log = V.variables_dir(root) / "topicshift" / "log.toml"
    good = log.read_bytes()
    text = good.decode("utf-8")
    blocks = text.split("\n[[entry]]")
    if damage == "unparseable":
        log.write_text(text + "\n[[entry\n", encoding="utf-8")
    elif damage == "removed":
        log.unlink()
    elif damage == "truncated":
        log.write_text("\n[[entry]]".join(blocks[:3]), encoding="utf-8")
    elif damage == "lost_abandoned":
        log.write_text("\n[[entry]]".join(blocks[:-1]), encoding="utf-8")
        assert _card(root).abandoned is None  # what the file alone would say
    else:
        log.write_bytes(b"")
    decision = _decision(root)
    assert not decision.may_write
    if damage in ("removed", "truncated", "lost_abandoned", "zero_bytes"):
        assert decision.verdict is Trust.SHRUNK  # asked, never obeyed
    with pytest.raises(cards.CardRefused) as err:
        cards.revive(root, "topicshift", note="x")
    assert err.value.code == "untrusted"
    assert (log.read_bytes() if log.exists() else None) != good
    cards.abandon(root, "rv", note="另一张卡照常工作")  # other cards keep working
    log.write_bytes(good)
    cards.revive(root, "topicshift", note="修好了")
    assert _card(root).abandoned is None


def test_scenario_15_restore_answer_puts_entries_and_copies_back(tmp_path):
    """9.3's second answer for a card: 「把缺少的补回文件」 appends the missing
    entries (via recovered) after putting the frozen copies back from the
    index's copy -- here for a card whose whole directory vanished."""
    root = tmp_path / "p"
    _with_cards(root)
    before = _card(root, "rv")
    shutil.rmtree(before.directory)
    assert V.card_dirs(root) and "rv" not in [p.name for p in V.card_dirs(root)]
    assert _decision(root, "rv").verdict is Trust.SHRUNK
    assert cli.main(["variable", "answer", "rv", "restore", str(root)]) == 0
    rv = _card(root, "rv")
    assert [e.get("via") for e in rv.entries] == ["recovered"]
    assert rv.versions[1].file_state == "absent" and not rv.versions[1].copy_missing
    ref = V.reference_to(root, "rv")
    assert V.resolve(root, ref).text is not None
    _no_entry_without_copy(root)


def test_scenario_15_conflict_copy_or_case_twin_makes_only_that_card_unreadable(tmp_path, capsys, monkeypatch):
    """9.11 #15: a sync conflict copy beside v2.toml makes that card
    unreadable and says why; so do two directories equal up to case."""
    root = tmp_path / "p"
    _with_cards(root)
    ts = V.variables_dir(root) / "topicshift"
    shutil.copy(ts / "v2.toml", ts / "v2 2.toml")
    card = _card(root)
    assert card.state == "unreadable" and "v2 2.toml" in card.detail
    with pytest.raises(cards.CardRefused):
        cards.revive(root, "topicshift", note="x")
    capsys.readouterr()
    assert cli.main(["variable", "list", str(root)]) == 0
    out = capsys.readouterr().out
    assert "UNREADABLE (conflict_copy" in out and "rv: v1 in use" in out
    (ts / "v2 2.toml").unlink()
    assert _card(root).readable
    rv = V.variables_dir(root) / "rv"
    twin = V.variables_dir(root) / "RV"
    try:
        twin.mkdir()
    except FileExistsError:  # a case-insensitive volume cannot hold both: show RCE two anyway
        real = V.card_dirs
        monkeypatch.setattr(V, "card_dirs", lambda r: [*real(r), twin])
    card = V.read_card(root, rv)
    assert card.state == "unreadable" and card.reason == "case_duplicate"
    assert _card(root).readable


_CARD_WRITER = r"""
import sys
from pathlib import Path
from rce.records import cards
root, card, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
for i in range(n):
    cards.abandon(root, card, note=f"{card} {i}", timeout=120)
    cards.revive(root, card, note=f"{card} {i}", timeout=120)
"""


def test_scenario_15_two_concurrent_writers(tmp_path):
    """9.11 #15 with 9.9 #10: two processes acting on cards of one project at
    once -- every act is in its log afterwards, in order, and nothing raises."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root, "a")
    _new_confirmed(root, "b")
    procs = [
        subprocess.Popen([sys.executable, "-c", _CARD_WRITER, str(root), card, "15"], stderr=subprocess.PIPE)
        for card in ("a", "b")
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=300)
        assert proc.returncode == 0, err.decode()
    for card in ("a", "b"):
        entries = _card(root, card).entries
        assert len(entries) == 31 and [e.seq for e in entries] == list(range(1, 32))
    assert cli.main(["records", "--verify", str(root)]) == 0


def test_merged_logs_freeze_the_card_and_pick_no_winner(tmp_path):
    """9.3 for a card log: two histories merged (seq repeats) put the card
    in conflict; nothing is obeyed and nothing picks a winner."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    log = V.variables_dir(root) / "topicshift" / "log.toml"
    text = log.read_text(encoding="utf-8")
    other = text.split("\n[[entry]]")[1].replace('id = "v-', 'id = "v-0', 1)
    log.write_text(text + "\n[[entry]]" + other, encoding="utf-8")
    card = _card(root)
    assert card.state == "frozen" and card.reason == "conflict" and card.in_use is None


# -- 16: dead and back ---------------------------------------------------------------------------


_ATTEMPTS = '\n'.join([
    'file = "map.md"', 'heading = "H"', 'dead_variables = {dead}', "", "[columns]", 'id = "#"', 'date = "date"',
    'description = "desc"', 'variables = "vars"', 'result = "result"', 'verdict = "verdict"', "",
])


def test_scenario_16_dead_and_back(tmp_path, capsys):
    """9.11 #16: abandon with a reason; revive: the view shows the state and
    both entries in order, and flags the disagreement with attempts.toml in
    whichever direction it exists (matched through aliases, the existing
    case-insensitive substring rule)."""
    root = tmp_path / "p"
    _project(root)
    (root / "map.md").write_text("## H\n\n| # | date | desc | vars | result | verdict |\n|---|---|---|---|---|---|\n")
    config = root / ".rce" / "attempts.toml"
    config.write_text(_ATTEMPTS.format(dead='["信息熵"]'), encoding="utf-8")
    _new_confirmed(root, text=_text(aliases=("TopicShift", "私有 Shannon 信息熵")))
    assert cards.dead_variable_flags(root)[0]["message"] == cards.DEAD_ATTEMPTS_ONLY

    assert cli.main(["variable", "abandon", "topicshift", "--note", "与汇率无稳定关系", str(root)]) == 0
    card = _card(root)
    assert card.in_use is None and card.abandoned.get("note") == "与汇率无稳定关系" and card.abandoned.at
    assert cards.dead_variable_flags(root) == []
    config.write_text(_ATTEMPTS.format(dead='["lnRate"]'), encoding="utf-8")
    flags = cards.dead_variable_flags(root)
    assert flags == [{"card": "topicshift", "direction": "card_only", "message": cards.DEAD_CARD_ONLY, "dead": []}]
    assert V.resolve(root, V.Reference("topicshift", 1, card.versions[1].entry.id,
                                       card.versions[1].entry.get("content"))) is not None  # still resolves

    assert cli.main(["variable", "revive", "topicshift", "--note", "新数据下重新有效", str(root)]) == 0
    card = _card(root)
    assert card.in_use == 1 and card.abandoned is None
    assert [e.get("act") for e in card.entries] == ["confirmed", "abandoned", "revived"]
    assert cards.dead_variable_flags(root) == []
    with pytest.raises(cards.CardRefused):
        cards.revive(root, "topicshift", note="again")
    with pytest.raises(cards.CardRefused):
        cards.abandon(root, "topicshift", note="  ")
    capsys.readouterr()
    assert cli.main(["variable", "show", "topicshift", str(root)]) == 0
    out = capsys.readouterr().out
    assert out.index("abandoned at") < out.index("revived at")


# -- integration ----------------------------------------------------------------------------------


def test_records_inventory_counts_cards_and_drafts(tmp_path, capsys):
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    cards.new_card(root, "rv")
    capsys.readouterr()
    assert cli.main(["records", str(root)]) == 0
    assert "Variable definition cards: .rce/variables -- 2 card(s), 1 draft(s)" in capsys.readouterr().out


def test_card_files_are_snapshotted_into_their_own_backup_folder(tmp_path):
    """9.11: snapshots go to .rce/backups/variables/<id>/ (the log before an
    append the first time each day; hand edits of a version when seen)."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    cards.abandon(root, "topicshift", note="x")  # the second append snapshots the one-entry log
    backups = root / ".rce" / "backups" / "variables" / "topicshift"
    assert any(p.name.startswith("log.toml.") for p in backups.iterdir())
    inventory.snapshot_records(root)
    assert any(p.name.startswith("v1.toml.") for p in backups.iterdir())


def test_the_watcher_reapplies_the_cards_when_their_files_change(tmp_path):
    from rce.webapp import watcher as watcher_mod

    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    w = watcher_mod.ProjectWatcher(lambda: root)
    w.poll_once()  # baseline
    log = V.variables_dir(root) / "topicshift" / "log.toml"
    log.write_text(log.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert w.poll_once() is True
    conn = db.connect(paths.graph_db_path(root))
    try:
        status = db.get_record_status(conn, cards.RECORD_STATUS_NAME)
        assert status["cards"]["topicshift"]["state"] == "ok"
    finally:
        conn.close()


def test_a_lost_identity_with_cards_is_a_question_and_adopt_keeps_them(tmp_path):
    """9.4 / 9.12: `.rce/variables/` counts as records -- a folder that lost
    its identity file but holds cards is asked about, and 「沿用这些记录」
    keeps every card where it is."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    files_before = _card_files(root)
    (root / ".rce" / "project.toml").unlink()
    assert cli.main(["status", "--path", str(root)]) == 1
    project_identity.adopt(root)
    assert _card_files(root) == files_before and _card(root).in_use == 1


def test_an_entry_whose_copy_is_missing_still_detects_an_edit_but_cannot_restore(tmp_path):
    """9.11: an entry whose frozen copy is missing on read (a sync that has
    not delivered it) is shown as 「确认记录引用的副本缺失」; the hash in the
    entry still detects an edit, but the version cannot be restored from it."""
    root = tmp_path / "p"
    _project(root)
    _new_confirmed(root)
    card = _card(root)
    card.versions[1].frozen_path.unlink()
    card = _card(root)
    assert card.versions[1].copy_missing
    assert cards.card_payload(root, card)["versions"][0]["copy_missing"] == "确认记录引用的副本缺失"
    _write(root, "topicshift", 1, _text(formula="edited"))
    assert _card(root).questions == [1]
    with pytest.raises(cards.CardRefused) as err:
        cards.answer_edited(root, "topicshift", "new")
    assert err.value.code == "copy_missing" and err.value.message_zh == "确认记录引用的副本缺失"
    assert (V.variables_dir(root) / "topicshift" / "v1.toml").read_text(encoding="utf-8") == _text(formula="edited")
    assert not (V.variables_dir(root) / "topicshift" / "v2.toml").exists()


# -- 9.12 (acceptance, 2026-10-05): two histories settled by naming what stands ------------------


TEXT_A = _text(formula="TS_t = 1 − cos(p_t, p_{t−2})")
TEXT_B = _text(formula="TS_t = JS(p_t, p_{t−1})")


def _merged_two_v2(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Two copies of a card each confirming a different v2, merged the way
    a sync merges them: the logs concatenated (seq 2 twice), both frozen
    copies and code copies present, and v2.toml as copy A wrote it.
    Returns (root, v1's entry id, A's v2 entry id, B's v2 entry id)."""
    root = tmp_path / "p"
    _project(root)
    v1 = _new_confirmed(root).entry.id
    cards.revise(root, "topicshift")
    other = tmp_path / "other"
    shutil.copytree(root, other)
    project_identity.fork(other)
    _write(root, "topicshift", 2, TEXT_A)
    a = cards.confirm(root, "topicshift", attested="yes").entry.id
    _write(other, "topicshift", 2, TEXT_B)
    b = cards.confirm(other, "topicshift", attested="no").entry.id
    mine, theirs = (V.variables_dir(r) / "topicshift" for r in (root, other))
    tail = (theirs / "log.toml").read_text(encoding="utf-8").split("\n[[entry]]")[-1]
    (mine / "log.toml").write_text((mine / "log.toml").read_text(encoding="utf-8") + "\n[[entry]]" + tail, encoding="utf-8")
    for sub in ("frozen",):
        for f in (theirs / sub).iterdir():
            if not (mine / sub / f.name).exists():
                shutil.copy2(f, mine / sub / f.name)
    for f in V.code_dir(other).iterdir():
        if not (V.code_dir(root) / f.name).exists():
            shutil.copy2(f, V.code_dir(root) / f.name)
    return root, v1, a, b


def test_two_merged_v2_confirmations_freeze_the_card_and_ask_which_stands(tmp_path, capsys):
    root, v1, a, b = _merged_two_v2(tmp_path)
    card = _card(root)
    assert card.state == "frozen" and card.reason == "conflict" and card.in_use is None
    d = cards.dispute_payload(card)
    assert [[e["id"] for e in br] for br in d["branches"]] == [[a], [b]]
    assert [e["id"] for e in d["common"]][:1] == [v1]
    assert d["versions"] == [{"version": 2, "candidates": [a, b]}]
    assert set(d["shown"]) == {a, b}
    # Writes other than settling stay refused.
    with pytest.raises(cards.CardRefused):
        cards.abandon(root, "topicshift", note="x")
    # The CLI shows both histories with their ids and the command.
    assert cli.main(["variable", "show", "topicshift", str(root)]) == 0
    out = capsys.readouterr().out
    assert a in out and b in out and "rce variable settle topicshift --keep" in out


def test_settle_refuses_any_answer_that_does_not_name_exactly_one_per_disputed_version(tmp_path):
    root, v1, a, b = _merged_two_v2(tmp_path)
    log = V.variables_dir(root) / "topicshift" / "log.toml"
    before = log.read_bytes()
    for keeps in ([], [a, b], [v1], ["v-nope"], [a, a]):
        with pytest.raises(cards.CardRefused):
            cards.settle(root, "topicshift", keeps)
    with pytest.raises(cards.CardRefused, match="changed since"):
        cards.settle(root, "topicshift", [a], expected_shown=[a])
    assert log.read_bytes() == before  # nothing written, nothing chosen by position or time


def test_settle_keeping_the_file_s_history_puts_v2_in_use_and_marks_the_other_not_in_force(tmp_path, capsys):
    root, v1, a, b = _merged_two_v2(tmp_path)
    log = V.variables_dir(root) / "topicshift" / "log.toml"
    before = log.read_bytes()
    assert cli.main(["variable", "settle", "topicshift", "--keep", a, str(root)]) == 0
    out = capsys.readouterr().out
    assert f"v2: {a}" in out and b in out
    after = log.read_bytes()
    assert after.startswith(before)  # appended, never rewritten
    card = _card(root)
    settled = card.entries[-1]
    assert settled.get("act") == "settled" and settled.get("keeps") == [a] and b in settled.settles
    assert card.state == "ok" and card.in_use == 2 and not card.questions
    assert card.versions[1].status == "superseded" and card.versions[2].entry.id == a
    assert card.not_in_force == {b}
    payload = cards.card_payload(root, card)
    assert {h["id"]: h["in_force"] for h in payload["history"]}[b] is False
    assert {h["id"]: h["in_force"] for h in payload["history"]}[a] is True
    assert payload["dispute"] is None
    # Unfrozen: an ordinary act is written again, and needs no settles of its own to read.
    cards.abandon(root, "topicshift", note="试一下")
    assert _card(root).abandoned is not None
    assert cli.main(["records", "--verify", str(root)]) == 0
    assert _decision(root).verdict is Trust.OK


def test_settle_keeping_the_other_history_asks_the_edited_in_place_question(tmp_path):
    """A kept entry whose content hash differs from v2.toml as it stands
    raises the ordinary 「v2 的定义在确认后被改动了」 question; answering
    「另存为新版本」 puts B's frozen text back and keeps A's as draft v3."""
    root, v1, a, b = _merged_two_v2(tmp_path)
    done = cards.settle(root, "topicshift", [b], expected_shown=[a, b], via="app")
    assert done.keeps == (b,) and done.not_in_force == (a,)
    card = _card(root)
    assert card.in_use == 2 and card.versions[2].entry.id == b
    assert card.questions == [2] and card.versions[2].question_kind == "edited"
    assert cards.card_payload(root, card)["versions"][1]["content"]["construction"]["formula"] == "TS_t = JS(p_t, p_{t−1})"
    cards.answer_edited(root, "topicshift", "new", version=2)
    card = _card(root)
    assert not card.questions and card.draft == 3
    v2 = (V.variables_dir(root) / "topicshift" / "v2.toml").read_text(encoding="utf-8")
    v3 = (V.variables_dir(root) / "topicshift" / "v3.toml").read_text(encoding="utf-8")
    assert "JS(p_t" in v2 and "p_{t−2}" in v3


def test_a_second_merge_after_a_settlement_asks_again(tmp_path):
    """A settlement settles the histories it saw; a later merge of another
    history is a new conflict, asked again -- never decided by the old one."""
    root, v1, a, b = _merged_two_v2(tmp_path)
    cards.settle(root, "topicshift", [a])
    log = V.variables_dir(root) / "topicshift" / "log.toml"
    text = log.read_text(encoding="utf-8")
    blocks = text.split("\n[[entry]]")
    stray = blocks[1].replace('id = "v-', 'id = "v-9', 1)  # a third copy's seq-1 entry
    log.write_text(text + "\n[[entry]]" + stray, encoding="utf-8")
    card = _card(root)
    assert card.state == "frozen" and card.reason == "conflict"
