"""DESIGN.md Section 11 (task V7), phase A: synced folders as project
locations (11.1) and a project linked to its GitHub repository (11.2).
Each test names the 11.5 scenario it covers.

No test touches the network. The GitHub remote is a local bare repository
standing in for GitHub: the work repository's remote URL says
`https://github.com/rce-test/demo.git`, and a throwaway GLOBAL git config
(`GIT_CONFIG_GLOBAL`) rewrites that URL to the bare repository's path
(`url.<path>.insteadOf`) and forbids the https and ssh transports outright
(`protocol.https.allow=never`), so a fetch that tried to reach github.com
would fail instead. The OneDrive folder is a fixture tree under a throwaway
HOME (`Library/CloudStorage/OneDrive-测试`) with the dataless flag and the
client's state faked.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import stat
import subprocess
import threading
import types
from pathlib import Path

import pytest

from rce import addproject, cloud, github, paths
from rce.ingest import scan as scan_mod
from rce.records import situation as records_situation
from rce.webapp import registry, server

from test_v6_add_project import Live, _live  # noqa: F401 -- the V6 test server helpers

OWNER, REPO = "rce-test", "demo"
GITHUB_URL = f"https://github.com/{OWNER}/{REPO}.git"


# -- helpers -----------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return result.stdout


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch) -> Path:
    """The bare repository standing in for GitHub, wired by a throwaway
    global git config (module docstring). Returns the bare repository."""
    bare = tmp_path / "github-stand-in" / "demo.git"
    bare.parent.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    config = tmp_path / "gitconfig-global"
    config.write_text(
        "[user]\n\tname = T\n\temail = t@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        "[protocol \"https\"]\n\tallow = never\n"
        "[protocol \"ssh\"]\n\tallow = never\n"
        "[protocol \"git\"]\n\tallow = never\n"
        "[protocol \"file\"]\n\tallow = always\n"
        f"[url \"{bare}\"]\n\tinsteadOf = {GITHUB_URL}\n",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return bare


@pytest.fixture
def linked(tmp_path: Path, stand_in: Path) -> Path:
    """A work repository linked to the stand-in, one commit pushed."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "a.py").write_text("print('a')\n")
    (work / "数据 说明.md").write_text("# 说明\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "first")
    _git(work, "remote", "add", "origin", GITHUB_URL)
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


def _colleague_pushes(tmp_path: Path, stand_in: Path) -> None:
    other = tmp_path / "colleague"
    subprocess.run(["git", "clone", "-q", str(stand_in), str(other)], check=True, capture_output=True)
    (other / "b.py").write_text("print('b')\n")
    _git(other, "add", "b.py")
    _git(other, "commit", "-q", "-m", "colleague")
    _git(other, "push", "-q", "origin", "main")


# -- 11.5 scenario 2: parsing GitHub remote URLs ------------------------------------


@pytest.mark.parametrize("url", [
    "https://github.com/rce-test/demo.git",
    "https://github.com/rce-test/demo",
    "https://github.com/rce-test/demo/",
    "https://GitHub.com/rce-test/demo.git",
    "git@github.com:rce-test/demo.git",
    "git@github.com:rce-test/demo",
    "ssh://git@github.com/rce-test/demo.git",
    "ssh://github.com/rce-test/demo",
])
def test_github_remote_urls_in_the_three_forms_are_linked(url):
    """11.5 scenario 2: https, git@ and ssh:// forms are linked."""
    assert github.parse_url(url) == ("rce-test", "demo")


HOSTILE = [
    "https://user:tok@github.com/rce-test/demo.git",  # embedded credentials
    "https://tok@github.com/rce-test/demo.git",
    "https://x-access-token:ghp_secret123@github.com/rce-test/demo",
    "https://github.com.evil.example/rce-test/demo.git",  # not github.com
    "https://evil.example/github.com/rce-test/demo.git",
    "https://github.com@evil.example/rce-test/demo.git",
    "https://gіthub.com/rce-test/demo.git",  # Cyrillic і
    "https://github.com/rce-tеst/demo.git",  # Cyrillic е in the owner
    "https://github.com/rce-test/dеmo.git",  # Cyrillic е in the name
    "https://github.com/rce-test/demo/extra",  # extra path segment
    "https://github.com/rce-test/demo/tree/main",
    "https://github.com/rce-test",
    "https://github.com/rce_test/demo.git",  # owner outside GitHub's set
    "https://github.com/" + "o" * 40 + "/demo.git",  # owner too long
    "https://github.com/rce-test/" + "r" * 101,
    "https://github.com/rce-test/..",
    "https://github.com/rce-test/.",
    "https://github.com/rce-test/de%2Fmo.git",
    "https://github.com:8443/rce-test/demo.git",  # a port
    "https://github.com/rce-test/demo.git?x=1",
    "https://github.com/rce-test/demo.git#frag",
    "http://github.com/rce-test/demo.git",  # not https
    "git://github.com/rce-test/demo.git",
    "ext::sh -c touch% /tmp/pwned",
    "git@github.com.evil.example:rce-test/demo.git",
    "evil@github.com:rce-test/demo.git",
    "ssh://evil@github.com/rce-test/demo.git",
    "ssh://git@github.com:2222/rce-test/demo.git",
    "/srv/repos/demo.git",
    "file:///github.com/rce-test/demo.git",
    "https://github.com/rce-test/demo .git",
    "",
]


@pytest.mark.parametrize("url", HOSTILE)
def test_hostile_or_foreign_remote_urls_are_not_linked(url):
    """11.5 scenario 2: a remote URL with an owner or name outside GitHub's
    character set -- or credentials, a lookalike host, extra segments -- is
    not linked."""
    assert github.parse_url(url) is None


def test_a_remote_with_credentials_is_not_linked_and_never_echoed(tmp_path, stand_in, caplog):
    """11.5 scenario 2: a remote URL with credentials embedded is not
    linked, and the URL never reaches the page or the log."""
    caplog.set_level(logging.DEBUG)
    work = tmp_path / "creds"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "a.py").write_text("x = 1\n")
    _git(work, "add", "a.py")
    _git(work, "commit", "-q", "-m", "c")
    secret = "https://user:ghp_SECRETTOKEN@github.com/rce-test/demo.git"
    _git(work, "remote", "add", "origin", secret)
    assert github.github_remote(work) is None
    shown = github.state(work)
    assert shown == {"linked": False, "git": True}
    assert github.link_for(work, "a.py") is None
    with pytest.raises(github.GitHubError) as refused:
        github.fetch(work)
    assert refused.value.code == "not_linked"
    seen = json.dumps(shown) + str(refused.value) + refused.value.detail + caplog.text
    assert "SECRETTOKEN" not in seen
    assert github.scrub(f"fatal: unable to access '{secret}'") == "fatal: unable to access 'https://***@github.com/rce-test/demo.git'"


