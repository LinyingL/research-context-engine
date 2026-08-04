"""Deterministic data-lineage extractor (task W2, DESIGN.md section 5-style
connector) -- answers "which script produced this data file, and who reads
it back". Zero-model, zero-git: unlike `rce.ingest.pyfig` (whose `generates`
edge's src is a Commit, resolved via `git blame`), a `reads`/`writes` edge's
src is the script file itself (`script:<repo-relative path>`, a
deterministic id with no history lookup), so this extractor runs identically
on a git repo or a plain filesystem-scanned project (W1, no git at all).

**Python** (`parse_py_file`): stdlib `ast`, no regex. Recognizes both a bare
call and a `<name>.attr(...)` attribute-chain call (`pd.read_csv` and
`read_csv` are both matched, exactly like `rce.ingest.pyfig` already does for
`savefig`) -- the object/module name before the dot is never itself checked,
only the trailing call name:

    read:  read_csv, read_excel, read_parquet, read_stata, read_json,
           loadtxt, open(...) with mode omitted or the literal 'r'
    write: to_csv, to_excel, to_parquet, savefig, open(...) with mode 'w'

`open(...)` with any other explicit mode (`'a'`, `'rb'`, `'x'`, ...) -- or a
non-literal mode expression -- is outside the two documented forms and is
skipped, never guessed at. The path argument is the call's first positional
argument, or (only when there is no positional argument at all) its
`path`/`filepath`/`fname` keyword, first match in that order -- either way
only a plain string literal or a same-file constant-foldable expression
counts (T9, folding logic shared with `rce.ingest.pyfig` via
`rce.ingest.pyconst` rather than duplicated).

**R / R Markdown** (`parse_r_file`/`parse_rmd_file`): no ast for R exists in
the stdlib, so this is a conservative regex + hand-rolled paren/quote scanner
-- never a full R grammar. Recognized names:

    read:  read.csv, read_csv, read_excel, read_xlsx, fread, readRDS,
           read_dta (bare or `haven::`-qualified)
    write: write.csv, write_csv, fwrite, saveRDS, ggsave, pdf(, png(
           (the last two are graphics-device opens, not exactly "writing a
           dataset", but their target is a Figure the same way savefig's is)

Only a bare quoted string literal (single or double quotes) is ever accepted
as the path -- `file.path(...)`, a variable, or any other expression is
skipped and logged, never guessed at, exactly as the task specifies. Which
argument position is "the path" is a documented fact of each function's own
signature (e.g. `write.csv(x, file, ...)` -- the object being written comes
first, the path second), not something the code has to infer; see
`_R_PATH_ARG_INDEX`. A `.Rmd` file is masked to its ```` ```{r} ```` fenced
code chunks before this same scanner runs over it (`_extract_r_chunks`) --
prose and non-R chunks are never scanned, and line numbers stay aligned with
the original file either way.

**Path resolution and the `missing` flag.** A resolved literal is tried both
relative to the script's own directory and relative to the project root,
preferring whichever is an actually-existing file (`_resolve_target`). When
*neither* exists, the edge is still written -- with `evidence.missing=True`
-- rather than skipped: this is a deliberate, documented divergence from
`rce.ingest.pyfig` (which *skips* an unresolved `savefig` literal entirely,
never fabricating a ghost Figure for what is usually a LaTeX typo). Here, a
script that reads or writes a file that does not exist on disk is itself the
finding this connector exists to surface, not noise to filter out -- "the
script wants to read a file that doesn't exist" is a legitimate discovery
(task W2), and existence is checked against the real filesystem, not against
which files happen to be git-tracked (a script's own output is routinely
gitignored). A literal that cannot be safely mapped into the repo at all (an
absolute path, or one that normalizes outside the project root via `..`) has
no repo-relative path to report even as "missing", so that case is skipped
and logged same as everywhere else in this codebase.

A resolved target's extension decides its node type: `rce.ingest.git.
DATA_EXTENSIONS` -> `dataset`, `rce.ingest.git.IMAGE_EXTENSIONS` -> the
existing `figure` node type (no new "image" node type -- a
`savefig`/`ggsave` write is modeled as `script --writes--> figure`, sharing
the same Figure nodes `rce.ingest.latex`/`rce.ingest.pyfig` already produce).
Any other extension is neither, and is skipped and logged rather than
guessed at.

**R constant folding (this module's own counterpart to Python's T9).** A
same-file/document name bound exactly once, via `name <- "literal"` or
`name = "literal"` (a depth-0 `=` only -- a `=` inside a function call's
parentheses is a keyword argument, never a binding), folds into a call's
path argument the same way a Python module constant does. Recognized
argument shapes: a bare literal, a bare foldable name, `paste0(a, b, ...)`
(no separator -- paste0's own semantics; every argument itself required to
be a literal or a foldable name, no further nesting), and `file.path(a, b,
...)` (joined with `"/"`). Any other appearance of the same name anywhere in
the scanned text -- a second `<-`/`=`, a right-assign `value -> name`, or an
`assign("name", ...)` call -- disqualifies folding for that name, mirroring
`pyconst`'s "touched more than once, don't guess" rule exactly. A `.Rmd`'s
binding count spans every R chunk concatenated (the same
`_extract_r_chunks`-masked text `_scan_r_calls` already scans for calls),
since chunks in one document share one R environment -- a name bound in one
chunk and used, or rebound, in another is still one document-wide scope.
**Known limitation:** a conservative regex + depth scanner, not a real R
parser, same as the rest of this module -- an assignment nested inside an
unclosed call's own parentheses (e.g. inside a lambda passed to
`sapply(...)`) is not counted toward that name's binding total.

**Absolute-path remapping (Python and R alike).** A folded or literal path
that turns out to be *absolute* is not an automatic skip: if it resolves
(symlinks included, so macOS's `/private/tmp` vs `/tmp` never causes a false
miss) under the project root's own resolved path, the root prefix is
stripped and ingestion proceeds exactly as if the literal had been written
relative all along -- `evidence.remapped_from_absolute = true` marks the
occurrence. An absolute path that does not resolve under the project root is
unchanged from before this feature existed: skipped and logged, never
guessed at.

**Python default-parameter folding.** `def f(out=DATA + "x.csv"): ...
open(out, "w")` -- a call's own literal argument, `out`, is a parameter, not
a module constant, so T9 alone never resolves it. When `out`'s default value
folds to a literal under pyconst's existing rules, `out` is never reassigned
anywhere in `f`'s own body, and every call to `f(...)` found in this same
file consistently *omits* `out` (positional and keyword alike), that default
value stands in for `out` inside `f`'s own body only -- scoped per function,
never leaking to an unrelated function or module scope that happens to reuse
the same parameter name. Any ambiguity -- the parameter reassigned in the
body, a call that supplies it, calls that disagree with each other, or the
function never called at all in this file -- makes that parameter
ineligible, logged, never guessed at.

Evidence on every edge: `{"file", "line", "callee"}`, plus `"folded_from"`
(Python T9 / R constant folding only), `"missing": true` when the target
does not exist on disk, and `"remapped_from_absolute": true` when the
literal was an absolute path remapped under the project root (above).
`extractor="dataflow"`, `confidence=1.0`, `status="auto"` (machine-owned,
per `db.upsert_edge`). Idempotent via `db.upsert_node`/`db.upsert_edge`.
"""

