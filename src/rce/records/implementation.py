"""When the implementation moves under a confirmed version (DESIGN.md 9.11,
"stage (b)"; task V5 phase 9). Reads only; the one write it leads to -- a
`reaffirmed` entry -- goes through `rce.records.cards.reaffirm`.

What is compared
----------------

For every card whose version in use is confirmed, on every application of
the record (the end of every scan, the watcher, every record write), the
version's implementation as it is NOW against what its latest
`confirmed`, `corrected` or `reaffirmed` entry recorded (the baseline):

- the script's CODE: Python with its comments and blank lines removed (by
  `tokenize`), R with its comment lines and blank lines removed, an Rmd by
  its code chunks only (each chunk as R). Narrowed when the version names
  `chunk = "<Rmd chunk label>"` or `function = "<name>"` under
  `[implementation]`: the region is located exactly -- one chunk with that
  label, one function with that name -- never fuzzily; when it cannot be
  found the version is under review for that reason (「找不到该代码块」 /
  「找不到该函数」). The baseline's code is read from the code copy the
  entry names (`_code/<sha>.<ext>`), normalized the same way; without a
  copy, the entry's recorded `code` hash, else its raw sha256.
- each input dataset: hashed below `variables.LARGE_FILE_BYTES`, compared
  by size above it (the mtime only chooses the wording). 「完整比对」
  (`full=True`) hashes the large ones too.

A difference puts the version under review -- 「实现脚本在确认后有改动」 or
「输入数据在确认后有变化」 -- and RCE says it cannot tell whether the
definition changed. A script or input that cannot be read (absent, in the
cloud, unreadable) or a script that cannot be tokenized raises nothing.

"Nothing changed" always says how far it looked
-----------------------------------------------

Every comparison carries its coverage, and the page words it: `full`
「全文未变」, `region` 「指定范围未变（范围外未比对）」, `size` 「大小未变
（内容未比对）」, `mtime` 「修改时间变化，内容未比对」. The bare claim that the
implementation and the data are unchanged is never made.

What the index keeps
--------------------

`db` record status `variables_implementation`: `{"cards": {id: review},
"hashes": {dataset: {size, mtime, sha256}}}`. `hashes` is a cache of the
content hashes computed for each input at a given size and mtime -- a file
whose size and mtime are what they were when it was hashed is not hashed
again; a large file hashed by 「完整比对」 stays compared by content until
its size or mtime moves. The index may forget it; nothing in the record
depends on it.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import logging
import posixpath
import re
import tokenize
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Mapping

from rce import db, paths
from rce.records import files
from rce.records import variables as V
from rce.records.files import RecordState

logger = logging.getLogger(__name__)

RECORD_STATUS_NAME = "variables_implementation"

# -- product language (8.8) ---------------------------------------------------------------

SCRIPT_CHANGED = "script_changed"
INPUT_CHANGED = "input_changed"
CHUNK_MISSING = "chunk_missing"
FUNCTION_MISSING = "function_missing"
REASON_LABELS = {
    SCRIPT_CHANGED: "实现脚本在确认后有改动",
    INPUT_CHANGED: "输入数据在确认后有变化",
    CHUNK_MISSING: "找不到该代码块",
    FUNCTION_MISSING: "找不到该函数",
}
SCRIPT_REASONS = (SCRIPT_CHANGED, CHUNK_MISSING, FUNCTION_MISSING)
GROUP_MESSAGE = "此脚本的改动涉及 {n} 个变量"
CANNOT_TELL = "RCE 只看得出实现有变化，看不出口径有没有变，需要你来判断。"
COVERAGE_LABELS = {
    "full": "全文未变",
    "region": "指定范围未变（范围外未比对）",
    "size": "大小未变（内容未比对）",
    "mtime": "修改时间变化，内容未比对",
}
NOT_COMPARED_LABELS = {
    "unreadable": "暂时无法读取，未比对",
    "unparseable": "脚本无法解析，未比对",
    "no_baseline": "确认时没有记下它，未比对",
    "none": "没有写实现脚本",
}
CHANGED_LABEL = "有变化"
DRAFT_WAITING = "已打开草稿 v{n}：确认它之后，它取代这一版"

#: Fingerprint coverages recorded on a `reaffirmed` entry.
RECORDED_FULL = "full"
RECORDED_REGION = "region"
RECORDED_SIZE = "size"


def large_threshold() -> int:
    """Read at call time, so a test can set `variables.LARGE_FILE_BYTES`."""
    return V.LARGE_FILE_BYTES


class Unparseable(Exception):
    """The script could not be read as code (a comparison raises nothing)."""


class RegionMissing(Exception):
    """The named chunk or function is not in the script, or not exactly once."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