# -- 11.5 scenario 2: the menu line and the state -------------------------------------


def test_never_fetched_and_in_sync_and_ahead_behind_after_a_fetch(tmp_path, stand_in, linked):
    """11.5 scenario 2: the state after 「获取最新状态」 -- in step after the
    push, then a colleague's commit and a local one: a fetch says 1 ahead,
    1 behind, with its date; never a merge."""
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _git(fresh, "init", "-q", "-b", "main")
    _git(fresh, "remote", "add", "origin", GITHUB_URL)
    assert github.state(fresh)["message"] == "还没有从 GitHub 获取过"

    st = github.state(linked)
    assert st["linked"] and (st["owner"], st["repo"], st["remote"]) == (OWNER, REPO, "origin")
    assert st["url"] == "https://github.com/rce-test/demo"
    assert st["message"] == "与 GitHub 一致" and st["base"] == "origin/main" and st["base_kind"] == "upstream"
    assert st["fetched_at"] is None

    _colleague_pushes(tmp_path, stand_in)
    (linked / "c.py").write_text("print('c')\n")
    _git(linked, "add", "c.py")
    _git(linked, "commit", "-q", "-m", "local")
    head_before = _git(linked, "rev-parse", "HEAD")
    assert github.state(linked)["ahead"] == 1 and github.state(linked)["behind"] == 0  # local refs only

    st = github.fetch(linked)
    assert (st["ahead"], st["behind"]) == (1, 1)
    assert st["fetched_at"] is not None
    assert st["message"].startswith("本地领先 1 个提交，落后 1 个（") and st["message"].endswith(" 获取）")
    assert _git(linked, "rev-parse", "HEAD") == head_before  # never pull, never merge
    assert not (linked / "b.py").exists()
    assert st["commit_url"] == f"https://github.com/rce-test/demo/commit/{st['commit']}"


