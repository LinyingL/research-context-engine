"""Tests for rce.webapp.macapp: `rce app`'s two products -- the native shell
RCE.app (task V4 phase 3, DESIGN.md 8.9) and the V3 launcher-script bundle
it falls back to -- plus the CLI wiring.

The launcher tests run on any platform (pure file writing; its bash source
is asserted textually, plus a `bash -n` syntax check where bash exists).
The native-build tests compile the real Swift sources with the system
swiftc, once per module, and skip cleanly where macOS or swiftc is absent;
the fallback paths are exercised everywhere by simulating a missing or
failing compiler. Nothing here LAUNCHES an app: the shell's runtime
promises (menus, bridge shapes, runtime-only configuration, escaped log
text, stop-only-what-it-spawned) are pinned in the Swift text, the same
way the server tests assert `subprocess` argument lists rather than
really opening Finder.
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from rce import cli
from rce.webapp import macapp


def _fake_rce(tmp_path: Path, dirname: str = "bin") -> Path:
    """A stand-in entry-point file so generation tests bake in a KNOWN path
    instead of whatever interpreter runs the test suite."""
    bin_dir = tmp_path / dirname
    bin_dir.mkdir(parents=True, exist_ok=True)
    rce = bin_dir / "rce"
    rce.write_text("#!/bin/sh\n")
    return rce


def _launcher(bundle: Path) -> Path:
    return bundle / "Contents" / "MacOS" / macapp.EXECUTABLE_NAME


# -- generate_bundle: bundle shape --------------------------------------------


def test_generate_bundle_writes_parseable_plist_with_required_keys(tmp_path):
    bundle = macapp.generate_bundle(tmp_path, rce_executable=_fake_rce(tmp_path))
    assert bundle == tmp_path / "RCE.app"
    with (bundle / "Contents" / "Info.plist").open("rb") as fh:
        plist = plistlib.load(fh)
    assert plist["CFBundleName"] == "RCE"
    assert plist["CFBundleIdentifier"] == "dev.researchos.rce"
    assert plist["CFBundleExecutable"] == macapp.EXECUTABLE_NAME
    # The launcher must never bounce in the Dock -- it runs and exits.
    assert plist["LSUIElement"] is True


def test_generate_bundle_launcher_is_executable(tmp_path):
    bundle = macapp.generate_bundle(tmp_path, rce_executable=_fake_rce(tmp_path))
    launcher = _launcher(bundle)
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert os.access(launcher, os.X_OK)


def test_generate_bundle_is_idempotent_overwrite(tmp_path):
    """Regeneration is the reinstall story: a second run over an existing
    bundle succeeds and leaves the same content, not a duplicate or an
    error."""
    rce = _fake_rce(tmp_path)
    first = macapp.generate_bundle(tmp_path, rce_executable=rce)
    script_before = _launcher(first).read_text()
    second = macapp.generate_bundle(tmp_path, rce_executable=rce)
    assert second == first
    assert _launcher(second).read_text() == script_before


# -- generate_bundle: launcher script content ---------------------------------


def test_launcher_contains_resolved_rce_path_and_port(tmp_path):
    rce = _fake_rce(tmp_path)
    bundle = macapp.generate_bundle(tmp_path, rce_executable=rce)
    script = _launcher(bundle).read_text()
    assert script.startswith("#!/bin/bash\n")
    assert shlex.quote(str(rce)) in script
    # Probe URL and serve invocation come from the same constant (7357).
    assert f"http://127.0.0.1:{macapp.DEFAULT_PORT}" in script
    assert f"serve --port {macapp.DEFAULT_PORT} --no-browser" in script
    assert "/api/summary" in script
    assert "nohup" in script and "serve.log" in script


def test_launcher_quotes_shell_metacharacter_paths(tmp_path):
    """No shell-injection surface: a path carrying quote/semicolon
    metacharacters must appear only in its `shlex.quote`d form -- the raw
    string (whose single quotes would terminate a naive '...'-wrapping)
    must not appear anywhere in the script."""
    rce = _fake_rce(tmp_path, dirname="evil'; touch pwned; 'dir")
    bundle = macapp.generate_bundle(tmp_path, rce_executable=rce)
    script = _launcher(bundle).read_text()
    assert shlex.quote(str(rce)) in script
    assert str(rce) not in script  # only the quoted form, never the raw one
    # ...and the quoted result is still valid bash, not just different.
    if shutil.which("bash"):
        proc = subprocess.run(["bash", "-n", str(_launcher(bundle))], capture_output=True)
        assert proc.returncode == 0, proc.stderr


def test_launcher_expands_baked_path_only_double_quoted(tmp_path):
    """The baked path is assigned once (RCE=<quoted>) and every later use
    is the double-quoted expansion -- a bare $RCE would re-split a path
    with spaces at execution time even though generation quoted it."""
    rce = _fake_rce(tmp_path, dirname="dir with spaces")
    script = _launcher(macapp.generate_bundle(tmp_path, rce_executable=rce)).read_text()
    assert '"$RCE" serve' in script
    assert script.count("$RCE") == script.count('"$RCE"')  # every expansion is the quoted one


# -- resolve_rce_executable ----------------------------------------------------


def test_resolve_rce_executable_uses_interpreters_own_bin_dir(tmp_path, monkeypatch):
    rce = _fake_rce(tmp_path)
    monkeypatch.setattr(macapp.sys, "executable", str(rce.parent / "python"))
    assert macapp.resolve_rce_executable() == rce


def test_resolve_rce_executable_refuses_when_entry_point_missing(tmp_path, monkeypatch):
    """No `rce` next to the interpreter is a refusal naming the missing
    path -- never a $PATH-lookup fallback (module docstring)."""
    monkeypatch.setattr(macapp.sys, "executable", str(tmp_path / "python"))
    with pytest.raises(macapp.MacAppError, match="rce"):
        macapp.resolve_rce_executable()


def test_generate_bundle_defaults_to_current_interpreters_rce(tmp_path, monkeypatch):
    rce = _fake_rce(tmp_path)
    monkeypatch.setattr(macapp.sys, "executable", str(rce.parent / "python"))
    bundle = macapp.generate_bundle(tmp_path)
    assert shlex.quote(str(rce)) in _launcher(bundle).read_text()




# -- build_app: the native shell (task V4 phase 3, DESIGN.md 8.9) -------------
# These compile the real Swift sources with the system swiftc (about ten
# seconds per program), once per module, and skip cleanly on a machine
# without macOS or the toolchain. The build deliberately lands on top of an
# installed V3 launcher bundle, so the same fixture also proves the
# replace-in-place rule.

_HAS_SWIFTC = macapp.is_macos() and shutil.which("swiftc") is not None
native_only = pytest.mark.skipif(not _HAS_SWIFTC, reason="needs macOS with swiftc")

_TRIAL_PORT = 7468
# Spaces, both quote kinds and CJK: the sidecar must carry it byte-exact.
_HOSTILE_DIR = "venv with 'single' \"double\" 中文/bin"


@pytest.fixture(scope="module")
def native_build(tmp_path_factory):
    if not _HAS_SWIFTC:
        pytest.skip("needs macOS with swiftc")
    root = tmp_path_factory.mktemp("native")
    rce = _fake_rce(root, dirname=_HOSTILE_DIR)
    target = root / "apps"
    old = macapp.generate_bundle(target, rce_executable=rce)
    (old / "Contents" / "stale.txt").write_text("left over from the launcher")
    result = macapp.build_app(target, rce_executable=rce, port=_TRIAL_PORT)
    return result, rce, target


def _plist(bundle: Path) -> dict:
    with (bundle / "Contents" / "Info.plist").open("rb") as fh:
        return plistlib.load(fh)


@native_only
def test_native_build_produces_an_executable_mach_o_binary(native_build):
    result, _, _ = native_build
    assert result.native is True
    assert macapp.NOTICE_COMPILE_FAILED not in " ".join(result.notices)
    binary = _launcher(result.bundle)
    assert binary.is_file() and os.access(binary, os.X_OK)
    magic = binary.read_bytes()[:4]
    assert magic in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")  # Mach-O 64 / universal, not a script


@native_only
def test_native_build_info_plist_keys(native_build):
    result, _, _ = native_build
    plist = _plist(result.bundle)
    assert plist["CFBundleExecutable"] == "RCE"
    assert plist["CFBundleIdentifier"] == "dev.researchos.rce"
    assert plist["CFBundleName"] == "RCE"
    assert plist["CFBundleIconFile"] == "RCE"
    assert plist["LSUIElement"] is False  # a windowed app now
    assert plist["NSHighResolutionCapable"] is True
    assert plist["LSMinimumSystemVersion"] == macapp.MIN_MACOS
    assert plist["NSAppTransportSecurity"] == {"NSAllowsLocalNetworking": True}


@native_only
def test_native_build_has_an_icns_icon(native_build):
    result, _, _ = native_build
    assert macapp.NOTICE_NO_ICON not in result.notices
    icns = result.bundle / "Contents" / "Resources" / "RCE.icns"
    assert icns.is_file() and icns.read_bytes()[:4] == b"icns"


@native_only
def test_native_build_sidecars_carry_a_hostile_path_byte_exact(native_build):
    """The rce path is data, never source: written byte-exact (no quoting,
    no newline) and read whole by the shell at runtime."""
    result, rce, _ = native_build
    resources = result.bundle / "Contents" / "Resources"
    assert (resources / "rce-path").read_bytes() == str(rce).encode("utf-8")
    assert (resources / "rce-port").read_text(encoding="utf-8") == str(_TRIAL_PORT)


@native_only
def test_native_build_replaced_the_launcher_bundle_in_place(native_build):
    result, _, target = native_build
    assert result.bundle == target / "RCE.app"
    assert not (result.bundle / "Contents" / "stale.txt").exists()
    assert not _launcher(result.bundle).read_bytes().startswith(b"#!")
    assert [p.name for p in target.iterdir()] == ["RCE.app"]  # no staging directory left behind


@native_only
def test_native_build_is_ad_hoc_signed(native_build):
    result, _, _ = native_build
    codesign = shutil.which("codesign")
    if codesign is None:
        pytest.skip("codesign not available")
    proc = subprocess.run([codesign, "--verify", "--strict", str(result.bundle)], capture_output=True)
    assert proc.returncode == 0, proc.stderr


# -- build_app: falling back to the launcher bundle ---------------------------


def test_build_app_without_swiftc_falls_back_to_the_launcher(tmp_path, monkeypatch):
    """No toolchain: the V3 launcher bundle, one notice line, and an
    existing native bundle replaced -- its signature and sidecars must not
    survive into a bundle whose executable is now a script."""
    monkeypatch.setattr(macapp, "find_swiftc", lambda: None)
    rce = _fake_rce(tmp_path)
    old = tmp_path / "apps" / "RCE.app" / "Contents"
    (old / "_CodeSignature").mkdir(parents=True)
    (old / "_CodeSignature" / "CodeResources").write_text("sig")
    (old / "Resources").mkdir()
    (old / "Resources" / "rce-path").write_text("/old/rce")

    result = macapp.build_app(tmp_path / "apps", rce_executable=rce, port=_TRIAL_PORT)

    assert result.native is False
    assert result.notices == [macapp.NOTICE_NO_SWIFTC]
    assert "\n" not in macapp.NOTICE_NO_SWIFTC  # "says so in one line"
    script = _launcher(result.bundle).read_text()
    assert script.startswith("#!/bin/bash\n") and f"serve --port {_TRIAL_PORT}" in script
    assert _plist(result.bundle)["LSUIElement"] is True
    assert not (old / "_CodeSignature").exists() and not (old / "Resources").exists()


def test_build_app_falls_back_when_swiftc_cannot_compile(tmp_path, monkeypatch):
    """A swiftc that exists but fails (the command-line-tools shim on a Mac
    without them) is the same fallback, with the compiler's last line."""
    fake = tmp_path / "swiftc"
    fake.write_text("#!/bin/sh\necho 'error: no developer tools were found' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(macapp, "find_swiftc", lambda: str(fake))

    result = macapp.build_app(tmp_path / "apps", rce_executable=_fake_rce(tmp_path))

    assert result.native is False
    assert len(result.notices) == 1
    assert result.notices[0].startswith(macapp.NOTICE_COMPILE_FAILED)
    assert "no developer tools were found" in result.notices[0]
    assert _launcher(result.bundle).read_text().startswith("#!/bin/bash\n")


def test_build_app_replaces_a_symlinked_bundle_without_following_it(tmp_path, monkeypatch):
    monkeypatch.setattr(macapp, "find_swiftc", lambda: None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("not ours")
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "RCE.app").symlink_to(elsewhere, target_is_directory=True)

    result = macapp.build_app(apps, rce_executable=_fake_rce(tmp_path))

    assert (elsewhere / "keep.txt").read_text() == "not ours"
    assert not result.bundle.is_symlink() and _launcher(result.bundle).is_file()


def test_build_app_refuses_a_missing_entry_point_before_compiling(tmp_path, monkeypatch):
    monkeypatch.setattr(macapp.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(macapp, "find_swiftc", lambda: pytest.fail("must not look for swiftc"))
    with pytest.raises(macapp.MacAppError):
        macapp.build_app(tmp_path / "apps")
    assert not (tmp_path / "apps" / "RCE.app").exists()


def test_native_info_plist_names_the_icon_only_when_it_was_built():
    assert "CFBundleIconFile" not in macapp._native_info_plist(7357, with_icon=False)
    assert macapp._native_info_plist(7357, with_icon=True)["CFBundleIconFile"] == "RCE"


# -- The Swift sources: what the shell promises, checked in the text ----------
# Runs everywhere (no compiling): the menus, the bridge's fixed shapes and
# the runtime-only configuration are part of DESIGN.md 8.9's contract.

_SHELL = macapp.SHELL_SOURCE.read_text(encoding="utf-8")


def test_swift_sources_ship_as_package_data():
    import tomllib

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert "webapp/shell/*.swift" in pyproject["tool"]["setuptools"]["package-data"]["rce"]
    assert macapp.SHELL_SOURCE.is_file() and macapp.ICON_SOURCE.is_file()


def test_shell_menus_match_the_design():
    for snippet in (
        'submenu("RCE"', 'item("关于 RCE"', 'item("退出", #selector(NSApplication.terminate(_:)), "q")',
        'submenu("文件"', 'cmd("新增尝试", "new-attempt", "n")',
        'cmd("在 Finder 中显示项目", "reveal-project", "r", [.command, .shift])',
        'item("关闭", #selector(NSWindow.performClose(_:)), "w")',
        'submenu("编辑"', '"undo:"', '"redo:"', "NSText.cut", "NSText.copy", "NSText.paste", "NSText.selectAll",
        'submenu("视图"', 'cmd("决策树", "tree", "1")', 'cmd("血缘", "lineage", "2")', 'cmd("画布", "canvas", "3")',
        'item("重新载入", #selector(reloadPage(_:)), "r")', 'cmd("放大", "zoom-in", "+")',
        'cmd("缩小", "zoom-out", "-")', 'cmd("实际大小", "zoom-reset", "0")', 'cmd("适应全部", "fit", "f", [])',
        'submenu("窗口"', 'item("最小化", #selector(NSWindow.performMiniaturize(_:)), "m")',
        'item("缩放", #selector(NSWindow.performZoom(_:))',
        'submenu("帮助", [cmd("打开项目地图", "open-map")])',
    ):
        assert snippet in _SHELL, f"menu item missing from RCEShell.swift: {snippet}"


def test_shell_view_menu_has_the_variables_view_on_cmd_4():
    """V5 phase 9 (9.11 "In the app"): 视图 gains 变量 ⌘4, a whitelisted
    command name the page's dispatcher also accepts."""
    assert 'cmd("变量", "variables", "4")' in _SHELL
    whitelist = _SHELL[_SHELL.index("let shellCommands"):]
    whitelist = whitelist[: whitelist.index("]")]
    assert '"variables"' in whitelist
    page = (Path(macapp.__file__).parent / "app.html").read_text(encoding="utf-8")
    assert '"variables": () => activateView("variables")' in page


def test_shell_choose_folder_answers_through_a_fixed_callback_with_arguments():
    """V5 phase 9 (9.4 「选择新位置…」): `{type: "choose-folder", request}`
    opens a one-directory NSOpenPanel; the answer goes back through the
    fixed callback with the request id and the path passed as arguments --
    never interpolated into script text; any other shape is ignored; the
    page is told it may ask (RCEShellFeatures) at document start."""
    handler = _SHELL[_SHELL.index("didReceive message: WKScriptMessage"):]
    handler = handler[: handler.index("\n    }\n")]
    assert 'body["type"] as? String == "choose-folder"' in handler
    assert 'let request = body["request"] as? String' in handler and "isFolderRequest(request)" in handler
    assert "body.count == 2" in handler and "message.frameInfo.isMainFrame" in handler
    choose = _SHELL[_SHELL.index("func chooseFolder"):]
    choose = choose[: choose.index("\n    }\n")]
    assert "panel.canChooseDirectories = true" in choose and "panel.canChooseFiles = false" in choose
    assert "panel.allowsMultipleSelection = false" in choose
    answer = _SHELL[_SHELL.index("func answerFolder"):]
    answer = answer[: answer.index("\n    }\n")]
    assert 'callAsyncJavaScript("window.RCE && RCE.folderChosen(request, path)", arguments: arguments' in answer
    assert '["request": request, "path": path ?? NSNull()]' in answer
    assert "\\(" not in answer  # no string interpolation anywhere in the callback
    assert 'let shellFeaturesScript = "window.RCEShellFeatures = [\\"choose-folder\\"];"' in _SHELL
    assert "injectionTime: .atDocumentStart, forMainFrameOnly: true" in _SHELL
    validator = _SHELL[_SHELL.index("func isFolderRequest"):]
    validator = validator[: validator.index("\n}\n")]
    assert "folderRequestLimit" in validator and '$0 == "-"' in validator and "isASCII" in validator


def test_shell_window_geometry_and_quit_on_close():
    assert "width: 1280, height: 840" in _SHELL
    assert "NSSize(width: 900, height: 600)" in _SHELL
    assert 'setFrameAutosaveName("RCEMain")' in _SHELL
    assert 'window.title = "RCE"' in _SHELL
    assert "applicationShouldTerminateAfterLastWindowClosed" in _SHELL


def test_shell_bridge_uses_only_fixed_shapes():
    """Native -> page: one fixed JS string whose only variable part is a
    whitelisted name checked right before use. Page -> native: one handler,
    one message shape, main frame of the engine's origin only."""
    send = _SHELL[_SHELL.index("@objc func sendCommand"):]
    send = send[: send.index("\n    }\n")]
    assert "shellCommands.contains(name)" in send
    assert "evaluateJavaScript(\"window.RCE && RCE.command('\\(name)')\"" in send
    assert send.index("shellCommands.contains(name)") < send.index("evaluateJavaScript")
    assert _SHELL.count("evaluateJavaScript(") == 2  # sendCommand + the fixed 'reload'
    assert 'add(self, name: "rce")' in _SHELL
    handler = _SHELL[_SHELL.index("didReceive message: WKScriptMessage"):]
    assert 'body["type"] as? String == "title"' in handler
    assert "message.frameInfo.isMainFrame" in handler and "origin.port == port" in handler
    assert "sanitizedLabel(text)" in handler and '"RCE — " + label' in handler


def test_shell_reads_its_configuration_at_runtime_and_never_enables_devtools():
    assert 'resourceText("rce-path")' in _SHELL and 'resourceText("rce-port")' in _SHELL
    assert '["serve", "--port", String(port), "--no-browser"]' in _SHELL
    assert '"api/projects"' in _SHELL and '"api/shutdown"' in _SHELL
    assert "developerExtrasEnabled" not in _SHELL and "isInspectable" not in _SHELL


def test_shell_quit_names_its_own_child_in_the_shutdown_request():
    """DESIGN.md 8.9 "an engine the user started from a terminal is left
    alone": the shutdown carries the spawned child's pid, so an engine
    that holds the port but is NOT our child refuses it (server side:
    `_check_shutdown_target`). Adversarial review of the V4 work."""
    quit_path = _SHELL[_SHELL.index("func applicationShouldTerminate(_ sender"):]
    quit_path = quit_path[: quit_path.index("\n    }\n")]
    assert 'guard let process = child, process.isRunning' in quit_path
    assert '\\"pid\\": \\(process.processIdentifier)' in quit_path
    assert 'Data("{}".utf8)' not in quit_path


def test_shell_placeholder_shows_the_log_only_as_escaped_text():
    assert "正在启动引擎…" in _SHELL and "#F7F2E9" in _SHELL
    page = _SHELL[_SHELL.index("func placeholderPage"):]
    page = page[: page.index("\n}\n")]
    assert "<pre>\\(htmlEscape(logText))</pre>" in page
    # Every interpolation into the markup is escaped text, except the one
    # that inserts `body` -- itself built only from escaped pieces.
    assert sorted(set(re.findall(r"\\\((\w+)", page))) == ["body", "htmlEscape"]
    assert page.count("\\(body)") == 1
    assert "\\(htmlEscape(headline))" in page and "\\(htmlEscape(detail))" in page


def test_shell_stops_only_an_engine_it_spawned():
    quit_ = _SHELL[_SHELL.index("func applicationShouldTerminate"):]
    quit_ = quit_[: quit_.index("// MARK: menus")]
    assert quit_.lstrip().startswith("func applicationShouldTerminate")
    assert "guard let process = child, process.isRunning else { return .terminateNow }" in quit_


# -- cli wiring: `rce app` -----------------------------------------------------


def test_cli_app_with_dir_builds_anywhere_and_prints_the_fallback_notice(tmp_path, monkeypatch, capsys):
    """`--dir` works on every platform; without swiftc the output carries
    the one-line notice, where the bundle went, and the usage hint."""
    rce = _fake_rce(tmp_path)
    monkeypatch.setattr(macapp.sys, "executable", str(rce.parent / "python"))
    monkeypatch.setattr(macapp, "find_swiftc", lambda: None)
    target = tmp_path / "apps"

    assert cli.main(["app", "--dir", str(target)]) == 0

    assert (target / "RCE.app" / "Contents" / "Info.plist").is_file()
    out = capsys.readouterr().out
    assert macapp.NOTICE_NO_SWIFTC in out
    assert str(target / "RCE.app") in out
    assert "双击" in out


def test_cli_app_without_dir_on_non_macos_errors_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(macapp, "is_macos", lambda: False)
    assert cli.main(["app"]) == 1
    err = capsys.readouterr().err
    assert "Error" in err and "macOS" in err and "--dir" in err  # names the way out


def _stub_build_app(monkeypatch) -> list[tuple[Path, int]]:
    calls: list[tuple[Path, int]] = []

    def fake(target_dir, rce_executable=None, port=macapp.DEFAULT_PORT, force=False):
        calls.append((target_dir, port))
        return macapp.AppBuild(bundle=target_dir / "RCE.app", native=True)

    monkeypatch.setattr(cli.macapp, "build_app", fake)
    return calls


def test_cli_app_without_dir_on_macos_targets_home_applications(tmp_path, monkeypatch):
    """The default install location is ~/Applications and the default port
    7357 -- observed via a stubbed build_app (nothing must be written into
    the real home)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(macapp, "is_macos", lambda: True)
    calls = _stub_build_app(monkeypatch)

    assert cli.main(["app"]) == 0

    assert calls == [(home / "Applications", 7357)]


def test_cli_app_hidden_port_reaches_the_build(tmp_path, monkeypatch, capsys):
    calls = _stub_build_app(monkeypatch)
    assert cli.main(["app", "--dir", str(tmp_path), "--port", str(_TRIAL_PORT)]) == 0
    assert calls == [(tmp_path.resolve(), _TRIAL_PORT)]
    assert "引擎没在运行时会自动启动" in capsys.readouterr().out


def test_cli_app_rejects_an_out_of_range_port(tmp_path, monkeypatch, capsys):
    _stub_build_app(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["app", "--dir", str(tmp_path), "--port", "70000"])
    assert exc.value.code == 2
    assert "65535" in capsys.readouterr().err


def test_cli_app_port_is_hidden_from_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["app", "--help"])
    out = capsys.readouterr().out
    assert "--dir" in out and "--port" not in out


def test_cli_app_reports_missing_entry_point_as_clean_error(tmp_path, monkeypatch, capsys):
    """A MacAppError (no rce next to the interpreter) surfaces as the
    standard 'Error: ...' line and exit 1, never a traceback."""
    monkeypatch.setattr(macapp.sys, "executable", str(tmp_path / "python"))
    assert cli.main(["app", "--dir", str(tmp_path / "apps")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error:") and "rce" in err


# -- 9.12: an unchanged shell is not rebuilt (macOS would ask for Documents again) ----


def _fake_native_build(monkeypatch) -> list[str]:
    """A stand-in toolchain: `swiftc` "compiles" by writing a file, codesign
    succeeds, no iconutil. Returns the list of programs run, in order."""
    ran: list[str] = []

    def run(args):
        ran.append(Path(args[0]).name)
        if "-o" in args:
            out = Path(args[args.index("-o") + 1])
            out.write_bytes(b"\xcf\xfa\xed\xfe fake binary")
            out.chmod(0o755)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(macapp, "find_swiftc", lambda: "/usr/bin/swiftc")
    monkeypatch.setattr(macapp, "_run", run)
    monkeypatch.setattr(macapp.shutil, "which", lambda name: "/usr/bin/codesign" if name == "codesign" else None)
    return ran


def test_build_id_hashes_every_input_of_a_native_build(tmp_path, monkeypatch):
    rce = _fake_rce(tmp_path)
    base = macapp.build_id(rce, 7357)
    assert base.startswith("sha256:") and base == macapp.build_id(rce, 7357)
    assert macapp.build_id(rce, 7358) != base
    assert macapp.build_id(rce.parent / "other-rce", 7357) != base
    shell = tmp_path / "RCEShell.swift"
    shell.write_text(macapp.SHELL_SOURCE.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
    monkeypatch.setattr(macapp, "SHELL_SOURCE", shell)
    assert macapp.build_id(rce, 7357) != base
    monkeypatch.undo()
    icon = tmp_path / "RCEIcon.swift"
    icon.write_text("// another icon\n", encoding="utf-8")
    monkeypatch.setattr(macapp, "ICON_SOURCE", icon)
    assert macapp.build_id(rce, 7357) != base
    monkeypatch.undo()
    monkeypatch.setattr(macapp, "_rce_version", lambda: "99.0")  # a plist field
    assert macapp.build_id(rce, 7357) != base


def test_an_unchanged_bundle_is_left_untouched_and_force_rebuilds_it(tmp_path, monkeypatch, capsys):
    rce = _fake_rce(tmp_path)
    monkeypatch.setattr(macapp.sys, "executable", str(rce.parent / "python"))
    ran = _fake_native_build(monkeypatch)
    target = tmp_path / "apps"
    first = macapp.build_app(target, rce_executable=rce, port=_TRIAL_PORT)
    assert first.native and not first.up_to_date and "swiftc" in ran and "codesign" in ran
    bundle = target / "RCE.app"
    binary = bundle / "Contents" / "MacOS" / "RCE"
    recorded = (bundle / "Contents" / "Resources" / macapp.BUILD_ID_SIDECAR).read_text(encoding="utf-8")
    assert recorded == macapp.build_id(rce, _TRIAL_PORT)
    stamp = (binary.stat().st_ino, binary.stat().st_mtime_ns)
    ran.clear()
    again = macapp.build_app(target, rce_executable=rce, port=_TRIAL_PORT)
    assert again.up_to_date and again.native and ran == []  # nothing compiled, nothing re-signed
    assert (binary.stat().st_ino, binary.stat().st_mtime_ns) == stamp
    assert cli.main(["app", "--dir", str(target), "--port", str(_TRIAL_PORT)]) == 0
    assert "RCE.app is up to date" in capsys.readouterr().out and ran == []
    assert cli.main(["app", "--dir", str(target), "--port", str(_TRIAL_PORT), "--force"]) == 0
    assert "up to date" not in capsys.readouterr().out and "swiftc" in ran and "codesign" in ran
    ran.clear()
    # A changed input (here the port) rebuilds; so does a bundle without a build-id (the V3 launcher).
    assert not macapp.build_app(target, rce_executable=rce, port=_TRIAL_PORT + 1).up_to_date and "swiftc" in ran
    launcher = tmp_path / "launcher"
    macapp.generate_bundle(launcher, rce_executable=rce)
    assert not macapp.is_up_to_date(launcher / "RCE.app", macapp.build_id(rce, macapp.DEFAULT_PORT))


def test_shell_waiting_page_names_the_documents_prompt_and_shows_the_log_only_after_an_exit():
    """9.12: after a few seconds without an answer the waiting page says
    the system may ask for the Documents folder; the log tail is shown only
    once the engine process has EXITED; the child's output is unbuffered."""
    # As Swift source: the quotes inside the string literal are escaped.
    assert r'"正在启动引擎… 如果系统询问是否允许 RCE 访问\"文稿\"文件夹，请点\"允许\"。"' in _SHELL
    assert 'environment["PYTHONUNBUFFERED"] = "1"' in _SHELL
    poll = _SHELL[_SHELL.index("func pollTick"):_SHELL.index("func showLog")]
    assert "showLog" not in poll and "documentsHint" in poll
    exited = _SHELL[_SHELL.index("func engineExited"):_SHELL.index("func schedulePolling")]
    assert "showLog" in exited
