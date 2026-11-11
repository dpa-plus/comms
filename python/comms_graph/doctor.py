"""``comms-graph doctor``: says whether this install is stale, and fixes what it can.

Every path comes from ``HOME`` so a test (or a second account) can point it at
an empty directory. Nothing here imports the claim machinery: doctor must work
when the rest of the install is what is broken.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from comms_graph import __version__

LABEL = "plus.dpa.comms-ui"
HOOK_MATCHER = "Edit|Write|NotebookEdit"
DEFAULT_BOARD_URL = "http://127.0.0.1:7878"
SKILL_NAME = "using-comms"
#: (event, matcher, verb). `comms-graph hook <verb>` is what each runs.
HOOKS = (
    ("SessionStart", None, "session-start"),
    ("PreToolUse", HOOK_MATCHER, "pre-edit"),
    ("SessionEnd", None, "session-end"),
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    command: str = ""
    fixer: object = None
    changed: list[str] = field(default_factory=list)


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _bin_dir() -> Path:
    """Where this install's console scripts live (the venv's bin)."""
    return Path(sys.executable).parent


def _script(name: str) -> str:
    return str(_bin_dir() / name)


def _packaged_skill() -> bytes:
    return (resources.files("comms_graph") / "data" / SKILL_NAME / "SKILL.md").read_bytes()


def _skill_targets() -> list[Path]:
    home = _home()
    return [home / ".claude" / "skills" / SKILL_NAME / "SKILL.md",
            home / ".codex" / "skills" / SKILL_NAME / "SKILL.md"]


def _run(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------------------
# PATH
# ---------------------------------------------------------------------------


def _version_of(path: str) -> str:
    done = _run([path, "version"])
    if done is None or done.returncode != 0:
        return ""
    m = re.search(r"\b(\d+\.\d+\.\d+\S*)", done.stdout)
    return m.group(1) if m else ""


def _link_dir() -> Path:
    return _home() / ".local" / "bin"


def _link_cmd(name: str) -> str:
    return f"ln -sf {shlex.quote(_script(name))} {shlex.quote(str(_link_dir() / name))}"


def _check_path(name: str) -> Check:
    found = shutil.which(name)
    if not found:
        return Check(f"{name} on PATH", False, "not found on PATH", _link_cmd(name),
                     lambda: _fix_link(name))
    got = _version_of(found)
    if got != __version__:
        return Check(f"{name} on PATH", False,
                     f"{found} is {got or 'not runnable'}, this install is {__version__}",
                     _link_cmd(name), lambda: _fix_link(name))
    return Check(f"{name} on PATH", True, f"{found} is {got}")


def _fix_link(name: str) -> list[str]:
    target = _link_dir() / name
    if target.exists() and not target.is_symlink():
        return [f"left {target} alone: it is a real file, not a link comms made"]
    if not Path(_script(name)).exists():
        return [f"cannot link {name}: {_script(name)} does not exist"]
    _link_dir().mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    target.symlink_to(_script(name))
    return [f"linked {target} -> {_script(name)}"]


# ---------------------------------------------------------------------------
# Claude Code hooks
# ---------------------------------------------------------------------------


def _settings_path() -> Path:
    return _home() / ".claude" / "settings.json"


def _expected_command(verb: str) -> str:
    return f"{shlex.quote(_script('comms-graph'))} hook {verb}"


def _read_settings() -> tuple[dict | None, str]:
    path = _settings_path()
    if not path.exists():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path} is not readable JSON ({exc})"
    if not isinstance(data, dict):
        return None, f"{path} is not a JSON object"
    return data, ""


def _entries(settings: dict, event: str) -> list:
    hooks = settings.get("hooks")
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    return entries if isinstance(entries, list) else []


def _commands(entry: object) -> list[str]:
    inner = entry.get("hooks") if isinstance(entry, dict) else None
    if not isinstance(inner, list):
        return []
    return [h["command"] for h in inner
            if isinstance(h, dict) and isinstance(h.get("command"), str)]


def _hook_ok(settings: dict, event: str, matcher: str | None, verb: str) -> bool:
    want = _expected_command(verb)
    for entry in _entries(settings, event):
        if isinstance(entry, dict) and entry.get("matcher") == matcher \
                and want in _commands(entry):
            return True
    return False


def _fix_hint() -> str:
    return f"{shlex.quote(_script('comms-graph'))} doctor --fix"


def _check_hooks() -> Check:
    settings, why = _read_settings()
    if settings is None:
        return Check("claude hooks", False, why, "fix the JSON by hand, then run doctor --fix")
    bad_shape = _malformed(settings)
    if bad_shape:
        return Check("claude hooks", False, bad_shape,
                     f"repair {_settings_path()} by hand, then run doctor --fix")
    missing = [f"{event}" for event, matcher, verb in HOOKS
               if not _hook_ok(settings, event, matcher, verb)]
    stale = any(_runs_comms(c) for event, _m, _v in HOOKS
                for entry in _entries(settings, event) for c in _commands(entry)
                if c not in {_expected_command(v) for _e, _mm, v in HOOKS})
    if not missing and not stale:
        return Check("claude hooks", True, f"{_settings_path()} points at this install")
    detail = (f"missing or wrong: {', '.join(missing)}" if missing
              else "an old comms hook command is still present")
    return Check("claude hooks", False, detail, _fix_hint(), _fix_hooks)


_PROGRAMS = frozenset({"comms", "comms-graph"})
_SEPARATORS = frozenset({"&&", "||", ";", "|"})


def _runs_comms(command: str) -> bool:
    """Is comms the program of some command in this line, not just a word in it?"""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    starts = True
    for token in tokens:
        if token in _SEPARATORS:
            starts = True
            continue
        if starts and os.path.basename(token) in _PROGRAMS:
            return True
        starts = False
    return False


def _is_comms_hook(h: object) -> bool:
    return (isinstance(h, dict) and isinstance(h.get("command"), str)
            and _runs_comms(h["command"]))


def _malformed(settings: dict) -> str:
    """Why the hooks in ``settings`` are not a shape doctor can safely rewrite."""
    hooks = settings.get("hooks")
    if hooks is None:
        return ""
    if not isinstance(hooks, dict):
        return '"hooks" is not an object'
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            return f'"hooks.{event}" is not a list'
        for entry in entries:
            if isinstance(entry, dict) and not isinstance(entry.get("hooks", []), list):
                return f'a "hooks" value under "{event}" is not a list'
    return ""


def _rewrite_hooks(settings: dict) -> dict:
    """Drop every comms hook, keep all other hooks, add the three we want."""
    hooks = settings.setdefault("hooks", {})
    for event, matcher, verb in HOOKS:
        kept: list = []
        for entry in hooks.get(event, []):
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            if isinstance(inner, list) and any(_is_comms_hook(h) for h in inner):
                rest = [h for h in inner if not _is_comms_hook(h)]
                if not rest:
                    continue
                entry = {**entry, "hooks": rest}
            kept.append(entry)
        new = {"hooks": [{"type": "command", "command": _expected_command(verb)}]}
        if matcher:
            new = {"matcher": matcher, **new}
        kept.append(new)
        hooks[event] = kept
    return settings


def _indent_of(text: str) -> int | str:
    m = re.search(r"^([ \t]+)\S", text, re.M)
    if not m:
        return 2
    return m.group(1) if "\t" in m.group(1) else len(m.group(1))


def _fix_hooks() -> list[str]:
    path = _settings_path()
    settings, why = _read_settings()
    if settings is None:
        return [why]
    if _malformed(settings):
        return [f"left {path} alone: {_malformed(settings)}"]
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = _rewrite_hooks(json.loads(raw) if raw.strip() else {})
    text = json.dumps(updated, indent=_indent_of(raw), ensure_ascii=False) + "\n"
    out: list[str] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    # A dotfiles setup links settings.json elsewhere: write the file the link
    # names, so the link survives and the real file gets the hooks.
    path = path.resolve()
    if path.exists():
        stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
        out.append(f"backed up settings to {backup}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    out.append(f"wrote the SessionStart, PreToolUse and SessionEnd hooks to {path}")
    return out


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------


def _check_skills() -> Check:
    want = _packaged_skill()
    bad = []
    for target in _skill_targets():
        try:
            same = target.read_bytes() == want
        except OSError:
            same = False
        if not same:
            bad.append(target)
    if not bad:
        return Check("skill copies", True, "both copies match the packaged skill")
    cmds = " && ".join(f"mkdir -p {shlex.quote(str(t.parent))} && "
                       f"cp {shlex.quote(str(_packaged_path()))} {shlex.quote(str(t))}"
                       for t in bad)
    return Check("skill copies", False,
                 "stale or missing: " + ", ".join(str(t) for t in bad), cmds, _fix_skills)


def _packaged_path() -> Path:
    return Path(str(resources.files("comms_graph") / "data" / SKILL_NAME / "SKILL.md"))


def _fix_skills() -> list[str]:
    want = _packaged_skill()
    out = []
    for target in _skill_targets():
        try:
            if target.read_bytes() == want:
                continue
        except OSError:
            pass
        if target.is_symlink() or target.parent.is_symlink():
            out.append(f"left {target} alone: it is a symlink, so writing would change what it points at")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(want)
        out.append(f"copied the skill to {target}")
    return out


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------


def _board_url() -> str:
    return os.environ.get("COMMS_BOARD_URL", DEFAULT_BOARD_URL).rstrip("/")


def _plist_path() -> Path:
    return _home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _uid() -> int:
    return os.getuid()


def _board_version() -> tuple[str, str]:
    try:
        with urllib.request.urlopen(_board_url() + "/api/status", timeout=2) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return "", f"{_board_url()} did not answer ({getattr(exc, 'reason', exc)})"
    return str(payload.get("version", "")), ""


def _plist_text() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_script('comms-graph')}</string>
        <string>ui</string>
        <string>--no-open</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>COMMS_ACTOR</key>
        <string>operator</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>/tmp/comms-ui.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/comms-ui.log</string>
</dict>
</plist>
"""


