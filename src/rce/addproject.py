"""Adding a project, and rescanning one (DESIGN.md Section 10, task V6).

Three acts, each the engine half of what the app's project menu and `rce
projects add` do:

`inspect(path)` -- **look before writing** (10.2 step 2). Says what the
folder is: already in the list, an RCE project (with its 9.4 situation and
answers exactly as `rce.records.situation.classify` reports them), one from
before V5, a folder RCE has not seen (with the preview's counts), something
that cannot be a project (a refusal, one Chinese sentence), or -- when the
listing has not come back within the deadline because macOS is holding it
behind a permission prompt -- `waiting_permission`. It writes nothing
anywhere. Of the folder it reads only directory listings, `lstat`, and git
(`git ls-files`, counted); the one file it reads is RCE's own identity file
`.rce/project.toml` (and, as the identity check needs them, its snapshots
in `.rce/backups/` and the identity file of a moved project's old home),
and none of these when it is still in the cloud: the identity check runs
inside `paths.downloads_suppressed()`, so it is reported as 9.4 reports
it, without the download `classify` would ask for. Git runs hardened
(`rce.ingest.git.HARDENING`): no command the folder's `.git/config` names
is executed. It never follows a symlink out of the folder: the chosen
path itself is resolved, symlinks followed (10.6), the walk below it
follows none, and a `.rce` that is a symlink is refused before anything
reads through it.

The preview's counts are the scan's: the git inventory
(`rce.ingest.git.iter_tracked`) when git knows the folder, the filesystem
walk (`rce.ingest.files.iter_files`) when it does not -- the same
generators `list_source_files` builds the scan's inventory from, decided
the same way the scan decides (git's "not a git repository" -> the walk).

The answer carries an `inspected` token: an HMAC, with a key that lives
only in this process, over what was seen (the kind, the resolved folder,
the identity and situation, the counts, the refusal). It only has to
detect change, but it cannot be computed from the path alone.

`add(path, label=..., inspected=...)` -- **write** (10.2 step 3). Inspects
again and refuses (`inspected_changed`) when the folder is no longer what
was inspected. Then, per kind: already in the list -> nothing written; an
RCE project that opens (normal, moved, no index here) -> the identity
check's own open, then registered with the display name; one whose
situation needs an answer -> NOTHING written, the registry included (the
engine serves it blocked and the answer goes through `POST
/api/project/resolve`); a pre-V5 project -> registered; a new folder ->
`rce init`'s own creation (`.rce/project.toml` exclusively, under the
path-keyed project lock, the index under the new id, the README), then
registered. Never the folder itself. The folder written into is pinned
by its (device, inode) at the inspection and checked again under the
project lock just before the identity file is created: a folder swapped
for a symlink in between is refused, nothing written.

`start_scan(root)` / `rescan(root)` -- **the full scan** (10.3): the same
calls `rce ingest` (every source extractor), `rce attempts --check` (when
`.rce/attempts.toml` configures a table), `rce mappings` and the record's
application make, in that order, under the project lock with the identity
re-checked, reporting `(step, n, m)` to a callback. Refused for a pre-V5
project (frozen until migrated, 9.12), and while a scan of the same project
runs -- in this process (a guard set) or in another (the project lock,
waited for only briefly).
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import logging
import os
import stat as stat_module
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from rce import cloud, consistency, db, paths
from rce import project as project_identity
from rce.ingest import attempts as attempts_ingest
from rce.ingest import files as files_ingest
from rce.ingest import git as git_ingest
from rce.ingest import mappings as mappings_ingest
from rce.ingest import pipeline
from rce.ingest import scan as scan_mod
from rce.records import identity as identity_mod
from rce.records import judgements
from rce.records import lock as records_lock
from rce.records import situation as records_situation
from rce.records.situation import Classification, Situation
from rce.webapp import registry as project_registry

logger = logging.getLogger(__name__)

# -- kinds, limits ----------------------------------------------------------------

ALREADY_REGISTERED = "already_registered"
RCE_PROJECT = "rce_project"
PRE_V5 = "pre_v5"
NEW_FOLDER = "new_folder"
REFUSED = "refused"
WAITING_PERMISSION = "waiting_permission"

DEFAULT_DEADLINE_S = 5.0
ENTRY_CAP = 100_000  # 10.6: stop counting at 100,000 entries, and say so
LARGE_THRESHOLD = 5_000  # 10.2: above this many files to scan, ask twice
# A listing that finished after its caller stopped waiting is handed to the
# next caller (the page asking again) if it is at most this old.
_RESULT_FRESH_S = 30.0

# The situations in which the identity check opens the folder (9.4), so that
# adding it is 「加入列表并打开」.
OPENABLE = frozenset({Situation.NORMAL, Situation.MOVED, Situation.NO_INDEX})

# 8.8: the app's sentence for each kind and refusal.
KIND_MESSAGES = {
    ALREADY_REGISTERED: "这个项目已经在列表里",
    RCE_PROJECT: "这是一个 RCE 项目，可以加入列表并打开",
    PRE_V5: "这是旧版 RCE 项目：加入后需要先迁移，才能记录判断",
    NEW_FOLDER: "RCE 还没有读过这个文件夹",
    WAITING_PERMISSION: "正在等待系统授权访问这个文件夹……如果系统询问，请点“允许”",
}
TOP_LEVEL_MESSAGE = "请选择具体的项目文件夹，而不是「文稿」这样的总文件夹"
REFUSAL_MESSAGES = {
    "missing": "这个文件夹不存在",
    "not_a_folder": "这不是一个文件夹",
    "unreadable": "无法读取这个文件夹",
    "top_level": TOP_LEVEL_MESSAGE,
    "system_folder": TOP_LEVEL_MESSAGE,
    "rce_home": "这是 RCE 自己保存数据的文件夹，不能作为项目",
    "inside_project": "这个文件夹在项目「{label}」里面",
    "contains_project": "这个文件夹里已经有项目「{label}」",
    "rce_link": "这个文件夹里的 .rce 是指向别处的链接，RCE 不会通过它读写",
}

# The preview's groups (10.2) over the inventory's categories.
GROUPS = {
    "scripts": ("py", "r", "rmd"),
    "data": ("data",),
    "drafts": ("md", "tex", "bib"),
    "images": ("image",),
}
CATEGORIES = ("tex", "bib", "image", "py", "md", "r", "rmd", "data")


# -- the inspection ----------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str  # Chinese, one sentence
    detail: str = ""  # English, for 「详情」
    project_label: str | None = None


@dataclass(frozen=True)
class Preview:
    """What the first scan would read (10.2 "The preview")."""

    source: str  # "git" | "walk"
    inventory: dict[str, int]  # per inventory category
    other: int  # listed files no extractor reads
    dataless: int  # files to scan still in the cloud
    truncated: bool  # stopped counting at ENTRY_CAP
    tracked: int | None = None  # git: tracked files present in the folder
    untracked: int | None = None  # git: untracked, not ignored -- not read
    untracked_truncated: bool = False
    git_error: str | None = None
    # 11.1: the synced folder holding it, its client, and -- when files are
    # only in the cloud and the client is not running -- the sentence
    cloud: dict[str, Any] | None = None

    @property
    def counts(self) -> dict[str, int]:
        grouped = {name: sum(self.inventory[c] for c in cats) for name, cats in GROUPS.items()}
        grouped["other"] = self.other
        return grouped

    @property
    def to_scan(self) -> int:
        return sum(self.inventory.values())

    @property
    def large(self) -> bool:
        return self.to_scan > LARGE_THRESHOLD

    def payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "counts": self.counts,
            "inventory": dict(self.inventory),
            "to_scan": self.to_scan,
            "large": self.large,
            "dataless": self.dataless,
            "truncated": self.truncated,
            "git": None if self.source != "git" else {
                "tracked": self.tracked,
                "untracked": self.untracked,
                "untracked_truncated": self.untracked_truncated,
                "error": self.git_error,
            },
            "cloud": self.cloud,
        }


@dataclass(frozen=True)
class Inspection:
    kind: str
    requested: str
    root: str | None = None  # the resolved folder, as the file system spells it
    name: str | None = None  # the folder's name: the default display name
    classification: Classification | None = None
    entry: dict[str, Any] | None = None  # already_registered: the registry entry
    preview: Preview | None = None
    refusal: Refusal | None = None
    # pre_v5: opening it moves its in-project `.rce/graph.db` out (8.10)
    moves_graph: bool = False
    # the folder's (st_dev, st_ino) when it was inspected (10.6: the folder
    # checked is the folder written into)
    pin: tuple[int, int] | None = None
    token: str = ""

    @property
    def project_id(self) -> str | None:
        if self.classification is not None:
            return self.classification.project_id
        if self.entry is not None:
            return self.entry.get("id")
        return None

    @property
    def can_add(self) -> bool:
        """Whether 「加入…」 is offered: everything but a refusal, a wait,
        an entry already in the list, and a situation to answer first."""
        if self.kind in (NEW_FOLDER, PRE_V5):
            return True
        if self.kind == RCE_PROJECT and self.classification is not None:
            return self.classification.situation in OPENABLE
        return False

    @property
    def message(self) -> str:
        if self.refusal is not None:
            return self.refusal.message
        if self.kind == RCE_PROJECT and self.classification is not None and not self.can_add:
            return self.classification.message or KIND_MESSAGES[RCE_PROJECT]
        return KIND_MESSAGES.get(self.kind, "")

    def payload(self) -> dict[str, Any]:
        """Machine-readable, for the page and `--json` consumers."""
        return {
            "kind": self.kind,
            "path": self.requested,
            "root": self.root,
            "name": self.name,
            "label": self.name,
            "message": self.message,
            "can_add": self.can_add,
            "project_id": self.project_id,
            "situation": self.classification.payload() if self.classification is not None else None,
            "entry": self.entry,
            "preview": self.preview.payload() if self.preview is not None else None,
            "refusal": None if self.refusal is None else {
                "code": self.refusal.code,
                "message": self.refusal.message,
                "detail": self.refusal.detail,
                "project_label": self.refusal.project_label,
            },
            "writes": _writes(self),
            "moves_graph": self.moves_graph,
            "inspected": self.token,
        }


IDENTITY_SNAPSHOT = ".rce/backups/project.toml.<time>.toml"


def _writes(insp: Inspection) -> list[str] | None:
    """What adding writes (10.2: "It says what adding writes"), opening
    included: a new folder's identity file, its 9.12 snapshot and README;
    a pre-V5 folder whose in-project graph opening moves out (8.10) loses
    `.rce/graph.db` and gains the README saying where it went."""
    if insp.kind == NEW_FOLDER:
        return [
            ".rce/project.toml", IDENTITY_SNAPSHOT, ".rce/README",
            str(paths.rce_home() / paths.GRAPHS_DIRNAME / "<id>"),
        ]
    if insp.kind == PRE_V5 and insp.moves_graph:
        return [
            str(project_registry.registry_path()), ".rce/README",
            f".rce/{paths.DB_FILENAME} -> {paths.rce_home() / paths.GRAPHS_DIRNAME / '<path hash>'}",
        ]
    if insp.kind in (PRE_V5, RCE_PROJECT) and insp.can_add:
        return [str(project_registry.registry_path())]
    return None


# A key that exists only in this process: the token detects a change, and
# cannot be made up from the path alone.
_TOKEN_KEY = os.urandom(32)


def _token(insp: Inspection) -> str:
    c = insp.classification
    seen = {
        "kind": insp.kind,
        "root": insp.root,
        "project_id": insp.project_id,
        "situation": c.situation.value if c is not None else None,
        "reason": c.reason if c is not None else None,
        "entry": insp.entry,
        # The client's state is not the folder's: starting OneDrive between
        # looking and adding does not make the folder another one.
        "preview": _sealed_preview(insp.preview),
        "refusal": insp.refusal.code if insp.refusal is not None else None,
        "refusal_label": insp.refusal.project_label if insp.refusal is not None else None,
        "moves_graph": insp.moves_graph,
        "pin": list(insp.pin) if insp.pin is not None else None,
    }
    data = json.dumps(seen, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hmac.new(_TOKEN_KEY, data, hashlib.sha256).hexdigest()


def _sealed_preview(preview: Preview | None) -> dict[str, Any] | None:
    if preview is None:
        return None
    seen = preview.payload()
    seen.pop("cloud", None)
    return seen


def _sealed(insp: Inspection) -> Inspection:
    return Inspection(**{**insp.__dict__, "token": _token(insp)})


def _refused(requested: str, code: str, *, root: str | None = None, detail: str = "", label: str | None = None) -> Inspection:
    message = REFUSAL_MESSAGES[code].format(label=label or "")
    name = Path(root).name if root else None
    return _sealed(Inspection(REFUSED, requested, root=root, name=name, refusal=Refusal(code, message, detail, label)))


# -- where a project may not be ----------------------------------------------------


def _canonical_if_exists(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
    except OSError:
        return None
    return paths._canonical_path(path)


def _top_level_folders() -> set[str]:
    """The folders refused when chosen EXACTLY (10.2): a top-level folder
    rather than a project. Canonical spellings of those that exist."""
    home = Path.home()
    mobile = home / "Library" / "Mobile Documents"
    candidates = [
        Path("/"), home, home / "Documents", home / "Desktop", home / "Downloads", home / "Library",
        mobile, mobile / "com~apple~CloudDocs",
        Path("/Users"), Path("/private"), Path("/tmp"), Path("/var"), Path("/private/tmp"),
        Path("/private/var"), Path("/Volumes"), Path("/home"),
    ]
    found = {_canonical_if_exists(p) for p in candidates}
    found.add("/")
    return {f for f in found if f}


_SYSTEM_FOLDERS = ("/System", "/Library", "/Applications", "/usr", "/bin", "/sbin", "/etc")


def _system_folders() -> list[str]:
    """Folders refused by containment (10.2): anything inside them."""
    found = set(_SYSTEM_FOLDERS)
    for name in _SYSTEM_FOLDERS:
        canonical = _canonical_if_exists(Path(name))
        if canonical:
            found.add(canonical)
    return sorted(found)


def _inside(child: str, parent: str) -> bool:
    """`child` is `parent` or below it (canonical strings)."""
    return Path(child).is_relative_to(Path(parent))


def _volume_root(canonical: str) -> bool:
    parts = Path(canonical).parts
    return len(parts) == 3 and parts[1] == "Volumes"


def _location_refusal(requested: str, canonical: str) -> Inspection | None:
    if canonical in _top_level_folders() or _volume_root(canonical) or cloud.is_top_level(canonical):
        return _refused(requested, "top_level", root=canonical, detail=f"{canonical} is a top-level folder, not a project")
    for system in _system_folders():
        if _inside(canonical, system):
            return _refused(requested, "system_folder", root=canonical, detail=f"{canonical} is inside {system}")
    home = paths.rce_home()
    home_canonical = _canonical_if_exists(home) or str(home.resolve())
    if _inside(canonical, home_canonical):
        return _refused(requested, "rce_home", root=canonical, detail=f"{canonical} is RCE's own folder ({home_canonical})")
    if _inside(home_canonical, canonical):
        # Spec-silent: the index would sit inside the project, which the
        # project lock refuses outright (9.7); said the same way.
        return _refused(requested, "rce_home", root=canonical, detail=f"{canonical} contains RCE's own folder ({home_canonical})")
    return None


# -- the listing --------------------------------------------------------------------


def _is_dataless(full: Path) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        st = os.lstat(full)
    except OSError:
        return False
    return bool(getattr(st, "st_flags", 0) & paths.SF_DATALESS)


def _preview(root: Path) -> Preview:
    """The counts, from the scan's own inventory (module docstring)."""
    source = "git"
    git_error: str | None = None
    try:
        listed = list(itertools.islice(git_ingest.iter_tracked(root), ENTRY_CAP + 1))
    except git_ingest.NotAGitRepositoryError:
        source = "walk"
        listed = list(itertools.islice(files_ingest.iter_files(root), ENTRY_CAP + 1))
    except git_ingest.GitIngestError as exc:
        # The scan itself would stop here (`IngestFailed`); said, not hidden.
        listed, git_error = [], str(exc)
    truncated = len(listed) > ENTRY_CAP
    listed = listed[:ENTRY_CAP]
    inventory = {c: 0 for c in CATEGORIES}
    other = dataless = 0
    for rel, category in listed:
        if category is None:
            other += 1
            continue
        inventory[category] += 1
        if _is_dataless(root / rel):
            dataless += 1
    tracked = untracked = None
    untracked_truncated = False
    if source == "git" and git_error is None:
        tracked = len(listed)
        try:
            untracked, untracked_truncated = git_ingest.count_untracked(root, limit=ENTRY_CAP)
        except git_ingest.GitIngestError as exc:
            git_error = str(exc)
    return Preview(
        source=source, inventory=inventory, other=other, dataless=dataless, truncated=truncated,
        tracked=tracked, untracked=untracked, untracked_truncated=untracked_truncated, git_error=git_error,
        cloud=_cloud_state(root, dataless),
    )


