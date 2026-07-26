"""Deterministic savefig() static analysis ingester -- zero-model extractor
layer (T6, HANDOFF-SPEC.md section 5 connector 5). Uses stdlib `ast` (Occam
rule 1/2: no third-party AST/regex library) to find
`plt.savefig("...")`/`fig.savefig("...")`/`savefig("...")` call sites in
git-tracked .py files whose first positional argument is a plain string
literal, or one of a narrow set of same-file constant-foldable expressions
(T9, see `_fold_expr`/`_collect_module_string_constants`). Anything outside
those shapes is never guessed at -- HANDOFF-SPEC.md section 5: "拼不出来就
放弃，不猜" -- it is skipped and logged. Writes `Commit --generates--> Figure`
edges via rce.db's upsert_node/upsert_edge.

T9: a name folds only if assigned exactly once, as a plain single-target
`NAME = "..."` statement directly in the module's top-level body, to a bare
string literal -- AND that name is touched nowhere else in the entire file
(T-blocker fix: "touched" is checked via a whole-file `ast.walk`, not just
`tree.body`, so this also excludes a name reassigned inside if/for/try/
with/def at module level, reused as a function or lambda parameter or
local variable, captured by a `match` pattern, aliased by a PEP 695 `type`
statement, declared `global`/`nonlocal`, imported, or deleted -- see
`_count_all_name_bindings` for the exact, non-exhaustive enumeration this
scan recognizes). Given that table, three shapes fold: an
f-string whose every interpolation is itself foldable; `"+"` concatenation
of foldable operands; `os.path.join(...)` whose every argument is foldable.
`pathlib.Path`'s `/` operator is deliberately excluded -- it dispatches on
the left operand's runtime type, which would mean guessing at cross-module
semantics rather than deterministic same-file analysis (architecture
decision, not an oversight). Anything else makes the expression unfoldable;
skipped and logged exactly as pre-T9.

Each edge's src commit is resolved per call site via `git blame`
(rce.ingest.git.blame_line), pinned to whichever commit last touched that
specific savefig(...) line -- HANDOFF-SPEC.md section 4 erratum: "src=生成
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
from typing import Callable

from rce import db
from rce.ingest import git as git_ingest

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


def _touch_binding_target(target: ast.expr, touch: Callable[[str], None]) -> None:
    """Recursively record every bare name a single binding target touches --
    a plain `Name`, or a `Tuple`/`List`/`Starred` unpacking pattern nested
    arbitrarily deep (e.g. `a, (b, *c) = ...`). `Attribute`/`Subscript`
    targets (`obj.attr = ...`, `d[k] = ...`) bind no bare name, so they are
    not touched at all."""
    if isinstance(target, ast.Name):
        touch(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _touch_binding_target(elt, touch)
    elif isinstance(target, ast.Starred):
        _touch_binding_target(target.value, touch)


# ast.TypeAlias (PEP 695 `type NAME = ...` statements) exists only on
# Python 3.12+; this project requires >=3.11 (pyproject.toml). Looked up
# once via getattr with a default so a missing attribute never raises --
# writing `ast.TypeAlias` directly in the isinstance check below would blow
# up on 3.11 before HANDOFF-SPEC.md's "don't guess" rule ever got a chance
# to apply.
_TYPE_ALIAS_NODE_TYPE = getattr(ast, "TypeAlias", None)


def _count_all_name_bindings(tree: ast.Module) -> dict[str, int]:
    """Count every name-binding touch anywhere in the whole file (T-blocker
    fix, replaces a `tree.body`-only scan -- see `_collect_module_string_
    constants`'s docstring for why that was insufficient). Uses `ast.walk`
    over the entire tree, so a binding nested inside if/for/try/with/def at
    module level -- invisible to a `tree.body`-only scan -- is counted, and
    so is a same-named binding inside a function's (or lambda's) own
    scope, a `match` capture pattern, or a PEP 695 `type` alias that would
    otherwise silently shadow a module-level constant of the same name.

    This is a concrete enumeration of what this scan recognizes, not a
    claim of exhaustive Python-grammar coverage -- a binding form not
    listed here is not tracked, and a future language addition could
    silently reopen this same class of bug (2026-07 Opus re-review
    blocker fix: Lambda parameters, `match` capture patterns, and
    `TypeAlias` names were the three forms missing before this fix, each
    letting a rebound name still fold to its stale module-level literal).
    Forms counted today: `Assign`/`AugAssign`/`AnnAssign`/`NamedExpr`
    (walrus) targets; `For`/`AsyncFor` targets; `With`/`AsyncWith`
    `optional_vars`; `ExceptHandler.name`; comprehension targets;
    `FunctionDef`/`AsyncFunctionDef`/`ClassDef` names and every parameter
    name; `Lambda` parameter names; `global`/`nonlocal` declarations;
    import (as-)names; `Delete` targets; `match` capture patterns
    (`MatchAs`/`MatchStar` `.name`, `MatchMapping.rest`); and, on Python
    3.12+ only, a PEP 695 `type NAME = ...` statement's `TypeAlias.name`.
    """
    touch_count: dict[str, int] = {}

    def touch(name: str) -> None:
        touch_count[name] = touch_count.get(name, 0) + 1

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                _touch_binding_target(target, touch)
        elif isinstance(node, ast.NamedExpr):  # walrus (D := ...)
            _touch_binding_target(node.target, touch)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _touch_binding_target(node.target, touch)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _touch_binding_target(item.optional_vars, touch)
        elif isinstance(node, ast.ExceptHandler):
            if node.name is not None:
                touch(node.name)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for generator in node.generators:
                _touch_binding_target(generator.target, touch)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            touch(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                    touch(arg.arg)
                if args.vararg is not None:
                    touch(args.vararg.arg)
                if args.kwarg is not None:
                    touch(args.kwarg.arg)
        elif isinstance(node, ast.Lambda):
            # Same arg-touching shape as FunctionDef/AsyncFunctionDef above,
            # minus `node.name` (a Lambda has none) -- a lambda parameter
            # shadows a same-named module constant inside its body exactly
            # like a `def`'s parameter does (Opus re-review blocker fix).
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                touch(arg.arg)
            if args.vararg is not None:
                touch(args.vararg.arg)
            if args.kwarg is not None:
                touch(args.kwarg.arg)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            # `case OUT:` / `case [*OUT]` both bind through `.name`, which
            # is None for a bare wildcard (`case _:` / `case [*_]`) -- no
            # binding to count there (Opus re-review blocker fix).
            if node.name is not None:
                touch(node.name)
        elif isinstance(node, ast.MatchMapping):
            # `case {**OUT}` binds through `.rest`, same None-means-no-
            # capture convention as MatchAs/MatchStar.name above.
            if node.rest is not None:
                touch(node.rest)
        elif _TYPE_ALIAS_NODE_TYPE is not None and isinstance(node, _TYPE_ALIAS_NODE_TYPE):
            # PEP 695 `type OUT = ...` (Python 3.12+ only -- see
            # _TYPE_ALIAS_NODE_TYPE above); `.name` is itself a `Name` node,
            # so it goes through the same target-walker as Assign/For/etc.
            _touch_binding_target(node.name, touch)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                touch(name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                touch(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                touch(alias.asname or alias.name)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                _touch_binding_target(target, touch)

    return touch_count


def _collect_module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level names foldable into a savefig(...) expression (T9,
    T-blocker fix).

    Pass 1 (`_count_all_name_bindings`) counts every name binding anywhere
    in the *whole file*, not just `tree.body` -- see that function's
    docstring for the full list of binding forms and why a `tree.body`-only
    scan missed reassignments nested inside if/for/try/with/def at module
    level, and same-name shadowing inside a function's own scope (a
    savefig() call inside `def plot(D): ...` must never fold using the
    module-level D -- the function's own D parameter shadows it at
    runtime).

    Pass 2 keeps only a name whose *sole* touch (count == 1) is a top-level
    `NAME = "..."` Assign directly in `tree.body`, to a bare string literal
    -- unchanged from pre-fix. Any name touched anywhere else in the file
    (conditionally, in a loop, imported, deleted, declared global, used as a
    parameter or function/class name, ...) is excluded regardless of how
    many times it looks foldable at the top level -- HANDOFF-SPEC.md
    section 5: "拼不出来就放弃，不猜".
    """
    touch_count = _count_all_name_bindings(tree)

    values: dict[str, str] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            continue  # multi-target chain or tuple/list unpacking -- never single-name-foldable
        name = stmt.targets[0].id
        if touch_count.get(name, 0) != 1:
            continue  # touched elsewhere in the file -- ambiguous, don't fold
        if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            values[name] = stmt.value.value
    return values


