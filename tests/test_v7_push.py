"""DESIGN.md Section 11 (task V7), phase B: pushing a project to GitHub as
a backup (11.3). Each test names the 11.5 scenario it covers (#3 push,
#4 not a git repository, #7 origin and secrets).

No test touches the network and nothing is pushed to github.com. The
GitHub remote is a local bare repository (`stand_in`, from the phase A
tests): a throwaway GLOBAL git config rewrites `https://github.com/...`
to the bare repository's path and forbids the https, ssh and git
transports outright. `gh` is a test double on PATH that records its argv
and, for `repo create`, makes another bare repository and wires it the
same way. `git` itself is wrapped on PATH so that EVERY git command line
-- RCE's and the fixtures' -- is recorded, and the autouse check asserts
that none of them ever force-pushes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rce import cli, githubpush, paths
from rce.webapp import registry

from test_v6_add_project import _live  # noqa: F401
from test_v7_connections import GITHUB_URL, OWNER, REPO, _colleague_pushes, _git, linked, stand_in  # noqa: F401

REAL_GIT = shutil.which("git")
FORCE_FLAGS = ("--force", "-f", "--force-with-lease", "--force-if-includes", "--mirror", "--tags", "--all", "--delete", "-d", "--prune")


# -- recording every git and gh command line ------------------------------------------


@pytest.fixture(autouse=True)
def recorded(tmp_path, monkeypatch):
    """`git` wrapped on PATH (every argv logged), `gh` absent by default;
    afterwards: no git push ever carried a force flag, a `+` refspec, tags
    or another branch, and gh was never asked for a public repository."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "git-argv.log"
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"for a in \"$@\"; do printf '%s\\037' \"$a\" >> '{log}'; done\n"
        f"printf '\\036' >> '{log}'\n"
        f"exec '{REAL_GIT}' \"$@\"\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(githubpush, "GH_LOOKUP", lambda: None)
    state = {"log": log, "bin": bin_dir, "gh_log": tmp_path / "gh-argv.log"}
    yield state
    for argv in _git_argvs(log):
        if "push" not in argv:
            continue
        after = argv[argv.index("push") + 1:]
        assert not any(a in FORCE_FLAGS or a.startswith("--force") for a in after), argv
        assert not any(a.startswith("+") or a.startswith(":") for a in after), argv
    for argv in _gh_argvs(state["gh_log"]):
        assert "--public" not in argv and "--internal" not in argv