def test_the_default_branch_is_the_base_when_there_is_no_upstream(tmp_path, stand_in, linked):
    """11.2: no upstream -> the remote's default branch."""
    _git(linked, "branch", "--unset-upstream")
    assert github.state(linked)["base"] is None
    _git(linked, "remote", "set-head", "origin", "main")
    st = github.state(linked)
    assert st["base"] == "origin/main" and st["base_kind"] == "default"


def test_several_github_remotes_and_none_chosen_is_not_linked(tmp_path, stand_in):
    """11.2 (never a guess): two GitHub remotes, no upstream, no origin."""
    work = tmp_path / "two"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "remote", "add", "a", "https://github.com/one/x.git")
    _git(work, "remote", "add", "b", "https://github.com/two/y.git")
    assert github.github_remote(work) is None
    _git(work, "remote", "remove", "b")
    assert github.github_remote(work) == {"owner": "one", "repo": "x", "remote": "a", "url": "https://github.com/one/x"}


# -- 11.5 scenario 2: links ---------------------------------------------------------


def test_a_link_pinned_to_the_pushed_commit_no_link_for_an_unpushed_file_and_the_local_changes_note(
    tmp_path, stand_in, linked,
):
    """11.5 scenario 2: a link pinned to the pushed commit; no link for a
    file not yet pushed (and the reason); the local-changes note."""
    pushed = _git(linked, "rev-parse", "origin/main").strip()
    link = github.link_for(linked, "a.py")
    assert link == {
        "url": f"https://github.com/rce-test/demo/blob/{pushed}/a.py", "commit": pushed,
        "local_changes": False, "message": None,
    }
    named = github.link_for(linked, "数据 说明.md")
    assert named["url"] == f"https://github.com/rce-test/demo/blob/{pushed}/%E6%95%B0%E6%8D%AE%20%E8%AF%B4%E6%98%8E.md"

    (linked / "a.py").write_text("print('changed')\n")
    changed = github.link_for(linked, "a.py")
    assert changed["url"] == link["url"] and changed["local_changes"] is True
    assert changed["message"] == "本地有未推送或未提交的改动"

    (linked / "new.py").write_text("x = 1\n")
    _git(linked, "add", "new.py")
    _git(linked, "commit", "-q", "-m", "not pushed")
    assert github.link_for(linked, "new.py") == {
        "reason": "not_pushed", "message": "这个文件还没有推送到 GitHub", "local_changes": False,
    }
    (linked / "loose.py").write_text("y = 2\n")
    assert github.link_for(linked, "loose.py")["reason"] == "not_tracked"
    for hostile in ("../outside.py", "/etc/passwd", ":(glob)*", "a.py/../a.py"):
        assert github.link_for(linked, hostile)["reason"] == "not_tracked"


def test_a_commit_gets_a_link_only_when_github_has_it(tmp_path, stand_in, linked):
    """11.2: a commit gets a link only when GitHub has it."""
    pushed = _git(linked, "rev-parse", "HEAD").strip()
    assert github.link_for_commit(linked, pushed) == {"url": f"https://github.com/rce-test/demo/commit/{pushed}", "commit": pushed}
    assert github.link_for_commit(linked, pushed[:10])["commit"] == pushed
    (linked / "z.py").write_text("")
    _git(linked, "add", "z.py")
    _git(linked, "commit", "-q", "-m", "local only")
    local = _git(linked, "rev-parse", "HEAD").strip()
    assert github.link_for_commit(linked, local) is None
    for hostile in ("HEAD", "main", "--all", "0" * 40, "zz" * 20, "abc"):
        assert github.link_for_commit(linked, hostile) is None


