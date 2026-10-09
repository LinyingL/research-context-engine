"""The 「文献」 view's endpoints (DESIGN.md 11.4, last paragraph; 11.5 #6;
task V7 phase D). Routed by `rce.webapp.server`, whose handler runs
`_check_local_origin` first on every request.

    GET  /api/citations                  per draft, its citations and how each
                                         resolves (`rce.ingest.citations.report`)
                                         plus, per candidate, the graph links a
                                         确认 / 否决 writes (`links`), the DOI and
                                         Zotero addresses, the PDF to open; the
                                         summary; the online setting; whether
                                         the Zotero program is installed
    POST /api/citations/lookup-setting   {on: bool} -- 「用 DOI 联网查文献信息」
                                         (a machine setting under the RCE home;
                                         nothing is asked here, the next scan
                                         asks, and only the DOIs)
    POST /api/zotero/open-attachment     {item_key, attachment_key?} -- 「打开 PDF」
    POST /api/zotero/open-item           {item_key} -- 「在 Zotero 中打开」

Reads only, except the setting (RCE's own file) and the two openers, which
start a program and write nothing. Confirming or rejecting a candidate is
the existing `POST /api/judgements` with a link from `links` (the ledger,
9.3): this module writes no record.

The drafts are the ones the last scan's `citations` extractor considered
(`scan_sources`), so the view and the graph speak about the same files;
each is parsed afresh (the text as written now), and the statuses of the
candidates come from the graph (a confirmed or rejected candidate shows as
such). An index from before the extractor existed says so (`scanned`
false) rather than showing nothing as if nothing were cited.

「打开 PDF」 never takes a path: the item key (and optionally the attachment
key) are validated as Zotero keys, the library is read afresh, and only the
file the database names for that item, inside Zotero's storage directory
after symlinks are resolved, is opened (`rce.literature.attachment_file`).
「在 Zotero 中打开」 opens `zotero://select/library/items/<key>` built from a
validated key of an item the library holds; it goes through the engine
rather than as a page link because RCE.app's window opens only http(s) and
mailto links (8.9).
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Callable

from rce import literature
from rce.ingest import citations as citations_ingest
from rce.ingest import scan as scan_mod


class LiteratureRefused(Exception):
    """An action of the 文献 view refused: nothing opened or written.
    `state` is `literature_<code>`; `extra` carries `message_zh` (the page's
    sentence) and `detail` (the engine's text, behind 「详情」)."""

    def __init__(self, code: str, status: int, message_zh: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.state = "literature_" + code
        self.extra = {"message_zh": message_zh, "detail": detail}


_ATTACHMENT_STATUS = {"bad_key": 400, "library": 409, "no_item": 404, "no_attachment": 404,
                      "not_pdf": 409, "missing": 404, "outside": 403, "cloud": 409}


def _citations_ran(conn: Connection) -> bool:
    for (extractors,) in conn.execute("SELECT extractors FROM scans WHERE outcome = 'finished'"):
        if f'"{citations_ingest.EXTRACTOR}"' in (extractors or ""):
            return True
    return False


def scanned_drafts(conn: Connection, root: Path) -> list[str]:
    """The drafts the last scan's `citations` extractor read or tried to
    read, still present in the folder (the Zotero half of each source is
    not a file)."""
    rows = conn.execute(
        "SELECT source FROM scan_sources WHERE extractor = ? AND status != ?",
        (citations_ingest.EXTRACTOR, scan_mod.ABSENT),
    ).fetchall()
    drafts = {src for (src,) in rows if scan_mod.SOURCE_SEPARATOR not in src}
    return sorted(d for d in drafts if os.path.lexists(root / d))


def link_statuses(conn: Connection) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        "SELECT src, dst, status FROM edges WHERE extractor = ? AND type = 'cites'",
        (citations_ingest.EXTRACTOR,),
    ).fetchall()
    return {(src, dst): status for src, dst, status in rows}


def doi_url(doi: str) -> str:
    return "https://doi.org/" + urllib.parse.quote(doi, safe="/:;()._-")


def _pdf_of(target: dict[str, Any]) -> dict[str, Any] | None:
    key = target.get("zotero_key")
    if not literature.valid_zotero_key(key):
        return None
    for att in target.get("attachments") or []:
        if (att.get("available") and att.get("content_type") == "application/pdf"
                and str(att.get("file") or "").lower().endswith(".pdf") and literature.valid_zotero_key(att.get("key"))):
            return {"item_key": key, "attachment_key": att["key"], "file": att["file"]}
    return None


def _judged(links: list[dict[str, Any]]) -> str | None:
    """What the researcher decided for one candidate, over its links."""
    found = [link["status"] for link in links]
    if "confirmed" in found:
        return "confirmed"
    if found and all(s == "rejected" for s in found):
        return "rejected"
    return None


def citations_payload(conn: Connection, root: Path) -> dict[str, Any]:
    """`GET /api/citations` (module docstring)."""
    statuses = link_statuses(conn)
    library = literature.load_library()
    rep = citations_ingest.report(root, scanned_drafts(conn, root), library=library, statuses=statuses)
    for draft in rep["drafts"]:
        for c in draft["citations"]:
            froms = ([c["section"]] if c.get("section") else []) + list(c.get("claims") or [])
            candidates = c.get("status") == citations_ingest.PENDING
            for t in c["targets"]:
                node = t["node"]
                # A candidate's links: what 确认 / 否决 write (one ledger entry each).
                t["links"] = [
                    {"src": f, "dst": node, "type": "cites", "extractor": citations_ingest.EXTRACTOR,
                     "status": statuses[(f, node)]}
                    for f in froms if (f, node) in statuses
                ] if candidates else []
                t["judged"] = _judged(t["links"]) if candidates else None
                t["doi_url"] = doi_url(t["doi"]) if t.get("doi") else None
                key = t.get("zotero_key")
                t["zotero_url"] = literature.zotero_select_url(key) if literature.valid_zotero_key(key) else None
                t["pdf"] = _pdf_of(t)
    rep["scanned"] = _citations_ran(conn)
    rep["zotero"]["installed"] = literature.zotero_installed()
    return rep


def lookup_setting_payload(body: dict[str, Any]) -> dict[str, Any]:
    """`POST /api/citations/lookup-setting` {on}: turns the online lookup
    on or off. Asks nothing now; the next scan asks for the DOIs only."""
    on = body.get("on")
    if not isinstance(on, bool):
        raise LiteratureRefused("bad_setting", 400, "设置没有保存", "request body 'on' must be true or false")
    try:
        literature.set_doi_lookup(on)
    except OSError as exc:
        raise LiteratureRefused("setting_failed", 500, "设置没有保存", f"{literature.settings_path()}: {exc}") from exc
    return {"ok": True, "lookup": {"enabled": literature.doi_lookup_enabled()}}


def open_attachment_payload(body: dict[str, Any], opener: Callable[[str], None]) -> dict[str, Any]:
    """`POST /api/zotero/open-attachment` {item_key, attachment_key?}: any
    other key of the body (a `path`, say) is never read."""
    try:
        target = literature.attachment_file(body.get("item_key"), body.get("attachment_key"))
    except literature.AttachmentRefused as exc:
        raise LiteratureRefused(exc.code, _ATTACHMENT_STATUS.get(exc.code, 409), exc.message, str(exc)) from exc
    opener(str(target))
    return {"ok": True, "opened": target.name}


def open_item_payload(body: dict[str, Any], opener: Callable[[str], None]) -> dict[str, Any]:
    """`POST /api/zotero/open-item` {item_key}: 「在 Zotero 中打开」, only
    when the program is installed and the library holds the item."""
    key = body.get("item_key")
    if not literature.valid_zotero_key(key):
        raise LiteratureRefused("bad_key", 400, "这不是一个 Zotero 条目", f"not a Zotero item key: {key!r}")
    if not literature.zotero_installed():
        raise LiteratureRefused("no_zotero", 409, "这台电脑上没有安装 Zotero", "no application with bundle id "
                                + literature.ZOTERO_BUNDLE_ID)
    library = literature.load_library()
    if library.item(key) is None:
        raise LiteratureRefused("no_item", 404, "Zotero 文献库里没有这个条目",
                                f"no item {key} in {library.data_dir} ({library.status})")
    url = literature.zotero_select_url(key)
    opener(url)
    return {"ok": True, "opened": url}