def _cloud_state(root: Path, dataless: int) -> dict[str, Any] | None:
    """11.1: which provider holds the folder, whether its client runs, and
    -- files only in the cloud, the client not running -- that those files
    are not read, in 11.1's sentence. None outside a synced folder."""
    prov = cloud.provider(root)
    if prov is None:
        return None
    running = cloud.client_running(prov.kind)
    blocked = dataless if running is not True else 0
    return {
        **prov.payload(),
        "client_running": running,
        "blocked": blocked,
        "message": cloud.message(prov) if blocked else None,
    }


def _identity_in_cloud(root: Path) -> bool:
    """`.rce/project.toml` is still in the cloud: `classify` would ask for
    its download, which inspecting must not (module docstring)."""
    path = identity_mod.identity_path(root)
    placeholder = path.parent / f".{path.name}.icloud"
    try:
        return os.path.lexists(placeholder) or (os.path.lexists(path) and _is_dataless(path))
    except OSError:
        return False


def _classify(root: Path) -> Classification:
    if _identity_in_cloud(root):
        # What classify reports for it (9.4: 「项目身份文件无法读取」),
        # without the download request.
        return Classification(
            Situation.UNREADABLE_ID, root, reason="identity_in_cloud", detail="the file is in the cloud",
        )
    try:
        with paths.downloads_suppressed():
            return records_situation.classify(root)
    except identity_mod.SnapshotInCloud as exc:
        # No project.toml, and a snapshot of one still in the cloud: once an
        # identity (9.12), so never a new folder; which one cannot be said
        # without the download. Unreadable, like the file itself in the cloud.
        return Classification(
            Situation.UNREADABLE_ID, root, reason="identity_snapshot_in_cloud",
            detail=f"no project.toml, and a snapshot of one is in the cloud ({exc})",
        )


