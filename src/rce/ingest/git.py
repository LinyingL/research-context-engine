"""Deterministic git history ingester -- zero-model extractor layer.

Shells out to system `git` (Occam rule 1: no pygit2/GitPython dependency) to
read commit history, writing Commit/Contributor nodes and `authored_by`
edges via rce.db's upsert_node/upsert_edge (idempotency is inherited from
there, not reimplemented here). list_source_files() additionally inventories
.tex/.bib/image/.py files for T2's LaTeX/.bib ingester and T6's pyfig
ingester (DESIGN.md section 5); it creates no graph nodes.
read_head_sha() (T6) reads the repo's current HEAD commit SHA, for
extractors that need "the commit as of ingestion time" rather than any
historical commit. blame_line() (T6 batch3-fix) resolves the commit that
last touched one specific file:line -- used by the pyfig ingester so a
savefig() call's src edge stays pinned to the commit that actually
introduced that line (DESIGN.md section 4 erratum: "src=生成代码所在
commit"), not whichever commit happens to be HEAD at ingestion time.
Unparseable git output is skipped and logged, never guessed at (section 5's
savefig rule: "拼不出来就放弃，不猜").

W1: a project root that is not a git repository at all (the common case for
a researcher's working directory that was never `git init`ed) is a normal,
supported state, not a fatal one. `_run_git` raises the narrower
`NotAGitRepositoryError` (a `GitIngestError` subclass) specifically for
git's own "not a git repository (or any of the parent directories)" message
-- distinct from every other `GitIngestError` cause (missing git binary,
permission error, a corrupt repo) that a caller still very much wants to
surface as fatal. `rce.cli.cmd_ingest` catches only this narrower type to
print one explanatory line and fall back to `rce.ingest.files.
list_source_files` (a filesystem walk) for the file inventory, then
continues running every other extractor; `rce.ingest.pyfig.
ingest_pyfig_repo` catches it (via the same `GitIngestError` base -- no
commit source node is resolvable either way) around its own
`read_head_sha` call and skips its scan the same way it already does for an
unborn repo, since a `generates` edge needs a real commit node to attach to
and neither case has one.

Non-ASCII path bug fix (real repro: a Chinese-filename research repo): git's
default `core.quotepath=true` escapes any non-ASCII byte in a path into a
backslash-octal-escaped, double-quoted string (e.g. `"\\346\\226\\207.py"`) for
any *human-oriented* (newline-separated) porcelain output --
`git ls-files` and `git log --name-only` both do this. `list_source_files`
and `read_commits` used to read exactly that human-oriented form, so a
tracked file whose name contained non-ASCII bytes (Chinese/Japanese/German
umlaut/French accent/Cyrillic, or a plain filename picked up by
quotepath's own non-ASCII detection) came back as an escaped, quoted string
that never matched any real path on disk -- silently invisible to every
downstream extractor, no warning at all. Both now pass `-z`, which git
documents as unconditionally disabling this quoting (independent of
`core.quotepath`) and NUL-terminating each path instead of newline --
verified via a dedicated regression test with `core.quotepath` explicitly
set to `true`. `_run_git` also now decodes git's output as UTF-8 with
`errors="surrogateescape"` rather than relying on the locale's preferred
encoding, and `_has_undecodable_bytes` flags the rare path that still
didn't survive that decode (e.g. a non-UTF-8 filesystem encoding) so that
one path is skipped + logged individually -- never silently dropped, and
never taking the rest of the listing/commit down with it.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

from rce import db

logger = logging.getLogger(__name__)

# ASCII record/unit separators: won't occur in ordinary commit metadata, so
# `git log --format=...` splits unambiguously without a real parser.
_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_LOG_FORMAT = _RECORD_SEP + _FIELD_SEP.join(["%H", "%an", "%ae", "%aI", "%B"]) + _FIELD_SEP

IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".pdf", ".svg", ".eps", ".gif", ".tiff", ".tif"}
)

# W1: tabular/data-file extensions a research project commonly carries
# alongside its code/paper -- shared with rce.ingest.files (the non-git
# filesystem-walk counterpart) so both inventories categorize identically,
# one source of truth rather than two extension lists that could drift.
DATA_EXTENSIONS = frozenset({".csv", ".xlsx", ".parquet", ".rds", ".dta", ".json"})

@dataclass(frozen=True)
class GitCommit:
    """One parsed `git log` record, raw as git reported it -- email case is
    preserved; lowercasing for the contributor identity key happens in
    ingest_git_repo, not here."""

    sha: str
    author_name: str
    author_email: str
    authored_at: str  # ISO 8601 (git %aI)
    message: str
    files: tuple[str, ...]

class GitIngestError(RuntimeError):
    """`git` failed for a reason other than "no commits yet" (not a repo,
    missing git binary, permission error, etc.)."""


class NotAGitRepositoryError(GitIngestError):
    """`repo_path` is not inside a git repository at all -- git's own "not a
    git repository (or any of the parent directories)" message, raised for
    every git subcommand identically regardless of which one was run. A
    narrower, catchable condition than `GitIngestError` in general (W1):
    callers that want to degrade gracefully for "there is simply no git
    here" (rce.cli.cmd_ingest, rce.ingest.pyfig.ingest_pyfig_repo) catch
    this specifically, while every other git failure (missing git binary,
    permission error, a corrupt repo) still surfaces as a fatal
    `GitIngestError` to them."""


def _run_git(repo_path: Path, args: list[str]) -> str:
    """Run `git <args>` and return stdout, decoded as UTF-8.

    `encoding="utf-8", errors="surrogateescape"` (rather than `text=True`,
    which would decode using the locale's preferred encoding -- not
    necessarily UTF-8, and not necessarily the same on every machine this
    runs on) makes the decode itself deterministic and never-raising: a
    byte that isn't valid UTF-8 becomes a lone surrogate codepoint instead
    of raising UnicodeDecodeError. That keeps a single bad byte from
    crashing the whole `git` call; callers that split this into individual
    path entries (list_source_files, read_commits) use
    `_has_undecodable_bytes` to detect -- and skip + log, never silently
    keep -- the one entry that round-trip actually failed on.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
    except FileNotFoundError as exc:
        raise GitIngestError(f"git executable not found: {exc}") from exc
    if result.returncode != 0:
        message = f"git {' '.join(args)} failed in {repo_path}: {result.stderr.strip()}"
        if "not a git repository" in result.stderr:
            raise NotAGitRepositoryError(message)
        raise GitIngestError(message)
    return result.stdout