from __future__ import annotations

import ast
import logging
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from rce import db
from rce.ingest import git as git_ingest
from rce.ingest import pyconst

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataflowCall:
    """One read/write call site found by a Python or R/Rmd scan, before path
    resolution. `kind` is "read" or "write". `literal` is the resolved
    argument string (verbatim, or folded -- T9/default-parameter folding for
    Python, same-file constant folding for R). `folded_from` is None for a
    plain literal, else the original expression's exact source text."""

    script_path: str
    line: int
    callee: str
    kind: str
    literal: str
    folded_from: str | None = None


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

_READ_CALL_NAMES = frozenset(
    {"read_csv", "read_excel", "read_parquet", "read_stata", "read_json", "loadtxt"}
)
_WRITE_CALL_NAMES = frozenset({"to_csv", "to_excel", "to_parquet", "savefig"})
_PATH_KEYWORDS = ("path", "filepath", "fname")


def _callee_label_and_name(func: ast.expr) -> tuple[str, str] | None:
    """A `(label, bare_name)` pair for a call of the form `<name>.attr(...)`
    (label e.g. `"pd.read_csv"`) or a bare `name(...)` (label == bare_name)
    -- the same two shapes `rce.ingest.pyfig._callee_name` recognizes,
    generalized to any attribute name instead of only `savefig`. Anything
    else (a subscript, a call result's own attribute, a deeper dotted chain
    like `a.b.c(...)`) returns None -- not even as an unresolved skip,
    matching pyfig's own convention."""
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}", func.attr
        return None
    if isinstance(func, ast.Name):
        return func.id, func.id
    return None


def _classify_open_mode(node: ast.Call) -> str | None:
    """`open(path[, mode])`: mode omitted or the literal `'r'` -> read,
    `'w'` -> write. Any other explicit mode (`'a'`, `'rb'`, `'x'`, ...) -- or
    a mode given as a non-literal expression -- is outside the two
    documented forms (DESIGN.md section 0, "never guess") and is not
    classified as either."""
    mode_expr: ast.expr | None = None
    if len(node.args) >= 2:
        mode_expr = node.args[1]
    else:
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode_expr = keyword.value
                break
    if mode_expr is None:
        return "read"
    if not (isinstance(mode_expr, ast.Constant) and isinstance(mode_expr.value, str)):
        return None
    if mode_expr.value == "r":
        return "read"
    if mode_expr.value == "w":
        return "write"
    return None


def _classify_call(bare_name: str, node: ast.Call) -> str | None:
    if bare_name == "open":
        return _classify_open_mode(node)
    if bare_name in _READ_CALL_NAMES:
        return "read"
    if bare_name in _WRITE_CALL_NAMES:
        return "write"
    return None