def _rce_link(root: Path) -> bool:
    """`.rce` is a symlink: reading or writing through it would leave the
    folder (10.6). Refused whatever it points at."""
    try:
        return stat_module.S_ISLNK(os.lstat(root / paths.RCE_DIRNAME).st_mode)
    except OSError:
        return False


def _pin_of(path: str | Path) -> tuple[int, int] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat_module.S_ISDIR(st.st_mode):
        return None
    return (st.st_dev, st.st_ino)


def _entry_folders(entries: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Registered entries whose folder is there, with its canonical path.
    An entry whose folder is gone does not count (10.2)."""
    found = []
    for entry in entries:
        folder = Path(entry["path"])
        try:
            if not folder.is_dir():
                continue
        except OSError:
            continue
        found.append((entry, paths._canonical_path(folder)))
    return found


def _inspect_now(requested: str, entries: list[dict[str, Any]]) -> Inspection:
    """The inspection itself, run to the end (in the worker thread)."""
    if not requested:
        return _refused(requested, "missing", detail="no path given")
    if "\x00" in requested:
        return _refused(requested, "missing", detail="the path contains a NUL character; no folder has such a name")
    chosen = Path(requested)
    try:
        resolved = chosen.resolve()
        st = os.stat(resolved)
    except FileNotFoundError:
        return _refused(requested, "missing", detail=f"{requested} does not exist")
    except (OSError, RuntimeError, ValueError) as exc:
        return _refused(requested, "unreadable", detail=f"{requested}: {exc}")
    if not stat_module.S_ISDIR(st.st_mode):
        return _refused(requested, "not_a_folder", root=str(resolved), detail=f"{resolved} is not a folder")
    canonical = paths._canonical_path(resolved)
    refusal = _location_refusal(requested, canonical)
    if refusal is not None:
        return refusal
    try:
        os.listdir(canonical)  # the call macOS holds behind its permission prompt
    except OSError as exc:
        return _refused(requested, "unreadable", root=canonical, detail=f"{canonical}: {exc}")
    root = Path(canonical)
    name = root.name
    if _rce_link(root):
        return _refused(requested, "rce_link", root=canonical,
                        detail=f"{root / paths.RCE_DIRNAME} is a symlink; RCE neither reads nor writes through it")
    pin = _pin_of(root)

    registered = _entry_folders(entries)
    classification = _classify(root)
    for entry, folder in registered:
        if folder == canonical and (entry.get("id") is None or entry.get("id") == classification.project_id):
            return _sealed(Inspection(
                ALREADY_REGISTERED, requested, root=canonical, name=name, classification=classification,
                entry={"id": entry.get("id"), "path": entry["path"], "label": entry["label"]},
            ))
    for entry, folder in registered:
        if folder == canonical:
            continue  # a stale entry at this path (it now holds another project): not a containment
        if _inside(canonical, folder):
            return _refused(requested, "inside_project", root=canonical, label=entry["label"],
                            detail=f"{canonical} is inside the registered project {folder}")
        if _inside(folder, canonical):
            return _refused(requested, "contains_project", root=canonical, label=entry["label"],
                            detail=f"{canonical} contains the registered project {folder}")

    if classification.situation is Situation.LEGACY:
        return _sealed(Inspection(
            PRE_V5, requested, root=canonical, name=name, classification=classification,
            moves_graph=_moves_graph(root), pin=pin,
        ))
    if classification.situation is not Situation.NOT_A_PROJECT:
        return _sealed(Inspection(RCE_PROJECT, requested, root=canonical, name=name, classification=classification, pin=pin))
    return _sealed(Inspection(NEW_FOLDER, requested, root=canonical, name=name, preview=_preview(root), pin=pin))


def _moves_graph(root: Path) -> bool:
    """Opening this pre-V5 folder moves its in-project `.rce/graph.db` out
    (`paths.migrate_legacy_graph`, 8.10): the same conditions, stat only."""
    try:
        return (
            any(key == paths.IN_PROJECT_SOURCE for key, _db in paths.legacy_sources(root))
            and not paths.legacy_index_db_path(root).exists()
        )
    except OSError:
        return False


def _still_the_folder(insp: Inspection) -> None:
    """Raise `AddRefused` unless the folder at `insp.root` is still the
    directory inspected (same device and inode, not a symlink, spelled the
    same when resolved) and its `.rce` is no symlink -- checked under the
    project lock, just before the first write (10.6: the resolved folder
    is the one checked and the one written into)."""
    root = insp.root or ""
    try:
        resolved = os.path.realpath(root)
    except (OSError, ValueError):
        resolved = None
    if insp.pin is None or _pin_of(root) != insp.pin or resolved is None or paths._canonical_path(resolved) != root:
        raise _changed(insp, f"{root} is no longer the folder that was inspected; nothing written")
    if _rce_link(Path(root)):
        raise _changed(insp, f"{Path(root) / paths.RCE_DIRNAME} is now a symlink; nothing written")


# -- the deadline (10.2 "Waiting for the system") -----------------------------------


@dataclass
class _Pending:
    done: threading.Event = field(default_factory=threading.Event)
    result: Inspection | None = None
    error: BaseException | None = None
    finished: float = 0.0


_PENDING: dict[str, _Pending] = {}
_PENDING_LOCK = threading.Lock()


def _run_pending(pending: _Pending, requested: str, entries: list[dict[str, Any]]) -> None:
    try:
        pending.result = _inspect_now(requested, entries)
    except BaseException as exc:  # noqa: BLE001 -- handed to the waiting caller
        pending.error = exc
    finally:
        pending.finished = time.monotonic()
        pending.done.set()


def requested_path(path: str | os.PathLike[str]) -> str:
    """The path as asked for: `~` expanded, made absolute against the
    current directory -- no file system access (the resolution, which
    touches the folder, happens in the worker)."""
    text = os.fspath(path)
    if not text:
        return ""
    return os.path.abspath(os.path.expanduser(text))


def inspect(
    path: str | os.PathLike[str],
    *,
    registry: list[dict[str, Any]] | None = None,
    deadline: float | None = DEFAULT_DEADLINE_S,
) -> Inspection:
    """What the folder at `path` is (module docstring). Writes nothing.

    The listing runs in a worker thread; past `deadline` seconds the
    answer is `waiting_permission`, and the listing keeps running: the
    same request made again (while it runs, or within `_RESULT_FRESH_S`
    of its end) waits on that listing instead of starting another, and
    gets its result once it is there. `deadline=None` waits as long as it
    takes (the command line). `registry` is the registry's entries (read
    now when not given)."""
    requested = requested_path(path)
    entries = registry if registry is not None else project_registry.load()
    if deadline is None:
        return _inspect_now(requested, entries)
    with _PENDING_LOCK:
        pending = _PENDING.get(requested)
        if pending is not None and pending.done.is_set() and time.monotonic() - pending.finished > _RESULT_FRESH_S:
            del _PENDING[requested]
            pending = None
        if pending is None:
            pending = _Pending()
            _PENDING[requested] = pending
            threading.Thread(
                target=_run_pending, args=(pending, requested, list(entries)),
                name="rce-inspect", daemon=True,
            ).start()
    if not pending.done.wait(max(0.0, deadline)):
        return Inspection(WAITING_PERMISSION, requested)
    with _PENDING_LOCK:
        if _PENDING.get(requested) is pending:
            del _PENDING[requested]
    if pending.error is not None:
        raise pending.error
    assert pending.result is not None
    return pending.result


# -- adding --------------------------------------------------------------------------


class AddRefused(Exception):
    """Adding wrote nothing. `code` is machine-readable (a refusal code,
    `inspected_changed`, `waiting_permission`, `bad_label`); `message` the
    app's Chinese sentence; `inspection` the fresh look, when there is one
    (the page shows it instead of the stale one)."""

    def __init__(self, code: str, message: str, detail: str = "", inspection: Inspection | None = None) -> None:
        super().__init__(detail or message)
        self.code = code
        self.message = message
        self.detail = detail or message
        self.inspection = inspection


INSPECTED_CHANGED_MESSAGE = "这个文件夹在你查看之后变了，请重新查看"


@dataclass(frozen=True)
class Added:
    kind: str
    root: Path
    project_id: str | None
    label: str | None
    entry: dict[str, Any] | None  # the registry entry (existing or written)
    registered: bool  # whether this call wrote the registry
    needs_scan: bool  # a new folder: its first full scan comes next
    inspection: Inspection
    classification: Classification | None = None  # a situation to answer: nothing written

    @property
    def blocked(self) -> bool:
        return self.classification is not None and self.classification.blocked


def _label_for(label: str | None, insp: Inspection) -> str:
    if label is None or not str(label).strip():
        name = insp.name or Path(insp.root or "").name or "project"
        return project_registry.clean_label(name[: project_registry.LABEL_MAX_LENGTH])
    return project_registry.clean_label(label)


def _changed(insp: Inspection, detail: str) -> AddRefused:
    return AddRefused("inspected_changed", INSPECTED_CHANGED_MESSAGE, detail, inspection=insp)


def add(
    path: str | os.PathLike[str],
    *,
    label: str | None,
    inspected: str,
    registry: list[dict[str, Any]] | None = None,
    deadline: float | None = DEFAULT_DEADLINE_S,
) -> Added:
    """Add the folder (module docstring). Raises `AddRefused` (nothing
    written) or `project_registry.LabelError` (a display name that cannot
    be stored; nothing written)."""
    if label is not None and str(label).strip():
        project_registry.clean_label(label)  # refuse a bad name before anything else
    insp = inspect(path, registry=registry, deadline=deadline)
    if insp.kind == WAITING_PERMISSION:
        raise AddRefused("waiting_permission", insp.message, "the folder's listing has not returned yet", inspection=insp)
    if not isinstance(inspected, str) or not hmac.compare_digest(inspected, insp.token):
        raise _changed(insp, f"{insp.root or insp.requested} is no longer what was inspected; inspect it again")
    if insp.kind == REFUSED:
        assert insp.refusal is not None
        raise AddRefused(insp.refusal.code, insp.refusal.message, insp.refusal.detail, inspection=insp)
    root = Path(insp.root or "")
    if insp.kind == ALREADY_REGISTERED:
        return Added(insp.kind, root, insp.project_id, (insp.entry or {}).get("label"), insp.entry, False, False, insp)
    chosen = _label_for(label, insp)
    c = insp.classification
    if insp.kind == RCE_PROJECT:
        assert c is not None
        if c.situation not in OPENABLE:
            # 9.4: nothing -- the registry included -- is written until the
            # question is answered.
            return Added(insp.kind, root, c.project_id, chosen, None, False, False, insp, classification=c)
        _still_the_folder(insp)
        try:
            opened = project_identity.open_project(root)
        except (project_identity.ProjectBlocked, project_identity.AnswerRefused) as exc:
            raise _changed(insp, str(exc)) from exc
        if opened.project_id != c.project_id:
            raise _changed(insp, f"{root} now carries another identity")
        project_registry.register(root, opened.project_id, label=chosen)
        return Added(insp.kind, root, opened.project_id, chosen, _entry(opened.project_id, root), True, False, insp)
    if insp.kind == PRE_V5:
        _still_the_folder(insp)
        project_registry.register(root, label=chosen)
        return Added(insp.kind, root, None, chosen, _entry(None, root), True, False, insp)
    assert insp.kind == NEW_FOLDER
    try:
        result = project_identity.init_project(root, before_write=lambda: _still_the_folder(insp))
    except (
        project_identity.ProjectBlocked, project_identity.AnswerRefused,
        identity_mod.IdentityError, records_situation.WriteRefused,
    ) as exc:
        raise _changed(insp, str(exc)) from exc
    project_id = result.identity.id
    project_registry.register(root, project_id, label=chosen)
    logger.info("RCE: added %s as project %s (%s)", root, project_id, chosen)
    return Added(insp.kind, root, project_id, chosen, _entry(project_id, root), True, True, insp)


def _entry(project_id: str | None, root: Path) -> dict[str, Any] | None:
    resolved = str(Path(root).resolve())
    for entry in project_registry.load():
        if (project_id is not None and entry.get("id") == project_id) or (
            project_id is None and entry.get("id") is None and entry["path"] == resolved
        ):
            return entry
    return None


# -- the full scan (10.3) -------------------------------------------------------------

SCAN_STEPS = ("sources", "attempts", "mappings", "judgements")
STEP_LABELS = {
    "sources": "读取脚本、数据和文稿",
    "attempts": "尝试表及其检查",
    "mappings": "手画的连线",
    "judgements": "应用判断记录",
}
STEP_NAMES = {
    "sources": "every source extractor",
    "attempts": "the attempt table and its check",
    "mappings": "the hand-drawn links",
    "judgements": "the judgment record applied",
}
# How long a scan waits for another process's hold on the project lock
# before saying a scan is running (10.3: one scan at a time per project).
SCAN_LOCK_TIMEOUT_S = 2.0

SCAN_MESSAGES = {
    "scan_running": "这个项目正在扫描，请等它结束",
    "frozen": "这是旧版 RCE 项目：迁移之后才能重新扫描",
    "blocked": "这个项目有一个问题需要先回答，才能扫描",
    "project_moved": "项目已移动或已在别处认领，请重新打开",
    "not_a_project": "这个文件夹还不是 RCE 项目",
    "no_index": "这个项目在本机还没有索引，请重新打开它",
    "lock_failed": "无法取得项目锁，没有扫描",
}

Progress = Callable[[str, int, int], None]
Echo = Callable[[str], None]


class ScanRefused(Exception):
    """No scan ran; nothing was written. `code` is one of `SCAN_MESSAGES`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.message = SCAN_MESSAGES[code]
        self.detail = detail


