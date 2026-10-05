"""The project identity file, `.rce/project.toml` (DESIGN.md section 9.4 --
the file only; the situation table that decides move / copy / restore is
a later phase built on this).

A project is identified by a file it carries, not by where it sits:

```toml
id      = "p-3f9c2a7e5b1d4c68a0e7b2d94c1f6a35"
created = "2026-10-04"
ledger  = true
# forked_from = "p-…"
# migrating_from = "…"
```

The rules this module enforces:

- **Created exclusively, never overwritten by creation.** `create_identity`
  writes the full content to a temp file, syncs it, and `link`s it into
  place -- `link` fails if the name exists, so two processes racing to
  mint an id cannot both win, and nobody ever sees a half-written
  identity. Minting an id over an existing one would silently make a
  different project of the folder (9.4: "never mint an id silently").
- **Read with four outcomes the caller must tell apart** (9.4's table has
  a row for each): absent, in the cloud, unreadable (OS error, not UTF-8,
  not TOML, or failing validation -- named), and a sync conflict copy
  beside it. A conflict copy makes the identity unusable even if the main
  file parses: two candidate identities is "cannot tell who this is".
- **Rewritten only by RCE, only by compare-and-swap.** The file is
  RCE-owned (the researcher has nothing to say in it), so `set_flag` and
  `replace_identity` re-emit it whole -- but only if what is on disk is
  still exactly the identity the caller read, and after a snapshot. The
  `ledger` flag can only be raised: lowering it would let a missing
  ledger be re-created as an empty one, which is what 9.3's
  never-recreate rule forbids.
- **Every version is kept.** Each write (creation included) leaves a
  snapshot of what it wrote in `.rce/backups/`, and a rewrite also one of
  what it replaced -- so a lost identity file can be restored from the
  newest snapshot (`restore_identity_file`, DESIGN.md 9.12's first answer
  to 「项目身份文件不见了」), created exclusively like any identity.
- **Unknown keys are refused on read**, naming the key: this file is not
  edited by hand, so a key RCE does not know is either corruption or a
  newer RCE's format, and in both cases guessing is worse than stopping.
"""

from __future__ import annotations

import enum
import os
import re
import secrets
import tomllib
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from rce import paths
from rce.records import files
from rce.records.files import RecordState
from rce.records.lock import PROJECT_ID_RE

PROJECT_FILENAME = "project.toml"

_HEADER = (
    "# RCE 的项目身份文件，由 RCE 写入，请勿手改。\n"
    "# 它让项目在文件夹移动、改名后仍能找回自己的人工记录与图谱。\n"
)

_KNOWN_KEYS = ("id", "created", "ledger", "forked_from", "migrating_from")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SETTABLE = ("ledger", "migrating_from", "forked_from")


class IdentityError(Exception):
    """The identity file could not be created or rewritten as asked.
    Nothing was written."""


class IdentityExistsError(IdentityError):
    """`create_identity` found a `project.toml` already there."""


class IdentityState(str, enum.Enum):
    ABSENT = "absent"
    DATALESS = "dataless"
    UNREADABLE = "unreadable"
    CONFLICT_COPY = "conflict_copy"
    PRESENT = "present"


@dataclass(frozen=True)
class ProjectIdentity:
    id: str
    created: str
    ledger: bool = False
    forked_from: str | None = None
    migrating_from: str | None = None


@dataclass(frozen=True)
class IdentityRead:
    state: IdentityState
    path: Path
    identity: ProjectIdentity | None = None
    error: str | None = None
    line: int | None = None
    conflict_copies: tuple[Path, ...] = ()


def identity_path(project_root: str | Path) -> Path:
    return Path(project_root) / paths.RCE_DIRNAME / PROJECT_FILENAME


def new_project_id() -> str:
    """`p-` + 128 random bits as hex."""
    return "p-" + secrets.token_hex(16)


# -- parse / validate -----------------------------------------------------------


def _line_of_key(text: str, key: str) -> int | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for number, line in enumerate(text.split("\n"), start=1):  # TOML lines end at \n only
        if pattern.match(line):
            return number
    return None


class _Invalid(Exception):
    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


def _toml_error_line(exc: tomllib.TOMLDecodeError) -> int | None:
    lineno = getattr(exc, "lineno", None)
    if lineno:
        return int(lineno)
    m = re.search(r"line (\d+)", str(exc))
    return int(m.group(1)) if m else None


