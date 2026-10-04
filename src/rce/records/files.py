"""Bytes on disk for the human record (DESIGN.md sections 9.2, 9.3, 9.7):
how a record file is written, appended to, snapshotted and read.

Writing: one durable path
-------------------------

`durable_write` is the only way a record file's bytes change: the full new
content to a temp file in the same directory, `fsync`, `os.replace`, then
an `fsync` of the directory. Unlike `rce.webapp.mapedit.atomic_replace_bytes`
(which this generalizes), the temp name is unique per write -- pid plus a
random token -- because a fixed name is exactly what two processes
collided on in 9.0. A writer killed at any point before the replace leaves
the old file intact and, at worst, an orphan temp file whose name says
whose it was.

`append_bytes` is how an append-only record grows: the file's existing
bytes, untouched, plus the new bytes, written through `durable_write`.
"Append" is not `open(..., "a")`: an in-place append that is interrupted
leaves a torn last entry in the researcher's file, and an `O_APPEND`
write cannot be verified before it lands. Here the whole result is built
first, the old bytes are checked to be its prefix, and only then does it
replace the file. A file without a trailing newline gets one before the
new bytes, so the appended text never fuses with the last line.

Snapshots: one per day, not per write (9.2)
-------------------------------------------

`snapshot_if_first_change_today` copies a record file into
`.rce/backups/[<subdir>/]` the first time RCE sees it changed on a given
(local) day: when no snapshot of it has been taken today and its bytes
differ from the newest snapshot. A review session of thirty clicks must
not rotate the last good copy away, so the 20 kept per file are 20 days of
history, not 20 clicks. Writers call it *before* writing (the
snapshot-before-entry rule of 9.10), so the copy is the state the day
started from; a hand edit is covered the first time RCE looks at the file
after it. `snapshot_now` is the explicit copy taken before an act that
discards something (「重新排列」 dropping an arrangement), whatever the day.
The naming is `rce.webapp.mapedit.write_backup_bytes`' --
`<name>.<UTC stamp>Z<suffix>` -- so the backups those writers already take
and these snapshots are one series per file, pruned to one budget.

Reading: four answers, not two
------------------------------

`read_record` never blocks on the cloud and never confuses "could not
read" with "not there" (the Section 4 error that 9.3's trust rules exist
to avoid): ABSENT, DATALESS (8.10's flag, or an `.icloud` placeholder;
the download is requested on a background thread), UNREADABLE (an OS or
UTF-8 error), or PRESENT with the bytes.

Conflict copies (9.7)
---------------------

A sync service that cannot merge leaves a second file beside the first:
`judgements 2.toml` (iCloud, Finder), `judgements (1).toml` (Drive,
OneDrive), `judgements (… conflicted copy …).toml` (Dropbox).
`conflict_copies` lists them. It only reports; RCE never deletes or merges
one by itself.
"""

from __future__ import annotations

import enum
import logging
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rce import paths

logger = logging.getLogger(__name__)

BACKUPS_DIRNAME = "backups"
SNAPSHOT_KEEP = 20

_TMP_MARK = ".rce-tmp"

Clock = Callable[[], datetime]


class RecordFileError(Exception):
    """A record file could not be written as asked. Nothing was changed."""


class RecordState(str, enum.Enum):
    """What reading a record file found. INVALID is set by parsers built on
    `read_record` (the bytes were read but are not a valid record)."""

    ABSENT = "absent"
    DATALESS = "dataless"
    UNREADABLE = "unreadable"
    INVALID = "invalid"
    PRESENT = "present"


@dataclass(frozen=True)
class RecordRead:
    state: RecordState
    path: Path
    data: bytes | None = None
    text: str | None = None
    error: str | None = None


# -- writing -------------------------------------------------------------------


def _fsync_dir(dir_path: Path) -> None:
    """Best-effort: some filesystems refuse fsync on a directory fd; the
    file fsync before the rename is the load-bearing one."""
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def temp_path_for(path: Path) -> Path:
    """A fresh, unique temp name beside `path` (same filesystem, so the
    rename is atomic). Hidden, and marked with the writer's pid."""
    return path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}{_TMP_MARK}"