def _check_board(install_board: bool) -> Check:
    if sys.platform != "darwin" and not shutil.which("launchctl"):
        return Check("board job", True, "launchd is macOS only, skipped")
    plist = _plist_path()
    domain = f"gui/{_uid()}"
    loaded = _run(["launchctl", "list", LABEL])
    is_loaded = loaded is not None and loaded.returncode == 0
    if not plist.exists():
        return Check("board job", False, f"no {plist} for {LABEL}",
                     f"{_fix_hint()} --board", lambda: _fix_board(True) if install_board else [])
    got, why = _board_version()
    if is_loaded and got == __version__:
        return Check("board job", True, f"{LABEL} loaded, board reports {got}")
    detail = (f"{LABEL} is not loaded" if not is_loaded
              else why or f"board runs {got or 'an unversioned build'}, this install is {__version__}")
    cmd = (f"launchctl kickstart -k {domain}/{LABEL}" if is_loaded
           else f"launchctl bootstrap {domain} {shlex.quote(str(plist))}")
    return Check("board job", False, detail, cmd, lambda: _fix_board(False))


def _fix_board(write_plist: bool) -> list[str]:
    plist = _plist_path()
    domain = f"gui/{_uid()}"
    out = []
    if write_plist:
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(_plist_text(), encoding="utf-8")
        out.append(f"wrote {plist}")
    if not plist.exists():
        return out + ["cannot start the board: no plist"]
    loaded = _run(["launchctl", "list", LABEL])
    if loaded is not None and loaded.returncode == 0:
        done = _run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"])
        out.append("restarted the board job" if done and done.returncode == 0
                   else "launchctl kickstart failed")
    else:
        done = _run(["launchctl", "bootstrap", domain, str(plist)])
        out.append("loaded the board job" if done and done.returncode == 0
                   else "launchctl bootstrap failed")
    return out