def parse_identity(text: str) -> ProjectIdentity:
    """Parse and validate `project.toml`'s text. Raises `_Invalid` (caught
    by `read_identity`) naming the line where it can."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _Invalid(f"not valid TOML: {exc}", _toml_error_line(exc)) from exc
    for key in data:
        if key not in _KNOWN_KEYS:
            raise _Invalid(f"unknown key {key!r} in project.toml", _line_of_key(text, key))

    def need_id(key: str, required: bool) -> str | None:
        value = data.get(key)
        if value is None:
            if required:
                raise _Invalid(f"project.toml has no {key!r}")
            return None
        if not isinstance(value, str) or not PROJECT_ID_RE.match(value):
            raise _Invalid(f"{key!r} is not a project id (p- and 32 hex digits): {value!r}", _line_of_key(text, key))
        return value

    pid = need_id("id", True)
    forked = need_id("forked_from", False)
    created = data.get("created")
    if isinstance(created, (date,)) and not isinstance(created, datetime):
        created = created.isoformat()
    if not isinstance(created, str) or not _DATE_RE.match(created):
        raise _Invalid(f"'created' must be a date like \"2026-10-04\": {created!r}", _line_of_key(text, "created"))
    ledger = data.get("ledger", False)
    if not isinstance(ledger, bool):
        raise _Invalid(f"'ledger' must be true or false: {ledger!r}", _line_of_key(text, "ledger"))
    migrating = data.get("migrating_from")
    if migrating is not None and (not isinstance(migrating, str) or not migrating.strip()):
        raise _Invalid(f"'migrating_from' must be a non-empty string: {migrating!r}", _line_of_key(text, "migrating_from"))
    return ProjectIdentity(id=pid, created=created, ledger=ledger, forked_from=forked, migrating_from=migrating)


def _basic_string(value: str) -> str:
    from rce.records.ledger import toml_string  # noqa: PLC0415 -- one emitter, no import cycle

    return toml_string(value)


def emit_identity(identity: ProjectIdentity) -> bytes:
    """The fixed-schema text of `identity`; `parse_identity` reads it back
    to an equal value (checked on every write)."""
    lines = [_HEADER.rstrip("\n"), f"id      = {_basic_string(identity.id)}", f"created = {_basic_string(identity.created)}"]
    lines.append(f"ledger  = {'true' if identity.ledger else 'false'}")
    if identity.forked_from is not None:
        lines.append(f"forked_from = {_basic_string(identity.forked_from)}")
    if identity.migrating_from is not None:
        lines.append(f"migrating_from = {_basic_string(identity.migrating_from)}")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        back = parse_identity(data.decode("utf-8"))
    except _Invalid as exc:
        raise IdentityError(f"not a valid identity: {exc}") from exc
    if back != identity:  # pragma: no cover -- emitter guard
        raise IdentityError("internal error: project.toml does not read back as written")
    return data


# -- read ------------------------------------------------------------------------


def read_identity(project_root: str | Path) -> IdentityRead:
    """Read `.rce/project.toml` without blocking on the cloud. A conflict
    copy beside it wins over everything else: it is reported as
    CONFLICT_COPY even when the main file parses (`identity` is then still
    filled in, for display)."""
    path = identity_path(project_root)
    copies = tuple(files.conflict_copies(path))
    got = files.read_record(path)
    identity, error, line = None, got.error, None
    state = {
        RecordState.ABSENT: IdentityState.ABSENT,
        RecordState.DATALESS: IdentityState.DATALESS,
        RecordState.UNREADABLE: IdentityState.UNREADABLE,
    }.get(got.state, IdentityState.PRESENT)
    if got.state is RecordState.PRESENT:
        try:
            identity = parse_identity(got.text or "")
        except _Invalid as exc:
            state, error, line = IdentityState.UNREADABLE, str(exc), exc.line
    if copies:
        names = ", ".join(p.name for p in copies)
        return IdentityRead(IdentityState.CONFLICT_COPY, path, identity, f"sync conflict copies beside project.toml: {names}", line, copies)
    return IdentityRead(state, path, identity, error, line, copies)


# -- create / rewrite ------------------------------------------------------------


def create_identity(
    project_root: str | Path,
    *,
    forked_from: str | None = None,
    migrating_from: str | None = None,
    ledger: bool = False,
    today: date | None = None,
) -> ProjectIdentity:
    """Mint an id and create `.rce/project.toml` exclusively. The project
    folder must exist (it is never re-created); `.rce/` is created inside
    it if needed. Raises `IdentityExistsError` if the file is already
    there -- in whatever state (a dataless or unparseable one included)."""
    root = Path(project_root)
    if not root.is_dir():
        raise IdentityError(f"{root} is not an existing folder; not creating it")
    path = identity_path(root)
    if os.path.lexists(path) or files.read_record(path).state is not RecordState.ABSENT:
        raise IdentityExistsError(f"{path} already exists; an identity is never overwritten")
    identity = ProjectIdentity(
        id=new_project_id(),
        created=(today or date.today()).isoformat(),
        ledger=ledger,
        forked_from=forked_from,
        migrating_from=migrating_from,
    )
    _link_exclusively(path, emit_identity(identity))
    _keep_version(root, path)
    return identity


def _keep_version(project_root: Path, path: Path) -> None:
    """A snapshot of the identity as just written (module docstring): the
    newest snapshot is always the current identity. Never fails a write --
    the identity itself has landed."""
    try:
        files.snapshot_now(project_root, path)
    except (OSError, files.RecordFileError) as exc:  # pragma: no cover -- a full disk, a read-only .rce/backups
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning("could not keep a snapshot of %s: %s", path, exc)


def _link_exclusively(path: Path, data: bytes) -> None:
    """`data` at `path`, only if nothing is there (temp file + `link`)."""
    path.parent.mkdir(exist_ok=True)
    tmp = files.temp_path_for(path)
    try:
        files.write_new_file(tmp, data)
        try:
            os.link(tmp, path)
        except FileExistsError as exc:
            raise IdentityExistsError(f"{path} already exists; an identity is never overwritten") from exc
        except OSError:  # a filesystem without hard links: O_EXCL on the final name
            try:
                files.write_new_file(path, data)
            except FileExistsError as exc:
                raise IdentityExistsError(f"{path} already exists; an identity is never overwritten") from exc
        files._fsync_dir(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def identity_snapshots(project_root: str | Path) -> list[tuple[Path, ProjectIdentity]]:
    """The snapshots of `project.toml` in `.rce/backups/` that read as a
    valid identity, newest first (an unreadable one is skipped, never
    guessed at)."""
    root = Path(project_root)
    found = []
    for snap in reversed(files.snapshots(root, identity_path(root))):
        try:
            text = snap.read_bytes().decode("utf-8")
            found.append((snap, parse_identity(text)))
        except (OSError, UnicodeDecodeError, _Invalid):
            continue
    return found


def restore_identity_file(project_root: str | Path, snapshot: Path) -> ProjectIdentity:
    """Put `snapshot`'s bytes back as `.rce/project.toml`, created
    exclusively -- never over an identity that is there (in whatever
    state). The snapshot must read as a valid identity. Hold the path's
    project lock."""
    root = Path(project_root)
    if not root.is_dir():
        raise IdentityError(f"{root} is not an existing folder; not creating it")
    path = identity_path(root)
    if os.path.lexists(path) or files.read_record(path).state is not RecordState.ABSENT:
        raise IdentityExistsError(f"{path} already exists; an identity is never overwritten")
    data = snapshot.read_bytes()
    try:
        identity = parse_identity(data.decode("utf-8"))
    except (UnicodeDecodeError, _Invalid) as exc:
        raise IdentityError(f"the snapshot {snapshot.name} is not a valid identity: {exc}") from exc
    _link_exclusively(path, data)
    return identity


def replace_identity(
    project_root: str | Path,
    expected: ProjectIdentity,
    new: ProjectIdentity,
    *,
    snapshot: bool = True,
) -> ProjectIdentity:
    """Rewrite `project.toml` from `expected` to `new`, durably, only if the
    file currently reads as exactly `expected` (and has no conflict copy).
    Snapshots the old file first (unconditionally: an id change must never
    be lost to the once-a-day rule). Used by `set_flag`, and by the fork
    and claim answers of 9.4 in a later phase. Hold the project lock."""
    current = read_identity(project_root)
    if current.state is not IdentityState.PRESENT or current.identity != expected:
        raise IdentityError(
            f"project.toml is not what was read ({current.state.value}); nothing written"
        )
    if new == expected:
        return new
    data = emit_identity(new)
    if snapshot:
        files.snapshot_now(project_root, current.path)
    files.durable_write(current.path, data)
    _keep_version(Path(project_root), current.path)
    return new


def set_flag(project_root: str | Path, expected: ProjectIdentity, key: str, value: object) -> ProjectIdentity:
    """Set one of `ledger` (true only -- it is never lowered),
    `migrating_from` (a string, or None to clear it), `forked_from` (an id,
    or None) via `replace_identity`."""
    if key not in _SETTABLE:
        raise IdentityError(f"{key!r} cannot be set (settable: {', '.join(_SETTABLE)})")
    if key == "ledger":
        if value is not True:
            raise IdentityError("the ledger flag is only ever raised, never lowered")
        if expected.ledger:
            return expected
        new = replace(expected, ledger=True)
    elif key == "migrating_from":
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise IdentityError(f"migrating_from must be a non-empty string or None: {value!r}")
        new = replace(expected, migrating_from=value)
    else:
        if value is not None and (not isinstance(value, str) or not PROJECT_ID_RE.match(value)):
            raise IdentityError(f"forked_from must be a project id or None: {value!r}")
        new = replace(expected, forked_from=value)
    return replace_identity(project_root, expected, new)
