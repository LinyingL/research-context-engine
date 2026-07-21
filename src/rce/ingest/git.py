"""Deterministic git history ingester -- zero-model extractor layer.

Shells out to system `git` (Occam rule 1: no pygit2/GitPython dependency) to
read commit history, writing Commit/Contributor nodes and `authored_by`
edges via rce.db's upsert_node/upsert_edge (idempotency is inherited from
there, not reimplemented here). list_source_files() additionally inventories
.tex/.bib/image files for T2's LaTeX/.bib ingester (HANDOFF-SPEC.md section
5); it creates no graph nodes. Unparseable git output is skipped and logged,
never guessed at (section 5's savefig rule: "拼不出来就放弃，不猜").
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

def _run_git(repo_path: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args], capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise GitIngestError(f"git executable not found: {exc}") from exc
    if result.returncode != 0:
        raise GitIngestError(
            f"git {' '.join(args)} failed in {repo_path}: {result.stderr.strip()}"
        )
    return result.stdout

def read_commits(repo_path: str | Path) -> list[GitCommit]:
    """Read full commit history via `git log`, oldest first. An unborn repo
    (no commits yet) returns [] rather than raising -- that is expected
    state, not a failure. Any other git failure raises GitIngestError.
    """
    repo_path = Path(repo_path)
    try:
        raw = _run_git(
            repo_path, ["log", "--reverse", "--name-only", f"--format={_LOG_FORMAT}"]
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
        files = tuple(
            line.strip() for line in file_blob.strip("\n").splitlines() if line.strip()
        )
        commits.append(GitCommit(
            sha=sha.strip(), author_name=author_name, author_email=author_email,
            authored_at=authored_at, message=message.rstrip("\n"), files=files,
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
    """Inventory git-tracked .tex/.bib/image files, grouped by category.

    Uses `git ls-files` (tracked files only, .gitignore respected for free)
    instead of a filesystem walk -- no ignore-matching logic to maintain.
    Creates no graph nodes.
    """
    repo_path = Path(repo_path)
    output = _run_git(repo_path, ["ls-files"])
    inventory: dict[str, list[str]] = {"tex": [], "bib": [], "image": []}
    for line in output.splitlines():
        path = line.strip()
        if not path:
            continue
        suffix = Path(path).suffix.lower()
        if suffix == ".tex":
            inventory["tex"].append(path)
        elif suffix == ".bib":
            inventory["bib"].append(path)
        elif suffix in IMAGE_EXTENSIONS:
            inventory["image"].append(path)
    return inventory