def _git_argvs(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [rec.split("\x1f")[:-1] for rec in log.read_text().split("\x1e") if rec]


def _gh_argvs(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


def _pushes(recorded) -> list[list[str]]:
    return [a for a in _git_argvs(recorded["log"]) if "push" in a and "--porcelain" in a]  # RCE's own pushes


GH_DOUBLE = r'''
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["GH_DOUBLE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
mode = os.environ.get("GH_DOUBLE_MODE", "logged_in")
if args[:2] == ["auth", "status"]:
    if mode == "logged_out":
        sys.stderr.write("You are not logged into any GitHub hosts. To log in, run: gh auth login\n")
        sys.exit(1)
    sys.stdout.write("github.com\n  ✓ Logged in to github.com account rce-test (keyring)\n  - Token: gho_************************************\n")
    sys.exit(0)
if args[:2] == ["repo", "create"]:
    full = args[2]
    owner, name = full.split("/", 1)
    src = args[args.index("--source") + 1]
    root = os.environ["GH_DOUBLE_ROOT"]
    bare = os.path.join(root, name + ".git")
    if os.path.exists(bare):
        sys.stderr.write("GraphQL: Name already exists on this account (createRepository)\n")
        sys.exit(1)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
    url = "https://github.com/%s/%s.git" % (owner, name)
    with open(os.environ["GIT_CONFIG_GLOBAL"], "a") as f:
        f.write('[url "%s"]\n\tinsteadOf = %s\n' % (bare, url))
    subprocess.run(["git", "-C", src, "remote", "add", "origin", url], check=True)
    sys.stdout.write("https://github.com/%s/%s\n" % (owner, name))
    sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def gh_double(tmp_path, monkeypatch, recorded):
    """`gh` replaced by a test double that records its argv."""
    gh = recorded["bin"] / "gh"
    gh.write_text(f"#!{sys.executable}\n" + GH_DOUBLE)
    gh.chmod(0o755)
    made = tmp_path / "gh-made"
    made.mkdir()
    monkeypatch.setenv("GH_DOUBLE_LOG", str(recorded["gh_log"]))
    monkeypatch.setenv("GH_DOUBLE_ROOT", str(made))
    monkeypatch.setattr(githubpush, "GH_LOOKUP", lambda: str(gh))
    return made


def _commit(root: Path, name: str, text: str, message: str) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(root, "add", "--", name)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _refs(bare: Path) -> str:
    return subprocess.run([REAL_GIT, "-C", str(bare), "for-each-ref"], capture_output=True, text=True, check=True).stdout


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """Every file under `root` with size, mtime and (small files) content hash."""
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = Path(dirpath) / name
            st = full.lstat()
            digest = hashlib.sha256(full.read_bytes()).hexdigest() if st.st_size < 1_000_000 else ""
            snap[str(full.relative_to(root))] = (st.st_size, st.st_mtime_ns, digest)
    return snap


# -- 11.5 scenario 3: the dialog's plan --------------------------------------------------


def test_plan_lists_exactly_the_unpushed_commits_and_the_uncommitted_files(stand_in, linked, recorded):
    """11.5 scenario 3: the dialog lists exactly the unpushed commits (their
    first lines) and the uncommitted files; planning writes nothing."""
    _commit(linked, "b.py", "b = 1\n", "Add b\n\nlonger body")
    _commit(linked, "数据/c.csv", "x\n1\n", "数据：加 c")
    (linked / "a.py").write_text("print('changed')\n")
    (linked / "untracked.txt").write_text("u\n")
    (linked / ".rce").mkdir()
    (linked / ".rce" / "judgements.toml").write_text("# records\n")
    before = _tree_snapshot(linked / ".git")
    plan = githubpush.plan(linked)
    assert _tree_snapshot(linked / ".git") == before  # nothing written, not even the index
    assert plan["branch"] == "main" and plan["remote"]["remote"] == "origin" and plan["target"] == "origin/main"
    assert [c["subject"] for c in plan["commits"]] == ["数据：加 c", "Add b"]
    assert plan["commit_count"] == 2 and not plan["commits_truncated"]
    assert plan["uncommitted"] == 2
    assert plan["uncommitted_message"] == "另有 2 个文件的改动还没有提交，推送不包括它们"
    assert plan["records"]["changed"] == [".rce/judgements.toml"] and not plan["records"]["committed"]
    assert plan["records"]["message"] == "人工记录（.rce/）有 1 个文件还没有提交"
    assert plan["blockers"] == [] and plan["can_push"] and not plan["set_upstream"] and not plan["needs_repo"]
    assert _pushes(recorded) == []  # planning never runs a push


def test_push_pushes_the_planned_commits_and_the_github_line_is_in_step(stand_in, linked, recorded):
    """11.5 scenario 3: 「推送」 sends exactly the planned branch to the
    stand-in; afterwards the GitHub state is 「与 GitHub 一致」."""
    head = _commit(linked, "b.py", "b = 1\n", "Add b")
    plan = githubpush.plan(linked)
    result = githubpush.push(linked, plan["token"])
    assert result["pushed"] and result["count"] == 1
    assert result["message"] == "已推送到 GitHub：1 个提交（origin/main）"
    assert f"{head} commit\trefs/heads/main" in _refs(stand_in)
    assert result["state"]["message"] == "与 GitHub 一致"
    [argv] = _pushes(recorded)
    assert argv[argv.index("push"):] == [
        "push", "--porcelain", "--no-verify", "--no-signed", "--recurse-submodules=no",
        "origin", "refs/heads/main:refs/heads/main",
    ]
    again = githubpush.plan(linked)
    assert again["nothing_to_push"] and not again["can_push"]
    nothing = githubpush.push(linked, again["token"])
    assert nothing["pushed"] is False and nothing["message"] == "GitHub 已经有全部提交，没有要推送的"
    assert len(_pushes(recorded)) == 1


def test_commit_records_commits_only_rce_files_with_other_changes_staged(stand_in, linked):
    """11.5 scenario 3: 「先把人工记录提交一次」 commits only `.rce/` files,
    with the message 「RCE：人工记录」, even with the researcher's own
    changes staged and unstaged -- those are left exactly as they were."""
    rce = linked / ".rce"
    rce.mkdir()
    (rce / "canvas.json").write_text("{}\n")
    _commit(linked, ".rce/old.toml", "old\n", "records once")
    (rce / "old.toml").unlink()  # a deleted record file
    (rce / "judgements.toml").write_text("# new\n")
    (rce / "backups").mkdir()
    (rce / "backups" / "b1.toml").write_text("b\n")
    (linked / "a.py").write_text("print('staged')\n")
    _git(linked, "add", "a.py")
    (linked / "a.py").write_text("print('staged then edited')\n")  # partly staged
    (linked / "数据 说明.md").write_text("# 未暂存的改动\n")
    (linked / "new.py").write_text("n = 1\n")
    _git(linked, "add", "new.py")
    staged_before = _git(linked, "diff", "--cached", "--", "a.py", "new.py")
    unstaged_before = _git(linked, "diff", "--", ".", ":!.rce")
    old_head = _git(linked, "rev-parse", "HEAD").strip()

    result = githubpush.commit_records(linked)
    assert result["committed"] and sorted(result["files"]) == sorted([
        ".rce/backups/b1.toml", ".rce/canvas.json", ".rce/judgements.toml", ".rce/old.toml",
    ])
    assert _git(linked, "log", "-1", "--format=%s").strip() == "RCE：人工记录"
    assert _git(linked, "rev-parse", "HEAD~1").strip() == old_head
    touched = _git(linked, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split()
    assert sorted(touched) == [".rce/backups/b1.toml", ".rce/canvas.json", ".rce/judgements.toml", ".rce/old.toml"]
    assert _git(linked, "diff", "--cached", "--", "a.py", "new.py") == staged_before
    assert _git(linked, "diff", "--", ".", ":!.rce") == unstaged_before and "未暂存的改动" in unstaged_before
    assert _git(linked, "diff", "--cached", "--name-only").split() == ["a.py", "new.py"]
    assert result["plan"]["records"]["committed"] and result["plan"]["records"]["changed"] == []
    again = githubpush.commit_records(linked)
    assert again["committed"] is False and again["message"] == "人工记录都已提交，没有要提交的"


def test_commit_records_in_a_project_below_the_repository_top(stand_in, tmp_path):
    """11.5 scenario 3: a project in a subfolder of its repository commits
    its own `.rce/` only, not a sibling's."""
    repo = tmp_path / "mono"
    project = repo / "paper"
    (project / ".rce").mkdir(parents=True)
    (repo / "other" / ".rce").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "README", "r\n", "first")
    (project / ".rce" / "judgements.toml").write_text("j\n")
    (repo / "other" / ".rce" / "judgements.toml").write_text("o\n")
    result = githubpush.commit_records(project)
    assert result["files"] == [".rce/judgements.toml"]
    assert _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split() == ["paper/.rce/judgements.toml"]
    assert "other/.rce/" in _git(repo, "status", "--porcelain", "--untracked-files=all")


def test_push_with_the_records_tick_commits_them_first_then_pushes(stand_in, linked):
    """11.5 scenario 3: the tick and 「推送」 together: the records commit
    is made (only `.rce/`) and pushed with the planned commits."""
    (linked / ".rce").mkdir()
    (linked / ".rce" / "judgements.toml").write_text("j\n")
    (linked / "a.py").write_text("print('staged')\n")
    _git(linked, "add", "a.py")
    plan = githubpush.plan(linked)
    assert plan["nothing_to_push"] and plan["records"]["changed"]
    result = githubpush.push(linked, plan["token"], commit_records=True)
    assert result["pushed"] and result["records"]["committed"]
    head = _git(linked, "rev-parse", "HEAD").strip()
    assert f"{head} commit\trefs/heads/main" in _refs(stand_in)
    assert _git(linked, "diff", "--cached", "--name-only").split() == ["a.py"]


# -- 11.5 scenario 3: no remote -> a private repository -------------------------------------


@pytest.fixture
def unlinked(tmp_path, stand_in) -> Path:
    work = tmp_path / "论文-rmb hysteresis"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _commit(work, "a.py", "a = 1\n", "first")
    return work


def test_no_remote_a_private_repository_is_requested_then_pushed(unlinked, gh_double, recorded):
    """11.5 scenario 3: no remote -> the plan offers a private repository
    named after the folder (ASCII only); creating it runs `gh repo create
    --private` (never --public), adds `origin`; then the push."""
    plan = githubpush.plan(unlinked)
    assert plan["needs_repo"] and plan["can_create_repo"] and not plan["can_push"]
    assert plan["suggested_name"] == "rmb-hysteresis"
    assert plan["gh"]["available"] and plan["gh"]["logged_in"] is None  # asked only when creating
    assert _gh_argvs(recorded["gh_log"]) == []  # planning never ran gh (gh auth status talks to GitHub)
    created = githubpush.create_repo(unlinked, "rmb-hysteresis", plan["token"])
    assert created["message"] == "已在 GitHub 上创建私有仓库 rce-test/rmb-hysteresis"
    calls = _gh_argvs(recorded["gh_log"])
    assert calls == [
        ["auth", "status", "--hostname", "github.com"],
        ["repo", "create", "rce-test/rmb-hysteresis", "--private", "--source", str(unlinked), "--remote", "origin"],
    ]
    after = created["plan"]
    assert after["remote"]["owner"] == "rce-test" and after["set_upstream"] and after["can_push"]
    assert [c["subject"] for c in after["commits"]] == ["first"]
    pushed = githubpush.push(unlinked, after["token"])
    assert pushed["pushed"]
    assert "refs/heads/main" in _refs(gh_double / "rmb-hysteresis.git")
    assert _git(unlinked, "rev-parse", "--abbrev-ref", "main@{upstream}").strip() == "origin/main"
    assert "--set-upstream" in _pushes(recorded)[-1]
    assert "gho_" not in json.dumps(created)


def test_create_repo_refused_without_gh_or_logged_out_names_the_command(unlinked, gh_double, monkeypatch, recorded):
    """11.5 scenario 3: gh missing or not logged in -> refused, the exact
    command shown, no repository requested, no remote added; a bad name
    is refused; a taken name is said."""
    monkeypatch.setattr(githubpush, "GH_LOOKUP", lambda: None)
    plan = githubpush.plan(unlinked)
    [blocker] = plan["blockers"]
    assert blocker["code"] == "gh_missing" and blocker["command"] == "gh auth login" and not plan["can_create_repo"]
    with pytest.raises(githubpush.PushError) as refused:
        githubpush.create_repo(unlinked, "demo", plan["token"])
    assert refused.value.code == "blocked" and "gh auth login" in refused.value.message

    gh = gh_double.parent / "bin" / "gh"
    monkeypatch.setattr(githubpush, "GH_LOOKUP", lambda: str(gh))
    monkeypatch.setenv("GH_DOUBLE_MODE", "logged_out")
    plan = githubpush.plan(unlinked)
    with pytest.raises(githubpush.PushError) as refused:
        githubpush.create_repo(unlinked, "demo", plan["token"])
    assert refused.value.code == "gh_logged_out" and refused.value.extra["command"] == "gh auth login"
    assert refused.value.message == "gh 还没有登录 GitHub，请在终端里运行：gh auth login"
    assert [c[:2] for c in _gh_argvs(recorded["gh_log"])] == [["auth", "status"]]
    assert _git(unlinked, "remote").strip() == ""

    for bad in ("", "a b", "名字", "../x", ".hidden", "x" * 101, "demo.git", None, 3):
        with pytest.raises(githubpush.PushError) as refused:
            githubpush.create_repo(unlinked, bad, plan["token"])
        assert refused.value.code == "invalid_name"

    monkeypatch.setenv("GH_DOUBLE_MODE", "logged_in")
    (gh_double / "taken.git").mkdir()
    with pytest.raises(githubpush.PushError) as refused:
        githubpush.create_repo(unlinked, "taken", plan["token"])
    assert refused.value.code == "name_taken" and refused.value.message == "GitHub 上已经有同名的仓库，请换一个名字"
    assert _pushes(recorded) == []


def test_suggested_names_are_ascii_and_valid():
    """11.3: the name defaults to the folder name, ASCII only, editable."""
    for folder, name in [
        ("rmb-hysteresis", "rmb-hysteresis"), ("默认安全锚_论文流水线", "research-project"),
        ("Thèse 2026", "These-2026"), ("my.repo.git", "my.repo"), ("--x--", "x"),
    ]:
        got = githubpush.suggested_name(Path("/tmp") / folder)
        assert got == name and githubpush.valid_repo_name(got)


# -- 11.5 scenario 3: every refusal of 11.3, nothing pushed ------------------------------------


def _refused(root: Path, code: str, bare: Path, recorded) -> dict:
    plan = githubpush.plan(root)
    assert code in [b["code"] for b in plan["blockers"]], plan["blockers"]
    assert not plan["can_push"]
    before = _refs(bare)
    with pytest.raises(githubpush.PushError) as refused:
        githubpush.push(root, plan["token"])
    assert refused.value.code == "blocked"
    assert code in [b["code"] for b in refused.value.extra["blockers"]]
    assert _refs(bare) == before and _pushes(recorded) == []
    return plan


def test_refused_detached_head(stand_in, linked, recorded):
    """11.5 scenario 3: detached HEAD -> refused, nothing pushed."""
    _commit(linked, "b.py", "b\n", "b")
    _git(linked, "checkout", "-q", "--detach")
    plan = _refused(linked, "detached", stand_in, recorded)
    assert plan["branch"] is None


def test_refused_merge_in_progress(stand_in, linked, recorded):
    """11.5 scenario 3: a merge in progress -> refused."""
    _git(linked, "checkout", "-q", "-b", "side")
    _commit(linked, "a.py", "side\n", "side")
    _git(linked, "checkout", "-q", "main")
    _commit(linked, "a.py", "main\n", "main")
    subprocess.run([REAL_GIT, "-C", str(linked), "merge", "side"], capture_output=True)
    assert (linked / ".git" / "MERGE_HEAD").exists()
    _refused(linked, "merge", stand_in, recorded)


def test_refused_rebase_in_progress(stand_in, linked, recorded):
    """11.5 scenario 3: a rebase in progress -> refused."""
    _git(linked, "checkout", "-q", "-b", "side")
    _commit(linked, "a.py", "side\n", "side")
    _git(linked, "checkout", "-q", "main")
    _commit(linked, "a.py", "main\n", "main")
    subprocess.run([REAL_GIT, "-C", str(linked), "rebase", "side"], capture_output=True)
    assert (linked / ".git" / "rebase-merge").exists() or (linked / ".git" / "rebase-apply").exists()
    _refused(linked, "rebase", stand_in, recorded)


def test_refused_no_branch_yet(tmp_path, stand_in, recorded):
    """11.5 scenario 3: no commit, so no branch to push -> refused."""
    work = tmp_path / "empty"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "remote", "add", "origin", GITHUB_URL)
    _refused(work, "no_branch", stand_in, recorded)


def test_refused_not_a_fast_forward_and_a_rejected_push_is_never_forced(tmp_path, stand_in, linked, recorded):
    """11.5 scenario 3: GitHub has commits the project lacks -> after a
    fetch the plan refuses (「GitHub 上有你本地没有的提交，请先在终端里处理」);
    before the fetch, git itself rejects the push and RCE says the same --
    never a force push."""
    _colleague_pushes(tmp_path, stand_in)
    _commit(linked, "mine.py", "m\n", "mine")
    plan = githubpush.plan(linked)  # not fetched yet: looks like a fast-forward
    assert plan["can_push"]
    with pytest.raises(githubpush.PushError) as refused:
        githubpush.push(linked, plan["token"])
    assert refused.value.code == "rejected" and refused.value.message == "GitHub 上有你本地没有的提交，请先在终端里处理"
    assert len(_pushes(recorded)) == 1
    _git(linked, "fetch", "-q", "origin")
    plan = githubpush.plan(linked)
    [blocker] = plan["blockers"]
    assert blocker == {"code": "not_fast_forward", "message": "GitHub 上有你本地没有的提交，请先在终端里处理"}
    with pytest.raises(githubpush.PushError):
        githubpush.push(linked, plan["token"])
    assert len(_pushes(recorded)) == 1


def test_refused_a_file_over_100_mb_before_any_network(stand_in, linked, recorded):
    """11.5 scenario 3: a 101 MB file in the commits to be pushed -> refused
    before any network, the file named."""
    big = linked / "data" / "大文件.dta"
    big.parent.mkdir()
    with open(big, "wb") as f:
        f.truncate(101 * 1024 * 1024)
    _git(linked, "add", "data")
    _git(linked, "commit", "-q", "-m", "big data")
    _commit(linked, "data/small.csv", "a\n", "small")
    plan = _refused(linked, "too_large", stand_in, recorded)
    [blocker] = plan["blockers"]
    assert blocker["files"] == [{"path": "data/大文件.dta", "size": 101 * 1024 * 1024, "size_text": "101 MB"}]
    assert blocker["message"] == "要推送的提交里有超过 100 MB 的文件，GitHub 会拒绝：data/大文件.dta（101 MB）"
    assert not any("fetch" in a or "ls-remote" in a for a in _git_argvs(recorded["log"]) if "-C" in a and str(linked) in a)


def test_refused_a_remote_not_on_github(tmp_path, stand_in, recorded):
    """11.5 scenario 3: the remote is not on GitHub -> refused; a push URL
    elsewhere is refused the same way."""
    work = tmp_path / "gitlab"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _commit(work, "a.py", "a\n", "a")
    _git(work, "remote", "add", "origin", "https://gitlab.com/rce-test/demo.git")
    plan = _refused(work, "remote_not_github", stand_in, recorded)
    assert not plan["needs_repo"] and not plan["can_create_repo"]
    _git(work, "remote", "set-url", "origin", GITHUB_URL)
    _git(work, "remote", "set-url", "--push", "origin", "https://gitlab.com/rce-test/demo.git")
    _refused(work, "remote_not_github", stand_in, recorded)


def test_refused_a_repository_naming_a_program_for_the_push(stand_in, linked, recorded):
    """11.3 / 10.9: a repository whose own config names a program for the
    push (a credential helper here) is refused."""
    _commit(linked, "b.py", "b\n", "b")
    _git(linked, "config", "credential.helper", "!touch /tmp/pwned-by-rce-test")
    _refused(linked, "unsafe_config", stand_in, recorded)


def test_refused_when_the_plan_changed_since_it_was_shown(stand_in, linked, recorded):
    """11.3: every push is shown before it happens -- a commit made after the
    dialog opened changes the plan, and the push is refused."""
    _commit(linked, "b.py", "b\n", "b")
    plan = githubpush.plan(linked)
    _commit(linked, "c.py", "c\n", "c")
    with pytest.raises(githubpush.PushError) as refused:
        githubpush.push(linked, plan["token"])
    assert refused.value.code == "changed" and refused.value.message == "项目在你确认之后有了变化，请重新检查后再推送"
    assert _pushes(recorded) == []


# -- 11.5 scenario 4: not a git repository ----------------------------------------------------


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _thesis(folder: Path) -> list[str]:
    """A thesis-like folder: drafts, and data files over 100 MB (sparse)."""
    (folder / "data").mkdir(parents=True)
    (folder / "论文.md").write_text("# 论文\n")
    names = []
    for i, size in enumerate((146, 172)):
        path = folder / "data" / f"panel_{i}.dta"
        with open(path, "wb") as f:
            f.truncate(size * 1024 * 1024)
        names.append(f"data/panel_{i}.dta")
    with open(folder / "data" / "small.dta", "wb") as f:
        f.truncate(99 * 1024 * 1024)
    return names


@pytest.mark.parametrize("where,service", [
    ("Library/CloudStorage/OneDrive-个人/论文项目", "OneDrive"),
    ("Documents/默认安全锚_论文流水线", "iCloud"),
    ("Library/Mobile Documents/com~apple~CloudDocs/论文", "iCloud"),
])
def test_not_a_git_repository_in_a_synced_folder_is_explained_and_nothing_written(tmp_path, monkeypatch, recorded, where, service):
    """11.5 scenario 4: not a git repository -> the dialog explains; inside a
    synced folder it says why (sync and git's internal files do not mix),
    names the way forward and the files over 100 MB; nothing is written."""
    home = _home(tmp_path, monkeypatch)
    (home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Documents").mkdir(parents=True)
    folder = home / where
    large = _thesis(folder)
    before = _tree_snapshot(home)
    plan = githubpush.plan(folder)
    assert plan["git"] is False and plan["blockers"] == [{"code": "not_git", "message": "这个文件夹还不是 git 仓库，RCE 不会把它变成仓库"}]
    ng = plan["not_git"]
    assert ng["synced"] == service
    assert ng["sync_message"] == f"这个文件夹在 {service} 同步的文件夹里。同步服务和 git 的内部文件合不来，放在这里的仓库可能被同步弄坏"
    assert ng["way_forward"] == "把项目移到一个不同步的文件夹（RCE 会认出移动后的项目），然后在那里运行 git init"
    assert [f["path"] for f in ng["large_files"]] == large
    assert ng["large_message"] == "GitHub 不接受超过 100 MB 的文件，这个文件夹里有 2 个：data/panel_0.dta（146 MB）、data/panel_1.dta（172 MB）"
    with pytest.raises(githubpush.PushError) as refused:
        githubpush.push(folder, plan["token"])
    assert refused.value.code == "not_git"
    with pytest.raises(githubpush.PushError):
        githubpush.commit_records(folder)
    assert _tree_snapshot(home) == before and not (folder / ".git").exists()
    assert _pushes(recorded) == [] and not any("init" in a for a in _git_argvs(recorded["log"]))


def test_not_a_git_repository_outside_a_synced_folder(tmp_path, monkeypatch):
    """11.5 scenario 4: outside a synced folder the dialog says so without
    the sync reason; `~/Documents` without iCloud's Documents is not synced."""
    home = _home(tmp_path, monkeypatch)
    folder = home / "Documents" / "plain"
    folder.mkdir(parents=True)
    plan = githubpush.plan(folder)
    ng = plan["not_git"]
    assert ng["synced"] is None and ng["sync_message"] is None and ng["large_files"] == []
    assert ng["way_forward"] == "在终端里进入这个文件夹运行 git init 并做第一次提交，然后再来推送"


# -- the server (11.3's endpoints) -----------------------------------------------------------

from test_v7_connections import linked_server  # noqa: E402,F401


def test_push_endpoints_plan_commit_records_and_push(stand_in, linked_server, recorded):
    """11.5 scenario 3 over HTTP: the plan (reads only), the records tick,
    and 「推送」 -- the only request that pushes."""
    live, work = linked_server
    _commit(work, "b.py", "b\n", "Add b")
    (work / "a.py").write_text("print('edited')\n")
    status, plan = live.get("/api/github/push-plan")
    assert status == 200 and [c["subject"] for c in plan["commits"]] == ["Add b"]
    assert plan["uncommitted"] == 1 and plan["records"]["changed"]  # init wrote .rce/
    assert _pushes(recorded) == []
    status, body = live.post("/api/github/push", {})
    assert status == 400 and _pushes(recorded) == []
    status, body = live.post("/api/github/push", {"token": "stale"})
    assert status == 409 and body["state"] == "github_changed"
    assert body["message_zh"] == "项目在你确认之后有了变化，请重新检查后再推送" and _pushes(recorded) == []
    status, done = live.post("/api/github/commit-records", {})
    assert status == 200 and done["committed"] and all(f.startswith(".rce/") for f in done["files"])
    assert _git(work, "log", "-1", "--format=%s").strip() == "RCE：人工记录"
    status, plan = live.get("/api/github/push-plan")
    assert plan["commit_count"] == 2 and plan["records"]["committed"]
    status, pushed = live.post("/api/github/push", {"token": plan["token"], "commit_records": True})
    assert status == 200 and pushed["pushed"] and pushed["state"]["message"] == "与 GitHub 一致"
    assert len(_pushes(recorded)) == 1


def test_push_endpoint_refusals_carry_the_sentence_and_the_blockers(tmp_path, stand_in, linked_server, recorded):
    """11.5 scenario 3 over HTTP: a refusal answers 409 with its sentence,
    the blockers, and nothing pushed."""
    live, work = linked_server
    _colleague_pushes(tmp_path, stand_in)
    _git(work, "fetch", "-q", "origin")
    _commit(work, "mine.py", "m\n", "mine")
    _, plan = live.get("/api/github/push-plan")
    status, body = live.post("/api/github/push", {"token": plan["token"]})
    assert status == 409 and body["state"] == "github_blocked"
    assert body["message_zh"] == "GitHub 上有你本地没有的提交，请先在终端里处理"
    assert [b["code"] for b in body["blockers"]] == ["not_fast_forward"]
    status, body = live.post("/api/github/create-repo", {"name": "x", "token": plan["token"]})
    assert status == 409 and body["state"] == "github_has_remote"
    assert _pushes(recorded) == []


@pytest.mark.parametrize("method,endpoint", [
    ("GET", "/api/github/push-plan"), ("POST", "/api/github/commit-records"),
    ("POST", "/api/github/create-repo"), ("POST", "/api/github/push"),
])
@pytest.mark.parametrize("headers", [{"Origin": "http://evil.example"}, {"Host": "evil.example:80"}])
def test_push_endpoints_refuse_cross_origin_and_write_nothing(stand_in, linked_server, recorded, method, endpoint, headers):
    """11.5 scenario 7: every new endpoint answers 403 cross-origin; nothing
    is committed, created or pushed."""
    live, work = linked_server
    _commit(work, "b.py", "b\n", "b")
    plan = githubpush.plan(work)
    head = _git(work, "rev-parse", "HEAD").strip()
    before = _refs(stand_in)
    sent = {"Host": f"127.0.0.1:{live.port}", "Content-Type": "application/json", **headers}
    status, _ = live.raw(method, endpoint, sent, {"token": plan["token"], "name": "x", "commit_records": True})
    assert status == 403
    assert _git(work, "rev-parse", "HEAD").strip() == head and _refs(stand_in) == before
    assert _pushes(recorded) == [] and _gh_argvs(recorded["gh_log"]) == []


# -- the command line ----------------------------------------------------------------------------


def test_cli_push_without_yes_prints_the_plan_and_writes_nothing(stand_in, linked, recorded, capsys):
    """11.5 scenario 3 (CLI): `rce github push` shows the plan and writes
    nothing; `--yes --commit-records` commits the records and pushes."""
    _commit(linked, "b.py", "b\n", "Add b")
    (linked / ".rce").mkdir()
    (linked / ".rce" / "judgements.toml").write_text("j\n")
    (linked / "a.py").write_text("edited\n")
    head = _git(linked, "rev-parse", "HEAD").strip()
    assert cli.main(["github", "push", str(linked), "--commit-records"]) == 0
    out = capsys.readouterr().out
    assert "Commits GitHub does not have yet: 1" in out and "Add b" in out
    assert "1 other file(s) have changes that are not committed" in out
    assert "Human records (.rce/): 1 file(s) not committed" in out and "Nothing written." in out
    assert _git(linked, "rev-parse", "HEAD").strip() == head and _pushes(recorded) == []
    assert cli.main(["github", "push", str(linked), "--commit-records", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Committed 1 record file(s)" in out and "Pushed 2 commit(s) to origin/main." in out
    assert cli.main(["github", "status", str(linked)]) == 0
    assert "GitHub: rce-test/demo" in capsys.readouterr().out
    assert cli.main(["github", "fetch", str(linked)]) == 0


def test_cli_push_creates_a_private_repository_only_when_told(unlinked, gh_double, recorded, capsys):
    """11.5 scenario 3 (CLI): no remote -> the plan suggests a name; only
    `--create-repo NAME --yes` creates a private repository, then pushes."""
    assert cli.main(["github", "push", str(unlinked), "--yes"]) == 1
    assert "--create-repo rmb-hysteresis" in capsys.readouterr().out
    assert _gh_argvs(recorded["gh_log"]) == []
    assert cli.main(["github", "push", str(unlinked), "--create-repo", "名字"]) == 1
    assert cli.main(["github", "push", str(unlinked), "--create-repo", "rmb-hysteresis"]) == 0
    assert _gh_argvs(recorded["gh_log"]) == [] and _git(unlinked, "remote").strip() == ""
    assert cli.main(["github", "push", str(unlinked), "--create-repo", "rmb-hysteresis", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Created the private repository rce-test/rmb-hysteresis" in out and "Pushed 1 commit(s) to origin/main." in out
    assert ["repo", "create", "rce-test/rmb-hysteresis", "--private", "--source", str(unlinked), "--remote", "origin"] in _gh_argvs(recorded["gh_log"])


def test_cli_push_of_a_folder_that_is_not_a_repository_explains(tmp_path, monkeypatch, capsys):
    """11.5 scenario 4 (CLI): explained; nothing written."""
    home = _home(tmp_path, monkeypatch)
    folder = home / "Library" / "CloudStorage" / "OneDrive-个人" / "论文"
    _thesis(folder)
    before = _tree_snapshot(home)
    assert cli.main(["github", "push", str(folder), "--yes"]) == 1
    out = capsys.readouterr().out
    assert "inside a folder OneDrive syncs" in out and "data/panel_1.dta (172 MB)" in out
    assert _tree_snapshot(home) == before


# -- the page (11.3) --------------------------------------------------------------------------------

from test_v6_page import NODE, _function, _src, needs_node  # noqa: E402

_PUSH_RUNNER = r"""
const block = require("fs").readFileSync(0, "utf8");
eval(block + "; global.W = { pushPlanLines, pushCanSend };");
const calls = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(calls.map(([fn, args]) => W[fn](...args))));
"""


def _push_page(*calls):
    block = _function("pushPlanLines") + "\n}\n" + _function("pushCanSend") + "\n}\n"
    result = subprocess.run(
        [NODE, "-e", _PUSH_RUNNER, json.dumps(list(calls))],
        input=block, capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


@needs_node
def test_page_shows_the_plan_in_plain_chinese_and_only_a_clean_plan_can_be_pushed(stand_in, linked, tmp_path, monkeypatch):
    """11.5 scenarios 3 and 4 (the page): the plan's lines -- branch, the
    unpushed commits, the uncommitted note, the records, every refusal with
    its command -- and 「推送」 enabled only for a plan without refusals."""
    _commit(linked, "b.py", "b\n", "Add b")
    (linked / "a.py").write_text("edited\n")
    (linked / ".rce").mkdir()
    (linked / ".rce" / "judgements.toml").write_text("j\n")
    ok = githubpush.plan(linked)
    clean = dict(ok, records=dict(ok["records"], changed=[]), commit_count=0, commits=[], can_push=False, nothing_to_push=True)
    blocked = dict(ok, blockers=[{"code": "gh_missing", "message": "gh 还没有登录 GitHub，请在终端里运行：gh auth login", "command": "gh auth login"}], can_push=False)
    home = _home(tmp_path, monkeypatch)
    folder = home / "Library" / "CloudStorage" / "OneDrive-个人" / "论文"
    _thesis(folder)
    not_git = githubpush.plan(folder)
    out = _push_page(
        ("pushPlanLines", [ok]), ("pushCanSend", [ok, False]),
        ("pushCanSend", [clean, False]), ("pushCanSend", [dict(clean, records=ok["records"]), True]),
        ("pushPlanLines", [blocked]), ("pushCanSend", [blocked, True]),
        ("pushPlanLines", [not_git]), ("pushCanSend", [not_git, True]),
    )
    lines = out[0]
    assert lines[0] == {"text": "分支 main → origin/main", "tone": ""}
    assert lines[1]["text"] == "GitHub 还没有的提交：1 个" and lines[1]["commits"][0]["subject"] == "Add b"
    assert {"text": "另有 1 个文件的改动还没有提交，推送不包括它们。", "tone": "warn"} in lines
    assert {"text": "人工记录（.rce/）有 1 个文件还没有提交。", "tone": "warn"} in lines
    assert out[1] is True and out[2] is False and out[3] is True
    assert {"text": "gh 还没有登录 GitHub，请在终端里运行：gh auth login。", "tone": "bad", "command": "gh auth login"} in out[4]
    assert out[5] is False
    texts = [line["text"] for line in out[6]]
    assert texts[0] == "这个文件夹还不是 git 仓库，RCE 不会把它变成仓库。"
    assert texts[1].startswith("这个文件夹在 OneDrive 同步的文件夹里。同步服务和 git 的内部文件合不来")
    assert texts[2] == "可以这样做：把项目移到一个不同步的文件夹（RCE 会认出移动后的项目），然后在那里运行 git init。"
    assert "data/panel_0.dta（146 MB）、data/panel_1.dta（172 MB）" in texts[3]
    assert out[7] is False


def test_page_menu_opens_the_push_panel_and_only_its_push_button_pushes():
    """11.3 (the page): 「推送到 GitHub…」 in the project menu; the panel loads
    the plan (GET, no network); the records tick; the private repository
    step with an editable name; 「推送」 is the only control that POSTs a
    push, with the plan's token; afterwards the GitHub line refreshes."""
    menu = _function("renderProjectMenu")
    assert 'pmItem("推送到 GitHub…", () => { closeProjectMenu(false); openPushDialog(); }, { disabled: state.noProject })' in menu
    assert 'apiGet("/api/github/push-plan")' in _function("loadPushPlan")
    render = _function("renderPushPlan")
    assert "先把人工记录提交一次" in render and 'mkButton(pushDialog.busy ? "正在推送…" : "推送", "btn", () => sendPush(subject, go))' in render
    step = _function("renderCreateRepoStep")
    assert "私有仓库" in step and "RCE 不会创建公开仓库" in step and 'input.id = "push-name";' in step
    assert 'apiPost("/api/github/create-repo", { name: name, token: pushDialog.plan.token })' in _function("createRepo")
    send = _function("sendPush")
    assert 'apiPost("/api/github/push", { token: pushDialog.plan.token, commit_records: !!pushDialog.tick })' in send
    assert 'pushStatus("err", "没能推送到 GitHub", err);' in send and "loadGitHub(); // 11.3" in send
    src = _src()
    assert src.count('"/api/github/push"') == 1 and src.count('"/api/github/create-repo"') == 1
    assert "--public" not in src


def test_create_repo_over_http_then_push_and_no_secret_is_written(tmp_path, unlinked, gh_double, recorded):
    """11.5 scenarios 3 and 7 over HTTP: no remote -> 「创建私有仓库」 (gh,
    --private), then 「推送」; gh's (masked) token line and any credential
    never reach the page, the project or the RCE home."""
    from rce import project as project_identity
    from rce.records import situation as records_situation

    project_identity.init_project(unlinked)
    registry.register(Path(paths._canonical_path(unlinked)), records_situation.classify(unlinked).project_id)
    live, thread = _live(unlinked)
    try:
        _, plan = live.get("/api/github/push-plan")
        assert plan["needs_repo"] and plan["suggested_name"] == "rmb-hysteresis"
        status, body = live.post("/api/github/create-repo", {"name": "名字", "token": plan["token"]})
        assert status == 400 and body["state"] == "github_invalid_name"
        status, created = live.post("/api/github/create-repo", {"name": "rmb-hysteresis", "token": plan["token"]})
        assert status == 200 and created["repo"]["url"] == "https://github.com/rce-test/rmb-hysteresis"
        status, pushed = live.post("/api/github/push", {"token": created["plan"]["token"], "commit_records": True})
        assert status == 200 and pushed["pushed"] and pushed["state"]["linked"]
        seen = json.dumps([plan, created, pushed])
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()
        thread.join(timeout=5)
    assert "gho_" not in seen and "Token" not in seen
    for where in (unlinked / ".rce", Path(os.environ["RCE_HOME"])):
        for path in where.rglob("*"):
            if path.is_file():
                text = path.read_bytes()
                assert b"gho_" not in text and b"oauth" not in text.lower() and b"password" not in text.lower(), path
    assert "--private" in _gh_argvs(recorded["gh_log"])[-1]


def test_commit_records_refuses_a_repository_naming_a_filter_program(stand_in, linked):
    """10.9 / 11.3: committing the records never runs a program the folder's
    own git config names (a clean filter here); nothing is committed."""
    _git(linked, "config", "filter.evil.clean", "touch /tmp/pwned-by-rce-test; cat")
    (linked / ".rce").mkdir()
    (linked / ".rce" / "judgements.toml").write_text("j\n")
    head = _git(linked, "rev-parse", "HEAD").strip()
    with pytest.raises(githubpush.PushError) as refused:
        githubpush.commit_records(linked)
    assert refused.value.code == "unsafe_config"
    assert _git(linked, "rev-parse", "HEAD").strip() == head
