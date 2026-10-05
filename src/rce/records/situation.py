"""Which project is this folder, and may RCE write for it? (DESIGN.md 9.4)

Two questions, asked at two moments:

- **At every entry point, before anything is written** (`classify`): the
  folder's identity file, the index on this machine for that id, and the
  home the index remembers, read against the 9.4 situation table --
  NORMAL, MOVED, COPY, CANNOT_CHECK, NO_INDEX, LOST_ID, UNREADABLE_ID,
  NOT_A_PROJECT -- plus LEGACY, a folder from before V5 (no identity
  file, but a pre-V5 index that may hold its judgments). `classify`
  only reads; what each situation leads to (adopt, build, stop and ask)
  is the entry-point layer's (`rce.project`).
- **At every write, under the project lock** (`write_guard`,
  `check_still_home`): the served folder still exists, still carries the
  id the engine opened, and is still that id's home. A running engine
  holds a folder open for hours; the folder can be moved in Finder under
  it, or claimed elsewhere. A write that finds otherwise writes nothing
  (`ProjectMovedError`, state `project_moved`: 「项目已移动或已在别处认领，
  请重新打开」).

`home.json`
-----------

Beside `graph.db` in `~/.rce/graphs/<id>/`: `{canonical_path, st_dev,
st_ino, updated}`. "This folder is the home" means *the same directory*:
the same canonical path (as the file system stores it), or the same
device and inode. A change of letter case, a Unicode-normalization
variant, a symlink, or a rename in place (the inode stays) is not a move
-- it only rewrites the spelling (`Classification.respelled`). It is
written before `graph.db` is created, so an index that exists always says
where its home is; an index without one (deleted by hand) is a home that
cannot be checked, never a home assumed to be here.

"Gone is never inferred from failing to look"
---------------------------------------------

When the home is elsewhere, the old home decides between a move and a
copy, and every probe of it has a third answer. MOVED needs the old home
*confirmed* gone -- its volume mounted, the nearest existing ancestor
readable, and the folder not there -- or confirmed to carry a different
id (or none, read from a readable folder). Anything else -- an unmounted
volume, an unreadable parent, an identity file still in the cloud or
unparseable or with a conflict copy beside it, a stat error -- is
CANNOT_CHECK. That is the Section 4 error in another coat: an unmounted
disk is not a deleted folder. The probes (`Probes`) are injectable so
every branch is tested without a real second volume.

LOST_ID looks for the record files only a project with an identity can
have produced (`judgements.toml`, `.rce/canvas.json`, `.rce/variables/`).
`attempts.toml` and `mappings.toml` predate project identity: a pre-V5
project, or a folder whose researcher wrote `attempts.toml` before `rce
init`, carries them without ever having had an id, and treating them as
"an id was lost" would make every such folder un-initializable.
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import stat as stat_module
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from rce import paths
from rce.records import files
from rce.records.identity import IdentityRead, IdentityState, ProjectIdentity, read_identity
from rce.records.lock import HeldLock, project_lock

logger = logging.getLogger(__name__)

HOME_FILENAME = "home.json"

# Record files only a folder that once had an identity can hold (module
# docstring). Names inside `.rce/`.
V5_RECORD_NAMES = ("judgements.toml", "canvas.json", "variables")


class Situation(str, enum.Enum):
    NORMAL = "normal"
    MOVED = "moved"
    COPY = "copy"
    CANNOT_CHECK = "cannot_check"
    NO_INDEX = "no_index"
    LOST_ID = "lost_id"
    UNREADABLE_ID = "unreadable_id"
    NOT_A_PROJECT = "not_a_project"
    LEGACY = "legacy"


# Situations in which nothing may be written until the researcher answers.
BLOCKING = frozenset({Situation.COPY, Situation.CANNOT_CHECK, Situation.LOST_ID, Situation.UNREADABLE_ID})

# The answers each blocking situation offers (9.4, 9.12). LOST_ID has three
# (9.12): restore `project.toml` from its snapshot -- offered only when
# `.rce/backups/` holds one that reads as an identity --, 「沿用这些记录，
# 建立新身份」 (`adopt`), or 「这是另一个项目」 (`other`, for a `.rce/`
# copied in from elsewhere). UNREADABLE_ID has no command answer: the file
# must be repaired or brought back from the cloud.
ANSWERS: dict[Situation, tuple[str, ...]] = {
    Situation.COPY: ("fork", "claim", "other"),
    Situation.CANNOT_CHECK: ("fork", "claim", "other", "readonly"),
    Situation.LOST_ID: ("adopt", "other"),
    Situation.UNREADABLE_ID: (),
}
RESTORE = "restore"

# 8.8: the label of each answer, for the app (`answer_labels` in a payload).
ANSWER_LABELS = {
    "fork": "作为独立分支继续",
    "claim": "这里才是原项目",
    "other": "这是另一个项目",
    "readonly": "原位置暂时不可用，先只读打开",
    RESTORE: "从备份恢复项目身份文件",
    "adopt": "沿用这些记录，建立新身份",
}

# 8.8: what the app says, one sentence per situation.
_MESSAGES = {
    Situation.COPY: "这个文件夹是另一个文件夹中项目的副本（两处带着同一个项目身份）。请选择如何继续。",
    Situation.CANNOT_CHECK: "无法确认原位置的项目是否还在（可能所在磁盘未连接或文件仍在云端），分不清是移动还是复制。",
    Situation.LOST_ID: "项目身份文件不见了，但 .rce/ 里还有人工记录。RCE 不会悄悄给它新身份。",
    Situation.UNREADABLE_ID: "项目身份文件无法读取",
    Situation.LEGACY: "此项目来自 V5 之前的版本：可以浏览，人工记录需先迁移后才能保存。",
}


class WriteRefused(Exception):
    """A write that must not happen; nothing was written. `state` is the
    machine-readable name the app has its own sentence for."""

    state = "write_refused"


class ProjectMovedError(WriteRefused):
    """The served folder is gone, carries another id (or none), or is no
    longer its id's home. 「项目已移动或已在别处认领，请重新打开」."""

    state = "project_moved"


