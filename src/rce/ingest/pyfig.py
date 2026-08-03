"""Deterministic savefig() static analysis ingester -- zero-model extractor
layer (T6, DESIGN.md section 5 connector 5). Uses stdlib `ast` (Occam
rule 1/2: no third-party AST/regex library) to find
`plt.savefig("...")`/`fig.savefig("...")`/`savefig("...")` call sites in
git-tracked .py files whose first positional argument is a plain string
literal, or one of a narrow set of same-file constant-foldable expressions
(T9, see `rce.ingest.pyconst`). Anything outside those shapes is never
guessed at -- DESIGN.md section 5: "拼不出来就
放弃，不猜" -- it is skipped and logged. Writes `Commit --generates--> Figure`
edges via rce.db's upsert_node/upsert_edge.

T9: constant folding (which module-level string names may be substituted into
a savefig(...) argument expression) is implemented in `rce.ingest.pyconst`,
shared with `rce.ingest.dataflow` (task W2) rather than duplicated -- see
that module's docstring for the exact folding rules (which name-binding forms
count as "touched", which three expression shapes fold, and why
`pathlib.Path`'s `/` operator is deliberately excluded).

Each edge's src commit is resolved per call site via `git blame`
(rce.ingest.git.blame_line), pinned to whichever commit last touched that
specific savefig(...) line -- DESIGN.md section 4 erratum: "src=生成
代码所在 commit". This (not the repo's current HEAD) is what keeps a
re-ingest idempotent: an unchanged plotting script blames to the same
commit run after run, so upsert_edge's (src, dst, type, extractor) key is
stable and updates the same row instead of accumulating one stale edge per
intervening commit (batch3-fix; HEAD-keyed src was the bug).
"""

from __future__ import annotations

import ast
import logging
import posixpath
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

from rce import db
from rce.ingest import git as git_ingest
from rce.ingest import pyconst

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SavefigCall:
    """One savefig(...) call site found by the AST scan, before path
    resolution -- `literal` is the resolved string (verbatim, or folded per
    T9). `folded_from` is None for a plain literal, else the original
    expression's exact source text (T9 evidence trail)."""

    py_path: str
    line: int
    callee: str
    literal: str
    folded_from: str | None = None


def _callee_name(func: ast.expr) -> str | None:
    """A label for the call target if it matches the documented shapes:
    `<name>.savefig(...)` (e.g. plt.savefig, fig.savefig) or a bare
    `savefig(...)`. Anything else (e.g. self.fig.savefig(...), a subscript,
    a call result's .savefig(...)) is not one of the three patterns T6
    handles, so it is not matched at all -- not even as an unresolved skip."""
    if isinstance(func, ast.Attribute) and func.attr == "savefig":
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.savefig"
        return None
    if isinstance(func, ast.Name) and func.id == "savefig":
        return "savefig"
    return None


