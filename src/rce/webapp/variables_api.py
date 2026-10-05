"""The 「变量」 view's endpoints (DESIGN.md 9.11 "In the app" and stage (b);
task V5 phase 9). Routed by `rce.webapp.server`, whose handler runs
`_check_local_origin` first on every request and wraps every write in its
write guard (project lock, the 9.4 re-check, read-only and pre-V5 refusals).

Reads (GET, files first, the index for the trust decision and stage (b)):

    /api/variables                 the cards, one row each
    /api/variables/card?id=        one card: its versions, the log as history,
                                   each check with its result and reason, the
                                   observations, the attestation, problems,
                                   references, the stage-(b) comparison
    /api/variables/code?id=&entry= 「查看当时的代码」: the code copy the entry
                                   names, as text
    /api/variables/frozen?id=&entry=  the frozen copy the entry names, as text

Writes (POST) -- ONLY what RCE authors, each through `rce.records.cards`
(`via="app"`, `expected_id` = the served id). Never the researcher's text:
there is no endpoint that writes a `v<n>.toml` or creates a card.

    /api/variables/confirm      {id, attested: yes|no|unknown, content_hash}
    /api/variables/revise       {id}                       (also 「口径已变」)
    /api/variables/reaffirm     {items: [{id, version, signature, data_version?}], note?}
                                「口径未变」 / 「全部口径未变」: one entry per card
    /api/variables/abandon      {id, note}
    /api/variables/revive       {id, note}
    /api/variables/answer       {id, question: "edited", answer: new|correct, version, content_hash}
                                {id, question: "shrunk", answer: file|restore, missing: [ids shown]}
    /api/variables/full-compare {id}                       「完整比对」 (the index only)
    /api/variables/settle       {id, keeps: [entry ids], shown: [entry ids the page showed]}
                                「以这一条为准」 (9.12): two merged histories settled
                                by naming, per version in dispute, what stands

`content_hash` is the hash the page showed for the version file (the
draft's, or the edited file's `content_hash`; null for a file that could
not be read): 9.12, "an answer belongs to the question that was shown" --
if the file differs now, the act is refused (`card_question_changed`) and
the page shows the file again. The attestation is recorded against the
text the researcher saw, never against what reached the disk after.

Paths: a card id is only ever compared with the names `.rce/variables/`
lists (never joined into a path); a copy named by a log entry is resolved
and must stay inside `.rce/variables/_code/` (code) or the card's
`frozen/` (frozen) -- resolve, then `relative_to` -- before it is read.
"""

from __future__ import annotations

from pathlib import Path
from sqlite3 import Connection
from typing import Any

from rce.records import cards
from rce.records import implementation
from rce.records import variables as V
from rce.records.identity import IdentityState, read_identity

#: Above this a kept copy is cut (and the page says so); scripts are small.
TEXT_LIMIT = 1024 * 1024

EMPTY_HELP = {
    "lines": [
        "变量定义卡记下一个变量是什么、怎样构建、为什么这样构建：研究口径由你书写，实现依据由 RCE 对照文件核对。",
        "卡片写在项目的 .rce/variables/<id>/ 里，由你在编辑器中填写；页面只显示它，并记录确认、弃用等动作。",
        "为真正进入模型、图表或论断的变量建卡。在终端运行下面的命令创建第一张：",
    ],
    "command": "rce variable new <id>",
}

# 8.8: the sentence for each refusal code that carries no sentence of its own.
REFUSED_ZH = {
    "invalid": "这个请求无法完成，卡片的内容或请求本身有问题",
    "exists": "已经有同名的变量卡",
    "no_such_card": "找不到这张变量卡，可能刚被移走或改名",
    "untrusted": "变量卡的记录文件当前无法读取，请先恢复它",
    "no_index": "项目的索引不在，请重新打开项目",
    "no_draft": "这张卡没有可以确认的草稿",
    "incomplete": "草稿还不完整：确认需要名称、含义、单位、粒度、至少一个输入、构建公式和决策理由",
    "unresolvable": "引用暂不可解析",
    "draft_open": "已有草稿，请先确认它",
    "no_question": "现在已经没有需要回答的问题了",
    "question_open": "这一版在确认后被改动了，请先回答那个问题",
    "copy_missing": "确认记录引用的副本缺失",
    "nothing_confirmed": "这张卡还没有确认过的版本，请直接编辑草稿",
    "already_abandoned": "这张卡已经弃用了",
    "not_abandoned": "这张卡没有被弃用",
    "would_lose": "记录文件当前无法使用，以文件为准会丢掉仅存的记录副本；请先恢复文件，或选择「把缺少的补回文件」",
    "question_changed": "文件在提问之后又变了，请重新查看再回答",
    "changed": "文件在写入时被改动了，没有记录任何东西；请重试",
    "region_missing": "找不到这一版指定的代码块或函数",
    "not_confirmed": "只有确认过的版本才能被引用",
    "no_conflict": "这张卡的记录里没有需要处理的两份历史",
}

