"""Pushing a project to GitHub as a backup (DESIGN.md 11.3, task V7).

Pushing is the first thing RCE does that writes somewhere other than this
machine, so every push is shown before it happens and needs its own
confirmation; nothing is remembered as "always push".

`plan(root)` -- what a push would do, read from the repository and nothing
else: no network, nothing written (`GIT_OPTIONAL_LOCKS=0`, so not even
`.git/index` is refreshed). The branch; the commits the GitHub remote does
not have (reachable from HEAD, not from any of that remote's
remote-tracking refs); the uncommitted files, which a push does not carry;
whether the human records under `.rce/` are committed; and every refusal
of 11.3 (`blockers`), each with its Chinese sentence. A 100 MB check runs
over the objects the push would send (`rev-list --objects` +
`cat-file --batch-check`), before any network. `token` names what was
planned; `push` and `create_repo` refuse when the repository no longer
matches it.

A folder that is not a git repository is not turned into one (V7): the
plan says so and, inside an iCloud, OneDrive or other synced folder, why
(sync services and git's internal files do not mix), names the way
forward, and names the files over 100 MB that GitHub would refuse -- read
from the folder's listing (`lstat`), never their contents.

`commit_records(root)` -- 「先把人工记录提交一次」: commits exactly the paths
under `.rce/` that are changed or untracked (`git add` then
`git commit --only` of those paths), never any other file and never the
researcher's own staged changes; afterwards the index outside `.rce/` is
checked to be what it was, and the new commit to touch only `.rce/`.

`create_repo(root, name, token)` -- no remote at all: a PRIVATE repository
under the account `gh` is logged in to (`gh repo create <owner>/<name>
--private --source <root> --remote origin`). Never `--public`. `gh` is an
optional external program: absent, it is a stated state. Whether it is
logged in is asked (`gh auth status`, which talks to GitHub) only inside
this explicit action, never while planning.

`push(root, token)` -- re-plans under the project lock, refuses on any
blocker or a changed plan, and runs `git push <remote>
refs/heads/<branch>:refs/heads/<branch>` (with `--set-upstream` when the
branch has none). Never `--force`, never `--force-with-lease`, never
another branch, never tags (`push.followTags` off), no hooks, no
submodules, never a password prompt, with a timeout. git's outcome comes
back as a Chinese sentence plus git's own text (credentials scrubbed).

No credential, token or password is read, asked for or written: git's own
credential helper and gh's own login are used as they are.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable

from rce import cloud, github, paths
from rce.ingest import git as git_ingest
from rce.records import lock as records_lock

logger = logging.getLogger(__name__)

LIMIT_BYTES = 100 * 1024 * 1024  # GitHub refuses a file over 100 MiB
COMMITS_SHOWN = 50
LARGE_WALK_CAP = 200_000
PUSH_TIMEOUT_S = 300.0
GIT_TIMEOUT_S = 120.0
GH_TIMEOUT_S = 60.0
LOCK_TIMEOUT_S = 5.0
RECORDS_DIR = ".rce"
RECORDS_MESSAGE = "RCE：人工记录"
DEFAULT_REPO_NAME = "research-project"

LOGIN_COMMAND = "gh auth login"
STATUS_COMMAND = "gh auth status"

BLOCKER_MESSAGES = {
    "not_git": "这个文件夹还不是 git 仓库，RCE 不会把它变成仓库",
    "detached": "当前不在任何分支上（detached HEAD），请先在终端里切换到一个分支",
    "merge": "有一次合并（merge）还没有完成，请先在终端里完成或放弃它",
    "rebase": "有一次变基（rebase）还没有完成，请先在终端里完成或放弃它",
    "no_branch": "这个仓库还没有任何提交，没有可以推送的分支",
    "not_fast_forward": "GitHub 上有你本地没有的提交，请先在终端里处理",
    "too_large": "要推送的提交里有超过 100 MB 的文件，GitHub 会拒绝",
    "remote_not_github": "这个项目的远程仓库不在 GitHub 上，RCE 只推送到 GitHub",
    "remote_ambiguous": "这个项目有几个 GitHub 远程仓库，RCE 不替你选，请在终端里推送",
    "upstream_elsewhere": "当前分支跟踪的是另一个名字的分支，RCE 不替你选，请在终端里推送",
    "unsafe_config": "这个仓库自己的 git 设置指定了推送时要运行的程序，RCE 不会替它运行，请在终端里推送",
    "gh_missing": "这台电脑上没有 gh（GitHub 的命令行工具），RCE 用它创建仓库。请先安装 gh，然后在终端里运行：gh auth login",
    "gh_logged_out": "gh 还没有登录 GitHub，请在终端里运行：gh auth login",
    "gh_unknown": "没能确认 gh 是否已登录 GitHub，请在终端里运行 gh auth status 查看",
}

ERROR_MESSAGES = {
    **BLOCKER_MESSAGES,
    "blocked": "这次推送不能进行",
    "changed": "项目在你确认之后有了变化，请重新检查后再推送",
    "no_remote": "这个项目还没有 GitHub 仓库，请先创建",
    "has_remote": "这个项目已经有远程仓库，不需要再创建",
    "invalid_name": "仓库名只能用英文字母、数字和 . _ -，不超过 100 个字符",
    "name_taken": "GitHub 上已经有同名的仓库，请换一个名字",
    "create_failed": "没能在 GitHub 上创建仓库",
    "busy": "另一个 RCE 操作正在使用这个项目，请稍后再试",
    "timeout": "推送到 GitHub 超时了，请检查网络后再试",
    "rejected": "GitHub 上有你本地没有的提交，请先在终端里处理",
    "auth": "GitHub 拒绝了这次登录，请在终端里检查 git 或 gh 的登录（gh auth login）",
    "push_failed": "没能推送到 GitHub",
    "commit_failed": "没能提交人工记录",
    "identity": "git 还不知道你的名字和邮箱，请先在终端里设置 git config user.name 和 user.email",
    "records_check": "提交人工记录后检查不一致，请在终端里查看 git status",
    "git_missing": "这台电脑上找不到 git",
}

# Beyond `git_ingest.HARDENING`, for every git command here: no hook, no
# signature program, no automatic maintenance.
_HARDENING = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "log.showSignature=false",
    "-c", "commit.gpgSign=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
)

# Keys of a repository's OWN config that name a program `git add` / `git
# commit` would run: committing the records refuses such a repository.
_COMMIT_PROGRAM_KEYS = (
    re.compile(r"filter\..*\.(clean|smudge|process)"),
    re.compile(r"gpg\.program"),
    re.compile(r"gpg\..*\.program"),
)

_GH_TOKEN = re.compile(r"\b(gh[pousr]_|github_pat_)[A-Za-z0-9_]+")
_REPO_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
_LOGIN_RE = re.compile(r"Logged in to github\.com (?:account|as) ([A-Za-z0-9-]{1,39})")


def scrub(text: str) -> str:
    """`github.scrub` plus any GitHub token pattern."""
    return _GH_TOKEN.sub("***", github.scrub(text or ""))


class PushError(Exception):
    def __init__(self, code: str, detail: str = "", *, message: str | None = None, extra: dict | None = None) -> None:
        detail = scrub(detail)
        super().__init__(detail or code)
        self.code = code
        self.message = message or ERROR_MESSAGES[code]
        self.detail = detail or self.message
        self.extra = extra or {}


# -- running git and gh ------------------------------------------------------------


def _git_env() -> dict[str, str]:
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",  # never a password prompt
        "GCM_INTERACTIVE": "never",
    }
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    return env


# Every git command line this module ran, in order: argv lists (tests read
# it to prove no force flag ever appears). Bounded.
RECORDED: list[list[str]] = []


def _run(root: Path, args: list[str], *, input: str | None = None, timeout: float = GIT_TIMEOUT_S,
         extra_config: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    argv = ["git", *git_ingest.HARDENING, *_HARDENING, *extra_config, "-C", str(root), *args]
    RECORDED.append(argv)
    del RECORDED[:-500]
    try:
        return subprocess.run(
            argv, capture_output=True, encoding="utf-8", errors="surrogateescape", env=_git_env(),
            timeout=timeout, input=input, stdin=None if input is not None else subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise PushError("git_missing", str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise PushError("timeout", f"git {args[0]} did not finish within {timeout:.0f}s") from exc


def _out(root: Path, args: list[str], **kw) -> str | None:
    """stdout, or None when git exits non-zero."""
    result = _run(root, args, **kw)
    return result.stdout if result.returncode == 0 else None


def _gh_default() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


# Where `gh` is (None: not installed). Tests replace it with a test double.
GH_LOOKUP: Callable[[], str | None] = _gh_default


def _gh_env() -> dict[str, str]:
    return {
        **_git_env(),
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_SPINNER_DISABLED": "1",
        "NO_COLOR": "1",
    }


def _gh(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    program = GH_LOOKUP()
    if not program:
        raise PushError("gh_missing", "gh is not installed")
    try:
        return subprocess.run(
            [program, *args], capture_output=True, encoding="utf-8", errors="replace", env=_gh_env(),
            timeout=GH_TIMEOUT_S, stdin=subprocess.DEVNULL, cwd=str(cwd),
        )
    except FileNotFoundError as exc:
        raise PushError("gh_missing", str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise PushError("timeout", f"gh {' '.join(args[:2])} did not finish within {GH_TIMEOUT_S:.0f}s",
                        message="gh 没有及时回应，请检查网络后再试") from exc


def gh_login() -> dict[str, Any]:
    """`{available, logged_in, account}` from `gh auth status` -- which talks
    to GitHub, so it is asked only inside an explicit action."""
    if not GH_LOOKUP():
        return {"available": False, "logged_in": False, "account": None}
    result = _gh(["auth", "status", "--hostname", "github.com"], cwd=Path.home())
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    accounts = _LOGIN_RE.findall(text)
    account = None
    if len(set(accounts)) == 1:
        account = accounts[0]
    elif accounts:
        active = re.search(r"Logged in to github\.com account ([A-Za-z0-9-]{1,39})[^\n]*\n(?:[^\n]*\n){0,3}?[^\n]*Active account: true", text)
        account = active.group(1) if active else None
    if result.returncode == 0:
        return {"available": True, "logged_in": True, "account": account}
    if "not logged" in text.lower():
        return {"available": True, "logged_in": False, "account": None, "detail": scrub(text.strip())}
    return {"available": True, "logged_in": None, "account": None, "detail": scrub(text.strip())}


# -- reading the repository ----------------------------------------------------------


def _is_repo(root: Path) -> bool:
    out = _out(root, ["rev-parse", "--is-inside-work-tree"])
    return (out or "").strip() == "true"


def _git_path_exists(root: Path, name: str) -> bool:
    out = _out(root, ["rev-parse", "--git-path", name])
    if not out:
        return False
    path = Path(out.strip())
    if not path.is_absolute():
        path = root / path
    return path.exists()


def _config(root: Path, key: str) -> str | None:
    out = _out(root, ["config", "--get", key])
    return out.strip() if out else None


def _prefix(root: Path) -> str:
    return (_out(root, ["rev-parse", "--show-prefix"]) or "").strip()


def _status(root: Path) -> list[str]:
    """Every changed or untracked path (repository-relative), from `git
    status --porcelain -z` (no index refresh written)."""
    out = _out(root, ["status", "--porcelain", "-z", "--untracked-files=all"]) or ""
    entries = out.split("\0")
    found: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        found.append(path)
        if "R" in xy or "C" in xy:
            i += 1  # the rename's source path follows
    return found


def _records_prefix(root: Path) -> str:
    return _prefix(root) + RECORDS_DIR + "/"


def _record_changes(root: Path) -> tuple[list[str], list[str]]:
    """(changed or untracked paths under the project's `.rce/`, every other
    changed path) -- repository-relative."""
    rec = _records_prefix(root)
    records, other = [], []
    for path in _status(root):
        (records if path.startswith(rec) else other).append(path)
    return sorted(set(records)), sorted(set(other))


def _records_state(root: Path, changed: list[str]) -> dict[str, Any]:
    tracked = _out(root, ["ls-files", "-z", "--", f":(literal){RECORDS_DIR}"]) or ""
    ignored = _out(root, ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", f":(literal){RECORDS_DIR}"]) or ""
    has_tracked = any(tracked.split("\0"))
    has_ignored = any(ignored.split("\0"))
    if changed:
        message = f"人工记录（.rce/）有 {len(changed)} 个文件还没有提交"
    elif has_ignored and not has_tracked:
        message = "人工记录（.rce/）被 git 忽略了，推送不包括它们"
    elif has_tracked:
        message = "人工记录（.rce/）都已提交"
    else:
        message = "这个项目还没有人工记录"
    return {
        "committed": not changed and has_tracked,
        "changed": changed,
        "ignored": has_ignored and not has_tracked,
        "message": message,
    }


def _remotes(root: Path) -> dict[str, dict[str, list[str]]]:
    """{remote: {"url": [...], "pushurl": [...]}}"""
    out = _out(root, ["config", "-z", "--get-regexp", r"^remote\..*\.(url|pushurl)$"]) or ""
    found: dict[str, dict[str, list[str]]] = {}
    for record in out.split("\0"):
        if not record:
            continue
        key, _, value = record.partition("\n")
        name, _, kind = key[len("remote."):].rpartition(".")
        found.setdefault(name, {"url": [], "pushurl": []})[kind].append(value)
    return found


def _remote_program_keys(root: Path, keys=None) -> list[str]:
    out = _out(root, ["config", "--local", "--includes", "--name-only", "-z", "--list"]) or ""
    patterns = keys if keys is not None else github._PROGRAM_KEYS
    found = {k.strip().lower() for k in out.split("\0") if k.strip()}
    return sorted(k for k in found if any(p.fullmatch(k) for p in patterns))


def _ref_sha(root: Path, ref: str) -> str | None:
    out = _out(root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    sha = (out or "").strip()
    return sha if github._SHA_RE.fullmatch(sha) else None


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    return _run(root, ["merge-base", "--is-ancestor", older, newer]).returncode == 0


def _range(remote: str | None) -> list[str]:
    return ["HEAD", "--not", f"--remotes={remote}"] if remote else ["HEAD"]


def _unpushed(root: Path, remote: str | None) -> tuple[int, list[dict[str, str]]]:
    count_out = _out(root, ["rev-list", "--count", *_range(remote)]) or "0"
    try:
        count = int(count_out.strip())
    except ValueError:
        count = 0
    out = _out(root, ["log", "--no-show-signature", "-z", f"-n{COMMITS_SHOWN}", "--format=%H%x1f%B", *_range(remote)]) or ""
    commits = []
    for record in out.split("\0"):
        sha, sep, body = record.partition("\x1f")
        sha = sha.strip()
        if not sep or not github._SHA_RE.fullmatch(sha):
            continue
        first = body.strip().split("\n", 1)[0].strip()
        commits.append({"sha": sha, "short": sha[:7], "subject": first})
    return count, commits


def _size_text(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MB"


def _large_objects(root: Path, remote: str | None) -> list[dict[str, Any]]:
    """Files over 100 MB among the objects the push would send."""
    listed = _out(root, ["rev-list", "--objects", *_range(remote)]) or ""
    lines = [line for line in listed.split("\n") if " " in line]
    if not lines:
        return []
    out = _out(root, ["cat-file", "--batch-check=%(objecttype) %(objectsize) %(rest)"], input="\n".join(lines) + "\n") or ""
    found: dict[str, int] = {}
    for line in out.split("\n"):
        parts = line.split(" ", 2)
        if len(parts) != 3 or parts[0] != "blob":
            continue
        try:
            size = int(parts[1])
        except ValueError:
            continue
        if size > LIMIT_BYTES:
            found[parts[2]] = max(size, found.get(parts[2], 0))
    return [{"path": p, "size": s, "size_text": _size_text(s)} for p, s in sorted(found.items())]


# -- a folder that is not a git repository ---------------------------------------------


def _canonical_if_exists(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
    except OSError:
        return None
    return paths._canonical_path(path)


def _within(child: str, parent: str | None) -> bool:
    return parent is not None and Path(child).is_relative_to(Path(parent))


def synced_service(root: str | Path) -> str | None:
    """The sync service whose folder holds `root` ("iCloud", "OneDrive",
    ...), or None. iCloud: under `~/Library/Mobile Documents`, or under
    `~/Documents` / `~/Desktop` when iCloud Drive holds a folder of that
    name (macOS's "Desktop & Documents Folders")."""
    prov = cloud.provider(root)
    if prov is not None:
        return prov.name
    canonical = _canonical_if_exists(Path(root)) or str(Path(root))
    home = Path.home()
    mobile = home / "Library" / "Mobile Documents"
    if _within(canonical, _canonical_if_exists(mobile)):
        return "iCloud"
    for name in ("Documents", "Desktop"):
        if _within(canonical, _canonical_if_exists(home / name)):
            if os.path.lexists(mobile / "com~apple~CloudDocs" / name):
                return "iCloud"
    return None


def large_files_in_folder(root: Path) -> tuple[list[dict[str, Any]], bool]:
    """Files over 100 MB in the folder, from its listing (`lstat`; no file
    is opened, none downloaded). (found, truncated)."""
    found: list[dict[str, Any]] = []
    seen = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=lambda e: None):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
        for name in filenames:
            seen += 1
            if seen > LARGE_WALK_CAP:
                truncated = True
                break
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if st.st_size > LIMIT_BYTES and not os.path.islink(full):
                rel = os.path.relpath(full, root)
                found.append({"path": rel, "size": st.st_size, "size_text": _size_text(st.st_size)})
        if truncated:
            break
    found.sort(key=lambda f: f["path"])
    return found, truncated


