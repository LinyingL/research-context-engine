import sqlite3
from pathlib import Path

import pytest

from rce import db


@pytest.fixture
def conn() -> sqlite3.Connection:
    """A fresh in-memory database with the schema fully migrated."""
    connection = db.connect(":memory:")
    db.migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def isolated_rce_home(tmp_path_factory, monkeypatch) -> Path:
    """Point `RCE_HOME` at a throwaway directory for EVERY test in the
    suite (DESIGN.md section 8.10 rule 1).

    Since the graph moved out of the project to `~/.rce/graphs/<id>/`, a
    test that runs `rce init` writes into the RCE home -- so without this,
    the suite would create (and later read) real graphs under the
    developer's own `~/.rce`, and one test's project path could collide
    with another's across runs. Autouse rather than opt-in precisely
    because forgetting it is silent: the test would still pass, against
    the wrong filesystem.

    `rce.paths.rce_home()` reads the variable on every call, so this takes
    effect for modules imported long before the fixture ran. Tests that
    need the registry in a specific place (the `fake_home` fixtures) set
    `RCE_HOME` themselves; their `monkeypatch.setenv` runs after this
    autouse one and wins.

    A sibling of `tmp_path`, never inside it (DESIGN.md 9.7): so many
    tests use `tmp_path` itself as the project root, and the project
    lock refuses an `RCE_HOME` that sits inside the project it locks.
    """
    home = tmp_path_factory.mktemp("rce-home")
    monkeypatch.setenv("RCE_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_engine_probe(monkeypatch) -> None:
    """The migration's retire step asks whether an RCE engine answers on
    the app's fixed port (DESIGN.md 9.5 step 5). No test may ever touch a
    real engine on this machine, so the probe is off (`RCE_ENGINE_PORT=0`)
    for every test; the tests of the probe itself bind their own port."""
    monkeypatch.setenv("RCE_ENGINE_PORT", "0")
