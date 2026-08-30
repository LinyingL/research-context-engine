"""Generate a double-clickable macOS launcher bundle for the RCE web app
(task V3 phase 4: "launch like a real Mac app").

What `rce app` installs is a *launcher*, not a port of the app: an
`RCE.app` bundle whose executable is a small generated bash script that
(1) checks whether the local server is already up at its fixed port and
just opens the page if so, else (2) starts `rce serve` detached, waits for
it to answer, and opens the page. The server itself stays exactly
`rce.webapp.server` -- same 127.0.0.1-only binding, same security model;
this module never touches networking at all.

Pure-python generation, testable anywhere (spec requirement): nothing in
here calls `open`, checks `sys.platform`, or requires macOS -- it only
writes two files under a caller-chosen directory. The macOS-only gate
lives in `rce.cli.cmd_app`, and applies solely to the *default* install
location (`~/Applications`); generation into an explicit `--dir` works on
any platform, which is exactly what the test suite uses. `is_macos()`
below exists for that gate (public, unlike `rce.webapp.server._is_macos`,
because its caller is another module -- same check, different visibility
need).

Bundle layout (the minimum macOS requires to treat a directory as an app):

    RCE.app/
      Contents/
        Info.plist        -- stdlib plistlib; CFBundleName "RCE",
                             CFBundleIdentifier "dev.researchos.rce",
                             CFBundleExecutable, and LSUIElement true --
                             the bundle is a launcher that runs and exits,
                             so it must never bounce in the Dock or claim
                             app-switcher presence.
        MacOS/
          RCE              -- the generated bash script, chmod 0o755.

The launcher bakes in the ABSOLUTE path of the current interpreter's
`rce` entry point (`<sys.executable's bin dir>/rce`), resolved at
generation time -- never `rce` off `$PATH`, because a double-clicked app
inherits the login session's environment, not the user's shell rc files,
so the venv that owns this install would not be on its PATH. A bin dir
with no `rce` script is a refusal (`MacAppError`), not a guessed
fallback (DESIGN.md section 0): it means this interpreter never had rce
installed as a console script, and a launcher pointing at a nonexistent
path would fail only later, silently, on double-click.

Shell-injection surface: none by construction. The one value interpolated
into the script -- the rce path -- goes through `shlex.quote` and is then
only ever expanded as `"$RCE"`; `$HOME` and the URL are written by this
module as fixed text, never taken from any input. There is no user- or
request-supplied string anywhere in generation (the target directory
names where the files land; it is never embedded in the script).

The port is fixed at `DEFAULT_PORT` (7357) rather than configurable: the
launcher's whole "already running?" check is a probe of one known URL,
and two launchers disagreeing about the port would each start a second
server instead of finding the first. `rce serve --port 7357` and the
probe URL are generated from the same constant so they cannot drift.
"""

from __future__ import annotations

import plistlib
import shlex
import sys
from pathlib import Path

DEFAULT_PORT = 7357

BUNDLE_NAME = "RCE.app"
BUNDLE_IDENTIFIER = "dev.researchos.rce"
EXECUTABLE_NAME = "RCE"


class MacAppError(Exception):
    """Bundle generation cannot proceed (currently: this interpreter has no
    `rce` entry point to point the launcher at). Message is user-facing;
    `rce.cli.cmd_app` re-raises it as `CliError`."""


def is_macos() -> bool:
    """Same check as `rce.webapp.server._is_macos` -- each subsystem owns
    its copy (existing convention); public here because the caller that
    gates on it (`rce.cli.cmd_app`) lives in another module."""
    return sys.platform == "darwin"


