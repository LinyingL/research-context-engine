"""A project linked to its GitHub repository (DESIGN.md 11.2, task V7).

A project whose git remote is on `github.com` is *linked*. Nothing is
configured and nothing is stored: everything here is read from the
repository each time it is asked.

`github_remote(root)` -- which remote, and which repository on GitHub. The
remote's configured URL (`remote.<name>.url`, exactly one) is parsed
strictly (`parse_url`): `https://github.com/<owner>/<repo>[.git][/]`,
`git@github.com:<owner>/<repo>[.git]`, `ssh://[git@]github.com/<owner>/
<repo>[.git][/]` -- ASCII only, no user name or password in an https URL,
no port, no query or fragment, no further path segment; the owner in
GitHub's character set (`[A-Za-z0-9-]{1,39}`), the name in its
(`[A-Za-z0-9._-]{1,100}`, not `.` or `..`). Anything else is not linked. The
remote is the current branch's upstream remote, else `origin`, else the one
remote whose URL is GitHub's; several and none chosen: not linked (never a
guess). The URL RCE shows is BUILT from the parsed owner and name; the
configured URL is never echoed -- it may carry a token.

`state(root)` -- from local refs only, no network: the commit GitHub is
known to have (the upstream branch's tip when the upstream is on the linked
remote, else the remote's default branch, `refs/remotes/<remote>/HEAD`),
how far the local branch is ahead of and behind it, and when that was
learnt: the last fetch (`FETCH_HEAD`'s modification time) or, newer, a
clone or push the reflog records (「（<日期> 获取）」 / 「（<日期> 推送）」).
Remote-tracking refs with no such date read 「还没有从 GitHub 获取过」.

`fetch(root)` -- `git fetch <remote>`, the only network RCE does here, only
on the researcher's click; never `pull`, never a merge. Under the project
lock. Hardened beyond 10.9 (`HARDENING`): hooks off (`core.hooksPath`), no
submodules, no automatic maintenance, never a password prompt. A
repository whose OWN config names a program for the fetch to run (an ssh
command, a credential helper, an askpass program, an upload-pack, a URL
rewrite) is refused: the folder must not choose what RCE runs. The
researcher's own (global) git settings and logins apply as they always do.

`link_for(root, rel_path)` / `link_for_commit(root, sha)` -- a GitHub link
pinned to a commit GitHub has, or the reason there is none. URLs are built
only from the parsed owner and name, a validated full commit id and the
percent-encoded path git itself reported -- never from the page's input.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rce import cloud
from rce.ingest import git as git_ingest
from rce.records import lock as records_lock

logger = logging.getLogger(__name__)

HOST = "github.com"
_OWNER_RE = re.compile(r"[A-Za-z0-9-]{1,39}")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
_REMOTE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_ABBREV_RE = re.compile(r"[0-9a-f]{4,64}")

FETCH_TIMEOUT_S = 120.0
FETCH_LOCK_TIMEOUT_S = 5.0

# 11.2's sentences
NOT_PUSHED = "not_pushed"
NOT_TRACKED = "not_tracked"
REASON_MESSAGES = {
    NOT_PUSHED: "这个文件还没有推送到 GitHub",
    NOT_TRACKED: "git 没有跟踪这个文件，GitHub 上不会有它",
}
LOCAL_CHANGES_MESSAGE = "本地有未推送或未提交的改动"
NEVER_FETCHED = "还没有从 GitHub 获取过"
IN_SYNC = "与 GitHub 一致"

ERROR_MESSAGES = {
    "not_linked": "这个项目没有关联 GitHub 仓库",
    "unsafe_config": "这个仓库自己的 git 设置指定了获取时要运行的程序，RCE 不会替它运行；请在终端里运行 git fetch",
    "busy": "另一个 RCE 操作正在使用这个项目，请稍后再试",
    "timeout": "从 GitHub 获取超时了，请检查网络后再试",
    "fetch_failed": "没能从 GitHub 获取",
    "git_missing": "这台电脑上找不到 git",
}

# Keys of a repository's OWN config that name a program `git fetch` would run
# (or a place it would be sent instead): `fetch` refuses such a repository.
_PROGRAM_KEYS = (
    re.compile(r"core\.sshcommand"),
    re.compile(r"core\.gitproxy"),
    re.compile(r"core\.askpass"),
    re.compile(r"credential\..*helper"),
    re.compile(r"remote\..*\.(uploadpack|receivepack|vcs|proxy)"),
    re.compile(r"url\..*\.(insteadof|pushinsteadof)"),
    re.compile(r"protocol\..*allow"),
    re.compile(r"http\..*(proxy|sslcainfo|sslcapath|sslcert|sslkey|cookiefile)"),
)

# Beyond `git_ingest.HARDENING`: nothing the repository configures runs
# during a fetch.
_FETCH_HARDENING = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "fetch.recurseSubmodules=false",
    "-c", "submodule.recurse=false",
    "-c", "maintenance.auto=false",
    "-c", "gc.auto=0",
    "-c", "fetch.writeCommitGraph=false",
)

_CREDENTIALS_IN_URL = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]*@")


def scrub(text: str) -> str:
    """`text` with any user name / password inside a URL removed -- for
    error text and logs: a remote URL may carry a token."""
    return _CREDENTIALS_IN_URL.sub(r"\1***@", text)


class GitHubError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        detail = scrub(detail)
        super().__init__(detail or code)
        self.code = code
        self.message = ERROR_MESSAGES[code]
        self.detail = detail or ERROR_MESSAGES[code]


# -- parsing ----------------------------------------------------------------------


def _owner_repo(owner: str, repo: str) -> tuple[str, str] | None:
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _OWNER_RE.fullmatch(owner) or not _REPO_RE.fullmatch(repo) or repo in (".", ".."):
        return None
    return owner, repo


def parse_url(url: str) -> tuple[str, str] | None:
    """(owner, repo) of a GitHub remote URL, or None (module docstring)."""
    if not isinstance(url, str) or not url.isascii() or not url.isprintable() or any(c.isspace() for c in url):
        return None
    scp = re.fullmatch(r"git@([^:/]+):([^/]+)/([^/]+)", url)
    if scp:
        if scp.group(1).lower() != HOST:
            return None
        return _owner_repo(scp.group(2), scp.group(3))
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("https", "ssh") or port is not None or parts.query or parts.fragment:
        return None
    if "?" in url or "#" in url:
        return None
    if (parts.hostname or "").lower() != HOST or parts.password is not None:
        return None
    netloc_host = parts.netloc.rsplit("@", 1)[-1]
    if netloc_host.lower() != HOST:
        return None
    if scheme == "https" and parts.username is not None:
        return None
    if scheme == "ssh" and parts.username not in (None, "git"):
        return None
    path = parts.path
    if path.endswith("/"):
        path = path[:-1]
    segments = path.split("/")
    if len(segments) != 3 or segments[0] != "":
        return None
    return _owner_repo(segments[1], segments[2])


# -- git ---------------------------------------------------------------------------


def _git(root: Path, args: list[str]) -> str:
    return git_ingest._run_git(root, args)


def _git_ok(root: Path, args: list[str]) -> str | None:
    """stdout, or None when git exits non-zero."""
    try:
        return _git(root, args)
    except git_ingest.GitIngestError:
        return None


def _is_repo(root: Path) -> bool:
    try:
        return _git(root, ["rev-parse", "--is-inside-work-tree"]).strip() == "true"
    except git_ingest.GitIngestError:
        return False


def _remote_urls(root: Path) -> dict[str, list[str]]:
    out = _git_ok(root, ["config", "-z", "--get-regexp", r"^remote\..*\.url$"]) or ""
    urls: dict[str, list[str]] = {}
    for record in out.split("\0"):
        if not record:
            continue
        key, _, value = record.partition("\n")
        name = key[len("remote."):-len(".url")]
        urls.setdefault(name, []).append(value)
    return urls


def _branch(root: Path) -> str | None:
    out = _git_ok(root, ["symbolic-ref", "-q", "--short", "HEAD"])
    return out.strip() or None if out else None


def _upstream(root: Path) -> str | None:
    """The upstream's full ref (`refs/remotes/origin/main`), or None."""
    out = _git_ok(root, ["rev-parse", "--symbolic-full-name", "@{upstream}"])
    ref = (out or "").strip()
    return ref if ref.startswith("refs/remotes/") else None