@dataclass
class ScanReport:
    root: Path
    project_id: str
    ok: bool = True
    error: str | None = None  # the source scan's failure (`IngestFailed`), if any
    warnings: int = 0
    findings: int | None = None  # the attempt check's findings (None: not run)
    unreadable_sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 11.1: of the unreadable files, those only in a synced folder's cloud
    # whose client is not running -- per provider, with its sentence
    cloud: list[dict[str, Any]] = field(default_factory=list)


_SCANNING: set[str] = set()
_SCANNING_LOCK = threading.Lock()


def scanning(root: str | Path) -> bool:
    """Whether this process is scanning the project at `root` now."""
    with _SCANNING_LOCK:
        return paths._canonical_path(root) in _SCANNING


class ScanTicket:
    """A scan this process may run (`start_scan`): the process-wide slot is
    held from `start_scan` until `run` returns or `release` is called."""

    def __init__(self, root: Path, project_id: str, key: str) -> None:
        self.root = root
        self.project_id = project_id
        self._key = key
        self._released = False

    def release(self) -> None:
        with _SCANNING_LOCK:
            if not self._released:
                _SCANNING.discard(self._key)
                self._released = True

    def run(self, *, progress: Progress | None = None, echo: Echo | None = None) -> ScanReport:
        try:
            return _run_scan(self.root, self.project_id, progress or (lambda *_a: None), echo or (lambda _l: None))
        finally:
            self.release()


