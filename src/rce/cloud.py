"""Synced folders as project locations (DESIGN.md 11.1, task V7).

A folder under `~/Library/CloudStorage/<provider>-<account>` is added like
any other (Section 10). Three things change, all of them here:

- **Which provider holds it** (`provider`): read from the directory name
  directly under `CloudStorage` (`OneDrive-个人` -> OneDrive, `Dropbox`,
  `GoogleDrive-<account>`, `Box-Box`); any other name is a provider RCE does
  not know, named by its folder.
- **Whether its client is running** (`client_running`): a process check
  (`pgrep -x`), cached for a few seconds. `None` when it cannot be told (a
  provider RCE does not know, no `pgrep`). Tests replace `PROCESS_CHECK`.
- **Never reading a file only the cloud has while nobody can download it**
  (`blocked_provider`, `check_readable`, `read_text`): a file whose content
  macOS has evicted (`SF_DATALESS`, `rce.paths.is_dataless`) inside a
  provider whose client is not known to be running is not opened -- an
  `open()` would wait for a download nobody will make -- and no download is
  requested (`rce.paths._request_download` asks `download_blocked`). The
  reader gets `CloudOnlyError` (an `OSError`, so every extractor's existing
  "cannot read" path reports the source `UNREADABLE`, 9.6). With the client
  running, a cloud file is read the way an iCloud one is (8.10).

`CloudStorage` itself and each provider's root are top-level folders, refused
when chosen exactly (`is_top_level`, used by `rce.addproject`).
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rce import paths

logger = logging.getLogger(__name__)

# kind -> (display name, client process names)
_KNOWN = {
    "OneDrive": ("OneDrive", ("OneDrive",)),
    "Dropbox": ("Dropbox", ("Dropbox",)),
    "GoogleDrive": ("Google Drive", ("Google Drive",)),
    "Box": ("Box", ("Box",)),
}
UNKNOWN = "unknown"

# 11.1's sentence, the provider's name substituted.
BLOCKED_SENTENCE = "这些文件只在 {name} 云端；{name} 客户端没有运行，打开它并登录后 RCE 才能读到"

CLIENT_CACHE_S = 5.0


@dataclass(frozen=True)
class Provider:
    kind: str  # OneDrive | Dropbox | GoogleDrive | Box | unknown
    name: str  # as the app says it
    folder: str  # the directory under CloudStorage, e.g. "OneDrive-个人"
    root: str  # that directory, canonical

    def payload(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "folder": self.folder}


def cloudstorage_root() -> Path:
    return Path.home() / "Library" / "CloudStorage"


def _canonical(path: Path) -> str:
    """The canonical spelling of a directory (`paths._canonical_path`), or of
    a file as its directory's canonical spelling plus its name -- a file
    itself is never opened to find its name."""
    try:
        if path.is_dir():
            return paths._canonical_path(path)
    except OSError:
        pass
    return str(Path(paths._canonical_path(path.parent)) / path.name)


def _base() -> str | None:
    base = cloudstorage_root()
    try:
        if not base.is_dir():
            return None
    except OSError:
        return None
    return paths._canonical_path(base)


def kind_of(folder: str) -> str:
    head = folder.split("-", 1)[0]
    return head if head in _KNOWN else UNKNOWN


def provider(path: str | Path) -> Provider | None:
    """The provider whose folder holds `path`, or None when `path` is not
    under `~/Library/CloudStorage/<folder>/` (CloudStorage itself included)."""
    base = _base()
    if base is None:
        return None
    try:
        rel = Path(_canonical(Path(path))).relative_to(base)
    except (ValueError, OSError):
        return None
    if not rel.parts:
        return None
    folder = rel.parts[0]
    kind = kind_of(folder)
    name = _KNOWN[kind][0] if kind in _KNOWN else folder
    return Provider(kind, name, folder, str(Path(base) / folder))


def is_top_level(canonical: str) -> bool:
    """`canonical` is `~/Library/CloudStorage` itself or a provider's root
    (11.1): matched exactly, after canonicalisation."""
    base = _base()
    if base is None:
        return False
    here = Path(canonical)
    return here == Path(base) or here.parent == Path(base)


# -- is the client running -------------------------------------------------------


def _pgrep(names: tuple[str, ...]) -> bool | None:
    """Whether a process with one of these exact names runs; None when that
    cannot be told."""
    for name in names:
        try:
            result = subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode == 0:
            return True
        if result.returncode != 1:
            return None
    return False


#: The process check (`names -> bool | None`); tests replace it.
PROCESS_CHECK: Callable[[tuple[str, ...]], bool | None] = _pgrep

_CACHE: dict[str, tuple[float, bool | None]] = {}
_CACHE_LOCK = threading.Lock()


def reset_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def client_running(kind: str) -> bool | None:
    """Whether the provider's client runs: True / False, or None for a
    provider RCE does not know or a check that could not be made."""
    known = _KNOWN.get(kind)
    if known is None:
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(kind)
        if hit is not None and now - hit[0] < CLIENT_CACHE_S:
            return hit[1]
    value = PROCESS_CHECK(known[1])
    with _CACHE_LOCK:
        _CACHE[kind] = (now, value)
    return value


# -- not reading what nobody can download ----------------------------------------


def message(prov: Provider) -> str:
    return BLOCKED_SENTENCE.format(name=prov.name)


def download_blocked(path: str | Path) -> Provider | None:
    """The provider when `path` lies in a synced folder whose client is not
    known to be running -- nothing may ask for its download. Asks no
    question of the file itself."""
    prov = provider(path)
    if prov is None or client_running(prov.kind) is True:
        return None
    return prov


def blocked_provider(path: str | Path) -> Provider | None:
    """The provider when `path` is only in the cloud and its client is not
    known to be running (module docstring), else None. A file that is
    local, or not in a synced folder, is never blocked."""
    if not paths.is_dataless(path):
        return None
    return download_blocked(path)


class CloudOnlyError(OSError):
    """The file is only in a provider's cloud and its client is not running:
    not opened, no download requested."""

    def __init__(self, path: str | Path, prov: Provider) -> None:
        super().__init__(
            f"{path} is only in the {prov.name} cloud and the {prov.name} client is not running; not read"
        )
        self.provider = prov
        self.message = message(prov)


def check_readable(path: str | Path) -> None:
    prov = blocked_provider(path)
    if prov is not None:
        raise CloudOnlyError(path, prov)


def read_text(path: str | Path) -> str:
    """`Path.read_text(errors="replace")`, but never on a file only the
    cloud has while its client is not running (`CloudOnlyError`)."""
    check_readable(path)
    return Path(path).read_text(errors="replace")


def read_bytes(path: str | Path) -> bytes:
    check_readable(path)
    return Path(path).read_bytes()


def blocked_files(root: str | Path, rel_paths) -> dict[Provider, list[str]]:
    """{provider: [rel path]} of the files under `root` that are blocked."""
    found: dict[Provider, list[str]] = {}
    root = Path(root)
    for rel in rel_paths:
        if not rel or os.path.isabs(rel):
            continue
        prov = blocked_provider(root / rel)
        if prov is not None:
            found.setdefault(prov, []).append(rel)
    return found


def notes(root: str | Path, rel_paths) -> list[dict]:
    """What a scan says of its unreadable files that are blocked (11.1):
    one entry per provider -- its name, the sentence, the files."""
    return [
        {**prov.payload(), "message": message(prov), "files": sorted(set(files))}
        for prov, files in sorted(blocked_files(root, rel_paths).items(), key=lambda kv: kv[0].folder)
    ]