STATUS = {"invalid": 400, "incomplete": 400, "no_such_card": 404}


class CardActionRefused(Exception):
    """Nothing was written. `state` is `card_<code>`; `message_zh` the
    product sentence the page shows (the engine's English stays behind
    「详情」)."""

    def __init__(self, message: str, code: str, message_zh: str | None, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = STATUS.get(code, 409)
        self.state = "card_" + code
        self.message_zh = message_zh or REFUSED_ZH.get(code)
        self.extra = {"message_zh": self.message_zh, **(extra or {})}


def _refused(exc: V.VariableError) -> CardActionRefused:
    extra = None
    decision = getattr(exc, "decision", None)
    if decision is not None:
        extra = {"records": {"state": decision.verdict.value, "reason": decision.reason, "detail": decision.detail,
                             "missing": [cards._summary(m) for m in decision.missing]}}
    return CardActionRefused(str(exc), exc.code, exc.message_zh, extra)


def _bad(message: str) -> CardActionRefused:
    return CardActionRefused(message, "invalid", None)


def _card_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise _bad("'id' must name a variable card")
    return value


# -- reads -------------------------------------------------------------------------------------


def _name(card_payload: dict[str, Any]) -> str:
    versions = {v["version"]: v for v in card_payload.get("versions") or []}
    for number in (card_payload.get("in_use"), card_payload.get("draft"), *sorted(versions, reverse=True)):
        content = (versions.get(number) or {}).get("content") if number is not None else None
        if isinstance(content, dict) and isinstance(content.get("name"), str) and content["name"].strip():
            return content["name"]
    return card_payload["id"]


def _identity(root: Path):
    got = read_identity(root)
    return got.identity if got.state is IdentityState.PRESENT else None


def _full(conn: Connection | None, root: Path, card: V.Card, dead: list[dict[str, Any]]) -> dict[str, Any]:
    decision = cards.assess_card(conn, root, card, _identity(root))
    out = cards.card_payload(root, card, decision, dead)
    out["name"] = _name(out)
    for version in out["versions"]:
        ref = version.get("reference")
        entry = card.versions[version["version"]].entry
        if ref:
            resolved = V.resolve(root, V.Reference(ref["variable"], ref["version"], ref["entry"], ref["content"]))
            ref["resolves"] = resolved is not None and not resolved.copy_missing
        version["upstream"] = []
        for item in (entry.get("upstream") or []) if entry is not None else []:
            if not isinstance(item, dict):
                continue
            pinned = V.Reference(str(item.get("variable")), int(item.get("version") or 0), str(item.get("entry")),
                                 str(item.get("content")))
            resolves = V.resolve(root, pinned) is not None
            version["upstream"].append({"ref": pinned.label, "resolves": resolves,
                                        "unresolvable": None if resolves else V.UNRESOLVABLE})
    out["implementation"] = implementation.stored(conn).get(card.id) if card.readable else None
    return out


def list_payload(conn: Connection | None, root: Path) -> dict[str, Any]:
    """`GET /api/variables`: one row per card (and per card the index knows
    whose directory is gone)."""
    reviews = implementation.stored(conn)
    rows = []
    for card in cards.overview(conn, root):
        review = reviews.get(card["id"]) if card["state"] == "ok" else None
        trust = card.get("trust") or {}
        rows.append({
            "id": card["id"],
            "name": _name(card),
            "state": card["state"],
            "reason": card.get("reason"),
            "message": card.get("message") or trust.get("message"),
            "trust_state": trust.get("state"),
            "in_use": card["in_use"],
            "draft": card["draft"],
            "abandoned": card["abandoned"] is not None,
            "questions": len(card["questions"]),
            "problems": len(card["problems"]),
            "dead_flags": card["dead_flags"],
            "under_review": bool(review and review.get("under_review")),
            "review_labels": [r["label"] for r in (review or {}).get("reasons") or []],
        })
    return {"cards": rows, "empty_help": EMPTY_HELP}


def _open(root: Path, card_id: str) -> V.Card:
    found = V.find_card_dirs(root, card_id)
    if not found:
        raise CardActionRefused(f"there is no variable card {card_id!r}", "no_such_card", None)
    return V.read_card(root, found[0])


def card_payload(conn: Connection | None, root: Path, card_id: str) -> dict[str, Any]:
    """`GET /api/variables/card?id=`: the whole card (module docstring)."""
    card = _open(root, _card_id(card_id))
    return _full(conn, root, card, cards.dead_variable_flags(root, [card]))


def _entry(card: V.Card, entry_id: Any):
    if not isinstance(entry_id, str) or not entry_id:
        raise _bad("'entry' must name a log entry")
    entry = card.log.ledger.by_id(entry_id) if card.log is not None and card.log.ledger is not None else None
    if entry is None:
        raise CardActionRefused(f"{card.id} has no log entry {entry_id!r}", "no_such_card", "找不到这条记录")
    return entry


def _confined_text(target: Path, allowed: Path) -> tuple[str, bool]:
    try:
        resolved = target.resolve()
        resolved.relative_to(allowed.resolve())
    except (ValueError, OSError) as exc:
        raise CardActionRefused(f"{target} is outside {allowed}; refusing", "invalid", "这个副本不在它应在的位置，不予读取") from exc
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise CardActionRefused(f"{target}: {exc}", "copy_missing", None) from exc
    return data[:TEXT_LIMIT].decode("utf-8", "replace"), len(data) > TEXT_LIMIT


def code_payload(root: Path, card_id: str, entry_id: str) -> dict[str, Any]:
    """「查看当时的代码」: the code copy entry `entry_id` names."""
    card = _open(root, _card_id(card_id))
    entry = _entry(card, entry_id)
    script = (entry.get("checked") or {}).get("script") or {}
    copy = script.get("copy") if isinstance(script, dict) else None
    if not isinstance(copy, str) or not copy:
        raise CardActionRefused(f"entry {entry_id} kept no code copy", "copy_missing", "这条记录没有保存代码副本")
    text, truncated = _confined_text(V.variables_dir(root) / copy, V.code_dir(root))
    return {"id": card.id, "entry": entry.id, "act": entry.get("act"), "version": entry.get("version"),
            "at": entry.at, "copy": copy, "sha256": script.get("sha256"), "text": text, "truncated": truncated}


def frozen_payload(root: Path, card_id: str, entry_id: str) -> dict[str, Any]:
    """The frozen copy entry `entry_id` names (a version as confirmed or
    corrected)."""
    card = _open(root, _card_id(card_id))
    entry = _entry(card, entry_id)
    frozen = entry.get("frozen")
    if not isinstance(frozen, str) or not frozen:
        raise CardActionRefused(f"entry {entry_id} names no frozen copy", "copy_missing", None)
    text, truncated = _confined_text(card.directory / frozen, card.directory / V.FROZEN_DIRNAME)
    return {"id": card.id, "entry": entry.id, "version": entry.get("version"), "frozen": frozen, "text": text,
            "truncated": truncated}


# -- writes ------------------------------------------------------------------------------------

ACTIONS = ("confirm", "revise", "reaffirm", "abandon", "revive", "answer", "full-compare", "settle")


def _note(body: dict[str, Any], *, required: bool) -> str | None:
    note = body.get("note")
    if note is None and not required:
        return None
    if not isinstance(note, str) or (required and not note.strip()):
        raise CardActionRefused("a reason is required in 'note'", "invalid", "请写下理由")
    return note


def _shown(body: dict[str, Any]) -> str | None:
    """The version file's content hash as the page showed it (required)."""
    if "content_hash" not in body:
        raise _bad("'content_hash' must carry the content hash of the version file the page showed")
    shown = body["content_hash"]
    if shown is not None and (not isinstance(shown, str) or not shown.startswith(V.CONTENT_PREFIX)):
        raise _bad("'content_hash' must be sha256:<hex> or null")
    return shown


def act(root: Path, project_id: str | None, action: str, body: dict[str, Any]) -> dict[str, Any]:
    """One POST action (module docstring). The caller holds the server's
    write guard; every write takes the project lock itself as well."""
    if action not in ACTIONS:
        raise CardActionRefused(f"no such action: {action}", "invalid", None)
    common = {"expected_id": project_id, "timeout": 10.0}
    try:
        if action == "reaffirm":
            return _reaffirm(root, body, common)
        card_id = _card_id(body.get("id"))
        if action == "confirm":
            attested = body.get("attested")
            if attested not in V.ATTESTED:
                raise _bad("'attested' must be yes, no or unknown")
            done = cards.confirm(root, card_id, attested=attested, expected_content=_shown(body), via="app", **common)
            return {"ok": True, "id": done.reference.variable, "version": done.entry.get("version"),
                    "entry": done.entry.id, "reference": done.reference.label}
        if action == "revise":
            path = cards.revise(root, card_id, **common)
            return {"ok": True, "id": card_id, "file": path.relative_to(root).as_posix(),
                    "version": int(path.stem[1:])}
        if action in ("abandon", "revive"):
            fn = cards.abandon if action == "abandon" else cards.revive
            entry = fn(root, card_id, note=_note(body, required=True), via="app", **common)
            return {"ok": True, "id": card_id, "entry": entry.id}
        if action == "full-compare":
            review = cards.full_compare(root, card_id, **common)
            return {"ok": True, "id": card_id, "implementation": review}
        if action == "settle":
            keeps, shown = body.get("keeps"), body.get("shown")
            for name, value in (("keeps", keeps), ("shown", shown)):
                if not isinstance(value, list) or not value or len(value) > 500 or not all(isinstance(i, str) and i for i in value):
                    raise _bad(f"'{name}' must list entry ids")
            done = cards.settle(root, card_id, keeps, expected_shown=shown, note=_note(body, required=False),
                                via="app", **common)
            return {"ok": True, "id": card_id, "entry": done.entry.id, "keeps": list(done.keeps),
                    "not_in_force": list(done.not_in_force), "questions": list(done.card.questions)}
        return _answer(root, card_id, body, common)
    except V.VariableError as exc:
        raise _refused(exc) from exc


def _reaffirm(root: Path, body: dict[str, Any], common: dict[str, Any]) -> dict[str, Any]:
    items = body.get("items")
    if not isinstance(items, list) or not items or len(items) > 200:
        raise _bad("'items' must list the cards answered")
    note = _note(body, required=False)
    done: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("version"), int) or not isinstance(item.get("signature"), str):
            raise _bad("each item needs id, version and signature")
        data_version = item.get("data_version")
        if data_version is not None and not isinstance(data_version, str):
            raise _bad("'data_version' must be text")
        card_id = _card_id(item.get("id"))
        try:
            entry = cards.reaffirm(root, card_id, version=item["version"], signature=item["signature"],
                                   data_version=data_version, note=note, via="app", **common)
        except V.VariableError as exc:
            # Cards answered before this one stay answered (each is its own
            # record); the page is told which.
            refused = _refused(exc)
            refused.extra["done"] = done
            refused.extra["card"] = card_id
            raise refused from exc
        done.append({"id": card_id, "entry": entry.id})
    return {"ok": True, "done": done}


def _answer(root: Path, card_id: str, body: dict[str, Any], common: dict[str, Any]) -> dict[str, Any]:
    question, answer = body.get("question"), body.get("answer")
    if question == "edited":
        version = body.get("version")
        if not isinstance(version, int):
            raise _bad("'version' must name the version asked about")
        done = cards.answer_edited(root, card_id, answer, version=version, expected_content=_shown(body),
                                   via="app", **common)
        return {"ok": True, "id": card_id, "answer": done.answer, "version": done.version,
                "new_version": done.new_version, "entry": None if done.entry is None else done.entry.id}
    if question == "shrunk":
        shown = body.get("missing")
        if not isinstance(shown, list) or not all(isinstance(i, str) and i for i in shown):
            raise _bad("'missing' must carry the ids of the missing entries the question showed")
        done = cards.answer_shrunk(root, card_id, answer, expected_missing=shown, via="app", **common)
        return {"ok": True, "id": card_id, "answer": done.answer, "missing": len(done.missing),
                "appended": len(done.appended)}
    raise _bad("'question' must be 'edited' or 'shrunk'")
