"""Tests for rce.webapp.macapp (task V3 phase 4): the `RCE.app` launcher
bundle generator, plus `rce app`'s CLI wiring.

Everything here runs on any platform on purpose -- bundle generation is
pure file writing (the module's own spec), and the macOS-only gate applies
solely to `cmd_app`'s *default* install location, which these tests never
use (`--dir` always points into tmp_path). Nothing in this file executes
the generated launcher; its bash source is asserted textually (plus a
`bash -n` syntax check where bash exists), the same way the server tests
assert `subprocess` argument lists rather than really opening Finder.
"""

from __future__ import annotations

import os
import plistlib
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


# -- cli wiring: `rce app` -----------------------------------------------------


def test_cli_app_with_dir_generates_bundle_anywhere(tmp_path, monkeypatch, capsys):
    """`--dir` works on every platform (that is what this very test relies
    on) and the output says where the bundle went plus a Chinese usage
    hint."""
    rce = _fake_rce(tmp_path)
    monkeypatch.setattr(macapp.sys, "executable", str(rce.parent / "python"))
    target = tmp_path / "apps"

    assert cli.main(["app", "--dir", str(target)]) == 0

    assert (target / "RCE.app" / "Contents" / "Info.plist").is_file()
    out = capsys.readouterr().out
    assert str(target / "RCE.app") in out
    assert "双击" in out  # the one-line Chinese usage hint


def test_cli_app_without_dir_on_non_macos_errors_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(macapp, "is_macos", lambda: False)
    assert cli.main(["app"]) == 1
    err = capsys.readouterr().err
    assert "Error" in err and "macOS" in err and "--dir" in err  # names the way out


def test_cli_app_without_dir_on_macos_targets_home_applications(tmp_path, monkeypatch):
    """The default install location is ~/Applications -- observed via a
    stubbed generate_bundle (nothing must be written into the real home)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(macapp, "is_macos", lambda: True)
    targets: list[Path] = []
    monkeypatch.setattr(
        cli.macapp, "generate_bundle",
        lambda target_dir, **kw: targets.append(target_dir) or (target_dir / "RCE.app"),
    )

    assert cli.main(["app"]) == 0

    assert targets == [home / "Applications"]


def test_cli_app_reports_missing_entry_point_as_clean_error(tmp_path, monkeypatch, capsys):
    """A MacAppError (no rce next to the interpreter) surfaces as the
    standard 'Error: ...' line and exit 1, never a traceback."""
    monkeypatch.setattr(macapp.sys, "executable", str(tmp_path / "python"))
    assert cli.main(["app", "--dir", str(tmp_path / "apps")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error:") and "rce" in err