def start_scan(root: str | Path, *, expected_id: str | None = None) -> ScanTicket:
    """Claim the scan of the project at `root` (10.3) or raise
    `ScanRefused`: a scan of it already running in this process, a
    folder whose question is unanswered, a pre-V5 project or one whose
    migration has not finished (frozen, 9.12), a folder that is not (or no
    longer, `expected_id`) the project. Writes nothing."""
    root = Path(root)
    if not root.is_dir():
        raise ScanRefused("project_moved", f"{root} is not an existing folder")
    c = records_situation.classify(root)
    if c.blocked:
        raise ScanRefused("blocked", f"{root} is {c.situation.value}: its question must be answered first")
    if c.needs_migration:
        raise ScanRefused("frozen", f"{root} is frozen until it is migrated (rce migrate); nothing scanned")
    if c.situation is Situation.NOT_A_PROJECT or c.project_id is None:
        raise ScanRefused("not_a_project", f"{root} is not an RCE project")
    if expected_id is not None and c.project_id != expected_id:
        raise ScanRefused("project_moved", f"{root} no longer carries the project {expected_id}")
    if c.situation is Situation.NO_INDEX:
        raise ScanRefused("no_index", f"project {c.project_id} has no index on this machine; open it to build one")
    key = paths._canonical_path(root)
    with _SCANNING_LOCK:
        if key in _SCANNING:
            raise ScanRefused("scan_running", f"a scan of {root} is already running")
        _SCANNING.add(key)
    return ScanTicket(root, c.project_id, key)