# -- 10.9 / 11.2: nothing the folder names runs ----------------------------------------


def test_fetch_runs_no_hook_and_refuses_a_repository_that_names_a_program(tmp_path, stand_in, linked):
    """11.2 (10.9): a fetch runs no hook the repository carries, and a
    repository whose own config names a program for the fetch (an ssh
    command, a credential helper) is refused -- the program never runs."""
    marker = tmp_path / "ran"
    hook = linked / ".git" / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    _colleague_pushes(tmp_path, stand_in)
    github.fetch(linked)
    assert not marker.exists()

    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    script.chmod(0o755)
    for key in ("core.sshCommand", "credential.helper"):
        _git(linked, "config", "--local", key, str(script))
        with pytest.raises(github.GitHubError) as refused:
            github.fetch(linked)
        assert refused.value.code == "unsafe_config" and key.lower() in refused.value.detail
        _git(linked, "config", "--local", "--unset", key)
    assert not marker.exists()


def test_state_without_a_fetch_is_dated_by_the_push_or_said_never_fetched(tmp_path, stand_in, linked):
    """11.5 scenario 2 (review fix): refs a `push -u` wrote, then a local
    commit -> 「本地领先 1 个提交，落后 0 个（<日期> 推送）」, dated, never
    passed off as a fetch; remote-tracking refs nothing dated (written with
    reflogs off) -> 「还没有从 GitHub 获取过」; a clone is a fetch."""
    _git(linked, "commit", "-q", "--allow-empty", "-m", "local")
    st = github.state(linked)
    assert st["fetched_at"] is None and st["known_by"] == "push"
    assert st["message"].startswith("本地领先 1 个提交，落后 0 个（") and st["message"].endswith(" 推送）")

    quiet = tmp_path / "quiet"
    quiet.mkdir()
    _git(quiet, "init", "-q", "-b", "main")
    _git(quiet, "config", "core.logAllRefUpdates", "false")
    _git(quiet, "remote", "add", "origin", GITHUB_URL)
    _git(quiet, "fetch", "-q", "origin")
    (quiet / ".git" / "FETCH_HEAD").unlink()
    _git(quiet, "reset", "-q", "--hard", "origin/main")
    _git(quiet, "branch", "-q", "--set-upstream-to", "origin/main")
    st = github.state(quiet)
    assert st["commit"] is not None and st["known_at"] is None
    assert st["message"] == "还没有从 GitHub 获取过"

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", GITHUB_URL, str(clone)], check=True, capture_output=True)
    st = github.state(clone)
    assert st["known_by"] == "fetch" and st["message"] == "与 GitHub 一致"


def test_fetch_refuses_a_program_named_in_the_worktree_config(tmp_path, stand_in, linked):
    """11.5 scenario 2 / 11.2 (review fix): `.git/config.worktree`, which git
    applies once `extensions.worktreeConfig` is set, is the repository's own
    config too -- an upload-pack (which the stand-in's local transport would
    run) or an ssh command named there is refused and never runs; so is one
    in a file `.git/config` includes."""
    marker = tmp_path / "ran"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec git-upload-pack \"$@\"\n")
    script.chmod(0o755)
    _git(linked, "config", "extensions.worktreeConfig", "true")
    for key in ("remote.origin.uploadpack", "core.sshCommand"):
        _git(linked, "config", "--worktree", key, str(script))
        assert "--local" not in github.OWN_CONFIG_ARGS
        with pytest.raises(github.GitHubError) as refused:
            github.fetch(linked)
        assert refused.value.code == "unsafe_config" and key.lower() in refused.value.detail
        _git(linked, "config", "--worktree", "--unset", key)
    included = tmp_path / "included.cfg"
    included.write_text(f'[remote "origin"]\n\tuploadpack = {script}\n')
    _git(linked, "config", "--local", "include.path", str(included))
    with pytest.raises(github.GitHubError) as refused:
        github.fetch(linked)
    assert refused.value.code == "unsafe_config"
    assert not marker.exists()
    # The researcher's own (global) settings still apply: they are not refused.
    assert github._program_keys(linked) == ["remote.origin.uploadpack"]