def _not_git(root: Path) -> dict[str, Any]:
    service = synced_service(root)
    large, truncated = large_files_in_folder(root)
    if service:
        why = (
            f"这个文件夹在 {service} 同步的文件夹里。同步服务和 git 的内部文件合不来，"
            "放在这里的仓库可能被同步弄坏"
        )
        forward = "把项目移到一个不同步的文件夹（RCE 会认出移动后的项目），然后在那里运行 git init"
    else:
        why = None
        forward = "在终端里进入这个文件夹运行 git init 并做第一次提交，然后再来推送"
    large_message = None
    if large:
        names = "、".join(f"{f['path']}（{f['size_text']}）" for f in large)
        large_message = f"GitHub 不接受超过 100 MB 的文件，这个文件夹里有 {len(large)} 个：{names}"
    return {
        "message": BLOCKER_MESSAGES["not_git"],
        "synced": service,
        "sync_message": why,
        "way_forward": forward,
        "large_files": large,
        "large_truncated": truncated,
        "large_message": large_message,
    }


# -- the plan ----------------------------------------------------------------------


def suggested_name(root: Path) -> str:
    """The folder's name, ASCII only (11.3)."""
    text = unicodedata.normalize("NFKD", Path(root).name)
    text = "".join(
        c if (c.isascii() and (c.isalnum() or c in "._-")) else "-"
        for c in text if not unicodedata.combining(c)
    )
    text = re.sub(r"-{2,}", "-", text).strip("-._")[:100].strip("-._")
    if text.lower().endswith(".git"):
        text = text[:-4].strip("-._")
    return text or DEFAULT_REPO_NAME


