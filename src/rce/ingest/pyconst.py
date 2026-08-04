"""Shared Python-source constant-folding helpers (T9, DESIGN.md section 5
connector 5).

Factored out of `rce.ingest.pyfig` (task W2) so `rce.ingest.dataflow`'s
read/write path-argument resolution can fold a same-file module-level string
constant exactly the same way `pyfig`'s savefig-target resolution already
does, instead of a second, drifting copy of the same rules. Behavior is
unchanged from pyfig's original implementation -- `tests/test_ingest_pyfig.py`
never called these functions directly (only the public `parse_py_file`/
`ingest_pyfig_repo`), so moving them here changes no observable behavior; see
`tests/test_ingest_pyfig.py` (still green) and `tests/test_ingest_dataflow.py`
(new caller) for the regression coverage on both sides.

A name folds only if assigned exactly once, as a plain single-target
`NAME = "..."` statement directly in the module's top-level body, to a bare
string literal -- AND that name is touched nowhere else in the entire file
("touched" is checked via a whole-file `ast.walk`, not just `tree.body`, so
this also excludes a name reassigned inside if/for/try/with/def at module
level, reused as a function or lambda parameter or local variable, captured
by a `match` pattern, aliased by a PEP 695 `type` statement or bound by one
of its type parameters, declared `global`/`nonlocal`, imported, or deleted --
see `_count_all_name_bindings` for the exact, non-exhaustive enumeration this
scan recognizes). Given that table, three shapes fold: an f-string whose
every interpolation is itself foldable; `"+"` concatenation of foldable
operands; `os.path.join(...)` whose every argument is foldable.
`pathlib.Path`'s `/` operator is deliberately excluded -- it dispatches on the
left operand's runtime type, which would mean guessing at cross-module
semantics rather than deterministic same-file analysis (architecture
decision, not an oversight). Anything else makes the expression unfoldable;
skipped and logged by the caller exactly as pre-T9.
"""

from __future__ import annotations

import ast
import logging
import posixpath
from typing import Callable

logger = logging.getLogger(__name__)


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
# up on 3.11 before DESIGN.md's "don't guess" rule ever got a chance
# to apply.
_TYPE_ALIAS_NODE_TYPE = getattr(ast, "TypeAlias", None)


def count_name_bindings(node: ast.AST) -> dict[str, int]:
    """Public entry point for `_count_all_name_bindings`, usable on any AST
    node -- a whole module, or a single function's own subtree.
    `rce.ingest.dataflow`'s default-parameter folding reuses this exact
    counting pass scoped to one function, to detect whether a parameter is
    reassigned anywhere inside that function's own body (the parameter's own
    declaration is itself one touch, so "reassigned" is touch count > 1,
    same convention `collect_module_string_constants` uses below)."""
    return _count_all_name_bindings(node)