# -- the server: 11.2's endpoints and the file panel --------------------------------------


@pytest.fixture
def linked_server(tmp_path, stand_in, linked):
    from rce import project as project_identity

    project_identity.init_project(linked)
    registry.register(Path(paths._canonical_path(linked)), records_situation.classify(linked).project_id)
    live, thread = _live(linked)
    try:
        yield live, linked
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()
        thread.join(timeout=5)


def test_github_endpoints_and_the_file_panel_link(tmp_path, stand_in, linked_server):
    """11.5 scenario 2 over HTTP: the menu's state, 「获取最新状态」, and the
    file panel's 「在 GitHub 上查看」 or its reason."""
    live, work = linked_server
    status, st = live.get("/api/github")
    assert status == 200 and st["linked"] and st["message"] == "与 GitHub 一致"
    _colleague_pushes(tmp_path, stand_in)
    status, st = live.post("/api/github/fetch", {})
    assert status == 200 and (st["ahead"], st["behind"]) == (0, 1)
    pushed = _git(work, "rev-parse", "origin/main").strip()
    status, panel = live.get("/api/file?path=a.py")
    assert status == 200 and panel["github"]["url"] == f"https://github.com/rce-test/demo/blob/{pushed}/a.py"
    (work / "loose.md").write_text("# x\n")
    _, panel = live.get("/api/file?path=loose.md")
    assert panel["github"]["reason"] == "not_tracked"
    (work / "blob.bin").write_bytes(b"\0\1\2")
    _git(work, "add", "blob.bin")
    _git(work, "commit", "-q", "-m", "bin")
    status, refused = live.get("/api/file?path=blob.bin")
    assert status == 415 and refused["github"]["reason"] == "not_pushed"


def test_github_fetch_failure_says_why_in_chinese_with_details(tmp_path, stand_in, linked_server):
    """11.2 / 8.8: a fetch that fails answers its sentence and git's text."""
    live, work = linked_server
    shutil_target = stand_in.parent / "gone.git"
    stand_in.rename(shutil_target)
    status, body = live.post("/api/github/fetch", {})
    assert status == 502 and body["state"] == "github_fetch_failed"
    assert body["message_zh"] == "没能从 GitHub 获取" and "git fetch origin failed" in body["detail"]


@pytest.mark.parametrize("method,endpoint", [("GET", "/api/github"), ("POST", "/api/github/fetch")])
@pytest.mark.parametrize("headers", [{"Origin": "http://evil.example"}, {"Host": "evil.example:80"}])
def test_new_github_endpoints_refuse_cross_origin_and_fetch_nothing(tmp_path, stand_in, linked_server, method, endpoint, headers):
    """11.5 scenario 7: every new endpoint answers 403 cross-origin; nothing
    is fetched."""
    live, work = linked_server
    _colleague_pushes(tmp_path, stand_in)
    sent = {"Host": f"127.0.0.1:{live.port}", "Content-Type": "application/json", **headers}
    status, _ = live.raw(method, endpoint, sent, {})
    assert status == 403
    assert not (work / ".git" / "FETCH_HEAD").exists()
    assert github.state(work)["behind"] == 0


# -- 11.5 scenario 1: OneDrive ------------------------------------------------------------