def valid_repo_name(name: Any) -> bool:
    return (
        isinstance(name, str) and bool(_REPO_NAME_RE.fullmatch(name)) and name not in (".", "..")
        and not name.lower().endswith(".git") and not name.startswith(".")
    )


def _blocker(code: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": BLOCKER_MESSAGES[code], **extra}


def _token(fields: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]


def plan(root: str | Path) -> dict[str, Any]:
    """What a push would do (module docstring). Reads only; no network."""
    root = Path(root)
    if not _is_repo(root):
        not_git = _not_git(root)
        return {
            "git": False, "root": str(root), "not_git": not_git,
            "blockers": [_blocker("not_git")], "can_push": False, "can_create_repo": False,
            "needs_repo": False, "token": _token({"root": str(root), "git": False}),
        }
    blockers: list[dict[str, Any]] = []
    head_ref = (_out(root, ["symbolic-ref", "-q", "HEAD"]) or "").strip()
    branch = head_ref[len("refs/heads/"):] if head_ref.startswith("refs/heads/") else None
    head = _ref_sha(root, "HEAD")
    if _git_path_exists(root, "MERGE_HEAD"):
        blockers.append(_blocker("merge"))
    if _git_path_exists(root, "rebase-merge") or _git_path_exists(root, "rebase-apply"):
        blockers.append(_blocker("rebase"))
    if branch is None:
        blockers.append(_blocker("detached"))
    elif head is None:
        blockers.append(_blocker("no_branch"))

    remotes = _remotes(root)
    linked = github._linked(root)
    remote = None
    remote_is_github = False
    if remotes and linked is None:
        github_ones = [n for n, v in remotes.items() if len(v["url"]) == 1 and github.parse_url(v["url"][0])]
        blockers.append(_blocker("remote_ambiguous" if len(github_ones) > 1 else "remote_not_github"))
    elif linked is not None:
        pushurls = remotes.get(linked.remote, {}).get("pushurl", [])
        if any(github.parse_url(u) != (linked.owner, linked.repo) for u in pushurls):
            blockers.append(_blocker("remote_not_github"))
        else:
            remote_is_github = True
            remote = linked.payload()
        if _remote_program_keys(root):
            blockers.append(_blocker("unsafe_config"))
    needs_repo = not remotes

    remote_name = remote["remote"] if remote else None
    upstream = None
    set_upstream = False
    target = None
    target_sha = None
    if branch is not None and remote_name is not None:
        target = f"{remote_name}/{branch}"
        target_sha = _ref_sha(root, f"refs/remotes/{remote_name}/{branch}")
        up_remote = _config(root, f"branch.{branch}.remote")
        up_merge = _config(root, f"branch.{branch}.merge")
        if up_remote is None:
            set_upstream = True
        elif up_remote == remote_name:
            upstream = f"{remote_name}/{up_merge[len('refs/heads/'):]}" if (up_merge or "").startswith("refs/heads/") else None
            if up_merge != f"refs/heads/{branch}":
                blockers.append(_blocker("upstream_elsewhere"))
        if head is not None and target_sha is not None and not _is_ancestor(root, target_sha, head):
            blockers.append(_blocker("not_fast_forward"))

    count, commits = (0, [])
    large: list[dict[str, Any]] = []
    if head is not None:
        count, commits = _unpushed(root, remote_name)
        large = _large_objects(root, remote_name)
    if large:
        names = "、".join(f"{f['path']}（{f['size_text']}）" for f in large)
        blockers.append(_blocker("too_large", files=large, message=BLOCKER_MESSAGES["too_large"] + "：" + names))

    record_paths, other = _record_changes(root)
    records = _records_state(root, record_paths)
    uncommitted = len(other)
    nothing_to_push = count == 0 and not set_upstream and not needs_repo
    gh = None
    if needs_repo:
        available = bool(GH_LOOKUP())
        gh = {
            "available": available, "logged_in": None, "command": LOGIN_COMMAND,
            "message": None if available else BLOCKER_MESSAGES["gh_missing"],
        }
        if not available:
            blockers.append(_blocker("gh_missing", command=LOGIN_COMMAND))
    fields = {
        "root": str(root), "branch": branch, "head": head, "remote": remote, "target_sha": target_sha,
        "set_upstream": set_upstream, "needs_repo": needs_repo,
    }
    return {
        "git": True,
        "root": str(root),
        "branch": branch,
        "head": head,
        "remote": remote,
        "has_remote": bool(remotes),
        "remote_is_github": remote_is_github,
        "needs_repo": needs_repo,
        "suggested_name": suggested_name(root) if needs_repo else None,
        "upstream": upstream,
        "set_upstream": set_upstream,
        "target": target,
        "commit_count": count,
        "commits": commits,
        "commits_truncated": count > len(commits),
        "nothing_to_push": nothing_to_push,
        "uncommitted": uncommitted,
        "uncommitted_message": f"另有 {uncommitted} 个文件的改动还没有提交，推送不包括它们" if uncommitted else None,
        "records": records,
        "large_files": large,
        "gh": gh,
        "blockers": blockers,
        "can_push": not blockers and not needs_repo and not nothing_to_push,
        "can_create_repo": needs_repo and not blockers,
        "not_git": None,
        "token": _token(fields),
    }


# -- committing the records -------------------------------------------------------------


def _index_outside(root: Path, rec: str) -> list[str]:
    out = _out(root, ["ls-files", "-s", "-z", "--full-name", "--", ":/"]) or ""
    return [e for e in out.split("\0") if e and not e.split("\t", 1)[-1].startswith(rec)]


def _commit_records_locked(root: Path) -> dict[str, Any]:
    head_ref = (_out(root, ["symbolic-ref", "-q", "HEAD"]) or "").strip()
    if not head_ref.startswith("refs/heads/"):
        raise PushError("detached")
    if _git_path_exists(root, "MERGE_HEAD"):
        raise PushError("merge")
    if _git_path_exists(root, "rebase-merge") or _git_path_exists(root, "rebase-apply"):
        raise PushError("rebase")
    unsafe = _remote_program_keys(root, _COMMIT_PROGRAM_KEYS)
    if unsafe:
        raise PushError(
            "unsafe_config", "the repository's own config names a program a commit would run (" + ", ".join(unsafe) + ")",
            message="这个仓库自己的 git 设置指定了提交时要运行的程序，RCE 不会替它运行，请在终端里提交",
        )
    rec = _records_prefix(root)
    changed, _ = _record_changes(root)
    if not changed:
        return {"committed": False, "files": [], "message": "人工记录都已提交，没有要提交的"}
    prefix = _prefix(root)
    specs = [f":(top,literal){p}" for p in changed]
    before = _index_outside(root, rec)
    old_head = _ref_sha(root, "HEAD")
    added = _run(root, ["add", "--all", "--", *specs])
    if added.returncode != 0:
        raise PushError("commit_failed", f"git add failed: {added.stderr.strip()}")
    committed = _run(root, ["commit", "-q", "--no-verify", "--no-gpg-sign", "--only", "-m", RECORDS_MESSAGE, "--", *specs])
    if committed.returncode != 0:
        text = committed.stderr.strip() or committed.stdout.strip()
        code = "identity" if ("tell me who you are" in text or "user.email" in text) else "commit_failed"
        raise PushError(code, f"git commit failed: {text}")
    after = _index_outside(root, rec)
    new_head = _ref_sha(root, "HEAD")
    touched = (_out(root, ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", new_head or "HEAD"]) or "").split("\0")
    touched = [t for t in touched if t]
    parent_ok = old_head is None or _ref_sha(root, "HEAD~1") == old_head
    if before != after or not parent_ok or any(not t.startswith(rec) for t in touched):
        raise PushError("records_check", "after committing the records, the index outside .rce/ or the commit's files are not as expected")
    shown = [p[len(prefix):] if p.startswith(prefix) else p for p in changed]
    return {"committed": True, "commit": new_head, "files": shown, "message": f"已提交 {len(changed)} 个人工记录文件"}


def _locked(root: Path, project_id: str | None):
    pid = project_id
    if pid is None:
        try:
            pid = paths._project_id_of(root)
        except paths.IdentityUnavailableError:
            pid = None  # locked by path, as before an id exists
    return records_lock.project_lock(root, pid, timeout=LOCK_TIMEOUT_S)


def commit_records(root: str | Path, project_id: str | None = None) -> dict[str, Any]:
    """「先把人工记录提交一次」 (module docstring), under the project lock."""
    root = Path(root)
    if not _is_repo(root):
        raise PushError("not_git")
    try:
        with _locked(root, project_id):
            result = _commit_records_locked(root)
    except records_lock.ProjectLockTimeout as exc:
        raise PushError("busy", str(exc)) from exc
    return {**result, "plan": plan(root)}


# -- creating the repository --------------------------------------------------------------


def create_repo(root: str | Path, name: Any, token: Any, project_id: str | None = None) -> dict[str, Any]:
    """A private repository for a project with no remote (module docstring)."""
    root = Path(root)
    if not valid_repo_name(name):
        raise PushError("invalid_name", f"not a repository name: {name!r}")
    try:
        with _locked(root, project_id):
            current = plan(root)
            if not current["git"]:
                raise PushError("not_git")
            if current["token"] != token:
                raise PushError("changed", "the repository changed since the plan was shown")
            if not current["needs_repo"]:
                raise PushError("has_remote")
            if current["blockers"]:
                first = current["blockers"][0]
                raise PushError("blocked", first["message"], message=first["message"], extra={"blockers": current["blockers"]})
            login = gh_login()
            if not login["available"]:
                raise PushError("gh_missing", extra={"command": LOGIN_COMMAND})
            if login["logged_in"] is False:
                raise PushError("gh_logged_out", login.get("detail", ""), extra={"command": LOGIN_COMMAND})
            if login["logged_in"] is None:
                raise PushError("gh_unknown", login.get("detail", ""), extra={"command": STATUS_COMMAND})
            full = f"{login['account']}/{name}" if login["account"] else name
            argv = ["repo", "create", full, "--private", "--source", str(root), "--remote", "origin"]
            result = _gh(argv, cwd=root)
            if result.returncode != 0:
                text = scrub((result.stderr or "") + (result.stdout or "")).strip()
                code = "name_taken" if "already exists" in text.lower() else "create_failed"
                raise PushError(code, f"gh repo create failed: {text}")
            linked = github._linked(root)
            if linked is None:
                raise PushError("create_failed", "gh repo create finished, but no GitHub remote 'origin' is configured")
    except records_lock.ProjectLockTimeout as exc:
        raise PushError("busy", str(exc)) from exc
    return {
        "created": True,
        "repo": linked.payload(),
        "message": f"已在 GitHub 上创建私有仓库 {linked.owner}/{linked.repo}",
        "plan": plan(root),
    }


# -- pushing ---------------------------------------------------------------------------


def _push_args(remote: str, branch: str, set_upstream: bool) -> list[str]:
    ref = f"refs/heads/{branch}"
    return [
        "push", "--porcelain", "--no-verify", "--no-signed", "--recurse-submodules=no",
        *(["--set-upstream"] if set_upstream else []),
        remote, f"{ref}:{ref}",
    ]


def _run_push(root: Path, remote: str, branch: str, set_upstream: bool) -> str:
    extra = (
        "-c", "push.followTags=false",
        "-c", "push.recurseSubmodules=no",
        "-c", "push.gpgSign=false",
        "-c", f"remote.{remote}.mirror=false",
    )
    result = _run(root, _push_args(remote, branch, set_upstream), timeout=PUSH_TIMEOUT_S, extra_config=extra)
    text = scrub(((result.stdout or "") + "\n" + (result.stderr or "")).strip())
    if result.returncode == 0:
        return text
    lower = text.lower()
    if "[rejected]" in lower or "non-fast-forward" in lower or "fetch first" in lower:
        code = "rejected"
    elif "gh001" in lower or "large files detected" in lower or "exceeds github's file size limit" in lower:
        code = "too_large"
    elif any(s in lower for s in ("authentication failed", "permission denied", "could not read username",
                                   "terminal prompts disabled", "403", "access denied")):
        code = "auth"
    else:
        code = "push_failed"
    logger.warning("git push %s failed: %s", remote, text)
    raise PushError(code, f"git push {remote} {branch} failed: {text}")


def push(root: str | Path, token: Any, project_id: str | None = None, *, commit_records: bool = False) -> dict[str, Any]:
    """Re-plan, refuse on any blocker or a changed plan, then `git push`
    (module docstring). With `commit_records`, the records are committed
    first (the plan's 「先把人工记录提交一次」)."""
    root = Path(root)
    committed = None
    try:
        with _locked(root, project_id):
            current = plan(root)
            if not current["git"]:
                raise PushError("not_git", extra={"plan": current})
            if current["token"] != token:
                raise PushError("changed", "the repository changed since the plan was shown", extra={"plan": current})
            if current["needs_repo"]:
                raise PushError("no_remote", extra={"plan": current})
            if current["blockers"]:
                first = current["blockers"][0]
                raise PushError("blocked", first["message"], message=first["message"],
                                extra={"blockers": current["blockers"], "plan": current})
            if commit_records and current["records"]["changed"]:
                committed = _commit_records_locked(root)
                current = plan(root)
                if current["blockers"]:
                    first = current["blockers"][0]
                    raise PushError("blocked", first["message"], message=first["message"],
                                    extra={"blockers": current["blockers"], "plan": current})
            if current["nothing_to_push"]:
                return {
                    "pushed": False, "records": committed, "message": "GitHub 已经有全部提交，没有要推送的",
                    "detail": "", "state": github.state(root), "plan": current,
                }
            remote = current["remote"]["remote"]
            branch = current["branch"]
            count = current["commit_count"]
            text = _run_push(root, remote, branch, current["set_upstream"])
    except records_lock.ProjectLockTimeout as exc:
        raise PushError("busy", str(exc)) from exc
    return {
        "pushed": True,
        "records": committed,
        "count": count,
        "message": f"已推送到 GitHub：{count} 个提交（{remote}/{branch}）",
        "detail": text,
        "state": github.state(root),
        "plan": plan(root),
    }