class NeedsMigrationError(WriteRefused):
    """A human record on a pre-V5 project (DESIGN.md 9.10: read-only for
    human records until it is migrated, so nothing new lands in the old
    store)."""

    state = "needs_migration"


# -- home.json -------------------------------------------------------------------


@dataclass(frozen=True)
class Home:
    canonical_path: str
    st_dev: int
    st_ino: int
    updated: str = ""
    # Set by `rce project claim` before it rebuilds the index from this
    # folder, cleared after: a claim killed in between must not leave the
    # folder NORMAL while it is served by the OTHER folder's index (9.4:
    # "nothing from the other folder's scans or record survives in it").
    # `rce.project.open_project` finishes such a claim first.
    claim_pending: bool = False


def home_path(project_id: str) -> Path:
    return paths.index_dir(project_id) / HOME_FILENAME


def index_db_path(project_id: str) -> Path:
    return paths.index_dir(project_id) / paths.DB_FILENAME


def read_home(project_id: str) -> Home | None:
    """What `home.json` says, or None when it is absent or not a valid
    record (both mean "where the home is cannot be said")."""
    try:
        data = json.loads(home_path(project_id).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    canonical, dev, ino = data.get("canonical_path"), data.get("st_dev"), data.get("st_ino")
    if not isinstance(canonical, str) or not canonical or not isinstance(dev, int) or not isinstance(ino, int):
        return None
    updated = data.get("updated") if isinstance(data.get("updated"), str) else ""
    return Home(canonical, dev, ino, updated, claim_pending=data.get("claim_pending") is True)


def home_of(project_root: str | Path) -> Home:
    """The home record `project_root` would have, read from the folder."""
    st = os.stat(project_root)
    return Home(
        canonical_path=paths._canonical_path(project_root),
        st_dev=st.st_dev,
        st_ino=st.st_ino,
        updated=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def write_home(project_id: str, project_root: str | Path, *, claim_pending: bool = False) -> Home:
    """Record `project_root` as the home of `project_id`'s index. The index
    directory is under `rce_home()` (created here if needed); the project
    folder must exist. Hold the project lock. `claim_pending` only from
    `rce project claim` (see `Home`)."""
    home = home_of(project_root)
    directory = paths.index_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "canonical_path": home.canonical_path, "st_dev": home.st_dev, "st_ino": home.st_ino, "updated": home.updated,
    }
    if claim_pending:
        record["claim_pending"] = True
    data = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    files.durable_write(home_path(project_id), data.encode("utf-8"))
    return home


def _same_spelling(a: str, b: str) -> bool:
    return a == b


def is_home(project_root: str | Path, home: Home, *, stat: Callable[[Any], os.stat_result] = os.stat) -> bool:
    """Whether `project_root` is the directory `home` names: the same
    canonical path, or the same (device, inode)."""
    if _same_spelling(paths._canonical_path(project_root), home.canonical_path):
        return True
    try:
        st = stat(project_root)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == (home.st_dev, home.st_ino)


def home_path_twin(
    project_root: str | Path, home: Home, project_id: str, *,
    stat: Callable[[Any], os.stat_result] = os.stat,
    read: Callable[[Any], IdentityRead] = read_identity,
) -> bool:
    """Whether the folder at `home`'s recorded PATH is another live
    directory carrying the same id, while `project_root` is the home only
    by (device, inode). That is "rename the original aside, copy it back to
    the original path": two live folders, one identity (9.4's copy), each
    matching `home.json` by one of its two criteria. Neither may be taken
    for the home silently; the one found by inode is the one asked here
    (the one at the path cannot find its twin -- nothing searches by inode
    -- and is asked when the twin claims). A path that is the same
    directory under another spelling, or holds nothing, is no twin."""
    if paths._canonical_path(project_root) == home.canonical_path:
        return False
    there = Path(home.canonical_path)
    try:
        twin = stat(there)
        mine = stat(project_root)
    except OSError:
        return False
    if (twin.st_dev, twin.st_ino) == (mine.st_dev, mine.st_ino) or not stat_module.S_ISDIR(twin.st_mode):
        return False
    got = read(there)
    return got.state is IdentityState.PRESENT and got.identity is not None and got.identity.id == project_id


# -- probes ----------------------------------------------------------------------


def default_volume_mounted(path: str | Path) -> bool:
    """Whether the volume `path` lives on is mounted. Only removable-volume
    roots can be unmounted in a way that makes a path vanish: macOS
    `/Volumes/<name>`, Linux `/media/<user>/<name>`, `/run/media/<user>/
    <name>`, `/mnt/<name>`. Everything else is on the system volume, which
    is mounted by definition while this code runs."""
    parts = Path(path).parts
    mount: Path | None = None
    if len(parts) >= 3 and parts[1] == "Volumes" and sys.platform == "darwin":
        mount = Path("/", "Volumes", parts[2])
    elif len(parts) >= 4 and parts[1] == "media":
        mount = Path("/", "media", parts[2], parts[3])
    elif len(parts) >= 5 and parts[1] == "run" and parts[2] == "media":
        mount = Path("/", "run", "media", parts[3], parts[4])
    elif len(parts) >= 3 and parts[1] == "mnt":
        mount = Path("/", "mnt", parts[2])
    if mount is None:
        return True
    return os.path.ismount(mount)


@dataclass(frozen=True)
class Probes:
    """How the old home is looked at. Injectable so every "cannot check"
    branch is tested; the defaults are the real file system."""

    stat: Callable[[Any], os.stat_result] = os.stat
    listdir: Callable[[Any], list[str]] = os.listdir
    volume_mounted: Callable[[Any], bool] = default_volume_mounted
    read_identity: Callable[[Any], IdentityRead] = read_identity


DEFAULT_PROBES = Probes()


def _norm_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


@dataclass(frozen=True)
class _OldHome:
    verdict: str  # "same_id" | "other_id" | "gone" | "unknown"
    reason: str
    other_id: str | None = None


def _probe_old_home(home: Path, project_id: str, probes: Probes) -> _OldHome:
    """Look at the old home and say what it is, never guessing (module
    docstring)."""
    try:
        mounted = probes.volume_mounted(home)
    except OSError:
        mounted = False
    if not mounted:
        return _OldHome("unknown", "volume_unmounted")
    try:
        st = probes.stat(home)
    except (FileNotFoundError, NotADirectoryError):
        return _confirm_gone(home, probes)
    except OSError:
        return _OldHome("unknown", "old_home_unreadable")
    if not stat_module.S_ISDIR(st.st_mode):
        return _OldHome("other_id", "old_home_not_a_folder")
    got = probes.read_identity(home)
    if got.state is IdentityState.PRESENT and got.identity is not None:
        if got.identity.id == project_id:
            return _OldHome("same_id", "old_home_has_this_id")
        return _OldHome("other_id", "old_home_has_another_id", got.identity.id)
    if got.state is IdentityState.ABSENT:
        # "Absent" must come from a folder that could be read, not from a
        # listing that failed.
        try:
            names = probes.listdir(home)
            if paths.RCE_DIRNAME in names:
                inner = {_norm_name(n) for n in probes.listdir(home / paths.RCE_DIRNAME)}
                if _norm_name("project.toml") in inner:
                    return _OldHome("unknown", "old_home_identity_unreadable")
        except OSError:
            return _OldHome("unknown", "old_home_unreadable")
        return _OldHome("other_id", "old_home_has_no_id")
    reason = {
        IdentityState.DATALESS: "old_home_identity_in_cloud",
        IdentityState.CONFLICT_COPY: "old_home_identity_conflict",
    }.get(got.state, "old_home_identity_unreadable")
    return _OldHome("unknown", reason)


def _confirm_gone(home: Path, probes: Probes) -> _OldHome:
    """`home` was not found. It is gone only if its nearest existing
    ancestor can be read and does not list it."""
    child, ancestor = home, home.parent
    while True:
        try:
            names = probes.listdir(ancestor)
        except (FileNotFoundError, NotADirectoryError):
            if ancestor.parent == ancestor:
                return _OldHome("unknown", "old_home_parent_unreadable")
            child, ancestor = ancestor, ancestor.parent
            continue
        except OSError:
            return _OldHome("unknown", "old_home_parent_unreadable")
        if _norm_name(child.name) in {_norm_name(n) for n in names}:
            # Listed but not stat-able: the listing and the stat disagree.
            return _OldHome("unknown", "old_home_unreadable")
        return _OldHome("gone", "old_home_gone")


# -- classify --------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    situation: Situation
    root: Path
    identity: ProjectIdentity | None = None
    identity_read: IdentityRead | None = None
    home: Home | None = None
    reason: str = ""
    detail: str = ""
    other_id: str | None = None
    respelled: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def project_id(self) -> str | None:
        return self.identity.id if self.identity is not None else None

    @property
    def blocked(self) -> bool:
        return self.situation in BLOCKING

    @property
    def needs_migration(self) -> bool:
        """Human records refused: a pre-V5 folder, or one whose migration
        has not finished (`migrating_from`, 9.5 step 3)."""
        if self.situation is Situation.LEGACY:
            return True
        return self.identity is not None and self.identity.migrating_from is not None

    @property
    def answers(self) -> tuple[str, ...]:
        found = ANSWERS.get(self.situation, ())
        if self.situation is Situation.LOST_ID and self.extra.get("snapshot"):
            return (RESTORE, *found)
        return found

    @property
    def message(self) -> str:
        return _MESSAGES.get(self.situation, "")

    def payload(self) -> dict[str, Any]:
        """Machine-readable, for the app (and `--json` consumers)."""
        return {
            "situation": self.situation.value,
            "path": str(self.root),
            "project_id": self.project_id,
            "forked_from": self.identity.forked_from if self.identity else None,
            "home": self.home.canonical_path if self.home else None,
            "reason": self.reason,
            "detail": self.detail,
            "other_id": self.other_id,
            "answers": list(self.answers),
            "answer_labels": {a: ANSWER_LABELS[a] for a in self.answers if a in ANSWER_LABELS},
            "message": self.message,
            "blocked": self.blocked,
            "needs_migration": self.needs_migration,
            **self.extra,
        }


def _v5_records_present(project_root: Path) -> list[str]:
    rce_dir = paths.project_rce_dir(project_root)
    found = []
    for name in V5_RECORD_NAMES:
        if os.path.lexists(rce_dir / name):
            found.append(name)
    for copy in files.conflict_copies(rce_dir / "judgements.toml"):
        found.append(copy.name)
    return found


def _identity_snapshot(project_root: Path) -> dict[str, Any] | None:
    """The snapshot 「从备份恢复项目身份文件」 would restore (9.12): the
    newest one in `.rce/backups/` that reads as an identity, with what it
    says and what this machine knows of that id -- so the researcher sees
    which identity comes back before choosing it. None when there is none.
    Reads only."""
    from rce.records.identity import identity_snapshots  # noqa: PLC0415

    try:
        found = identity_snapshots(project_root)
    except (OSError, files.RecordFileError):
        return None
    if not found:
        return None
    snap, ident = found[0]
    home = read_home(ident.id) if index_db_path(ident.id).exists() else None
    return {
        "file": snap.relative_to(project_root).as_posix() if snap.is_relative_to(project_root) else str(snap),
        "project_id": ident.id,
        "created": ident.created,
        "forked_from": ident.forked_from,
        "migrating_from": ident.migrating_from,
        "index_on_this_machine": index_db_path(ident.id).exists(),
        "index_home": home.canonical_path if home is not None else None,
    }


def classify(project_root: str | Path, *, probes: Probes | None = None) -> Classification:
    """The 9.4 situation of the existing folder `project_root`. Reads only
    -- never writes, never requests anything but a cloud download of the
    identity file. Raises `NotADirectoryError` for a path that is not an
    existing folder (the callers say so in their own words first)."""
    probes = probes or DEFAULT_PROBES
    root = Path(project_root)
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not an existing folder")
    got = read_identity(root)
    if got.state is IdentityState.ABSENT:
        records = _v5_records_present(root)
        if records:
            return Classification(
                Situation.LOST_ID, root, identity_read=got, reason="records_without_identity",
                detail=f".rce/ holds {', '.join(records)} but no project.toml",
                extra={"records": records, "snapshot": _identity_snapshot(root)},
            )
        if paths.has_legacy_index(root):
            return Classification(
                Situation.LEGACY, root, identity_read=got, reason="pre_v5_index",
                detail="a pre-V5 index exists for this folder's path",
            )
        return Classification(Situation.NOT_A_PROJECT, root, identity_read=got, reason="no_identity")
    if got.state is not IdentityState.PRESENT or got.identity is None:
        reason = {
            IdentityState.DATALESS: "identity_in_cloud",
            IdentityState.CONFLICT_COPY: "identity_conflict_copy",
        }.get(got.state, "identity_unparseable")
        extra = {"conflict_copies": [p.name for p in got.conflict_copies]} if got.conflict_copies else {}
        if got.line is not None:
            extra["line"] = got.line
        return Classification(
            Situation.UNREADABLE_ID, root, identity=None, identity_read=got, reason=reason,
            detail=got.error or "", extra=extra,
        )

    ident = got.identity
    if not index_db_path(ident.id).exists():
        return Classification(Situation.NO_INDEX, root, identity=ident, identity_read=got, reason="no_index")
    home = read_home(ident.id)
    if home is None:
        return Classification(
            Situation.CANNOT_CHECK, root, identity=ident, identity_read=got, reason="no_home_record",
            detail=f"the index for {ident.id} does not say which folder is its home",
        )
    if is_home(root, home, stat=probes.stat):
        if home_path_twin(root, home, ident.id, stat=probes.stat, read=probes.read_identity):
            return Classification(
                Situation.COPY, root, identity=ident, identity_read=got, home=home, reason="home_path_holds_a_copy",
                detail=f"{home.canonical_path} still carries {ident.id} (this folder is the same directory "
                       f"the home record names by inode, but another folder now sits at its path)",
            )
        respelled = paths._canonical_path(root) != home.canonical_path
        return Classification(Situation.NORMAL, root, identity=ident, identity_read=got, home=home, respelled=respelled)
    old = _probe_old_home(Path(home.canonical_path), ident.id, probes)
    if old.verdict == "same_id":
        return Classification(
            Situation.COPY, root, identity=ident, identity_read=got, home=home, reason=old.reason,
            detail=f"{home.canonical_path} still carries {ident.id}",
        )
    if old.verdict in ("gone", "other_id"):
        return Classification(
            Situation.MOVED, root, identity=ident, identity_read=got, home=home, reason=old.reason,
            other_id=old.other_id, detail=f"the old home {home.canonical_path} is confirmed not to be this project",
        )
    return Classification(
        Situation.CANNOT_CHECK, root, identity=ident, identity_read=got, home=home, reason=old.reason,
        detail=f"the old home {home.canonical_path} cannot be checked ({old.reason})",
    )


# -- the write-time re-check -----------------------------------------------------

READ_NOW: Any = object()  # sentinel: take the expected id from the folder as it is now


def check_still_home(project_root: str | Path, expected_id: str | None) -> ProjectIdentity | None:
    """Raise `ProjectMovedError` unless `project_root` still exists, still
    carries `expected_id` (None: still carries no identity -- a pre-V5 or
    not-yet-initialized folder), and, for an id, is still that id's home.
    Call under the project lock. Returns the identity read."""
    root = Path(project_root)
    if not root.is_dir():
        raise ProjectMovedError(f"{root} no longer exists; the project was moved or removed -- nothing written")
    got = read_identity(root)
    if expected_id is None:
        if got.state is not IdentityState.ABSENT:
            raise ProjectMovedError(f"{root} now carries a project identity it did not have when opened -- nothing written")
        return None
    if got.state is not IdentityState.PRESENT or got.identity is None or got.identity.id != expected_id:
        raise ProjectMovedError(f"{root} no longer carries the project {expected_id} -- nothing written")
    home = read_home(expected_id)
    if home is None or not is_home(root, home) or home_path_twin(root, home, expected_id):
        raise ProjectMovedError(
            f"{root} is no longer the home of {expected_id} (it was claimed or opened elsewhere) -- nothing written"
        )
    return got.identity


@contextlib.contextmanager
def write_guard(
    project_root: str | Path,
    expected_id: Any = READ_NOW,
    *,
    human: bool = False,
    timeout: float | None = None,
) -> Iterator[HeldLock]:
    """Hold the project lock and re-check identity (9.4) for one write to a
    record file or to the index. `human=True` additionally refuses a
    pre-V5 project -- no id, but a pre-V5 index that may hold its
    judgments (`NeedsMigrationError`): nothing new lands in the old store
    before it is migrated. A folder with neither (never indexed) has no
    store to protect; its own files may still be written.

    `expected_id` is the id the caller opened the project with; left out,
    it is read from the folder now (for writers called outside a serving
    engine, which re-check only that the folder is its id's home). An
    identity file that cannot be read refuses the write."""
    root = Path(project_root)
    if expected_id is READ_NOW:
        got = read_identity(root) if root.is_dir() else None
        if got is None:
            raise ProjectMovedError(f"{root} no longer exists -- nothing written")
        if got.state is IdentityState.ABSENT:
            expected_id = None
        elif got.state is IdentityState.PRESENT and got.identity is not None:
            expected_id = got.identity.id
        else:
            raise ProjectMovedError(f"the identity file of {root} cannot be read ({got.state.value}) -- nothing written")
    if human and expected_id is None and paths.has_legacy_index(root):
        raise NeedsMigrationError(
            f"{root} was indexed before V5: it is read-only for human records until it is migrated "
            f"(`rce migrate`) -- nothing written"
        )
    with project_lock(root, expected_id, timeout=timeout) as held:
        if expected_id is not None and root.is_dir():
            # A folder whose migration has not finished (9.5): no human
            # record, and -- until its new index is installed -- no index
            # write either (the old index keeps serving, read-only; the
            # migration itself is the one writer). Said as "migrating",
            # never as "moved", although no home may be recorded yet.
            got = read_identity(root)
            mid = got.identity
            if mid is not None and mid.id == expected_id and mid.migrating_from is not None:
                if human or not index_db_path(expected_id).exists():
                    raise NeedsMigrationError(
                        f"the migration of {root} has not finished ('rce migrate' resumes it) -- nothing written"
                    )
        ident = check_still_home(root, expected_id)
        if human and ident is not None and ident.migrating_from is not None:
            raise NeedsMigrationError(
                f"the migration of {root} has not finished; human records wait for it -- nothing written"
            )
        yield held