@pytest.fixture
def onedrive(tmp_path, monkeypatch) -> Path:
    """A throwaway HOME with `Library/CloudStorage/OneDrive-测试/论文项目`, a
    small research folder in it; the OneDrive client not running."""
    home = tmp_path / "home"
    project = home / "Library" / "CloudStorage" / "OneDrive-测试" / "论文项目"
    (project / "code").mkdir(parents=True)
    (project / "code" / "clean.py").write_text('import pandas as pd\npd.read_csv("data/raw.csv")\n')
    (project / "code" / "local.py").write_text('import pandas as pd\npd.read_csv("data/raw.csv")\n')
    (project / "paper.md").write_text("# Results\n\nThe effect is 0.42.\n")
    (project / "data").mkdir()
    (project / "data" / "raw.csv").write_text("a\n1\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cloud, "PROCESS_CHECK", lambda names: False)
    cloud.reset_cache()
    yield project
    cloud.reset_cache()


CLOUD_ONLY = ("clean.py", "paper.md")


def _fake_dataless(monkeypatch) -> None:
    monkeypatch.setattr(paths, "is_dataless", lambda p: Path(p).name in CLOUD_ONLY)
    monkeypatch.setattr(addproject, "_is_dataless", lambda p: Path(p).name in CLOUD_ONLY)


def test_cloudstorage_and_the_provider_root_are_refused(onedrive):
    """11.5 scenario 1: the provider root (and CloudStorage itself) are
    top-level folders, refused with 10.2's sentence; a folder inside is
    addable."""
    for target in (onedrive.parent, onedrive.parent.parent):
        insp = addproject.inspect(target)
        assert insp.kind == addproject.REFUSED and insp.refusal.code == "top_level"
        assert insp.message == "请选择具体的项目文件夹，而不是「文稿」这样的总文件夹"
    assert addproject.inspect(onedrive).kind == addproject.NEW_FOLDER


def test_provider_and_client_are_recognised(onedrive, monkeypatch):
    """11.1: the provider from the directory name, the client from a
    process check (cached), unknown providers named by their folder."""
    prov = cloud.provider(onedrive / "paper.md")
    assert (prov.kind, prov.name, prov.folder) == ("OneDrive", "OneDrive", "OneDrive-测试")
    assert cloud.provider(onedrive.parent.parent) is None
    assert cloud.provider(Path("/tmp")) is None
    asked = []
    monkeypatch.setattr(cloud, "PROCESS_CHECK", lambda names: asked.append(names) or True)
    cloud.reset_cache()
    assert cloud.client_running("OneDrive") is True and cloud.client_running("OneDrive") is True
    assert asked == [("OneDrive",)]
    other = onedrive.parent.parent / "Nextcloud-me" / "p"
    other.mkdir(parents=True)
    unknown = cloud.provider(other)
    assert (unknown.kind, unknown.name) == ("unknown", "Nextcloud-me")
    assert cloud.client_running("unknown") is None
    assert cloud.message(unknown) == "这些文件只在 Nextcloud-me 云端；Nextcloud-me 客户端没有运行，打开它并登录后 RCE 才能读到"


def test_the_add_preview_says_why_cloud_only_files_are_not_read(onedrive, monkeypatch):
    """11.5 scenario 1: the preview counts the cloud-only files and says, in
    11.1's sentence, why they are not read; with the client running, the
    sentence is gone."""
    _fake_dataless(monkeypatch)
    insp = addproject.inspect(onedrive)
    pv = insp.payload()["preview"]
    assert pv["dataless"] == 2
    assert pv["cloud"] == {
        "kind": "OneDrive", "name": "OneDrive", "folder": "OneDrive-测试", "client_running": False, "blocked": 2,
        "message": "这些文件只在 OneDrive 云端；OneDrive 客户端没有运行，打开它并登录后 RCE 才能读到",
    }
    monkeypatch.setattr(cloud, "PROCESS_CHECK", lambda names: True)
    cloud.reset_cache()
    again = addproject.inspect(onedrive)
    assert again.payload()["preview"]["cloud"]["message"] is None
    assert again.token == insp.token  # the client's state does not make it another folder


def test_added_and_scanned_cloud_only_files_are_never_opened_and_the_scan_says_why(onedrive, monkeypatch):
    """11.5 scenario 1: added; the scan never opens (nor asks to download)
    a cloud-only file, reports it unreadable with 11.1's sentence, and reads
    the rest; nothing waits."""
    _fake_dataless(monkeypatch)
    opened: list[str] = []
    real_read_text, real_read_bytes, real_open = pathlib.Path.read_text, pathlib.Path.read_bytes, open

    def guard(name):
        if name in CLOUD_ONLY:
            opened.append(name)
            raise AssertionError(f"{name} was opened")

    def read_text(self, *a, **k):
        guard(self.name)
        return real_read_text(self, *a, **k)

    def read_bytes(self, *a, **k):
        guard(self.name)
        return real_read_bytes(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
    started = []
    # `rce.paths` asks for a download by starting a thread: recorded, not run.
    monkeypatch.setattr(paths, "threading", types.SimpleNamespace(
        Thread=lambda *a, **k: started.append(k) or types.SimpleNamespace(start=lambda: None),
    ))
    insp = addproject.inspect(onedrive)
    added = addproject.add(onedrive, label="论文", inspected=insp.token)
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(report=addproject.rescan(added.root)))
    worker.start()
    worker.join(timeout=60)
    assert not worker.is_alive(), "the scan waited"
    report = result["report"]
    assert report.ok and opened == [] and started == []
    assert "dataflow: code/clean.py" in report.unreadable_sources and "mdpaper: paper.md" in report.unreadable_sources
    assert "dataflow: code/local.py" not in report.unreadable_sources
    [note] = report.cloud
    assert note["name"] == "OneDrive" and note["files"] == ["code/clean.py", "paper.md"]
    assert note["message"] == "这些文件只在 OneDrive 云端；OneDrive 客户端没有运行，打开它并登录后 RCE 才能读到"
    paths._request_download(onedrive / "paper.md")
    assert started == []  # no download is asked for either


def test_with_the_client_running_a_cloud_file_is_read(onedrive, monkeypatch):
    """11.1: with the client running, a cloud file is read the way an iCloud
    one is."""
    _fake_dataless(monkeypatch)
    monkeypatch.setattr(cloud, "PROCESS_CHECK", lambda names: True)
    cloud.reset_cache()
    assert cloud.read_text(onedrive / "paper.md").startswith("# Results")


def test_the_file_panel_and_the_held_review_items_say_why(onedrive, monkeypatch):
    """11.1: the file panel does not open a cloud-only file (503, the
    sentence), and a 「来源文件暂不可读」 item resting on one says why."""
    _fake_dataless(monkeypatch)
    with pytest.raises(server.CloudOnlyApiError) as refused:
        server.file_payload(onedrive, "paper.md")
    assert refused.value.extra["message_zh"].startswith("这些文件只在 OneDrive 云端")
    held = [
        {"src": "claim:paper.md#ab12", "detail": {"source_unreadable": True, "sources": ["paper.md"]}},
        {"src": "script:code/local.py", "detail": {"source_unreadable": True}},
    ]
    server._held_cloud_notes(onedrive, held)
    assert held[0]["cloud"].startswith("这些文件只在 OneDrive 云端") and "cloud" not in held[1]
    assert scan_mod.file_of("paper.md\x1fmlflow:mlruns") == "paper.md"


# -- the page (11.1 / 11.2) --------------------------------------------------------------

from test_v6_page import NODE, _function, _preview, _src, needs_node  # noqa: E402

_PAGE_RUNNER = r"""
const block = require("fs").readFileSync(0, "utf8");
eval(block + "; global.W = { addPreviewText, cloudNotesText, githubHref };");
const calls = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(calls.map(([fn, args]) => W[fn](...args))));
"""


def _page(*calls):
    html = _src()
    block = html[html.index("// -- Add-project wording (pure"):html.index("// -- end of add-project wording")]
    block += "\n" + _function("githubHref") + "\n}\n"
    result = subprocess.run(
        [NODE, "-e", _PAGE_RUNNER, json.dumps(list(calls))],
        input=block, capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


@needs_node
def test_page_preview_and_scan_say_why_cloud_only_files_are_not_read():
    """11.5 scenario 1 (the page): the preview says 11.1's sentence for
    files nobody can download, names the provider for files that will be;
    the scan's 「详情」 says the sentence after the files."""
    sentence = "这些文件只在 OneDrive 云端；OneDrive 客户端没有运行，打开它并登录后 RCE 才能读到"
    blocked = {"kind": "OneDrive", "name": "OneDrive", "folder": "OneDrive-测试", "client_running": False, "blocked": 2, "message": sentence}
    running = dict(blocked, client_running=True, blocked=0, message=None)
    out = _page(
        ("addPreviewText", [_preview(dataless=2, cloud=blocked)]),
        ("addPreviewText", [_preview(dataless=2, cloud=running)]),
        ("cloudNotesText", [[{"name": "OneDrive", "message": sentence, "files": ["paper.md"]}]]),
        ("cloudNotesText", [None]),
    )
    assert {"text": "其中 2 个文件不会被读取：" + sentence + "。", "tone": "warn"} in out[0]["notes"]
    assert {"text": "其中 2 个文件还在 OneDrive 云端，下载到这台电脑之后才会被读取。", "tone": ""} in out[1]["notes"]
    assert out[2] == " " + sentence + "。" and out[3] == ""
    src = _src()
    assert "unreadableFilesText(last.unreadable_sources) + cloudNotesText(last.cloud)" in src
    assert '(typeof item.cloud === "string" && item.cloud ? " · " + item.cloud : "")' in src


@needs_node
def test_page_puts_only_github_links_in_an_href_and_opens_them_outside():
    """11.2 (the page): only a link the engine built for github.com is put
    in an href, opened in a new tab (the shell sends it to the browser)."""
    out = _page(
        ("githubHref", ["https://github.com/rce-test/demo/blob/" + "a" * 40 + "/a.py"]),
        ("githubHref", ["javascript:alert(1)"]),
        ("githubHref", ["https://github.com.evil.example/x"]),
        ("githubHref", [None]),
        ("githubHref", ["https://user:tok@github.com/rce-test/demo"]),
        ("githubHref", ["http://github.com/rce-test/demo"]),
    )
    assert out[0].startswith("https://github.com/") and out[1:] == [None] * 5
    link = _function("externalLink")
    assert 'a.target = "_blank";' in link and 'a.rel = "noopener noreferrer";' in link


def test_page_menu_has_the_github_line_its_state_and_the_fetch_with_progress_and_errors():
    """11.5 scenario 2 (the page): the menu's 「GitHub：owner/repo」 line, the
    state under it, 「从 GitHub 获取最新状态」 with its progress, and a failure
    through the shared error helper; the file panel's link or reason."""
    menu = _function("renderGitHubMenu")
    assert 'line.append("GitHub：", externalLink(gh.owner + "/" + gh.repo, gh.url));' in menu
    assert '"pm-github-state"' in menu and "gh.message" in menu
    assert '"从 GitHub 获取最新状态"' in menu and "正在从 GitHub 获取…" in menu
    fetch = _function("fetchGitHub")
    assert 'apiPost("/api/github/fetch", {})' in fetch
    assert 'renderBlockingError(statusEl, "没能从 GitHub 获取", err);' in fetch
    assert "loadGitHub(); // 11.2: local refs only, no network" in _function("openProjectMenu")
    assert "if (renderGitHubMenu()) {" in _function("renderProjectMenu")
    link = _function("renderGitHubLink")
    assert '"在 GitHub 上查看"' in link and "link.local_changes" in link and "link.message" in link
    panel = _function("openFilePanel")
    assert "renderGitHubLink(githubHost, data.github);" in panel
    assert "renderGitHubLink(githubHost, err && err.github);" in panel
    assert 'renderBlockingError(box, "无法读取这个文件", err);' in panel