def rescan(root: str | Path, *, expected_id: str | None = None, progress: Progress | None = None, echo: Echo | None = None) -> ScanReport:
    """`start_scan(root).run(...)`: the whole scan, in this thread."""
    return start_scan(root, expected_id=expected_id).run(progress=progress, echo=echo)


def _run_scan(root: Path, project_id: str, progress: Progress, echo: Echo) -> ScanReport:
    report = ScanReport(root, project_id)
    m = len(SCAN_STEPS)
    try:
        with records_situation.write_guard(root, project_id, human=False, timeout=SCAN_LOCK_TIMEOUT_S):
            conn = db.connect(records_situation.index_db_path(project_id))
            try:
                progress("sources", 1, m)
                try:
                    report.warnings = pipeline.ingest_sources(conn, root, echo=echo, apply=False)
                except pipeline.IngestFailed as exc:
                    report.ok, report.error = False, str(exc)
                    echo(f"  sources: {exc}")
                progress("attempts", 2, m)
                _scan_attempts(conn, root, report, echo)
                progress("mappings", 3, m)
                try:
                    mapped = mappings_ingest.ingest_mappings(conn, root)
                    if mapped.file_present:
                        echo(f"  mappings: {' '.join(f'{k}={v}' for k, v in mapped.counts.items())}")
                except mappings_ingest.MappingsFileError as exc:
                    report.notes.append(f"mappings not read: {exc}")
                    echo(f"  mappings: not read ({exc})")
                progress("judgements", 4, m)
                judgements.apply_after_scan(conn, root, echo)
                report.unreadable_sources = sorted(
                    f"{row['extractor']}: {scan_mod.file_of(row['source'])}"
                    for row in db.all_scan_sources(conn) if row["status"] == scan_mod.UNREADABLE
                )
                report.cloud = cloud.notes(root, {s.split(": ", 1)[1] for s in report.unreadable_sources})
            finally:
                conn.close()
    except records_lock.ProjectLockTimeout as exc:
        raise ScanRefused("scan_running", f"another process holds the project lock ({exc})") from exc
    except records_situation.NeedsMigrationError as exc:
        raise ScanRefused("frozen", str(exc)) from exc
    except records_situation.WriteRefused as exc:
        raise ScanRefused("project_moved", str(exc)) from exc
    except records_lock.ProjectLockError as exc:
        raise ScanRefused("lock_failed", str(exc)) from exc
    return report


def _scan_attempts(conn, root: Path, report: ScanReport, echo: Echo) -> None:
    """`rce attempts --check`, when `.rce/attempts.toml` configures a table:
    the table ingested, then the three consistency checks."""
    try:
        config = attempts_ingest.load_config(root)
    except attempts_ingest.AttemptsConfigError:
        return  # no table configured: nothing to read, nothing to check
    try:
        counts = attempts_ingest.ingest_attempts_repo(conn, root, config)
    except attempts_ingest.AttemptsConfigError as exc:
        report.notes.append(f"attempts not read: {exc}")
        echo(f"  attempts: not read ({exc})")
        return
    echo(f"  attempts: {' '.join(f'{k}={v}' for k, v in counts.items())}")
    results = consistency.run_checks(conn, root, config)
    report.findings = sum(len(r.findings) for r in results)
    echo(f"  attempts --check: {report.findings} finding(s)")