@dataclass(frozen=True)
class Remote:
    owner: str
    repo: str
    remote: str

    @property
    def url(self) -> str:
        return f"https://{HOST}/{self.owner}/{self.repo}"

    def payload(self) -> dict[str, str]:
        return {"owner": self.owner, "repo": self.repo, "remote": self.remote, "url": self.url}


def _linked(root: Path) -> Remote | None:
    urls = _remote_urls(root)
    github: dict[str, tuple[str, str]] = {}
    for name, values in urls.items():
        if len(values) != 1 or not _REMOTE_NAME_RE.fullmatch(name):
            continue
        parsed = parse_url(values[0])
        if parsed is not None:
            github[name] = parsed
    if not github:
        return None
    upstream = _upstream(root)
    chosen: str | None = None
    if upstream is not None:
        for name in github:
            if upstream.startswith(f"refs/remotes/{name}/"):
                chosen = name
    if chosen is None and "origin" in github:
        chosen = "origin"
    if chosen is None and len(github) == 1:
        chosen = next(iter(github))
    if chosen is None:
        return None
    owner, repo = github[chosen]
    return Remote(owner, repo, chosen)


def github_remote(root: str | Path) -> dict[str, str] | None:
    """{owner, repo, remote, url} of the linked GitHub repository, or None.
    `url` is built from owner and name, never the configured URL."""
    root = Path(root)
    if not _is_repo(root):
        return None
    linked = _linked(root)
    return None if linked is None else linked.payload()