def _has_undecodable_bytes(path: str) -> bool:
    """True if `path` contains a lone surrogate codepoint (U+DC80-U+DCFF) --
    i.e. `_run_git`'s `errors="surrogateescape"` decode had to paper over a
    byte sequence that was not valid UTF-8 (a path recorded under a
    non-UTF-8 filesystem encoding). Detection only; callers decide the
    reaction -- always skip + log that one path, never silently keep a
    mangled string and never guess at its real bytes."""
    return any(0xDC80 <= ord(ch) <= 0xDCFF for ch in path)

def read_commits(repo_path: str | Path) -> list[GitCommit]:
    """Read full commit history via `git log`, oldest first. An unborn repo
    (no commits yet) returns [] rather than raising -- that is expected
    state, not a failure. Any other git failure raises GitIngestError.

    `-z` (alongside `--name-only`) is what keeps a non-ASCII changed-file
    name intact: without it, git's default `core.quotepath=true` escapes
    any non-ASCII byte into an octal-quoted `"..."` string in this
    newline-separated form, which then never matches a real path on disk.
    `-z` NUL-terminates the file list unconditionally regardless of
    `core.quotepath` (see module docstring). Per commit, git emits the
    `--format` text (ending in `_FIELD_SEP`), then a single NUL where the
    blank-line separator would otherwise be, then each changed file
    NUL-terminated -- `.split("\x00")` + per-entry `.strip()` below absorbs
    that leading artifact the same way `.strip()` already absorbed the old
    leading/trailing newlines.
    """
    repo_path = Path(repo_path)
    try:
        raw = _run_git(
            repo_path, ["log", "--reverse", "-z", "--name-only", f"--format={_LOG_FORMAT}"]
        )
    except GitIngestError as exc:
        if "does not have any commits yet" in str(exc):
            return []
        raise

    commits: list[GitCommit] = []
    for record in raw.split(_RECORD_SEP):
        if not record.strip():
            continue
        parts = record.split(_FIELD_SEP, 4)
        if len(parts) != 5:
            logger.warning(
                "skipping unparseable git log record (got %d fields, want 5): %r",
                len(parts), record[:120],
            )
            continue
        sha, author_name, author_email, authored_at, rest = parts
        if not sha.strip():
            logger.warning("skipping git log record with empty sha")
            continue
        message, _, file_blob = rest.partition(_FIELD_SEP)
        # `git log -z --name-only` emits NUL then a literal newline before the
        # first changed file. Strip that artifact once, on the blob -- never
        # per entry: with -z each entry is a byte-exact path, and a leading or
        # trailing space is a legal part of a filename, not whitespace to trim.
        file_blob = file_blob.removeprefix("\x00").removeprefix("\n")
        files: list[str] = []
        for path in file_blob.split("\x00"):
            if not path:
                continue
            if _has_undecodable_bytes(path):
                logger.warning(
                    "commit %s: skipping changed-file entry with bytes that are "
                    "not valid UTF-8 (cannot resolve the real filename, not "
                    "guessing): %r", sha.strip(), path,
                )
                continue
            files.append(path)
        commits.append(GitCommit(
            sha=sha.strip(), author_name=author_name, author_email=author_email,
            authored_at=authored_at, message=message.rstrip("\n"), files=tuple(files),
        ))
    return commits

