"""Deterministic savefig() static analysis ingester -- zero-model extractor
layer (T6, HANDOFF-SPEC.md section 5 connector 5). Uses stdlib `ast` (Occam
rule 1/2: no third-party AST/regex library) to find
`plt.savefig("...")`/`fig.savefig("...")`/`savefig("...")` call sites in
git-tracked .py files whose first positional argument is a plain string
literal. f-strings, name references, and any other computed expression are
never guessed at -- HANDOFF-SPEC.md section 5: "拼不出来就放弃，不猜" -- they
are skipped and logged. Writes `Commit --generates--> Figure` edges via
rce.db's upsert_node/upsert_edge (idempotency inherited from there).
"""

from __future__ import annotations

import ast
import logging
import posixpath
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

from rce import db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SavefigCall:
    """One savefig(...) call site found by the AST scan, before path
    resolution -- `literal` is the raw string exactly as written in source."""

    py_path: str
    line: int
    callee: str
    literal: str


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


def _first_arg_literal(call: ast.Call) -> str | None:
    """The first positional argument's value, only when it is a plain string
    literal. f-strings (ast.JoinedStr), name references, attribute access,
    concatenation, and any other expression all return None here --
    constitution: never guess at a path that isn't spelled out verbatim."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def parse_py_file(repo_root: str | Path, py_rel_path: str) -> list[SavefigCall]:
    """Scan one .py file for savefig(...) call sites. A file that fails to
    parse (SyntaxError -- e.g. non-Python source under a .py extension) is
    skipped + logged, not fatal to the whole ingest run."""
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

    calls: list[SavefigCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee_name(node.func)
        if callee is None:
            continue
        literal = _first_arg_literal(node)
        if literal is None:
            logger.warning(
                "%s:%d: %s(...) first argument is not a string literal "
                "(f-string/variable/expression); skipping, not guessing",
                py_rel_path, node.lineno, callee,
            )
            continue
        calls.append(SavefigCall(py_rel_path, node.lineno, callee, literal))
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
    never a guessed Figure node (HANDOFF-SPEC.md section 5)."""
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
    head_sha: str | None,
) -> dict[str, int]:
    """Ingest savefig(...) call sites into `Commit --generates--> Figure`
    edges (HANDOFF-SPEC.md section 5 connector 5).

    `head_sha` is the repo's current HEAD commit -- the src side of every
    edge, since this is static analysis of the code *at ingestion time*
    (HANDOFF-SPEC.md section 4, 2026-07-22 erratum: "src=生成代码所在
    commit"), not whichever commit historically introduced a line. `None`
    (unborn repo, no commits yet) skips the whole scan rather than inventing
    a placeholder commit.

    `image_paths` is the exact set of git-tracked image files (e.g. from
    rce.ingest.git.list_source_files()["image"]) a resolved literal must
    hit -- mirrors rce.ingest.latex's ghost-figure guard, so every Figure
    node created here is backed by a real repo file. Idempotent via
    db.upsert_node/upsert_edge.
    """
    counts = {"generates": 0}
    if head_sha is None:
        logger.warning("repo has no HEAD commit yet (unborn repo); skipping savefig scan")
        return counts

    commit_id = f"commit:{head_sha}"
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
            figure_id = f"figure:{figure_path}"
            db.upsert_node(conn, figure_id, "figure", title=figure_path)
            db.upsert_edge(
                conn, commit_id, figure_id, "generates", extractor="pyfig",
                evidence={"file": call.py_path, "line": call.line, "callee": call.callee},
                confidence=1.0, status="auto",
            )
            counts["generates"] += 1
    return counts