def _ref_sha(root: Path, ref: str) -> str | None:
    out = _git_ok(root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    sha = (out or "").strip()
    return sha if _SHA_RE.fullmatch(sha) else None


def _base(root: Path, linked: Remote) -> tuple[str | None, str | None]:
    """(ref, kind) of the commit GitHub is known to have: the upstream when
    it is on the linked remote, else the remote's default branch."""
    upstream = _upstream(root)
    if upstream is not None and upstream.startswith(f"refs/remotes/{linked.remote}/") and _ref_sha(root, upstream):
        return upstream, "upstream"
    out = _git_ok(root, ["symbolic-ref", "-q", f"refs/remotes/{linked.remote}/HEAD"])
    default = (out or "").strip()
    if default.startswith(f"refs/remotes/{linked.remote}/") and _ref_sha(root, default):
        return default, "default"
    return None, None


def _fetched_at(root: Path) -> datetime | None:
    out = _git_ok(root, ["rev-parse", "--git-path", "FETCH_HEAD"])
    if not out:
        return None
    path = Path(out.strip())
    if not path.is_absolute():
        path = root / path
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _reflog_last(root: Path, ref: str) -> tuple[datetime, str] | None:
    """(when, how) of the newest reflog entry of `ref` that a fetch, a
    clone or a push made -- how GitHub's state came to be known -- else
    None. The reflog line is `<old> <new> <who> <unix time> <zone>\t<message>`."""
    out = _git_ok(root, ["rev-parse", "--git-path", f"logs/{ref}"])
    if not out:
        return None
    path = Path(out.strip())
    if not path.is_absolute():
        path = root / path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        head, _, message = line.partition("\t")
        parts = head.split()
        if len(parts) < 2:
            continue
        how = "push" if message.startswith("update by push") else (
            "fetch" if message.startswith(("fetch", "clone:", "pull")) else None)
        if how is None:
            continue
        try:
            return datetime.fromtimestamp(int(parts[-2])), how
        except (ValueError, OverflowError, OSError):
            continue
    return None


def _known_at(root: Path, remote: str, base_ref: str | None) -> tuple[datetime, str] | None:
    """When the remote's state was last learnt, and how (`fetch` or
    `push`): the newest of the last fetch (`FETCH_HEAD`) and the reflog
    entries a fetch, a clone or a push wrote for the compared branch or the
    remote's default branch. None: never -- remote-tracking refs from an
    unknown source are not presented as GitHub's state."""
    found: list[tuple[datetime, str]] = []
    fetched = _fetched_at(root)
    if fetched is not None:
        found.append((fetched, "fetch"))
    for ref in dict.fromkeys(r for r in (base_ref, f"refs/remotes/{remote}/HEAD") if r):
        entry = _reflog_last(root, ref)
        if entry is not None:
            found.append(entry)
    return max(found, key=lambda e: e[0]) if found else None


_KNOWN_WORDS = {"fetch": "获取", "push": "推送"}


def _has_remote_refs(root: Path, remote: str) -> bool:
    out = _git_ok(root, ["for-each-ref", "--count=1", "--format=%(refname)", f"refs/remotes/{remote}/"])
    return bool((out or "").strip())


def _date_text(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M")


def state(root: str | Path) -> dict[str, Any]:
    """The linked repository's state as of the last fetch (module
    docstring), from local refs only; `{"linked": False, "git": bool}` for
    a project that is not linked."""
    root = Path(root)
    if not _is_repo(root):
        return {"linked": False, "git": False}
    linked = _linked(root)
    if linked is None:
        return {"linked": False, "git": True}
    base_ref, base_kind = _base(root, linked)
    fetched = _fetched_at(root)
    sha = _ref_sha(root, base_ref) if base_ref else None
    ahead = behind = None
    if sha is not None and _ref_sha(root, "HEAD"):
        out = _git_ok(root, ["rev-list", "--left-right", "--count", f"HEAD...{sha}"])
        try:
            ahead, behind = (int(n) for n in (out or "").split())
        except ValueError:
            ahead = behind = None
    known = _known_at(root, linked.remote, base_ref)
    when = f"（{_date_text(known[0])} {_KNOWN_WORDS[known[1]]}）" if known else ""
    if sha is None:
        if fetched is None and not _has_remote_refs(root, linked.remote):
            message = NEVER_FETCHED
        else:
            message = "GitHub 上没有可以对比的分支" + when
    elif known is None:
        # Remote-tracking refs no fetch, clone or push of this repository
        # is known to have written: not presented as GitHub's state.
        message = NEVER_FETCHED
    elif ahead == 0 and behind == 0:
        message = IN_SYNC
    elif ahead is None:
        message = "本地还没有提交" + when
    else:
        message = f"本地领先 {ahead} 个提交，落后 {behind} 个" + when
    return {
        "linked": True,
        "git": True,
        **linked.payload(),
        "branch": _branch(root),
        "base": base_ref[len("refs/remotes/"):] if base_ref else None,
        "base_kind": base_kind,
        "commit": sha,
        "commit_url": _commit_url(linked, sha) if sha else None,
        "ahead": ahead,
        "behind": behind,
        "fetched_at": fetched.isoformat(timespec="seconds") if fetched else None,
        "known_at": known[0].isoformat(timespec="seconds") if known else None,
        "known_by": known[1] if known else None,
        "message": message,
    }


# -- fetching -------------------------------------------------------------------------


#: Every config key git applies, with the file's scope, NUL-separated
#: (`scope\0key\0...`): the repository's own are `local` (`.git/config` and
#: what it includes) and `worktree` (`.git/config.worktree`, which git
#: applies once `extensions.worktreeConfig` is set -- `--local` alone never
#: lists it).
OWN_CONFIG_ARGS = ["config", "--includes", "--list", "--show-scope", "--name-only", "-z"]
_NOT_OWN_SCOPES = {"system", "global", "command"}


def own_config_keys(out: str) -> set[str]:
    """The keys of `OWN_CONFIG_ARGS`'s output that come from the
    repository's own config (any scope but the researcher's system / global
    files and the command line), lower case."""
    fields = out.split("\0")
    keys: set[str] = set()
    for i in range(0, len(fields) - 1, 2):
        scope, key = fields[i].strip().lower(), fields[i + 1].strip().lower()
        if key and scope not in _NOT_OWN_SCOPES:
            keys.add(key)
    return keys


def _program_keys(root: Path) -> list[str]:
    """The repository's OWN config keys that name a program for a fetch."""
    keys = own_config_keys(_git_ok(root, OWN_CONFIG_ARGS) or "")
    return sorted(k for k in keys if any(p.fullmatch(k) for p in _PROGRAM_KEYS))


def _run_fetch(root: Path, remote: str) -> None:
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",  # never a password prompt
        "GCM_INTERACTIVE": "never",
    }
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    args = [
        "git", *git_ingest.HARDENING, *_FETCH_HARDENING, "-C", str(root),
        "fetch", "--no-recurse-submodules", "--no-auto-gc", "--", remote,
    ]
    try:
        result = subprocess.run(
            args, capture_output=True, encoding="utf-8", errors="replace", env=env,
            timeout=FETCH_TIMEOUT_S, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise GitHubError("git_missing", str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubError("timeout", f"git fetch {remote} did not finish within {FETCH_TIMEOUT_S:.0f}s") from exc
    if result.returncode != 0:
        detail = scrub(f"git fetch {remote} failed: {result.stderr.strip()}")
        logger.warning("%s", detail)
        raise GitHubError("fetch_failed", detail)


def fetch(root: str | Path, project_id: str | None = None) -> dict[str, Any]:
    """`git fetch <remote>` for the linked remote (module docstring), under
    the project lock; returns the new `state`."""
    root = Path(root)
    if not _is_repo(root):
        raise GitHubError("not_linked", f"{root} is not a git repository")
    linked = _linked(root)
    if linked is None:
        raise GitHubError("not_linked", f"{root} has no remote on GitHub")
    unsafe = _program_keys(root)
    if unsafe:
        raise GitHubError(
            "unsafe_config",
            "the repository's own config names a program a fetch would run (" + ", ".join(unsafe) + "); not fetched",
        )
    try:
        with records_lock.project_lock(root, project_id, timeout=FETCH_LOCK_TIMEOUT_S):
            _run_fetch(root, linked.remote)
    except records_lock.ProjectLockTimeout as exc:
        raise GitHubError("busy", str(exc)) from exc
    return state(root)


# -- links ---------------------------------------------------------------------------


def _commit_url(linked: Remote, sha: str) -> str:
    assert _SHA_RE.fullmatch(sha)
    return f"{linked.url}/commit/{sha}"


def _blob_url(linked: Remote, sha: str, repo_path: str) -> str:
    assert _SHA_RE.fullmatch(sha)
    return f"{linked.url}/blob/{sha}/{urllib.parse.quote(repo_path, safe='/')}"


def _repo_path(root: Path, rel_path: str) -> str | None:
    """`rel_path` (relative to the project) as git names it from the
    repository's top, when git tracks it; None otherwise."""
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return None
    out = _git_ok(root, ["ls-files", "-z", "--full-name", "--", f":(literal){rel.as_posix()}"])
    names = [n for n in (out or "").split("\0") if n]
    prefix = (_git_ok(root, ["rev-parse", "--show-prefix"]) or "").strip()
    wanted = prefix + rel.as_posix()
    return wanted if wanted in names else None


def _blob_in(root: Path, sha: str, repo_path: str) -> str | None:
    """The blob id of `repo_path` in commit `sha`, or None."""
    out = _git_ok(root, ["ls-tree", "-z", "--full-tree", sha, "--", repo_path])
    for record in (out or "").split("\0"):
        meta, _, name = record.partition("\t")
        fields = meta.split()
        if name == repo_path and len(fields) == 3 and fields[1] == "blob":
            return fields[2]
    return None


def _differs(root: Path, rel_path: str, blob: str | None) -> bool:
    """Whether the file on disk differs from `blob` (None: there is none).
    No filter, no textconv, no diff program runs: git hashes the bytes
    (`hash-object --no-filters`). A file only in a cloud nobody can download
    is not read, and counts as not known to differ."""
    if blob is None:
        return True
    full = root / rel_path
    try:
        cloud.check_readable(full)
    except cloud.CloudOnlyError:
        return False
    out = _git_ok(root, ["hash-object", "--no-filters", "--", rel_path])
    return (out or "").strip() != blob


def link_for(root: str | Path, rel_path: str) -> dict[str, Any] | None:
    """A GitHub link for a project file, pinned to the newest commit GitHub
    has (`{url, commit, local_changes}`), or why there is none (`{reason,
    local_changes}`); None for a project that is not linked."""
    root = Path(root)
    if not _is_repo(root):
        return None
    linked = _linked(root)
    if linked is None:
        return None
    repo_path = _repo_path(root, rel_path)
    if repo_path is None:
        return {"reason": NOT_TRACKED, "message": REASON_MESSAGES[NOT_TRACKED], "local_changes": False}
    base_ref, _ = _base(root, linked)
    sha = _ref_sha(root, base_ref) if base_ref else None
    blob = _blob_in(root, sha, repo_path) if sha else None
    if blob is None:
        head = _ref_sha(root, "HEAD")
        head_blob = _blob_in(root, head, repo_path) if head else None
        return {
            "reason": NOT_PUSHED, "message": REASON_MESSAGES[NOT_PUSHED],
            "local_changes": _differs(root, rel_path, head_blob),
        }
    changes = _differs(root, rel_path, blob)
    return {
        "url": _blob_url(linked, sha, repo_path),
        "commit": sha,
        "local_changes": changes,
        "message": LOCAL_CHANGES_MESSAGE if changes else None,
    }


def link_for_commit(root: str | Path, sha: str) -> dict[str, str] | None:
    """`{url, commit}` for a commit GitHub has -- reachable from one of the
    linked remote's remote-tracking refs -- else None."""
    root = Path(root)
    if not isinstance(sha, str) or not _ABBREV_RE.fullmatch(sha) or not _is_repo(root):
        return None
    linked = _linked(root)
    if linked is None:
        return None
    full = _ref_sha(root, sha)
    if full is None:
        return None
    out = _git_ok(root, ["for-each-ref", "--count=1", "--format=%(refname)", f"--contains={full}", f"refs/remotes/{linked.remote}/"])
    if not (out or "").strip():
        return None
    return {"url": _commit_url(linked, full), "commit": full}