def _count_all_name_bindings(tree: ast.AST) -> dict[str, int]:
    """Count every name-binding touch anywhere in the whole file. Uses
    `ast.walk` over the entire tree, so a binding nested inside if/for/try/
    with/def at module level -- invisible to a `tree.body`-only scan -- is
    counted, and so is a same-named binding inside a function's (or
    lambda's) own scope, a `match` capture pattern, or a PEP 695 `type` alias
    that would otherwise silently shadow a module-level constant of the same
    name.

    This is a concrete enumeration of what this scan recognizes, not a claim
    of exhaustive Python-grammar coverage -- a binding form not listed here
    is not tracked, and a future language addition could silently reopen
    this same class of bug.
    Forms counted today: `Assign`/`AugAssign`/`AnnAssign`/`NamedExpr`
    (walrus) targets; `For`/`AsyncFor` targets; `With`/`AsyncWith`
    `optional_vars`; `ExceptHandler.name`; comprehension targets;
    `FunctionDef`/`AsyncFunctionDef`/`ClassDef` names and every parameter
    name; `Lambda` parameter names; `global`/`nonlocal` declarations;
    import (as-)names; `Delete` targets; `match` capture patterns
    (`MatchAs`/`MatchStar` `.name`, `MatchMapping.rest`); on Python 3.12+
    only, a PEP 695 `type NAME = ...` statement's `TypeAlias.name`; and,
    also 3.12+ only, every PEP 695 type parameter (`TypeVar`/`ParamSpec`/
    `TypeVarTuple`) bound via `.type_params` on a `FunctionDef`/
    `AsyncFunctionDef`/`ClassDef`/`TypeAlias` -- `p.name` there is a plain
    `str`, not a `Name` node, so it is touched directly rather than via
    `_touch_binding_target`.
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
            # PEP 695 type parameters (`def plot[OUT](x)` / `class P[OUT]` /
            # `class P[**OUT]`) bind OUT for the rest of the def/class body --
            # `getattr` with a default empty tuple is enough (no isinstance
            # check needed): on Python <3.12 `type_params` simply doesn't
            # exist on these nodes, so this is naturally a no-op there.
            # `p.name` is a plain `str` (TypeVar/ParamSpec/TypeVarTuple all
            # expose it that way), so it is touched directly.
            for type_param in getattr(node, "type_params", ()):
                touch(type_param.name)
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
            # like a `def`'s parameter does.
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
            # binding to count there.
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
            # `type Alias[OUT] = ...` also binds OUT via .type_params, same
            # str-valued `.name` as the FunctionDef/ClassDef branch above.
            for type_param in getattr(node, "type_params", ()):
                touch(type_param.name)
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


def _has_star_import(tree: ast.Module) -> bool:
    """True if the file contains `from x import *` anywhere."""
    return any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(tree)
    )


def collect_module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level names foldable into a call's string-literal argument
    (T9).

    Pass 1 (`_count_all_name_bindings`) counts every name binding anywhere in
    the *whole file*, not just `tree.body` -- see that function's docstring
    for the full list of binding forms and why a `tree.body`-only scan missed
    reassignments nested inside if/for/try/with/def at module level, and
    same-name shadowing inside a function's own scope (a call inside
    `def plot(D): ...` must never fold using the module-level D -- the
    function's own D parameter shadows it at runtime).

    Pass 2 keeps only a name whose *sole* touch (count == 1) is a top-level
    `NAME = ...` Assign directly in `tree.body`, RHS folded via `fold_expr`
    against the names already collected *earlier* in `tree.body` -- so a bare
    string literal folds as before, and so does a chain like `BASE = "..."`
    followed by `DATA = BASE + "sub/"` followed by `RAW = DATA + "_raw/"`,
    each resolved in the same top-to-bottom order Python itself would
    execute them in, using only names already resolved by that point. A name
    that references a *later* definition, or one excluded by the touch-count
    check below, is simply not yet in `values` when its own turn comes and so
    fails to fold -- exactly as if that reference had raised `NameError` at
    runtime, not a guess in either direction. Any name touched anywhere else
    in the file (conditionally, in a loop, imported, deleted, declared
    global, used as a parameter or function/class name, ...) is excluded
    regardless of how many times it looks foldable at the top level --
    DESIGN.md section 5: "拼不出来就放弃，不猜".

    A `from x import *` anywhere disables folding for the whole file: the set
    of names it binds is only knowable by importing that module, so no
    per-name touch count can be trusted. Giving up is the conservative
    reading of the same rule.
    """
    if _has_star_import(tree):
        logger.warning(
            "star import found; skipping constant folding for this file, not guessing"
        )
        return {}

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
        folded = fold_expr(stmt.value, values)
        if folded is not None:
            values[name] = folded
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


def fold_expr(expr: ast.expr, constants: dict[str, str]) -> str | None:
    """Fold `expr` to a string using only literals and `constants` (T9).
    Supports exactly three shapes (see module docstring for why
    `pathlib.Path`'s `/` is excluded): an f-string whose every interpolation
    has no conversion/format-spec and itself folds; `"+"` concatenation of
    two foldable operands; `os.path.join(...)` (no `**kwargs`/`*args`) whose
    every argument folds, joined with `posixpath.join` to match this
    package's forward-slash convention. Any other component returns None --
    treated exactly like an unresolvable literal: skip and log, never guess
    at a partial path.
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
                folded = fold_expr(value.value, constants)
                if folded is None:
                    return None
                parts.append(folded)
            else:
                return None
        return "".join(parts)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = fold_expr(expr.left, constants)
        right = fold_expr(expr.right, constants)
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
            folded = fold_expr(arg, constants)
            if folded is None:
                return None
            join_parts.append(folded)
        if not join_parts:
            return None
        return posixpath.join(*join_parts)
    return None
