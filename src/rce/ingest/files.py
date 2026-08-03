"""Filesystem-walk file inventory (W1) -- the non-git counterpart to
`rce.ingest.git.list_source_files`, used when a project root is not (or not
yet) a git repository at all.

`list_source_files(root)` returns the same category shape as its git
sibling (tex/bib/image/py/md/r/rmd/data), built by walking the filesystem
directly with stdlib `os.walk` (Occam rule 1: no third-party ignore-file
parser) instead of `git ls-files`. Creates no graph nodes, exactly like the
git version.

Because there is no `.gitignore` to consult, this walk skips a fixed set of
noise directories a research project routinely accumulates that a git-
tracked listing would never have surfaced anyway (a repo's own .gitignore
commonly excludes exactly these; see `_NOISE_DIRS` below) -- plus every
dot-prefixed directory outright (a project's own tool caches --
`.mypy_cache`, `.pytest_cache`, `.idea`, ... -- are not enumerable in a
fixed list, so "any directory whose name starts with '.'" is the
deterministic stand-in DESIGN.md section 0 asks for, not a guess). Hidden
files (dot-prefixed filenames) are skipped the same way. No symlink is ever
followed, to a directory or to a file -- `os.walk(..., followlinks=False)`
already keeps the walk from descending into a symlinked directory; a
symlinked *file* is still reported in `os.walk`'s own `filenames` list
regardless of `followlinks` (that flag only governs directory traversal),
so it is filtered out explicitly via `Path.is_symlink()`.
"""

from __future__ import annotations

import os
from pathlib import Path

from rce.ingest.git import DATA_EXTENSIONS, IMAGE_EXTENSIONS

# Directories a research project accumulates that are never source material
# in their own right -- version control internals, this tool's own state,
# Python/R tooling caches, and common dependency directories. Every dot-
# prefixed directory is *also* skipped (see list_source_files below), so
# ".git"/".rce"/".Rproj.user"/".ipynb_checkpoints" are listed here only for
# readability, not because the dot-prefix rule wouldn't already catch them.
_NOISE_DIRS = frozenset(
    {
        ".git", ".rce", "__pycache__", ".venv", "node_modules",
        ".Rproj.user", "_cache", ".ipynb_checkpoints",
    }
)

_CATEGORY_BY_SUFFIX: dict[str, str] = {
    ".tex": "tex",
    ".bib": "bib",
    ".py": "py",
    ".md": "md",
    ".r": "r",
    ".rmd": "rmd",
}


def _category_for(suffix: str) -> str | None:
    """Which inventory bucket `suffix` (already lowercased) belongs to, or
    None if it matches none of the tracked categories."""
    if suffix in _CATEGORY_BY_SUFFIX:
        return _CATEGORY_BY_SUFFIX[suffix]
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in DATA_EXTENSIONS:
        return "data"
    return None


def list_source_files(root: str | Path) -> dict[str, list[str]]:
    """Inventory .tex/.bib/image/.py/.md/.r/.rmd/data files under `root` by
    walking the filesystem, grouped by category -- same shape as
    `rce.ingest.git.list_source_files`, for a project root that has no git
    repository to ask instead. Creates no graph nodes.

    Returned paths are root-relative and forward-slash-normalized
    (`Path.as_posix()`), matching git's own path convention so downstream
    extractors (latex/pyfig/claims) can treat either inventory identically.
    """
    root = Path(root)
    inventory: dict[str, list[str]] = {
        "tex": [], "bib": [], "image": [], "py": [], "md": [], "r": [], "rmd": [], "data": [],
    }
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune in place (os.walk respects in-place mutation of dirnames)
        # so a noise/hidden directory is never even descended into, not
        # merely filtered out of the results afterward.
        dirnames[:] = sorted(d for d in dirnames if d not in _NOISE_DIRS and not d.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            full_path = Path(dirpath) / filename
            if full_path.is_symlink():
                continue
            category = _category_for(full_path.suffix.lower())
            if category is None:
                continue
            inventory[category].append(full_path.relative_to(root).as_posix())
    return inventory