def _resolve_first_arg(
    call: ast.Call, constants: dict[str, str]
) -> tuple[str, ast.expr | None] | None:
    """Resolve savefig(...)'s first arg. Returns `(value, folded_expr)`:
    `folded_expr` is None for a plain literal (unchanged pre-T9), else the
    original AST node (T9) -- caller extracts its exact source text for the
    evidence trail. None if unresolvable (not a literal, not foldable)."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value, None
    folded = pyconst.fold_expr(first, constants)
    if folded is None:
        return None
    return folded, first


def parse_py_file(repo_root: str | Path, py_rel_path: str) -> list[SavefigCall]:
    """Scan one .py file for savefig(...) call sites. A file that fails to
    parse (SyntaxError -- e.g. non-Python source under a .py extension) is
    skipped + logged, not fatal to the whole ingest run. Module-level string
    constants are collected once per file (T9) so every call site can fold
    against the same table."""
    path = Path(repo_root) / py_rel_path
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        logger.warning("cannot read %s: %s", py_rel_path, exc)
        return []
    try:
        tree = ast.parse(text, filename=py_rel_path)
    except SyntaxError as exc:
        logger.warning("%s: cannot parse as Python (%s); skipping file", py_rel_path, exc)
        return []

    module_constants = pyconst.collect_module_string_constants(tree)

    calls: list[SavefigCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee_name(node.func)
        if callee is None:
            continue
        resolved = _resolve_first_arg(node, module_constants)
        if resolved is None:
            logger.warning(
                "%s:%d: %s(...) first argument is not a string literal "
                "(f-string/variable/expression); skipping, not guessing",
                py_rel_path, node.lineno, callee,
            )
            continue
        literal, folded_expr = resolved
        folded_from = None
        if folded_expr is not None:
            folded_from = ast.get_source_segment(text, folded_expr) or ast.unparse(folded_expr)
        calls.append(SavefigCall(py_rel_path, node.lineno, callee, literal, folded_from))
    return calls


def _normalize_candidate(base_dir: str, raw_path: str) -> str | None:
    """Join+normalize a literal against `base_dir`, rejecting an absolute
    filesystem path (not something we can safely remap into the repo) or
    anything that normalizes outside the repo root -- same guard as
    rce.ingest.latex._resolve_figure_path."""
    if not raw_path or posixpath.isabs(raw_path):
        return None
    normalized = posixpath.normpath(posixpath.join(base_dir, raw_path))
    if normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def _resolve_figure_target(py_rel_path: str, literal: str, known_images: set[str]) -> str | None:
    """Resolve a savefig literal to a repo-relative path: first try it
    relative to the repo root (same `figure:<repo-relative path>` id
    convention as rce.ingest.latex), then fall back to the script's own
    directory (the common `plt.savefig("out.png")` case). Must land on a
    real git-tracked image file (`known_images`) -- otherwise unresolved,
    never a guessed Figure node (DESIGN.md section 5)."""
    root_candidate = _normalize_candidate(".", literal)
    if root_candidate is not None and root_candidate in known_images:
        return root_candidate
    script_dir = posixpath.dirname(py_rel_path) or "."
    dir_candidate = _normalize_candidate(script_dir, literal)
    if dir_candidate is not None and dir_candidate in known_images:
        return dir_candidate
    return None


def ingest_pyfig_repo(
    conn: Connection,
    repo_root: str | Path,
    py_paths: list[str],
    image_paths: list[str],
) -> dict[str, int]:
    """Ingest savefig(...) call sites into `Commit --generates--> Figure`
    edges (DESIGN.md section 5 connector 5).

    Each edge's src is resolved per call site via `git blame` (batch3-fix,
    see module docstring) -- the commit that last touched that exact
    savefig(...) line, not the repo's current HEAD. A repo with no HEAD
    commit yet (unborn repo), or no git repository at all (W1 -- a plain
    filesystem-scanned project, see rce.ingest.files), skips the whole scan
    rather than inventing a placeholder commit -- a `generates` edge's src
    is a real Commit node, and neither case has one to offer. A savefig
    line that is only a local, uncommitted edit skips just that one call
    site (all three logged, never guessed at).

    `image_paths` is the exact set of git-tracked image files (e.g. from
    rce.ingest.git.list_source_files()["image"]) a resolved literal must
    hit -- mirrors rce.ingest.latex's ghost-figure guard, so every Figure
    node created here is backed by a real repo file. Runs identically
    whether `call.literal` is a plain literal or was constant-folded (T9):
    folding is never a pass around this guard. Idempotent via
    db.upsert_node/upsert_edge plus the stable blame-resolved src.
    """
    counts = {"generates": 0}
    try:
        head_sha = git_ingest.read_head_sha(repo_root)
    except git_ingest.GitIngestError as exc:
        # W1: no git repository here at all (or some other git failure) --
        # either way there is no commit history to resolve a `generates`
        # edge's src node from, so the whole scan is skipped rather than
        # inventing a placeholder commit. Caught as the GitIngestError base
        # (not just NotAGitRepositoryError): every cause reduces to the same
        # "no commit source node available" outcome for this extractor.
        logger.warning(
            "no usable git history at %s (%s); a generates edge needs a real commit "
            "source node, which requires git -- skipping the savefig scan entirely",
            repo_root, exc,
        )
        return counts
    if head_sha is None:
        logger.warning("repo has no HEAD commit yet (unborn repo); skipping savefig scan")
        return counts

    known_images = set(image_paths)
    for py_rel_path in py_paths:
        for call in parse_py_file(repo_root, py_rel_path):
            figure_path = _resolve_figure_target(call.py_path, call.literal, known_images)
            if figure_path is None:
                logger.warning(
                    "%s:%d: %s(%r) does not resolve to a tracked repo image; skipping",
                    call.py_path, call.line, call.callee, call.literal,
                )
                continue
            blame_sha = git_ingest.blame_line(repo_root, call.py_path, call.line)
            if blame_sha is None:
                logger.warning(
                    "%s:%d: %s(...) cannot be attributed to a commit via git blame; skipping",
                    call.py_path, call.line, call.callee,
                )
                continue
            commit_id = f"commit:{blame_sha}"
            figure_id = f"figure:{figure_path}"
            evidence = {"file": call.py_path, "line": call.line, "callee": call.callee}
            if call.folded_from is not None:
                evidence["folded_from"] = call.folded_from  # T9: folded argument only
            db.upsert_node(conn, figure_id, "figure", title=figure_path)
            db.upsert_edge(
                conn, commit_id, figure_id, "generates", extractor="pyfig",
                evidence=evidence,
                confidence=1.0, status="auto",
            )
            counts["generates"] += 1
    return counts
