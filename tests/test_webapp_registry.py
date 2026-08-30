"""Tests for rce.webapp.registry (task V3 phase 1): the machine-managed
project registry at ~/.rce/projects.json.

Every test runs against a throwaway HOME (the `fake_home` fixture
monkeypatches the env var, which `Path.home()` honors on POSIX), so no test
here ever reads or writes the user's real `~/.rce/projects.json` --
`registry.registry_path()` resolves `Path.home()` per call, not at import
time, precisely to make this isolation possible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rce.webapp import registry


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _registry_file(home: Path) -> Path:
    return home / ".rce" / "projects.json"


def _mk_project(tmp_path: Path, name: str, *, initialized: bool = True) -> Path:
    project = tmp_path / name
    project.mkdir()
    if initialized:
        (project / ".rce").mkdir()
        (project / ".rce" / "graph.db").write_bytes(b"")  # existence is the check, not content
    return project


# -- registry_path / load: degradation, never a crash -------------------------


def test_registry_path_is_under_home_dot_rce(fake_home):
    assert registry.registry_path() == fake_home / ".rce" / "projects.json"


def test_load_missing_file_returns_empty(fake_home):
    assert registry.load() == []


def test_load_corrupt_json_returns_empty(fake_home):
    _registry_file(fake_home).parent.mkdir(parents=True)
    _registry_file(fake_home).write_text("{not json at all")
    assert registry.load() == []


def test_load_wrong_top_level_shape_returns_empty(fake_home):
    _registry_file(fake_home).parent.mkdir(parents=True)
    _registry_file(fake_home).write_text(json.dumps(["just", "a", "list"]))
    assert registry.load() == []
    _registry_file(fake_home).write_text(json.dumps({"projects": "not a list"}))
    assert registry.load() == []


def test_load_drops_malformed_entries_keeps_good_ones(fake_home):
    _registry_file(fake_home).parent.mkdir(parents=True)
    _registry_file(fake_home).write_text(json.dumps({"projects": [
        {"path": "/good", "label": "good"},
        {"path": 42, "label": "bad path type"},
        {"label": "no path at all"},
        "not even a dict",
        {"path": "", "label": "empty path"},
    ]}))
    assert registry.load() == [{"path": "/good", "label": "good"}]


# -- register: idempotent, MRU-first, atomic ----------------------------------


def test_register_creates_file_with_basename_label(fake_home, tmp_path):
    project = _mk_project(tmp_path, "myproj")
    registry.register(project)
    entries = registry.load()
    assert entries == [{"path": str(project.resolve()), "label": "myproj"}]
    assert _registry_file(fake_home).exists()


def test_register_is_idempotent(fake_home, tmp_path):
    project = _mk_project(tmp_path, "myproj")
    registry.register(project)
    registry.register(project)
    assert len(registry.load()) == 1


def test_register_moves_reregistered_entry_to_front(fake_home, tmp_path):
    a = _mk_project(tmp_path, "aaa")
    b = _mk_project(tmp_path, "bbb")
    registry.register(a)
    registry.register(b)
    assert [e["label"] for e in registry.load()] == ["bbb", "aaa"]
    registry.register(a)  # most-recently-served first
    assert [e["label"] for e in registry.load()] == ["aaa", "bbb"]


def test_register_preserves_a_stored_label_on_reregistration(fake_home, tmp_path):
    """The registry is machine-managed, but a stored label survives a
    recency bump -- register() only defaults the label for a NEW entry."""
    project = _mk_project(tmp_path, "myproj")
    registry.register(project)
    file = _registry_file(fake_home)
    data = json.loads(file.read_text())
    data["projects"][0]["label"] = "自定义标签"
    file.write_text(json.dumps(data, ensure_ascii=False))

    registry.register(project)

    assert registry.load() == [{"path": str(project.resolve()), "label": "自定义标签"}]


def test_register_stores_resolved_absolute_path(fake_home, tmp_path, monkeypatch):
    project = _mk_project(tmp_path, "myproj")
    monkeypatch.chdir(tmp_path)
    registry.register(Path("myproj"))  # relative on purpose
    assert registry.load()[0]["path"] == str(project.resolve())


def test_register_leaves_no_tmp_file_behind(fake_home, tmp_path):
    """The atomic-write dance (tmp + os.replace) must clean up after itself
    -- only projects.json remains in ~/.rce afterward."""
    registry.register(_mk_project(tmp_path, "myproj"))
    leftovers = sorted(p.name for p in (fake_home / ".rce").iterdir())
    assert leftovers == ["projects.json"]


def test_register_survives_a_corrupt_existing_file(fake_home, tmp_path):
    """A corrupt registry degrades to empty on load, so register() rebuilds
    it cleanly rather than crashing on the unreadable old content."""
    _registry_file(fake_home).parent.mkdir(parents=True)
    _registry_file(fake_home).write_text("{corrupt")
    project = _mk_project(tmp_path, "myproj")
    registry.register(project)
    assert registry.load() == [{"path": str(project.resolve()), "label": "myproj"}]


def test_register_refuses_to_clobber_an_unreadable_registry(fake_home, tmp_path):
    """Regression: a registry file that exists but cannot be READ (chmod
    slip, sync tool lock) used to degrade to `[]` inside register()'s
    read-modify-write, so the next `rce serve` atomically replaced the
    whole registry with a single entry -- silently discarding every other
    registered project. Now: registration is a logged no-op (the file's
    entries outrank recording one serve), `load()` still degrades to `[]`
    for readers, and the original content survives untouched."""
    file = _registry_file(fake_home)
    file.parent.mkdir(parents=True)
    original = json.dumps({"projects": [
        {"path": "/proj-b", "label": "b"},
        {"path": "/proj-c", "label": "c"},
    ]})
    file.write_text(original)
    file.chmod(0o000)
    try:
        assert registry.load() == []  # readers still degrade, never crash

        registry.register(_mk_project(tmp_path, "proj-a"))  # logged no-op
    finally:
        file.chmod(0o644)

    assert file.read_text() == original  # not one byte clobbered
    assert [e["label"] for e in registry.load()] == ["b", "c"]


# -- is_initialized ------------------------------------------------------------


def test_is_initialized_true_when_graph_db_exists(tmp_path):
    assert registry.is_initialized(_mk_project(tmp_path, "proj", initialized=True))


def test_is_initialized_false_without_graph_db(tmp_path):
    assert not registry.is_initialized(_mk_project(tmp_path, "proj", initialized=False))


def test_is_initialized_false_for_missing_directory(tmp_path):
    assert not registry.is_initialized(tmp_path / "never-created")