def resolve_rce_executable() -> Path:
    """The ABSOLUTE path of the current interpreter's `rce` console script:
    `sys.executable`'s own bin directory joined with `rce` -- the venv (or
    system prefix) this very process runs from, so the generated launcher
    always starts the same install that generated it. Deliberately NOT
    `Path.resolve()`d: `sys.executable` is already absolute, and a venv's
    `bin/python` is typically a symlink to the base interpreter -- resolving
    it would walk out of the venv into a bin dir that has no `rce` at all
    (a real failure on this repo's own `.venv`, whose python links to a
    conda install). Missing means this interpreter has no rce entry point;
    refuse rather than fall back to `$PATH` lookup (module docstring)."""
    candidate = Path(sys.executable).parent / "rce"
    if not candidate.is_file():
        raise MacAppError(
            f"no 'rce' entry point next to this interpreter ({candidate} does not exist); "
            f"install rce into this environment first (e.g. 'pip install -e .' in the repo)"
        )
    return candidate


def launcher_script(rce_executable: Path, port: int = DEFAULT_PORT) -> str:
    """The launcher's bash source. Fixed text except for the one
    `shlex.quote`d rce path (module docstring's injection note); every
    later use expands `"$RCE"`/`"$URL"`/`"$LOG"` double-quoted.

    Flow, matching the spec exactly: a `curl -sf` probe of
    `/api/summary` decides between "already running -- just open the
    page" and "start `rce serve` detached (nohup, log appended to
    ~/.rce/serve.log), poll the same URL up to ~10s (40 x 0.25s), then
    open it". The one deliberate addition: if the server never answers
    within the budget, the launcher opens the *log* instead of the URL --
    a browser's connection-refused page says nothing, while the log
    carries the server's actual startup error (e.g. the empty-registry
    message a first-ever run prints)."""
    quoted_rce = shlex.quote(str(rce_executable))
    return f"""#!/bin/bash
# Generated by 'rce app' -- double-clickable launcher for the RCE web app.
# The rce path below is baked in at generation time; re-run 'rce app'
# after moving or recreating the environment it points into.
set -u

URL='http://127.0.0.1:{port}'
RCE={quoted_rce}
LOG="$HOME/.rce/serve.log"

# Already running? Just open the page -- never start a second server.
if curl -sf "$URL/api/summary" > /dev/null 2>&1; then
  exec open "$URL"
fi

mkdir -p "$HOME/.rce"
nohup "$RCE" serve --port {port} --no-browser >> "$LOG" 2>&1 &

# Poll up to ~10s (40 x 0.25s) for the server to come up.
for _ in {{1..40}}; do
  sleep 0.25
  if curl -sf "$URL/api/summary" > /dev/null 2>&1; then
    exec open "$URL"
  fi
done

# Never came up -- surface the log (the real error), not a browser's
# connection-refused page.
open "$LOG"
exit 1
"""


def _info_plist(port: int) -> dict:
    """The minimum Info.plist for macOS to treat the directory as an app.
    `LSUIElement` true because this is a launcher that runs and exits --
    no Dock icon, no bounce, no app-switcher entry (spec requirement).
    `port` is recorded informationally so a human inspecting the bundle
    can see which server it probes without reading the script."""
    return {
        "CFBundleName": "RCE",
        "CFBundleIdentifier": BUNDLE_IDENTIFIER,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSUIElement": True,
        "RCEServerPort": port,
    }


def generate_bundle(
    target_dir: Path,
    rce_executable: Path | None = None,
    port: int = DEFAULT_PORT,
) -> Path:
    """Write (or overwrite -- regeneration is the reinstall story) the
    `RCE.app` bundle under `target_dir` and return the bundle's path.
    `rce_executable` defaults to `resolve_rce_executable()` -- the current
    interpreter's own entry point; injectable so tests can bake in a known
    path without monkeypatching `sys.executable`."""
    if rce_executable is None:
        rce_executable = resolve_rce_executable()
    bundle = target_dir / BUNDLE_NAME
    macos_dir = bundle / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)

    with (bundle / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump(_info_plist(port), fh)

    executable = macos_dir / EXECUTABLE_NAME
    executable.write_text(launcher_script(rce_executable, port), encoding="utf-8")
    executable.chmod(0o755)
    return bundle