def ingest_git_repo(conn: Connection, repo_path: str | Path) -> int:
    """Ingest a repo's commit history into the provenance graph.

    Per commit: upsert Commit node `commit:<sha>`, upsert Contributor node
    `contributor:<lowercase email>`, upsert `authored_by` edge
    (extractor="git", confidence=1.0, status="auto", evidence={"sha": ...}).
    Idempotent via db.upsert_node/upsert_edge's ON CONFLICT clauses -- no
    dedup logic here. A commit with no parseable author email skips only
    its contributor node/edge (logged, not guessed). Returns the number of
    commits for which a Commit node was written.
    """
    repo_path = Path(repo_path)
    commits = read_commits(repo_path)
    ingested = 0
    for commit in commits:
        commit_id = f"commit:{commit.sha}"
        subject = commit.message.splitlines()[0] if commit.message else ""
        db.upsert_node(
            conn, commit_id, "commit", title=subject,
            attrs={
                "message": commit.message,
                "authored_at": commit.authored_at,
                "author_name": commit.author_name,
                "author_email": commit.author_email,
                "files": list(commit.files),
            },
        )
        ingested += 1

        email = commit.author_email.strip().lower()
        if not email:
            logger.warning(
                "commit %s has no author email; skipping contributor edge", commit.sha
            )
            continue
        contributor_id = f"contributor:{email}"
        db.upsert_node(
            conn, contributor_id, "contributor", title=commit.author_name or email,
            attrs={"name": commit.author_name, "email": email},
        )
        db.upsert_edge(
            conn, commit_id, contributor_id, "authored_by",
            extractor="git", evidence={"sha": commit.sha}, confidence=1.0, status="auto",
        )
    return ingested