def _resolve_path_arg(
    call: ast.Call, constants: dict[str, str]
) -> tuple[str, ast.expr | None] | None:
    """Resolve a call's path argument: the first positional argument if the
    call has one at all, else its `path`/`filepath`/`fname` keyword (first
    match, in that order). A positional argument that is present but not a
    literal/foldable expression is a definite "not a string literal" --
    deliberately not treated as absent and papered over by then trying the
    keywords, since a real call would raise a duplicate-argument TypeError
    if it passed both. Returns `(value, folded_expr)`; `folded_expr` is None
    for a plain literal, else the original AST node (T9 evidence trail).
    None if unresolvable."""
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value, None
        folded = pyconst.fold_expr(first, constants)
        if folded is None:
            return None
        return folded, first
    for keyword in call.keywords:
        if keyword.arg in _PATH_KEYWORDS:
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value, None
            folded = pyconst.fold_expr(value, constants)
            if folded is None:
                return None
            return folded, value
    return None


def _positional_default_pairs(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, ast.expr]:
    """Every parameter of `func` that has a default value, mapped to that
    default expression -- positional-or-keyword params (paired from the end
    of `args.posonlyargs`/`args.args`, per Python's own default-alignment
    rule) and keyword-only params (`args.kwonlyargs`, each with its own
    possibly-None `kw_defaults` slot) alike."""
    args = func.args
    positional = [*args.posonlyargs, *args.args]
    pairs: dict[str, ast.expr] = {}
    offset = len(positional) - len(args.defaults)
    for i, default in enumerate(args.defaults):
        pairs[positional[offset + i].arg] = default
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            pairs[arg.arg] = default
    return pairs


def _call_provides_param(
    call: ast.Call, param: str, positional: list[str], kwonly: set[str]
) -> bool | None:
    """Whether `call` supplies `param`, positionally or by keyword. None
    when a `*args`/`**kwargs` splat makes this undecidable statically -- the
    caller treats None as an ambiguity that disqualifies the parameter,
    never guessing whether the splat happens to cover it."""
    if any(isinstance(a, ast.Starred) for a in call.args):
        return None
    if any(kw.arg is None for kw in call.keywords):  # a **kwargs splat
        return None
    if param not in kwonly:
        index = positional.index(param)
        if len(call.args) > index:
            return True
    return any(kw.arg == param for kw in call.keywords)


def _eligible_default_param_folds(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    all_calls: list[ast.Call],
    module_constants: dict[str, str],
    py_rel_path: str,
    tree: ast.Module,
) -> dict[str, str]:
    """Parameters of `func` whose *default value* safely stands in for that
    parameter inside `func`'s own body -- see module docstring, "Python
    default-parameter folding". Ambiguity for one parameter disqualifies
    only that parameter (logged); other eligible parameters of the same
    function are unaffected.

    A bare-name call is only trustworthy evidence that *this* def runs if
    the name resolves to it everywhere in the file. Two guards make that
    airtight rather than assumed: (1) if `func.name` is bound anywhere else
    in the file -- a same-named nested def, a shadowing local, a module
    rebinding -- every bare-name call site is ambiguous, so the whole
    function is ineligible (a nested `def build_x` inside a helper used to
    let a genuinely dead top-level `build_x` borrow the nested one's call
    sites and get folded anyway); (2) calls lexically inside `func`'s own
    body are recursion, not proof anything external ever invokes it, so
    they don't count."""
    candidates = {
        name: folded
        for name, expr in _positional_default_pairs(func).items()
        if (folded := pyconst.fold_expr(expr, module_constants)) is not None
    }
    if not candidates:
        return {}

    if pyconst.count_name_bindings(tree).get(func.name, 0) != 1:
        logger.warning(
            "%s: the name %r is bound more than once in this file (shadowing "
            "def, local variable, or module rebinding); bare-name calls cannot "
            "be attributed to this def unambiguously -- skipping default-value "
            "folding for it, not guessing",
            py_rel_path, func.name,
        )
        return {}

    recursion_sites = {id(c) for c in ast.walk(func) if isinstance(c, ast.Call)}
    calls_to_func = [
        c
        for c in all_calls
        if isinstance(c.func, ast.Name)
        and c.func.id == func.name
        and id(c) not in recursion_sites
    ]
    if not calls_to_func:
        logger.warning(
            "%s: %s(...) is never called in this file; skipping default-value "
            "folding for its parameters, not guessing",
            py_rel_path, func.name,
        )
        return {}

    positional = [a.arg for a in (*func.args.posonlyargs, *func.args.args)]
    kwonly = {a.arg for a in func.args.kwonlyargs}
    touch_count = pyconst.count_name_bindings(func)

    eligible: dict[str, str] = {}
    for name, folded_value in candidates.items():
        if touch_count.get(name, 0) != 1:
            logger.warning(
                "%s: %s(...)'s parameter %r is reassigned inside the function "
                "body; skipping default-value folding for it, not guessing",
                py_rel_path, func.name, name,
            )
            continue
        provided = {_call_provides_param(c, name, positional, kwonly) for c in calls_to_func}
        if provided == {False}:
            eligible[name] = folded_value
        else:
            logger.warning(
                "%s: %s(...)'s call sites do not consistently omit parameter "
                "%r (some provide it, or a *args/**kwargs splat makes it "
                "undecidable); skipping default-value folding for it, not guessing",
                py_rel_path, func.name, name,
            )
    return eligible


