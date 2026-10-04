"""Tests for rce.records.lock (DESIGN.md section 9.7): the cross-process
project lock. The two-process test is the point -- every pre-V5 lock was
in-process, which is how 600 concurrent writes lost 279 (9.0)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from rce import paths
from rce.records import lock

PID = "p-" + "ab" * 16


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project folder beside (not containing) the isolated RCE home."""
    root = tmp_path / "proj"
    root.mkdir()
    return root


def test_key_is_the_project_id_or_the_canonical_path_hash(tmp_path):
    assert lock.lock_key(tmp_path, PID) == PID
    assert lock.lock_key(tmp_path) == "path-" + paths.project_graph_id(tmp_path)


def test_malformed_ids_are_refused_not_sanitized(tmp_path):
    for bad in ("p-../../etc", "p-ABC", "x" * 34, "", "p-" + "0" * 31):
        with pytest.raises(lock.ProjectLockError):
            lock.lock_key(tmp_path, bad)


def test_lock_file_lives_under_rce_home_never_in_the_project(tmp_path, isolated_rce_home):
    project = tmp_path / "proj"
    project.mkdir()
    with lock.project_lock(project, PID) as held:
        assert held.path == isolated_rce_home / "locks" / f"{PID}.lock"
        assert held.is_held()
    assert not (project / ".rce").exists()


def test_rce_home_inside_the_project_is_refused(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("RCE_HOME", str(project / "home"))
    with pytest.raises(lock.ProjectLockError):
        with lock.project_lock(project, PID):
            pass


def test_same_folder_different_spelling_same_lock(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(project)
    assert lock.lock_key(project) == lock.lock_key(link)


def test_reentrant_within_a_thread(project, tmp_path):
    with lock.project_lock(project, PID) as outer:
        with lock.project_lock(project, PID) as inner:
            assert inner.is_held()
        assert outer.is_held()
    assert not outer.is_held()


def test_other_threads_wait(project, tmp_path):
    order: list[str] = []
    started = threading.Event()

    def other() -> None:
        started.set()
        with lock.project_lock(project, PID):
            order.append("other")

    with lock.project_lock(project, PID):
        t = threading.Thread(target=other)
        t.start()
        started.wait()
        time.sleep(0.1)
        order.append("main")
    t.join(5)
    assert order == ["main", "other"]


def test_held_lock_is_not_held_from_another_thread(project, tmp_path):
    seen: list[object] = []

    def probe(held: lock.HeldLock) -> None:
        seen.append(held.is_held())
        try:
            held.require_held()
        except lock.ProjectLockError as exc:
            seen.append(exc)

    with lock.project_lock(project, PID) as held:
        t = threading.Thread(target=probe, args=(held,))
        t.start()
        t.join()
    assert seen[0] is False
    assert isinstance(seen[1], lock.ProjectLockError)
    with pytest.raises(lock.ProjectLockError):
        held.require_held()  # released after the block


def test_timeout_raises_instead_of_proceeding(project, tmp_path):
    child = _spawn_holder(project, hold_s=3)
    try:
        with pytest.raises(lock.ProjectLockTimeout):
            with lock.project_lock(project, PID, timeout=0.2):
                pytest.fail("entered while another process held the lock")
    finally:
        child.wait(10)


def test_flock_failure_raises(project, monkeypatch):
    import fcntl

    def broken(fd, op):
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(fcntl, "flock", broken)
    with pytest.raises(lock.ProjectLockError):
        with lock.project_lock(project, PID):
            pytest.fail("proceeded unlocked")
    # the in-process state was released: a later, working attempt succeeds
    monkeypatch.undo()
    with lock.project_lock(project, PID) as held:
        assert held.is_held()


def _spawn_holder(project: Path, hold_s: float) -> subprocess.Popen:
    code = (
        "import sys, time\n"
        "from rce.records import lock\n"
        f"with lock.project_lock({str(project)!r}, {PID!r}):\n"
        "    print('held', flush=True)\n"
        f"    time.sleep({hold_s})\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, env=os.environ.copy())
    assert proc.stdout.readline().strip() == b"held"
    return proc


_COUNTER_CHILD = """
import sys, time
from pathlib import Path
from rce.records import lock
project, counter, n = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
for _ in range(n):
    with lock.project_lock(project, {pid!r}):
        value = int(counter.read_text())
        time.sleep(0.0005)
        counter.write_text(str(value + 1))
"""


def test_two_real_processes_exclude_each_other(project, tmp_path):
    """9.9 scenario 10's precondition: read-modify-write of one file from
    two processes loses nothing when both hold the project lock."""
    counter = tmp_path / "counter"
    counter.write_text("0")
    code = _COUNTER_CHILD.format(pid=PID)
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(project), str(counter), "150"], env=os.environ.copy())
        for _ in range(2)
    ]
    for p in procs:
        assert p.wait(60) == 0
    assert counter.read_text() == "300"