def list_source_files(repo_path: str | Path) -> dict[str, list[str]]:
    """Inventory git-tracked .tex/.bib/image/.py/.md/.r/.rmd/data files,
    grouped by category.

    Uses `git ls-files` (tracked files only, .gitignore respected for free)
    instead of a filesystem walk -- no ignore-matching logic to maintain.
    Creates no graph nodes.

    `-z` NUL-terminates each path and unconditionally disables git's
    quoting of non-ASCII paths (independent of `core.quotepath` -- see
    module docstring): without it, any git-tracked file whose name
    contains non-ASCII bytes comes back as an escaped `"\\NNN..."` string
    that matches no real path on disk and silently vanishes from every
    category below -- the actual bug this fixes. A path entry that still
    doesn't decode cleanly as UTF-8 (`_has_undecodable_bytes`) is skipped
    and logged individually rather than guessed at or silently dropped.

    W1: md/r/rmd/data categories were added alongside the non-git
    filesystem-walk counterpart (`rce.ingest.files.list_source_files`) so a
    git-tracked and a filesystem-scanned inventory carry the same shape --
    same category keys, same extension-to-category rules (`DATA_EXTENSIONS`
    is the one shared source of truth for the "data" category, see its
    definition above).
    """
    repo_path = Path(repo_path)
    output = _run_git(repo_path, ["ls-files", "-z"])
    inventory: dict[str, list[str]] = {
        "tex": [], "bib": [], "image": [], "py": [], "md": [], "r": [], "rmd": [], "data": [],
    }
    for path in output.split("\x00"):
        # No .strip(): `ls-files -z` entries are byte-exact, and leading or
        # trailing whitespace is a legal part of a filename.
        if not path:
            continue
        if _has_undecodable_bytes(path):
            logger.warning(
                "skipping git-tracked path with bytes that are not valid UTF-8 "
                "(cannot resolve the real filename, not guessing): %r", path,
            )
            continue
        suffix = Path(path).suffix.lower()
        if suffix == ".tex":
            inventory["tex"].append(path)
        elif suffix == ".bib":
            inventory["bib"].append(path)
        elif suffix in IMAGE_EXTENSIONS:
            inventory["image"].append(path)
        elif suffix == ".py":
            inventory["py"].append(path)
        elif suffix == ".md":
            inventory["md"].append(path)
        elif suffix == ".r":
            inventory["r"].append(path)
        elif suffix == ".rmd":
            inventory["rmd"].append(path)
        elif suffix in DATA_EXTENSIONS:
            inventory["data"].append(path)
    return inventory


def read_head_sha(repo_path: str | Path) -> str | None:
    """Current HEAD commit SHA, or None for an unborn repo (no commits yet)
    -- mirrors read_commits' treatment of that case as expected state, not a
    failure. Any other git failure (not a repo, missing git binary, ...)
    still raises GitIngestError via _run_git. Used by T6's pyfig ingester,
    which needs "the commit as of ingestion time" rather than any specific
    historical commit.
    """
    repo_path = Path(repo_path)
    try:
        raw = _run_git(repo_path, ["rev-parse", "HEAD"])
    except GitIngestError as exc:
        if "unknown revision" in str(exc):
            return None
        raise
    sha = raw.strip()
    return sha or None


def blame_line(repo_path: str | Path, file_path: str, line: int) -> str | None:
    """The commit SHA that last touched `file_path`'s `line` (1-indexed), via
    `git blame --porcelain -L <line>,<line>`.

    Returns None -- log a warning, never guess -- when:
    - the line is a local, not-yet-committed edit (git's all-zero pseudo-sha
      "0000...0", meaning there is no real commit to attribute it to yet)
    - `git blame` fails outright (e.g. unborn repo, path not tracked) or its
      output does not parse as expected

    Any other outcome is a real 40-hex commit SHA already reachable from
    HEAD's history, so callers may reference it as a Commit node id without
    upserting that node themselves (T1's ingest_git_repo, which always runs
    first in the CLI's ingest order, already created it).
    """
    try:
        raw = _run_git(
            Path(repo_path), ["blame", "--porcelain", "-L", f"{line},{line}", "--", file_path]
        )
    except GitIngestError as exc:
        logger.warning("git blame failed for %s:%d: %s", file_path, line, exc)
        return None
    first_line = raw.splitlines()[0] if raw.splitlines() else ""
    sha = first_line.split(" ", 1)[0] if first_line else ""
    if len(sha) != 40:
        logger.warning("could not parse git blame output for %s:%d: %r", file_path, line, first_line)
        return None
    if sha == "0" * 40:
        logger.warning(
            "%s:%d has uncommitted local changes; cannot attribute to a commit, skipping",
            file_path, line,
        )
        return None
    return sha