def write_new_file(path: Path, data: bytes) -> None:
    """Write `data` to a temp file and fsync it; the caller renames or
    links it into place. Exposed for `durable_write` and for the
    create-exclusively path in `rce.records.identity`."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write(path: str | Path, data: bytes) -> None:
    """Replace `path`'s content with `data`, atomically and durably. The
    parent directory must already exist: a record writer never re-creates
    a folder that has gone (9.4)."""
    path = Path(path)
    if not path.parent.is_dir():
        raise RecordFileError(f"{path.parent} does not exist; not creating it")
    tmp = temp_path_for(path)
    try:
        write_new_file(tmp, data)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover -- leftover is harmless
            logger.warning("could not remove temp file %s (%s)", tmp, exc)


def append_bytes(
    path: str | Path,
    new_bytes: bytes,
    *,
    expected_old: bytes | None = None,
    create: bool = False,
) -> bytes:
    """`path`'s existing bytes plus `new_bytes`, written via `durable_write`;
    returns the full content written.

    `expected_old`, when given, is what the caller parsed (under its lock):
    if the file no longer holds exactly those bytes -- a sync service or a
    hand edit got in between -- nothing is written. A missing file is
    refused unless `create`; a file in the cloud or unreadable is always
    refused. A missing trailing newline is supplied before `new_bytes`."""
    path = Path(path)
    current = read_record(path)
    if current.state is RecordState.ABSENT:
        if not create:
            raise RecordFileError(f"{path} does not exist; refusing to create it")
        old = b""
    elif current.state is RecordState.PRESENT:
        old = current.data or b""
    else:
        raise RecordFileError(f"{path} cannot be read right now ({current.state.value}: {current.error})")
    if expected_old is not None and old != expected_old:
        raise RecordFileError(f"{path} changed since it was read; nothing written")
    joint = b"\n" if old and not old.endswith(b"\n") else b""
    result = old + joint + new_bytes
    if not result.startswith(old):  # pragma: no cover -- guards the construction above
        raise RecordFileError("internal error: the old bytes are not a prefix of the result")
    durable_write(path, result)
    after = read_record(path)
    if after.state is not RecordState.PRESENT or after.data != result:
        raise RecordFileError(f"{path} does not read back as written")
    return result


# -- reading -------------------------------------------------------------------


def _icloud_placeholder(path: Path) -> Path:
    return path.parent / f".{path.name}.icloud"


def read_record(path: str | Path) -> RecordRead:
    """Read a record file without ever blocking on the cloud. The text is
    UTF-8; a leading byte-order mark is dropped from `text` (an editor's
    habit, not content) but kept in `data`."""
    path = Path(path)
    try:
        exists = path.exists()
    except OSError as exc:
        return RecordRead(RecordState.UNREADABLE, path, error=str(exc))
    if not exists:
        if _icloud_placeholder(path).exists():
            paths._request_download(path)
            return RecordRead(RecordState.DATALESS, path, error="the file is in the cloud")
        return RecordRead(RecordState.ABSENT, path)
    if paths.is_dataless(path):
        paths._request_download(path)
        return RecordRead(RecordState.DATALESS, path, error="the file is in the cloud")
    try:
        data = path.read_bytes()
    except OSError as exc:
        return RecordRead(RecordState.UNREADABLE, path, error=str(exc))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return RecordRead(RecordState.UNREADABLE, path, data=data, error=f"not UTF-8: {exc}")
    if text.startswith("﻿"):
        text = text[1:]
    return RecordRead(RecordState.PRESENT, path, data=data, text=text)


# -- conflict copies ----------------------------------------------------------


def _norm(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def conflict_copies(path: str | Path) -> list[Path]:
    """Sync conflict copies of `path` in its directory, sorted by name.
    Conservative in both senses: it matches only the shapes sync services
    are known to produce, and it never touches what it finds."""
    path = Path(path)
    stem, suffix = path.stem, path.suffix
    s, x = re.escape(_norm(stem)), re.escape(_norm(suffix))
    patterns = [
        re.compile(rf"^{s} \d+{x}$"),                      # "judgements 2.toml"
        re.compile(rf"^{s} ?\(\d+\){x}$"),                 # "judgements (1).toml"
        re.compile(rf"^{s} \(.*conflict.*\){x}$"),         # Dropbox
        re.compile(rf"^{s}[-_ ]conflict[-_ ].*{x}$"),      # Syncthing-style
    ]
    try:
        names = os.listdir(path.parent)
    except OSError:
        return []
    found = []
    for name in names:
        n = _norm(name)
        if n == _norm(path.name):
            continue
        if any(p.match(n) for p in patterns):
            found.append(path.parent / name)
    return sorted(found)


# -- snapshots ----------------------------------------------------------------


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _backups_dir(project_root: Path, subdir: str | None) -> Path:
    base = project_root / paths.RCE_DIRNAME / BACKUPS_DIRNAME
    if not subdir:
        return base
    target = (base / subdir).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError as exc:
        raise RecordFileError(f"snapshot subdir {subdir!r} leaves .rce/backups") from exc
    if target == base.resolve():
        raise RecordFileError(f"snapshot subdir {subdir!r} is not a subdirectory")
    return base / subdir


def _confine(project_root: Path, path: Path) -> None:
    try:
        path.resolve().relative_to(project_root.resolve())
    except ValueError as exc:
        raise RecordFileError(f"{path} is outside the project {project_root}") from exc


_STAMP_RE = r"(\d{8}T\d{12})Z(?:\.\d+)?"


def _snapshots_of(backups: Path, name: str, suffix: str) -> list[tuple[datetime, Path]]:
    pattern = re.compile(rf"^{re.escape(name)}\.{_STAMP_RE}{re.escape(suffix)}$")
    found = []
    try:
        entries = list(backups.iterdir())
    except OSError:
        return []
    for entry in entries:
        m = pattern.match(entry.name)
        if m and entry.is_file():
            stamp = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)
            found.append((stamp, entry))
    found.sort(key=lambda item: (item[0], item[1].name))
    return found


def _write_snapshot(backups: Path, path: Path, data: bytes, now: datetime) -> Path:
    backups.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    target = backups / f"{path.name}.{stamp}Z{path.suffix}"
    counter = 0
    while target.exists():  # same microsecond twice: disambiguate, never overwrite
        counter += 1
        target = backups / f"{path.name}.{stamp}Z.{counter}{path.suffix}"
    durable_write(target, data)
    for _stamp, old in _snapshots_of(backups, path.name, path.suffix)[:-SNAPSHOT_KEEP]:
        try:
            old.unlink()
            logger.info("pruned old snapshot %s (keeping newest %d)", old, SNAPSHOT_KEEP)
        except OSError as exc:  # pragma: no cover
            logger.warning("could not prune snapshot %s (%s)", old, exc)
    return target


def snapshot_if_first_change_today(
    project_root: str | Path,
    path: str | Path,
    subdir: str | None = None,
    *,
    now: Clock | None = None,
) -> Path | None:
    """Snapshot `path` if it has changed since its newest snapshot and no
    snapshot of it was taken today (local date). Returns the snapshot, or
    None when none was needed or the file is not readable right now (a
    file in the cloud is never materialized for a snapshot)."""
    project_root, path = Path(project_root), Path(path)
    _confine(project_root, path)
    current = read_record(path)
    if current.state is not RecordState.PRESENT:
        return None
    moment = (now or _local_now)()
    backups = _backups_dir(project_root, subdir)
    existing = _snapshots_of(backups, path.name, path.suffix)
    if existing:
        today = moment.astimezone().date()
        if any(stamp.astimezone(moment.astimezone().tzinfo).date() == today for stamp, _ in existing):
            return None
        try:
            if existing[-1][1].read_bytes() == current.data:
                return None
        except OSError:
            pass
    return _write_snapshot(backups, path, current.data or b"", moment)


def snapshot_now(
    project_root: str | Path,
    path: str | Path,
    subdir: str | None = None,
    *,
    now: Clock | None = None,
) -> Path | None:
    """Snapshot `path` unconditionally (before an act that discards
    something). None if the file is absent or not readable right now."""
    project_root, path = Path(project_root), Path(path)
    _confine(project_root, path)
    current = read_record(path)
    if current.state is not RecordState.PRESENT:
        return None
    moment = (now or _local_now)()
    return _write_snapshot(_backups_dir(project_root, subdir), path, current.data or b"", moment)


def newest_snapshot(project_root: str | Path, path: str | Path, subdir: str | None = None) -> Path | None:
    """The newest snapshot of `path`, for `rce records` (9.8)."""
    project_root, path = Path(project_root), Path(path)
    found = _snapshots_of(_backups_dir(project_root, subdir), path.name, path.suffix)
    return found[-1][1] if found else None