# ---------------------------------------------------------------------------
# data home and commit guard
# ---------------------------------------------------------------------------


def _data_home() -> Path:
    from comms_graph import log as _log
    return _log.user_data_home()


def _writable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def _check_data_home() -> Check:
    try:
        home = _data_home()
    except RuntimeError as exc:
        return Check("data home", False, str(exc))
    if _writable(home):
        return Check("data home", True, f"{home} is writable")
    return Check("data home", False, f"{home} is missing or not writable",
                 f"mkdir -p {shlex.quote(str(home))}", lambda: _fix_data_home(home))


def _fix_data_home(home: Path) -> list[str]:
    home.mkdir(parents=True, exist_ok=True)
    return [f"created {home}"]


def _check_guard() -> Check:
    from comms_graph import guard
    done = _run(["git", "rev-parse", "--show-toplevel"])
    if done is None or done.returncode != 0:
        return Check("commit guard", True, "not inside a git repo, skipped")
    st = guard.status(Path(done.stdout.strip()))
    if st.installed:
        return Check("commit guard", True, f"installed at {st.path}")
    return Check("commit guard", False,
                 st.reason or f"no comms commit guard in {done.stdout.strip()}",
                 f"{shlex.quote(_script('comms-graph'))} guard install")


# ---------------------------------------------------------------------------


def run_checks(install_board: bool = False) -> list[Check]:
    return [_check_path("comms-graph"), _check_path("comms"), _check_hooks(),
            _check_skills(), _check_board(install_board), _check_data_home(),
            _check_guard()]


def _print(checks: list[Check]) -> None:
    for c in checks:
        mark = "ok " if c.ok else "fix"
        print(f"{mark}  {c.name}: {c.detail}")
        if not c.ok and c.command:
            print(f"     run: {c.command}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        print("usage: comms-graph doctor [--fix] [--board]\n"
              "  Checks PATH, Claude Code hooks, skill copies, the board job, the data home\n"
              "  and the commit guard. --fix applies what it can; --board also installs the\n"
              "  launchd board job. Exit 0 when all ok, 1 when something needs fixing.")
        return 0
    unknown = [a for a in args if a not in ("--fix", "--board")]
    if unknown:
        print(f"error: doctor does not take {unknown[0]!r}; try: comms-graph doctor --fix",
              file=sys.stderr)
        return 2
    fix = "--fix" in args
    checks = run_checks(install_board="--board" in args)
    _print(checks)
    if fix:
        changed = False
        for c in checks:
            if c.ok or not callable(c.fixer):
                continue
            for line in c.fixer():
                changed = True
                print(f"fixed  {line}")
        if changed:
            print("--- after fix ---")
            checks = run_checks(install_board="--board" in args)
            _print(checks)
        else:
            print("nothing to change")
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