def _is_os_path_join(func: ast.expr) -> bool:
    """Matches only the dotted attribute chain `os.path.join` -- not an
    aliased import, which would need tracking imports, not a syntactic check."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "join"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "path"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "os"
    )


def _fold_expr(expr: ast.expr, constants: dict[str, str]) -> str | None:
    """Fold `expr` to a string using only literals and `constants` (T9).
    Supports exactly three shapes (see module docstring for why
    `pathlib.Path`'s `/` is excluded): an f-string whose every
    interpolation has no conversion/format-spec and itself folds; `"+"`
    concatenation of two foldable operands; `os.path.join(...)` (no
    `**kwargs`/`*args`) whose every argument folds, joined with
    `posixpath.join` to match this module's forward-slash convention. Any
    other component returns None -- treated exactly like an unresolvable
    literal: skip and log, never guess at a partial path.
    """
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        return constants.get(expr.id)
    if isinstance(expr, ast.JoinedStr):
        parts: list[str] = []
        for value in expr.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                if value.conversion != -1 or value.format_spec is not None:
                    return None
                folded = _fold_expr(value.value, constants)
                if folded is None:
                    return None
                parts.append(folded)
            else:
                return None
        return "".join(parts)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _fold_expr(expr.left, constants)
        right = _fold_expr(expr.right, constants)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(expr, ast.Call) and _is_os_path_join(expr.func):
        if expr.keywords:
            return None
        join_parts: list[str] = []
        for arg in expr.args:
            if isinstance(arg, ast.Starred):
                return None
            folded = _fold_expr(arg, constants)
            if folded is None:
                return None
            join_parts.append(folded)
        if not join_parts:
            return None
        return posixpath.join(*join_parts)
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
    folded = _fold_expr(first, constants)
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

    module_constants = _collect_module_string_constants(tree)

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
) -> dict[str, int]:
    """Ingest savefig(...) call sites into `Commit --generates--> Figure`
    edges (HANDOFF-SPEC.md section 5 connector 5).

    Each edge's src is resolved per call site via `git blame` (batch3-fix,
    see module docstring) -- the commit that last touched that exact
    savefig(...) line, not the repo's current HEAD. A repo with no HEAD
    commit yet (unborn repo) skips the whole scan rather than inventing a
    placeholder commit; a savefig line that is only a local, uncommitted
    edit skips just that one call site (both logged, never guessed at).

    `image_paths` is the exact set of git-tracked image files (e.g. from
    rce.ingest.git.list_source_files()["image"]) a resolved literal must
    hit -- mirrors rce.ingest.latex's ghost-figure guard, so every Figure
    node created here is backed by a real repo file. Runs identically
    whether `call.literal` is a plain literal or was constant-folded (T9):
    folding is never a pass around this guard. Idempotent via
    db.upsert_node/upsert_edge plus the stable blame-resolved src.
    """
    counts = {"generates": 0}
    if git_ingest.read_head_sha(repo_root) is None:
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