def _collect_default_param_overlays(
    tree: ast.Module, module_constants: dict[str, str], py_rel_path: str
) -> dict[int, dict[str, str]]:
    """`{id(call_node): {param_name: folded_default_value}}` for every Call
    lexically inside a top-level function whose default-parameter folding is
    eligible (`_eligible_default_param_folds`) -- merged with
    `module_constants` when that specific call's own path argument is
    resolved, so a folded default value is only ever visible inside its own
    function's body."""
    overlays: dict[int, dict[str, str]] = {}
    top_level_funcs = [
        n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not top_level_funcs:
        return overlays
    all_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    for func in top_level_funcs:
        local_folds = _eligible_default_param_folds(
            func, all_calls, module_constants, py_rel_path, tree
        )
        if not local_folds:
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                overlays[id(node)] = local_folds
    return overlays


def parse_py_file(repo_root: str | Path, py_rel_path: str) -> list[DataflowCall]:
    """Scan one .py file for read/write call sites. A file that fails to
    parse (SyntaxError -- e.g. non-Python source under a .py extension) is
    skipped + logged, not fatal to the whole ingest run. Module-level string
    constants are collected once per file (T9, `rce.ingest.pyconst`) so
    every call site can fold against the same table."""
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
    default_param_overlays = _collect_default_param_overlays(tree, module_constants, py_rel_path)

    calls: list[DataflowCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        info = _callee_label_and_name(node.func)
        if info is None:
            continue
        label, bare_name = info
        kind = _classify_call(bare_name, node)
        if kind is None:
            if bare_name == "open":
                # A recognized call name (unlike an arbitrary unrelated
                # function, which is silently not one of ours) whose mode
                # just isn't one of the two documented forms -- a real skip,
                # not background noise, so it gets its own log line.
                logger.warning(
                    "%s:%d: open(...) mode is neither the default/'r' nor 'w' "
                    "(or is not a literal); skipping, not guessing",
                    py_rel_path, node.lineno,
                )
            continue
        local_overlay = default_param_overlays.get(id(node))
        constants = {**module_constants, **local_overlay} if local_overlay else module_constants
        resolved = _resolve_path_arg(node, constants)
        if resolved is None:
            logger.warning(
                "%s:%d: %s(...) path argument is not a string literal (first "
                "positional argument, or a path/filepath/fname keyword); "
                "skipping, not guessing",
                py_rel_path, node.lineno, label,
            )
            continue
        literal, folded_expr = resolved
        folded_from = None
        if folded_expr is not None:
            folded_from = ast.get_source_segment(text, folded_expr) or ast.unparse(folded_expr)
        calls.append(DataflowCall(py_rel_path, node.lineno, label, kind, literal, folded_from))
    return calls


# ---------------------------------------------------------------------------
# R / R Markdown
# ---------------------------------------------------------------------------

_R_READ_NAMES = ("read.csv", "read_csv", "read_excel", "read_xlsx", "fread", "readRDS", "read_dta")
_R_WRITE_NAMES = ("write.csv", "write_csv", "fwrite", "saveRDS", "ggsave", "pdf", "png")

# Which positional argument (0-based, counting only the call's *positional*
# args) is the path, for a call that gives it positionally rather than via a
# file/path/filename keyword -- a documented fact of each function's own
# signature (e.g. `write.csv(x, file, ...)`: the object being written comes
# first, the path second), never a guess.
_R_PATH_ARG_INDEX: dict[str, int] = {
    "read.csv": 0, "read_csv": 0, "read_excel": 0, "read_xlsx": 0, "fread": 0,
    "readRDS": 0, "read_dta": 0, "ggsave": 0, "pdf": 0, "png": 0,
    "write.csv": 1, "write_csv": 1, "fwrite": 1, "saveRDS": 1,
}
_R_PATH_KEYWORDS = ("file", "path", "filename")

_R_CALL_RE = re.compile(
    r"(?:([A-Za-z][A-Za-z0-9_.]*)::)?\b("
    + "|".join(re.escape(name) for name in (*_R_READ_NAMES, *_R_WRITE_NAMES))
    + r")\s*\("
)

_R_KEYWORD_ARG_RE = re.compile(r"^([A-Za-z.][A-Za-z0-9_.]*)\s*=(?!=)\s*(.*)$", re.DOTALL)
_R_DQUOTE_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"$', re.DOTALL)
_R_SQUOTE_RE = re.compile(r"^'((?:[^'\\]|\\.)*)'$", re.DOTALL)

_RMD_FENCE_OPEN_RE = re.compile(r"^\s*```\{r\b")
_RMD_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")


def _find_matching_paren(text: str, open_idx: int) -> int | None:
    """Index of the ')' matching the '(' at `open_idx`, skipping over the
    contents of any quoted string (single or double, backslash-escaped) so a
    literal '(' or ')' inside a path string is never mistaken for real
    nesting. None if the text ends before a match is found (unbalanced --
    logged and skipped by the caller, never guessed at)."""
    depth = 1
    i = open_idx + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" and i + 1 < n else 1
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_top_level_args(text: str) -> list[str]:
    """Split `text` (the contents between a call's parens) on commas at
    depth 0, respecting nested parens/brackets/braces and quoted strings --
    e.g. `file.path(dir, "x.csv"), row.names = FALSE` splits into exactly
    two top-level args, not four."""
    if not text.strip():
        return []
    args: list[str] = []
    current: list[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            quote = ch
            current.append(ch)
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    current.append(text[i])
                    current.append(text[i + 1])
                    i += 2
                    continue
                current.append(text[i])
                i += 1
            if i < n:
                current.append(text[i])
                i += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    args.append("".join(current))
    return args


def _parse_arg(raw: str) -> tuple[str | None, str]:
    """Split one top-level argument into `(keyword, value)` -- keyword is
    None for a positional argument. The `(?!=)` in `_R_KEYWORD_ARG_RE` keeps
    `==` (an equality comparison, not a named-argument `=`) from being
    mistaken for one."""
    raw = raw.strip()
    match = _R_KEYWORD_ARG_RE.match(raw)
    if match:
        return match.group(1), match.group(2).strip()
    return None, raw


def _unquote_r_literal(value: str) -> str | None:
    """The string content of `value` if it is *exactly* a quoted R string
    literal (nothing else around it) -- None for anything else (a bare
    identifier, `file.path(...)`, a numeric/logical literal, an expression),
    which is precisely the "contains a variable or file.path(...)" case the
    task asks to skip and log, never guess."""
    value = value.strip()
    match = _R_DQUOTE_RE.match(value)
    if match:
        return match.group(1).replace('\\"', '"').replace("\\\\", "\\")
    match = _R_SQUOTE_RE.match(value)
    if match:
        return match.group(1).replace("\\'", "'").replace("\\\\", "\\")
    return None


# ---------------------------------------------------------------------------
# R constant folding (this module's counterpart to Python's T9 -- see module
# docstring, "R constant folding")
# ---------------------------------------------------------------------------

_R_LEFT_ASSIGN_RE = re.compile(r"<<-|<-")
_R_RIGHT_ASSIGN_RE = re.compile(r"->>|->")
# A statement-level `=` only: `==`/`<=`/`>=`/`!=` excluded by the
# lookaround, but a call's own keyword-argument `=` (e.g. `file = "x"`) is
# syntactically identical and can only be told apart by bracket depth --
# see `_r_call_depth_at` below, checked by every caller of this regex.
_R_BARE_EQ_RE = re.compile(r"(?<![=<>!])=(?!=)")
_R_ASSIGN_CALL_RE = re.compile(r"\bassign\s*\(")
_R_BARE_NAME_RE = re.compile(r"^[A-Za-z.][A-Za-z0-9_.]*$")
_R_PASTE0_RE = re.compile(r"^paste0\s*\((.*)\)$", re.DOTALL)
_R_FILE_PATH_RE = re.compile(r"^file\.path\s*\((.*)\)$", re.DOTALL)


def _r_call_depth_at(text: str) -> list[int]:
    """Bracket/paren depth (`(`/`)`/`[`/`]` only -- deliberately never
    `{`/`}`, which don't introduce call-argument-keyword syntax in R)
    immediately before each character offset in `text`, skipping quoted
    string contents. The only use is telling a statement-level `=` (depth 0
    -- an assignment) from a call's own keyword argument `=` (depth >= 1) --
    `<-`/`<<-`/`->`/`->>` are never keyword-argument syntax in R, so they
    need no depth check at all."""
    n = len(text)
    depths = [0] * (n + 1)
    depth = 0
    i = 0
    while i < n:
        depths[i] = depth
        ch = text[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" and i + 1 < n else 1
            i += 1
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        i += 1
    depths[n] = depth
    return depths


def _extract_simple_target(prefix: str) -> str | None:
    """The bare name `prefix` (all text before an assignment operator) ends
    in, or None if `prefix` does not end in exactly a plain identifier --
    `df$col`, `obj@slot`, and `names(x)` (a replacement-function call
    target) all correctly return None, none of those being the plain "name"
    shape this scan folds."""
    prefix = prefix.rstrip()
    match = re.search(r"[A-Za-z.][A-Za-z0-9_.]*$", prefix)
    if not match:
        return None
    before = prefix[: match.start()].rstrip()
    if before and before[-1] in "$@]":
        return None
    return match.group(0)


def _extract_right_target(suffix: str) -> str | None:
    """The bare name a right-assign (`value -> name`) targets, or None if
    what immediately follows the operator is not exactly a plain identifier
    (`-> df$col`, `-> x[i]`, ...)."""
    suffix = suffix.lstrip()
    match = re.match(r"[A-Za-z.][A-Za-z0-9_.]*", suffix)
    if not match:
        return None
    after = suffix[match.end():].lstrip()
    if after[:1] in "$@[(":
        return None
    return match.group(0)


def _collect_r_constants(text: str) -> dict[str, str]:
    """Every name in `text` (already comment-stripped; for a .Rmd this is
    the whole document's chunk-masked text, so binding counts span every
    chunk -- see module docstring) foldable into a call's path argument: a
    name whose SOLE binding, anywhere in `text`, among `<-`/`<<-`/
    `=`(statement-level only)/`->`/`->>`/`assign("name", ...)`, is a plain
    `name <- "literal"` or `name = "literal"`. Mirrors `pyconst.
    collect_module_string_constants`'s two-pass "count every touch, keep
    only a sole literal touch" shape."""
    touch_count: dict[str, int] = {}
    literal_values: dict[str, str] = {}

    def touch(name: str) -> None:
        touch_count[name] = touch_count.get(name, 0) + 1

    def record_left(match: re.Match[str]) -> None:
        name = _extract_simple_target(text[: match.start()])
        if name is None:
            return
        touch(name)
        rhs = text[match.end():].split("\n", 1)[0].strip()
        literal = _unquote_r_literal(rhs)
        if literal is not None:
            literal_values[name] = literal

    for match in _R_LEFT_ASSIGN_RE.finditer(text):
        record_left(match)

    depths = _r_call_depth_at(text)
    for match in _R_BARE_EQ_RE.finditer(text):
        if depths[match.start()] != 0:
            continue  # a call's keyword argument, not a statement-level assignment
        record_left(match)

    for match in _R_RIGHT_ASSIGN_RE.finditer(text):
        name = _extract_right_target(text[match.end():])
        if name is not None:
            touch(name)  # never itself a fold source -- see module docstring

    for match in _R_ASSIGN_CALL_RE.finditer(text):
        close_idx = _find_matching_paren(text, match.end() - 1)
        if close_idx is None:
            continue
        args = _split_top_level_args(text[match.end():close_idx])
        if not args:
            continue
        name = _unquote_r_literal(args[0].strip())
        if name is not None:  # a dynamic assign() target is not statically knowable
            touch(name)

    return {
        name: value for name, value in literal_values.items() if touch_count.get(name) == 1
    }


def _fold_r_arg_atom(raw: str, constants: dict[str, str]) -> str | None:
    """One `paste0(...)`/`file.path(...)` argument, folded to a string: a
    bare literal, or a bare name found in `constants` -- no further nesting
    (a nested `paste0(...)`/`file.path(...)` call as an argument is outside
    the documented shape and is not folded)."""
    raw = raw.strip()
    literal = _unquote_r_literal(raw)
    if literal is not None:
        return literal
    if _R_BARE_NAME_RE.match(raw):
        return constants.get(raw)
    return None


def _fold_r_value(value: str, constants: dict[str, str]) -> tuple[str, bool] | None:
    """`(literal, folded)` for a call argument's source text: a bare literal
    (`folded=False`), or a same-scope name/`paste0(...)`/`file.path(...)`
    folded via `constants` (`folded=True`). None if it folds to nothing --
    skip and log, never guess at a partial path."""
    value = value.strip()
    literal = _unquote_r_literal(value)
    if literal is not None:
        return literal, False
    if _R_BARE_NAME_RE.match(value):
        folded = constants.get(value)
        return (folded, True) if folded is not None else None
    match = _R_PASTE0_RE.match(value)
    if match:
        parts = [_fold_r_arg_atom(a, constants) for a in _split_top_level_args(match.group(1))]
        if parts and all(p is not None for p in parts):
            return "".join(parts), True  # type: ignore[arg-type]
        return None
    match = _R_FILE_PATH_RE.match(value)
    if match:
        parts = [_fold_r_arg_atom(a, constants) for a in _split_top_level_args(match.group(1))]
        if parts and all(p is not None for p in parts):
            return "/".join(parts), True  # type: ignore[arg-type]
        return None
    return None


def _resolve_r_call_path(
    name: str, args_text: str, constants: dict[str, str]
) -> tuple[str, str | None] | None:
    """The call's path argument as `(literal, folded_from)` -- `folded_from`
    is the argument's own raw source text when folding via `constants`
    produced the literal, else None for a bare quoted literal -- or None if
    it cannot be determined at all. Checks a `file`/`path`/`filename`
    keyword argument first (unambiguous regardless of the function -- and,
    if present but not resolvable, this is a definite skip, not a reason to
    fall back to guessing at a positional argument instead); only when no
    such keyword is given does it fall back to the function's own
    documented positional index (`_R_PATH_ARG_INDEX`, counting only the
    call's positional -- not keyword -- arguments)."""

    def resolve(value: str) -> tuple[str, str | None] | None:
        resolved = _fold_r_value(value, constants)
        if resolved is None:
            return None
        literal, folded = resolved
        return literal, (value.strip() if folded else None)

    parsed = [_parse_arg(raw) for raw in _split_top_level_args(args_text)]
    for keyword, value in parsed:
        if keyword in _R_PATH_KEYWORDS:
            return resolve(value)
    positional_values = [value for keyword, value in parsed if keyword is None]
    index = _R_PATH_ARG_INDEX.get(name)
    if index is not None and index < len(positional_values):
        return resolve(positional_values[index])
    return None


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _strip_r_line_comment(line: str) -> str:
    """Truncate `line` at its first unquoted '#' (an R comment runs to end
    of line) -- so a commented-out example call is never scanned as real
    code. Per-line, since an R comment cannot span multiple lines."""
    in_quote: str | None = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
            i += 1
            continue
        if ch in "\"'":
            in_quote = ch
            i += 1
            continue
        if ch == "#":
            return line[:i]
        i += 1
    return line


def _strip_r_comments(text: str) -> str:
    """Same line count as `text`, each line truncated at its own unquoted
    '#' -- keeps every remaining line's offset-to-line-number mapping
    identical to the original."""
    return "\n".join(_strip_r_line_comment(line) for line in text.splitlines())


def _extract_r_chunks(text: str) -> str:
    """A string with the same line count as `text`, where only lines inside
    a ```` ```{r ...} ``` ```` fenced code chunk are preserved verbatim and
    every other line (prose, a non-R chunk, the fence markers themselves) is
    blanked -- so a regex scan of the result only ever sees R code, while
    line numbers stay aligned with the original .Rmd file for evidence."""
    lines = text.splitlines()
    out: list[str] = []
    in_r_chunk = False
    for line in lines:
        if not in_r_chunk and _RMD_FENCE_OPEN_RE.match(line):
            in_r_chunk = True
            out.append("")
            continue
        if in_r_chunk and _RMD_FENCE_CLOSE_RE.match(line):
            in_r_chunk = False
            out.append("")
            continue
        out.append(line if in_r_chunk else "")
    return "\n".join(out)


def _scan_r_calls(text: str, script_rel_path: str) -> list[DataflowCall]:
    """Shared by `parse_r_file` (whole file) and `parse_rmd_file` (already
    masked to its R chunks by `_extract_r_chunks`) -- one scanner, one set of
    rules, for both source kinds."""
    calls: list[DataflowCall] = []
    scan_text = _strip_r_comments(text)
    constants = _collect_r_constants(scan_text)
    for match in _R_CALL_RE.finditer(scan_text):
        pkg, name = match.group(1), match.group(2)
        label = f"{pkg}::{name}" if pkg else name
        kind = "read" if name in _R_READ_NAMES else "write"
        line = _line_of(scan_text, match.start())
        open_paren_idx = match.end() - 1
        close_idx = _find_matching_paren(scan_text, open_paren_idx)
        if close_idx is None:
            logger.warning(
                "%s:%d: unbalanced parentheses scanning %s(...); skipping, not guessing",
                script_rel_path, line, label,
            )
            continue
        args_text = scan_text[open_paren_idx + 1:close_idx]
        resolved = _resolve_r_call_path(name, args_text, constants)
        if resolved is None:
            logger.warning(
                "%s:%d: %s(...) path argument is not a plain quoted string literal, "
                "a foldable same-scope name, paste0(...), or file.path(...) of only "
                "those (a variable, or other expression); skipping, not guessing",
                script_rel_path, line, label,
            )
            continue
        literal, folded_from = resolved
        calls.append(DataflowCall(script_rel_path, line, label, kind, literal, folded_from))
    return calls


def parse_r_file(repo_root: str | Path, r_rel_path: str) -> list[DataflowCall]:
    """Scan one .R file for read/write call sites."""
    path = Path(repo_root) / r_rel_path
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        logger.warning("cannot read %s: %s", r_rel_path, exc)
        return []
    return _scan_r_calls(text, r_rel_path)


def parse_rmd_file(repo_root: str | Path, rmd_rel_path: str) -> list[DataflowCall]:
    """Scan one .Rmd file's ```` ```{r} ```` fenced code chunks (only) for
    read/write call sites -- ordinary prose, and any non-R chunk, is never
    scanned."""
    path = Path(repo_root) / rmd_rel_path
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        logger.warning("cannot read %s: %s", rmd_rel_path, exc)
        return []
    return _scan_r_calls(_extract_r_chunks(text), rmd_rel_path)


# ---------------------------------------------------------------------------
# Path resolution + ingest
# ---------------------------------------------------------------------------


def _normalize_candidate(base_dir: str, raw_path: str) -> str | None:
    """Join+normalize a literal against `base_dir`, rejecting an absolute
    filesystem path (not something we can safely remap into the repo) or
    anything that normalizes outside the repo root -- same guard as
    `rce.ingest.pyfig._normalize_candidate`/`rce.ingest.latex.
    _resolve_figure_path`. Returns None only when the literal cannot be
    safely mapped into the repo at all; a real file simply not existing at
    the mapped path is a *different*, allowed outcome (see
    `_resolve_target`)."""
    if not raw_path or posixpath.isabs(raw_path):
        return None
    normalized = posixpath.normpath(posixpath.join(base_dir, raw_path))
    if normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def _remap_absolute_path(literal: str, repo_root: Path) -> str | None:
    """An absolute `literal` that resolves under the project root's own
    resolved filesystem path, remapped to a project-relative POSIX path (the
    root prefix stripped) -- see module docstring, "Absolute-path
    remapping". Both sides are resolved (symlinks included, so e.g. macOS's
    `/private/tmp` vs `/tmp` is never a false miss) before comparing, so this
    is a deterministic filesystem-prefix judgement, not a guess. None when
    the literal does not resolve under the project root at all -- the caller
    then treats it exactly as before this feature existed: skip and log."""
    try:
        resolved_literal = Path(literal).resolve()
        resolved_root = repo_root.resolve()
        relative = resolved_literal.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def _resolve_target(
    script_rel_path: str, literal: str, repo_root: Path
) -> tuple[str, bool, bool] | None:
    """Resolve a read/write literal to a repo-relative path plus whether the
    target currently exists on disk, plus whether it was remapped from an
    absolute path (below).

    An absolute `literal` is not an automatic skip: `_remap_absolute_path`
    first tries to remap it to a project-relative path; only when that fails
    (the literal does not resolve under the project root) does this function
    return None exactly as before this feature existed.

    Once relative (as written, or after remapping), tries the literal
    relative to the project root first, then relative to the script's own
    directory (both attempted, per the task spec; this checking order
    matches the existing `rce.ingest.pyfig._resolve_figure_target`
    convention elsewhere in this codebase) -- whichever is an
    actually-existing file wins. When *neither* exists, the root-relative
    candidate is still returned (same order, as a deterministic default
    label -- not a claim that it is the "correct" one, just a documented,
    consistent choice) with `missing=True` -- see the module docstring for
    why this deliberately does not skip the way `rce.ingest.pyfig` skips an
    unresolved savefig literal: a script reading/writing a file that doesn't
    exist is the finding, not noise.

    Returns None only when the literal cannot be safely mapped into the repo
    at all (an absolute path outside the project root, or a `..` escape) --
    there is then no repo-relative path to report even as "missing"."""
    remapped_from_absolute = False
    if posixpath.isabs(literal):
        remapped = _remap_absolute_path(literal, repo_root)
        if remapped is None:
            return None
        literal = remapped
        remapped_from_absolute = True
    root_candidate = _normalize_candidate(".", literal)
    script_dir = posixpath.dirname(script_rel_path) or "."
    script_candidate = _normalize_candidate(script_dir, literal)
    for candidate in (root_candidate, script_candidate):
        if candidate is not None and (repo_root / candidate).is_file():
            return candidate, False, remapped_from_absolute
    fallback = root_candidate if root_candidate is not None else script_candidate
    if fallback is None:
        return None
    return fallback, True, remapped_from_absolute


def _node_type_for_extension(path: str) -> str | None:
    """`dataset` for a tracked data extension, `figure` for a tracked image
    extension (reusing the existing node type -- see module docstring), None
    for anything else."""
    suffix = posixpath.splitext(path)[1].lower()
    if suffix in git_ingest.DATA_EXTENSIONS:
        return "dataset"
    if suffix in git_ingest.IMAGE_EXTENSIONS:
        return "figure"
    return None


def ingest_dataflow_repo(
    conn: Connection,
    repo_root: str | Path,
    py_paths: list[str],
    r_paths: list[str],
    rmd_paths: list[str],
) -> dict[str, int]:
    """Ingest read/write call sites from .py/.R/.Rmd files into
    `Script --reads/writes--> Dataset` (or `--writes--> Figure` for an image
    target) edges (task W2). Needs no git repository at all -- see module
    docstring; runs identically on a git repo or a plain filesystem-scanned
    project (W1). Idempotent via `db.upsert_node`/`db.upsert_edge`.
    """
    repo_root = Path(repo_root)
    counts = {"reads": 0, "writes": 0}

    calls: list[DataflowCall] = []
    for py_path in py_paths:
        calls.extend(parse_py_file(repo_root, py_path))
    for r_path in r_paths:
        calls.extend(parse_r_file(repo_root, r_path))
    for rmd_path in rmd_paths:
        calls.extend(parse_rmd_file(repo_root, rmd_path))

    for call in calls:
        resolved = _resolve_target(call.script_path, call.literal, repo_root)
        if resolved is None:
            logger.warning(
                "%s:%d: %s(%r) cannot be safely mapped into the repo (absolute path, "
                "or escapes the project root); skipping, not guessing",
                call.script_path, call.line, call.callee, call.literal,
            )
            continue
        target_path, missing, remapped_from_absolute = resolved
        node_type = _node_type_for_extension(target_path)
        if node_type is None:
            logger.warning(
                "%s:%d: %s(%r) resolves to %r, whose extension is neither a tracked "
                "data nor image extension; skipping, not guessing which node type it is",
                call.script_path, call.line, call.callee, call.literal, target_path,
            )
            continue
        script_id = f"script:{call.script_path}"
        target_id = f"{node_type}:{target_path}"
        edge_type = "reads" if call.kind == "read" else "writes"
        evidence: dict[str, Any] = {
            "file": call.script_path, "line": call.line, "callee": call.callee,
        }
        if call.folded_from is not None:
            evidence["folded_from"] = call.folded_from  # folded argument only
        if missing:
            evidence["missing"] = True
        if remapped_from_absolute:
            evidence["remapped_from_absolute"] = True
        db.upsert_node(conn, script_id, "script", title=call.script_path)
        db.upsert_node(conn, target_id, node_type, title=target_path)
        db.upsert_edge(
            conn, script_id, target_id, edge_type, extractor="dataflow",
            evidence=evidence, confidence=1.0, status="auto",
        )
        counts[edge_type] += 1
    return counts