# -- normalizing code -----------------------------------------------------------------------


def _py_lines(text: str) -> list[tuple[int, str]]:
    """(line number, line) of the code, comments and blank lines removed."""
    cuts: dict[int, int] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                cuts[tok.start[0]] = tok.start[1]
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        raise Unparseable(f"the Python script cannot be tokenized ({exc})") from exc
    out = []
    for number, line in enumerate(text.splitlines(), start=1):
        if number in cuts:
            line = line[: cuts[number]]
        line = line.rstrip()
        if line.strip():
            out.append((number, line))
    return out


def _r_lines(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """R: comment lines and blank lines removed (a trailing comment on a code
    line is code's business -- R has no tokenizer here, and a `#` inside a
    string must not be cut)."""
    out = []
    for number, line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((number, line.rstrip()))
    return out


_FENCE_OPEN_RE = re.compile(r"^\s*```+\s*\{\s*([A-Za-z0-9_]+)\b(.*)\}\s*$")
_FENCE_CLOSE_RE = re.compile(r"^\s*```+\s*$")
_LABEL_OPT_RE = re.compile(r"""\blabel\s*=\s*["']([^"']*)["']""")


def _chunk_label(options: str) -> str | None:
    opts = options.strip().lstrip(",").strip()
    first = opts.split(",", 1)[0].strip() if opts else ""
    if first and "=" not in first:
        return first.strip("'\"")
    m = _LABEL_OPT_RE.search(opts)
    return m.group(1) if m else None


def rmd_chunks(text: str) -> list[tuple[str | None, list[tuple[int, str]]]]:
    """Each fenced code chunk of an Rmd: (label, its body lines numbered)."""
    chunks: list[tuple[str | None, list[tuple[int, str]]]] = []
    current: list[tuple[int, str]] | None = None
    label: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if current is None:
            m = _FENCE_OPEN_RE.match(line)
            if m:
                current, label = [], _chunk_label(m.group(2))
            continue
        if _FENCE_CLOSE_RE.match(line):
            chunks.append((label, current))
            current, label = None, None
            continue
        current.append((number, line))
    if current is not None:  # an unclosed chunk runs to the end, as knitr reads it
        chunks.append((label, current))
    return chunks


def _kind(suffix: str) -> str:
    return {".py": "py", ".r": "r", ".rmd": "rmd"}.get(suffix.lower(), "other")


def _lines_of(text: str, kind: str) -> list[tuple[int, str]]:
    if kind == "py":
        return _py_lines(text)
    if kind == "r":
        return _r_lines(list(enumerate(text.splitlines(), start=1)))
    if kind == "rmd":
        out: list[tuple[int, str]] = []
        for _label, body in rmd_chunks(text):
            out.append((0, "```"))  # a chunk boundary is part of the code's shape
            out += _r_lines(body)
        return out
    # Another kind of script: its text with blank lines removed (no comment
    # syntax is assumed).
    return [(n, line.rstrip()) for n, line in enumerate(text.splitlines(), start=1) if line.strip()]


_R_FUNCTION_RE = r"^\s*(?:`{name}`|{name})\s*(?:<<-|<-|=)\s*function\b"


def _r_function_extent(lines: list[tuple[int, str]], start: int, column: int) -> int:
    """The last line number of the R function whose definition starts at
    index `start` of `lines` (strings and comments skipped while counting
    brackets). Without a `{` body, the line the argument list closes on."""
    paren = brace = 0
    seen_args = seen_brace = False
    i = start
    while i < len(lines):
        number, line = lines[i]
        j = 0
        if i == start:
            j = column  # just after the `function` keyword
        quote: str | None = None
        while j < len(line):
            ch = line[j]
            if quote:
                if ch == "\\":
                    j += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "'\"`":
                quote = ch
            elif ch == "#":
                break
            elif ch == "(":
                paren += 1
                seen_args = True
            elif ch == ")":
                paren -= 1
            elif ch == "{" and seen_args and paren == 0:
                brace += 1
                seen_brace = True
            elif ch == "}" and seen_brace:
                brace -= 1
                if brace == 0:
                    return number
            elif seen_args and paren == 0 and not seen_brace and not ch.isspace():
                return number  # a body without braces: one expression on this line
            j += 1
        if seen_args and paren == 0 and not seen_brace:
            nxt = lines[i + 1][1].lstrip() if i + 1 < len(lines) else ""
            if not nxt.startswith("{"):
                return lines[i + 1][0] if i + 1 < len(lines) else number
        i += 1
    return lines[-1][0]


def _region_lines(text: str, kind: str, chunk: str | None, function: str | None) -> list[tuple[int, str]]:
    """The normalized lines of the named region (module docstring)."""
    if chunk:
        if kind != "rmd":
            raise RegionMissing(CHUNK_MISSING, f"chunk {chunk!r} is named, but the script is not an Rmd")
        found = [body for label, body in rmd_chunks(text) if label == chunk]
        if len(found) != 1:
            raise RegionMissing(CHUNK_MISSING, f"{len(found)} chunks are labelled {chunk!r} (exactly one is needed)")
        lines = _r_lines(found[0])
        if function:
            return _function_lines(lines, function)
        return lines
    assert function
    if kind == "py":
        return _py_function_lines(text, function)
    if kind == "rmd":
        lines = [ln for _label, body in rmd_chunks(text) for ln in body]
        return _function_lines(lines, function)
    if kind == "r":
        return _function_lines(list(enumerate(text.splitlines(), start=1)), function)
    raise RegionMissing(FUNCTION_MISSING, f"function {function!r} cannot be located in this kind of script")


def _function_lines(lines: list[tuple[int, str]], name: str) -> list[tuple[int, str]]:
    pattern = re.compile(_R_FUNCTION_RE.format(name=re.escape(name)))
    starts = [(i, m.end()) for i, (_n, line) in enumerate(lines) if (m := pattern.match(line))]
    if len(starts) != 1:
        raise RegionMissing(FUNCTION_MISSING, f"{len(starts)} definitions of function {name!r} (exactly one is needed)")
    start, column = starts[0]
    first = lines[start][0]
    last = _r_function_extent(lines, start, column)
    return _r_lines([(n, line) for n, line in lines if first <= n <= last])


def _py_function_lines(text: str, name: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        raise Unparseable(f"the Python script cannot be parsed ({exc})") from exc
    body: list[ast.stmt] = tree.body
    parts = name.split(".")
    node: ast.AST | None = None
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        kinds = (ast.FunctionDef, ast.AsyncFunctionDef) if last else (ast.ClassDef,)
        matches = [n for n in body if isinstance(n, kinds) and n.name == part]
        if len(matches) != 1:
            raise RegionMissing(FUNCTION_MISSING, f"{len(matches)} definitions of {'.'.join(parts[:i + 1])!r} (exactly one is needed)")
        node = matches[0]
        body = getattr(node, "body", [])
    assert node is not None
    first = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
    last = getattr(node, "end_lineno", None) or node.lineno
    return [(n, line) for n, line in _py_lines(text) if first <= n <= last]


def code_lines(text: str, suffix: str, *, chunk: str | None = None, function: str | None = None) -> list[str]:
    """The code that is compared (module docstring). Raises `Unparseable` or
    `RegionMissing`."""
    kind = _kind(suffix)
    lines = _region_lines(text, kind, chunk, function) if (chunk or function) else _lines_of(text, kind)
    return [line for _n, line in lines]


def code_hash(text: str, suffix: str, *, chunk: str | None = None, function: str | None = None) -> str:
    joined = "\n".join(code_lines(text, suffix, chunk=chunk, function=function))
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def region_of(impl: Mapping[str, Any]) -> tuple[str | None, str | None]:
    chunk = (impl.get("chunk") or "").strip() or None
    function = (impl.get("function") or "").strip() or None
    return chunk, function


def region_text(chunk: str | None, function: str | None) -> str | None:
    parts = []
    if chunk:
        parts.append(f"chunk:{chunk}")
    if function:
        parts.append(f"function:{function}")
    return " ".join(parts) or None


# -- what a version stands on ----------------------------------------------------------------


def version_content(card: V.Card, number: int) -> dict[str, Any] | None:
    """The definition in force for `number`: the frozen text when the file
    was edited after its confirmation (the edit is shown, never obeyed)."""
    from rce.records import cards  # noqa: PLC0415 -- cards imports this module lazily too

    view = card.versions.get(number)
    if view is None:
        return None
    if view.question or view.content is None:
        return cards._frozen_content(card, view) if view.entry is not None else None
    return view.content


def baseline_entry(card: V.Card, number: int):
    """The latest confirmed, corrected or reaffirmed entry for `number`, in
    file order (the clock decides nothing)."""
    found = None
    for e in card.entries:
        if e.get("act") in ("confirmed", "corrected", "reaffirmed") and e.get("version") == number:
            found = e
    return found


def _script_base(entry) -> dict[str, Any]:
    script = (entry.get("checked") or {}).get("script")
    return dict(script) if isinstance(script, Mapping) else {}


def _input_bases(entry) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in (entry.get("observed") or {}).get("inputs") or []:
        if isinstance(item, Mapping) and isinstance(item.get("dataset"), str):
            out[posixpath.normpath(item["dataset"])] = dict(item)
    return out


def _mtime(st) -> str:
    return datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="microseconds")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# -- the comparisons --------------------------------------------------------------------------


def look_script(root: Path, impl: Mapping[str, Any], base: Mapping[str, Any]) -> dict[str, Any]:
    """The script now against `base` (an entry's `checked.script`).
    `state`: same, changed, region_missing, unreadable, unparseable, none.
    `now` is the fingerprint a `reaffirmed` entry would record."""
    from rce.records import cards  # noqa: PLC0415

    rel = (impl.get("script") or "").strip()
    chunk, function = region_of(impl)
    out: dict[str, Any] = {"path": rel or None, "region": region_text(chunk, function), "chunk": chunk,
                           "function": function, "coverage": None, "reason": None, "detail": None}
    if not rel:
        out["state"] = "none"
        return out
    path = cards._confined(root, rel)
    if path is None or not path.is_file() or paths.is_dataless(path):
        out.update(state="unreadable", detail="the script is not there, outside the project, or still in the cloud")
        return out
    try:
        data = path.read_bytes()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        out.update(state="unreadable", detail=str(exc))
        return out
    suffix = Path(rel).suffix
    sha = hashlib.sha256(data).hexdigest()
    now: dict[str, Any] = {"result": V.CHECKED, "sha256": sha, "size": len(data),
                           "copy": f"{V.CODE_DIRNAME}/{sha}{suffix}"}
    out["now"] = now
    out["bytes"] = data
    narrowed = bool(chunk or function)
    coverage = "region" if narrowed else "full"
    try:
        current = code_hash(text, suffix, chunk=chunk, function=function)
    except Unparseable as exc:
        out.update(state="unparseable", detail=str(exc))
        return out
    except RegionMissing as exc:
        out.update(state="region_missing", reason=exc.reason, detail=str(exc))
        return out
    now["code"] = current
    if narrowed:
        now["region"] = region_text(chunk, function)
    if base.get("sha256") == sha:
        out.update(state="same", coverage=coverage)
        return out
    baseline = _baseline_code(root, base, suffix, chunk, function)
    if baseline is None:
        # No copy and no recorded code hash to compare the code with: the
        # raw bytes differ, and that is all RCE can say.
        out.update(state="changed", reason=SCRIPT_CHANGED, detail="the script's bytes differ and no copy of the "
                   "confirmed script is there to compare its code with")
        return out
    if baseline == current:
        out.update(state="same", coverage=coverage)
    else:
        out.update(state="changed", reason=SCRIPT_CHANGED)
    return out


def _baseline_code(root: Path, base: Mapping[str, Any], suffix: str, chunk: str | None,
                   function: str | None) -> str | None:
    copy = base.get("copy")
    if isinstance(copy, str) and copy:
        directory = V.variables_dir(root)
        target = directory / copy
        try:
            target.resolve().relative_to(V.code_dir(root).resolve())
        except (ValueError, OSError):
            target = None
        if target is not None:
            got = files.read_record(target)
            if got.state is RecordState.PRESENT and got.data is not None:
                try:
                    return code_hash(got.data.decode("utf-8-sig"), suffix, chunk=chunk, function=function)
                except (Unparseable, RegionMissing, UnicodeDecodeError):
                    return "(the confirmed script had no such region)"
    recorded = base.get("code")
    if isinstance(recorded, str) and base.get("region") == region_text(chunk, function):
        return recorded
    return None


def look_input(root: Path, dataset: str, base: Mapping[str, Any] | None, cache: dict[str, Any], *,
               large_bytes: int, full: bool) -> dict[str, Any]:
    """One input dataset now against its baseline fingerprint. `state`:
    same, changed, unreadable, no_baseline. `cache` is updated in place
    with any hash computed (module docstring)."""
    from rce.records import cards  # noqa: PLC0415

    rel = posixpath.normpath(dataset)
    out: dict[str, Any] = {"dataset": dataset, "coverage": None, "detail": None}
    path = cards._confined(root, rel)
    try:
        if path is None:
            raise OSError("outside the project")
        st = path.stat()
    except OSError as exc:
        out.update(state="unreadable", detail=str(exc))
        return out
    if paths.is_dataless(path):
        out.update(state="unreadable", detail="still in the cloud")
        return out
    mtime = _mtime(st)
    now: dict[str, Any] = {"dataset": dataset, "size": st.st_size, "mtime": mtime}
    cached = cache.get(rel)
    sha: str | None = None
    if isinstance(cached, Mapping) and cached.get("size") == st.st_size and cached.get("mtime") == mtime:
        sha = cached.get("sha256")
    if sha is None and (st.st_size < large_bytes or full):
        try:
            sha = _hash_file(path)
        except OSError as exc:
            out.update(state="unreadable", detail=str(exc))
            return out
        cache[rel] = {"size": st.st_size, "mtime": mtime, "sha256": sha}
    if sha is not None:
        now["sha256"] = sha
    out["now"] = now
    if not base or ("sha256" not in base and "size" not in base):
        out["state"] = "no_baseline"
        return out
    if sha is not None and isinstance(base.get("sha256"), str):
        out.update(state="same" if base["sha256"] == sha else "changed", coverage="full")
        return out
    if base.get("size") != st.st_size:
        out.update(state="changed", coverage="size")
        return out
    moved = isinstance(base.get("mtime"), str) and base.get("mtime") != mtime
    out.update(state="same", coverage="mtime" if moved else "size")
    return out


def _signature(baseline_id: str, script: Mapping[str, Any], inputs: list[Mapping[str, Any]]) -> str:
    shape = {
        "baseline": baseline_id,
        "script": [script.get("state"), (script.get("now") or {}).get("code") or (script.get("now") or {}).get("sha256")],
        "inputs": [[i["dataset"], i.get("state"), (i.get("now") or {}).get("sha256"), (i.get("now") or {}).get("size")]
                   for i in inputs],
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def compare(root: Path, card: V.Card, cache: dict[str, Any], *, large_bytes: int | None = None,
            full: bool = False) -> dict[str, Any] | None:
    """The comparison for the card's version in use (module docstring), or
    None when there is nothing to compare (a card that cannot be trusted,
    none in use, a version with no confirmation entry). Internal shape:
    `script` and `inputs` carry `now` (the fingerprints a reaffirmation
    records) and the script's bytes."""
    if not card.readable or card.in_use is None:
        return None
    number = card.in_use
    content = version_content(card, number)
    base_entry = baseline_entry(card, number)
    if content is None or base_entry is None:
        return None
    large = large_threshold() if large_bytes is None else large_bytes
    impl = content.get("implementation") or {}
    script = look_script(root, impl, _script_base(base_entry))
    bases = _input_bases(base_entry)
    inputs = []
    for item in content.get("input") or []:
        dataset = (item.get("dataset") or "").strip()
        if dataset:
            inputs.append(look_input(root, dataset, bases.get(posixpath.normpath(dataset)), cache,
                                     large_bytes=large, full=full))
    reasons: list[str] = []
    if script.get("reason"):
        reasons.append(script["reason"])
    if any(i["state"] == "changed" for i in inputs):
        reasons.append(INPUT_CHANGED)
    return {
        "card": card.id,
        "name": content.get("name") or card.id,
        "version": number,
        "baseline": base_entry.id,
        "baseline_act": base_entry.get("act"),
        "baseline_at": base_entry.at,
        "script": script,
        "inputs": inputs,
        "reasons": reasons,
        "draft": card.draft,
        "signature": _signature(base_entry.id, script, inputs),
    }


# -- what readers see ---------------------------------------------------------------------------


def _coverage_text(item: Mapping[str, Any]) -> str:
    state = item.get("state")
    if state == "changed":
        return CHANGED_LABEL
    if state == "region_missing":
        return REASON_LABELS.get(item.get("reason") or "", CHANGED_LABEL)
    if state == "same":
        return COVERAGE_LABELS[item.get("coverage") or "full"]
    return NOT_COMPARED_LABELS.get(state or "", "未比对")


def public(result: Mapping[str, Any]) -> dict[str, Any]:
    """A comparison as a reader sees it: each part with its Chinese
    wording (`text`) -- never a bare "unchanged" -- and no file bytes."""
    script = {k: v for k, v in result["script"].items() if k not in ("bytes", "now")}
    script["text"] = _coverage_text(script)
    inputs = []
    for item in result["inputs"]:
        shown = {k: v for k, v in item.items() if k != "now"}
        shown["text"] = _coverage_text(item)
        shown["large"] = (item.get("now") or {}).get("size", 0) >= large_threshold()
        inputs.append(shown)
    reasons = list(result["reasons"])
    return {
        "card": result["card"],
        "name": result["name"],
        "version": result["version"],
        "baseline": result["baseline"],
        "baseline_act": result["baseline_act"],
        "baseline_at": result["baseline_at"],
        "script": script,
        "inputs": inputs,
        "reasons": [{"code": r, "label": REASON_LABELS[r]} for r in reasons],
        "under_review": bool(reasons),
        "needs_data_version": INPUT_CHANGED in reasons,
        "draft": result["draft"],
        "draft_note": DRAFT_WAITING.format(n=result["draft"]) if reasons and result["draft"] is not None else None,
        "counted": bool(reasons) and result["draft"] is None,
        "signature": result["signature"],
        "cannot_tell": CANNOT_TELL if reasons else None,
    }


def load_cache(conn: Connection | None) -> dict[str, Any]:
    if conn is None:
        return {}
    status = db.get_record_status(conn, RECORD_STATUS_NAME) or {}
    hashes = status.get("hashes")
    return dict(hashes) if isinstance(hashes, Mapping) else {}


def refresh(conn: Connection, root: Path, cards_list: list[V.Card], *, trusted: set[str],
            large_bytes: int | None = None, full_for: set[str] | None = None) -> dict[str, Any]:
    """Recompute and store every trusted card's comparison (called by
    `cards.apply_cards`, under the project lock). `full_for`: card keys
    whose large inputs are hashed now (「完整比对」)."""
    cache = load_cache(conn)
    reviews: dict[str, Any] = {}
    for card in cards_list:
        if card.key not in trusted:
            continue
        try:
            result = compare(root, card, cache, large_bytes=large_bytes, full=card.key in (full_for or set()))
        except Exception:  # noqa: BLE001 -- one card's comparison never stops the others
            logger.exception("comparing the implementation of variable card %s failed", card.id)
            continue
        if result is not None:
            reviews[card.id] = public(result)
    # Keep the cache to the inputs some card still names.
    named = {posixpath.normpath(i["dataset"]) for r in reviews.values() for i in r["inputs"]}
    cache = {k: v for k, v in cache.items() if k in named}
    db.set_record_status(conn, RECORD_STATUS_NAME, {"cards": reviews, "hashes": cache})
    return reviews


def stored(conn: Connection | None) -> dict[str, Any]:
    """{card id: public comparison} as the last application stored it."""
    if conn is None:
        return {}
    status = db.get_record_status(conn, RECORD_STATUS_NAME) or {}
    cards_ = status.get("cards")
    return dict(cards_) if isinstance(cards_, Mapping) else {}


def review_groups(conn: Connection | None) -> dict[str, Any]:
    """The cards' part of 9.6's list: one item per changed script (「此脚本
    的改动涉及 N 个变量」, naming every card on it), one per card whose
    inputs alone changed. `count` is the items with a member not waiting on
    an open draft (a 「口径已变」 answered by opening the next draft)."""
    by_script: dict[str, list[dict[str, Any]]] = {}
    alone: list[dict[str, Any]] = []
    for review in sorted(stored(conn).values(), key=lambda r: r["card"]):
        if not review.get("under_review"):
            continue
        codes = {r["code"] for r in review["reasons"]}
        if codes & set(SCRIPT_REASONS) and review["script"].get("path"):
            by_script.setdefault(review["script"]["path"], []).append(review)
        else:
            alone.append(review)
    groups = []
    for script, members in sorted(by_script.items()):
        groups.append({
            "key": "script:" + script, "script": script, "cards": members,
            "message": GROUP_MESSAGE.format(n=len(members)),
            "counted": any(m["counted"] for m in members),
        })
    for review in alone:
        groups.append({
            "key": "card:" + review["card"], "script": None, "cards": [review], "message": None,
            "counted": review["counted"],
        })
    return {"groups": groups, "count": sum(1 for g in groups if g["counted"]), "cannot_tell": CANNOT_TELL}


def watched_paths(root: Path) -> list[Path]:
    """The implementing scripts and input datasets of every card's version
    in use (the watcher's watch set: a change re-applies the record, which
    re-runs these comparisons). Never raises."""
    out: list[Path] = []
    try:
        for card in V.read_cards(root):
            if not card.readable or card.in_use is None:
                continue
            content = version_content(card, card.in_use)
            if not content:
                continue
            rels = [(content.get("implementation") or {}).get("script") or ""]
            rels += [(i.get("dataset") or "") for i in content.get("input") or []]
            for rel in rels:
                rel = rel.strip()
                if not rel or V._project_path_problem(rel):
                    continue
                out.append(root / posixpath.normpath(rel))
    except Exception:  # noqa: BLE001 -- the watch set degrades, the watcher goes on
        logger.exception("listing the variable cards' implementation files of %s failed", root)
    return out
