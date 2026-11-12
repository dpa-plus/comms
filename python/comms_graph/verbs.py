"""The verb table: the one place that says what the command line accepts.

The dispatcher, the global usage, per-verb ``--help`` and the unknown-verb and
unknown-flag hints are all built from ``VERBS``. A flag a verb reads that is
missing here is refused as unknown, so the table lists every flag the code reads.

Pure data and string building: no imports from the rest of the package, so it
costs nothing on the hook path.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

EXIT_USAGE = 2

_FLAG = re.compile(r"^--([A-Za-z][A-Za-z0-9_-]*)(=.*)?$", re.S)
_HELP = ("-h", "--help", "-?")


@dataclass(frozen=True)
class Flag:
    name: str
    arg: str = ""
    doc: str = ""
    aliases: tuple[str, ...] = ()
    needs: str = ""
    hint: str = ""
    retired: bool = False


@dataclass(frozen=True)
class Verb:
    name: str
    summary: str
    usage: tuple[str, ...] = ()
    flags: tuple[Flag, ...] = ()
    aliases: tuple[str, ...] = ()
    writes: bool = False
    subs: tuple["Verb", ...] = ()
    # An alias that runs another verb: the argv it stands for.
    expands: tuple[str, ...] = ()
    # The verb parses its own argv; global flags before it are dropped, not moved.
    raw: bool = False
    default_sub: str = ""
    # False for a verb that reads no positional words, so a stray one is refused.
    words: bool = True
    retired: bool = False


class Refusal(Exception):
    """A call the table refuses. ``lines`` is the whole answer, three lines at most."""

    def __init__(self, lines: list[str], code: int = EXIT_USAGE):
        super().__init__(lines[0])
        self.lines = lines
        self.code = code


@dataclass
class Call:
    verb: Verb
    path: str
    argv: list[str]
    root: str = ""
    help: bool = False
    retired: bool = False


GLOBAL_FLAGS = (
    Flag("as", "<actor>", "who you are", aliases=("actor",)),
    Flag("root", "<path>", "repo root (default: the git root above the cwd)", aliases=("repo",)),
    Flag("graph", "<path>", "graph.json (default: <root>/graphify-out/graph.json)"),
    Flag("json", "", "machine-readable output"),
)
_GLOBAL_BY_NAME = {f.name: f for f in GLOBAL_FLAGS}
_GLOBAL_ALIASES = {a: f for f in GLOBAL_FLAGS for a in f.aliases}

_ACTOR = "who you are; else COMMS_ACTOR, else this session's own name"

# Grace for one release. A verb or flag marked `retired` is accepted, does
# nothing and prints this line on stderr. The next release deletes the marks,
# the retired entries and this notice together.
RETIRED_NOTICE = "comms no longer has tasks; this command or flag does nothing and can be dropped."

VERBS: tuple[Verb, ...] = (
    Verb("claim", "take ground before you edit it", writes=True,
         usage=('claim <scope>... [--intent "..."]',
                '  claim <scope> --steal <claim-id> --reason "..."   take it off a holder who is gone',
                "  <scope> is a path, optionally anchored: src/foo.py  src/foo.py#parse  src/foo.py#L20-48"),
         flags=(
             Flag("intent", "<text>", "one line on what you are about to do", aliases=("why",)),
             Flag("task", "<id>", retired=True),
             Flag("steal", "<claim-id>", "displace the holder of this claim"),
             Flag("reason", "<text>", "why the holder is not coming back", needs="steal",
                  hint='To say why you are claiming, use --intent "...".'),
             Flag("map", "", "also print how this ground sits on the code map"),
         )),
    Verb("release", "give ground back", writes=True,
         usage=('release [<scope|claim-id>...] [--result "..."]   no scope: everything you hold',
                '  release <claim-id> --force --reason "..."   free ground somebody else abandoned'),
         flags=(
             Flag("result", "<text>", "one line on how it went"),
             Flag("all-mine", "", "release everything you hold (the default with no scope)"),
             Flag("latest", "", "release only your most recent claim"),
             Flag("task", "<id>", retired=True),
             Flag("force", "", "end a claim that is not yours (needs a claim id)"),
             Flag("reason", "<text>", "why it is not theirs to hold", needs="force",
                  hint='To say how it went, use --result "...".'),
         )),
    Verb("board", "your claims, and how many files others hold", words=False,
         usage=("board [--all]",),
         flags=(Flag("all", "", "every claim, the way it used to print"),)),
    Verb("check", "would an edit or a commit collide?",
         usage=("check <path>...                 would an edit here collide? exit 2 if any is held",
                "check --stdin-json              read a Claude Code hook payload (use `hook pre-edit`)",
                "check --staged                  would this COMMIT take somebody else's claimed work?"),
         flags=(
             Flag("staged", "", "check the git index instead of paths"),
             Flag("stdin-json", "", "read a Claude Code hook payload on stdin"),
         )),
    Verb("note", "a short FYI, up to 1000 characters", writes=True,
         usage=('note "..."                     start with @name to address one agent',)),
    Verb("doc", "the repo's own wiki", usage=("doc [--list | <slug> [--edit]]",),
         flags=(Flag("list", "", "list the docs"), Flag("edit", "", "open the doc in $EDITOR"))),
    Verb("session", "named coordination windows",
         usage=("session start|join|end|retire|lead ...",),
         subs=(
             Verb("start", "create a named session and join it", writes=True,
                  usage=('session start "<name>" [--label "..."]',),
                  flags=(Flag("label", "<text>", "display name for this actor"),)),
             Verb("join", "join one somebody else started", writes=True,
                  usage=('session join "<name>" [--label "..."]',),
                  flags=(Flag("label", "<text>", "display name for this actor"),)),
             Verb("end", "close it and release its claims", writes=True,
                  usage=('session end "<name>" [--reason "..."]',),
                  flags=(Flag("reason", "<text>", "why, in the log, under your name"),)),
             Verb("retire", "take an actor off the roster and free what it holds", writes=True,
                  usage=('session retire <actor> [--reason "..."]',),
                  flags=(Flag("reason", "<text>", "why, in the log, under your name"),)),
             Verb("lead", "hand leadership to one active actor", writes=True,
                  usage=('session lead [<actor>] [--reason "..."]',),
                  flags=(Flag("reason", "<text>", "why, in the log, under your name"),)),
         )),
    Verb("status", "your claims, who else holds what, notes to you", words=False,
         usage=("status [--all] [--since 24h]",),
         flags=(
             Flag("all", "", "everything: roster, every claim, notes"),
             Flag("since", "<dur>", "lookback for notes (default 24h; 30m, 6h, 7d)"),
             Flag("stale-after", "<dur>", "age past which a held claim is STALE (default 1h)"),
         )),
    Verb("log", "the history, filtered", words=False, usage=('log [--type claim,note] [--since 24h] [--limit 20]',),
         flags=(
             Flag("since", "<dur>", "lookback window (default 24h; 30m, 6h, 7d)"),
             Flag("type", "<a,b>", "only these event types"),
             Flag("limit", "<n>", "only the newest n events"),
             Flag("scope", "<path>", "only events whose scope overlaps this path"),
         )),
    Verb("hello", "announce yourself", writes=True,
         usage=('hello [<name>] [--label "..."] [--model <id>] [--vendor <name>]',),
         flags=(
             Flag("label", "<text>", "display name on the board"),
             Flag("model", "<id>", "the model you run on"),
             Flag("vendor", "<name>", "who made you (anthropic, openai)"),
         )),
    Verb("whoami", "who this call is, and where", usage=("whoami",), words=False),
    Verb("claims", "the ground you hold right now", aliases=("mine",), usage=("claims",),
         words=False),
    Verb("notes", "the notes log (same as log --type note, 7 days)",
         expands=("log", "--type", "note", "--since", "168h")),
    Verb("guard", "the commit check in this repo's git hooks",
         usage=("guard [install [--chain] | status | uninstall]",), default_sub="status",
         subs=(
             Verb("install", "put the commit check into this repo's git hooks",
                  flags=(Flag("chain", "", "keep a pre-commit hook that is already there"),)),
             Verb("status", "is it wired in, and where"),
             Verb("uninstall", "take it out, restoring any hook it displaced"),
         )),
    Verb("hook", "Claude Code hooks (JSON on stdin)", raw=True,
         usage=("hook session-start|pre-edit|session-end",),
         subs=(
             Verb("session-start", "print a short briefing when this repo uses comms", raw=True),
             Verb("pre-edit", "exit 2 blocks an edit of a file somebody else holds", raw=True),
             Verb("session-end", "release the session's automatic claims", raw=True),
         )),
    Verb("doctor", "check the install and fix what it can", raw=True, usage=("doctor [--fix] [--board]",),
         flags=(
             Flag("fix", "", "apply the fixes it can"),
             Flag("board", "", "also install the launchd board job"),
         )),
    Verb("mcp", "serve these as MCP tools", raw=True, usage=("mcp",)),
    Verb("ui", "live board: both graphs, live claims", words=False, usage=("ui [--port 7878]",),
         flags=(
             Flag("port", "<n>", "port to listen on (default 7878)"),
             Flag("host", "<addr>", "address to listen on (default 127.0.0.1)"),
             Flag("no-open", "", "accepted for old launchers; the board never opens a browser"),
         )),
    Verb("version", "print the version", usage=("version",)),
    Verb("help", "this list, or one command's flags", usage=("help [<command>]",)),
    Verb("task", "", retired=True),
    Verb("next", "", retired=True),
    Verb("brief", "", retired=True),
    Verb("tasks", "", retired=True),
)

_BY_NAME: dict[str, Verb] = {}
for _v in VERBS:
    _BY_NAME[_v.name] = _v
    for _a in _v.aliases:
        _BY_NAME[_a] = _v

_GUESSES = {
    "inbox": "notes", "messages": "notes",
    "unclaim": "release", "lock": "claim", "unlock": "release", "free": "release",
    "register": "hello", "join": "hello", "who": "whoami", "me": "whoami",
    "mine-all": "claims", "open": "claim",
}


def lookup(name: str) -> Verb | None:
    """The verb a name dispatches to. A retired verb is not one."""
    verb = _BY_NAME.get(name)
    return None if verb is None or verb.retired else verb


def leaves() -> list[tuple[str, Verb]]:
    """Every dispatchable (path, verb): ``guard install`` is one, ``guard`` alone is not."""
    out: list[tuple[str, Verb]] = []
    for v in VERBS:
        if v.retired:
            continue
        if v.subs:
            out.extend((f"{v.name} {s.name}", s) for s in v.subs)
        else:
            out.append((v.name, v))
    return out


def is_flag(arg: str) -> bool:
    return _FLAG.match(arg) is not None


def _flag_lookup(verb: Verb, name: str) -> Flag | None:
    for f in verb.flags:
        if f.name == name or name in f.aliases:
            return f
    return _GLOBAL_BY_NAME.get(name) or _GLOBAL_ALIASES.get(name)


def booleans(verb: Verb) -> frozenset[str]:
    return frozenset(f.name for f in (*verb.flags, *GLOBAL_FLAGS) if not f.arg)


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


def _shown_flags(verb: Verb) -> list[Flag]:
    return [f for f in verb.flags if not f.retired]


def _flag_line(f: Flag) -> str:
    head = f"--{f.name}" + (f" {f.arg}" if f.arg else "")
    also = f"  (also {', '.join('--' + a for a in f.aliases)})" if f.aliases else ""
    return f"    {head:<26}{f.doc}{also}"


def global_usage() -> str:
    lines = ["Usage: comms-graph <command> [options]      (`comms` is the same program)", ""]
    for v in VERBS:
        if v.retired:
            continue
        also = f"  (also {', '.join(v.aliases)})" if v.aliases else ""
        lines.append(f"  {v.name:<10}{v.summary}{also}")
    lines += [
        "",
        "  comms-graph <command> --help      that command's flags",
        "  Any position:  --as <actor>  --root <path>  --graph <path>  --json",
        "  --as: " + _ACTOR,
        "  Exit status: 0 ok, 1 blocked by somebody else's claim, 2 bad usage or error.",
    ]
    return "\n".join(lines)


def verb_help(verb: Verb, path: str | None = None) -> str:
    path = path or verb.name
    lines: list[str] = []
    if verb.subs and not verb.expands:
        lines.append(f"Usage: comms-graph {path} <command> [options]")
        lines.append(f"  {verb.summary}")
        for sub in verb.subs:
            lines.append(f"  {sub.name:<14}{sub.summary}")
        lines.append(f"  comms-graph {path} <command> --help   that command's flags")
        for sub in verb.subs:
            if sub.usage and not sub.flags:
                lines.extend("  " + u for u in sub.usage)
        if verb.default_sub:
            lines.append(f"  With no command: {verb.default_sub}.")
        return "\n".join(lines)
    first, *more = verb.usage or (path,)
    if not first.startswith(path.split()[0]):
        first = path
    lines.append(f"Usage: comms-graph {first}")
    lines.extend(f"  {u}" for u in more)
    lines.append(f"  {verb.summary}.")
    if _shown_flags(verb):
        lines.append("  Options:")
        lines.extend(_flag_line(f) for f in _shown_flags(verb))
    if not verb.raw:
        lines.append("  Also on every command: " + "  ".join(
            f"--{f.name}" + (f" {f.arg}" if f.arg else "") for f in GLOBAL_FLAGS))
        lines.append("  --as: " + _ACTOR)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Resolving a call
# ---------------------------------------------------------------------------


def _unknown_verb(name: str) -> Refusal:
    guess = _GUESSES.get(name)
    if not guess:
        close = difflib.get_close_matches(name, [n for n in _BY_NAME if lookup(n)], n=1, cutoff=0.6)
        guess = close[0] if close else ""
    lines = [f"error: unknown comms command {name!r}"]
    if guess:
        lines.append(f"  Did you mean `comms-graph {guess}`?")
    lines.append("  `comms-graph help` lists every command.")
    return Refusal(lines)


def _unknown_sub(verb: Verb, name: str) -> Refusal:
    subs = [s.name for s in verb.subs]
    close = difflib.get_close_matches(name, subs, n=1, cutoff=0.5)
    lines = [f"error: {verb.name} has no command {name!r}"]
    lines.append(f"  Did you mean `{verb.name} {close[0]}`?" if close
                 else f"  Commands: {', '.join(subs)}")
    lines.append(f"  `comms-graph {verb.name} --help` shows them.")
    return Refusal(lines)


def _unknown_flag(path: str, verb: Verb, name: str) -> Refusal:
    own = [f.name for f in _shown_flags(verb)]
    pool = own + [g.name for g in GLOBAL_FLAGS]
    pool += [a for f in (*_shown_flags(verb), *GLOBAL_FLAGS) for a in f.aliases]
    close = difflib.get_close_matches(name, pool, n=1, cutoff=0.6)
    lines = [f"error: {path} has no flag --{name}"]
    taken = " ".join(f"--{n}" for n in own) if own else "no flags of its own"
    lines.append((f"  Did you mean --{close[0]}? " if close else "  ") + f"{path} takes: {taken}")
    lines.append(f"  `comms-graph {path} --help` shows each one.")
    return Refusal(lines)


def _stray_word(path: str, word: str) -> Refusal:
    lines = [f"error: {path} takes no arguments, got {word!r}"]
    lines.append(f"  `comms-graph {path} --help` shows its flags.")
    return Refusal(lines)


def check_flags(path: str, verb: Verb, args: list[str]) -> tuple[list[str], bool]:
    """``args`` with aliases spelled canonically and retired flags dropped, and
    whether a retired flag was dropped. Raises Refusal for an unknown flag."""
    out: list[str] = []
    seen: set[str] = set()
    retired = False
    i = 0
    while i < len(args):
        tok = args[i]
        m = _FLAG.match(tok)
        i += 1
        if m is None:
            if not verb.words:
                raise _stray_word(path, tok)
            out.append(tok)
            continue
        flag = _flag_lookup(verb, m.group(1))
        if flag is None:
            raise _unknown_flag(path, verb, m.group(1))
        if flag.retired:
            retired = True
            if flag.arg and m.group(2) is None and i < len(args) and not _FLAG.match(args[i]):
                i += 1
            continue
        seen.add(flag.name)
        out.append(f"--{flag.name}{m.group(2) or ''}")
        if flag.arg and m.group(2) is None:
            if i >= len(args) or _FLAG.match(args[i]):
                raise Refusal([f"error: --{m.group(1)} needs a value",
                               f"  `comms-graph {path} --help` shows each flag."])
            out.append(args[i])
            i += 1
    for f in verb.flags:
        if f.name in seen and f.needs and f.needs not in seen:
            lines = [f"error: {path}: --{f.name} only goes with --{f.needs}"]
            if f.hint:
                lines.append(f"  {f.hint}")
            lines.append(f"  `comms-graph {path} --help` shows each flag.")
            raise Refusal(lines)
    return out, retired


def _split_globals(argv: list[str]) -> tuple[list[str], list[str], str]:
    """Leading global flags, the rest, and a global --root/--repo if one led."""
    lead: list[str] = []
    root = ""
    i = 0
    while i < len(argv):
        m = _FLAG.match(argv[i])
        flag = _flag_lookup(Verb("", ""), m.group(1)) if m else None
        if flag is None:
            break
        value = ""
        if m.group(2) is not None:
            value = m.group(2)[1:]
            i += 1
        elif flag.arg:
            if i + 1 >= len(argv):
                raise Refusal([f"error: --{m.group(1)} needs a "
                               + ("path" if flag.name in ("root", "graph") else "value")])
            value = argv[i + 1]
            i += 2
        else:
            i += 1
        if flag.name == "root":
            root = value
        elif flag.arg:
            lead += [f"--{flag.name}", value]
        else:
            lead.append(f"--{flag.name}")
    return lead, argv[i:], root


def prepare(argv: list[str]) -> Call:
    """Turn the raw argv into one validated Call, or raise Refusal."""
    lead, argv, root = _split_globals(list(argv))
    if not argv:
        return Call(_BY_NAME["help"], "help", [], root=root)
    name, rest = argv[0], argv[1:]
    if name in ("-h", "--help"):
        return Call(_BY_NAME["help"], "help", [], root=root)
    if name in ("-V", "--version"):
        return Call(_BY_NAME["version"], "version", [], root=root)
    verb = _BY_NAME.get(name)
    if verb is None:
        raise _unknown_verb(name)
    if verb.retired:
        return Call(verb, verb.name, [], root=root, retired=True)
    path = verb.name
    for _ in range(3):
        if verb.expands:
            rest = [*verb.expands[1:], *rest]
            verb = _BY_NAME[verb.expands[0]]
            path = verb.name
            continue
        if not verb.subs:
            break
        if rest and rest[0] in _HELP:
            break
        sub = next((s for s in verb.subs if rest and s.name == rest[0]), None)
        if sub is None:
            if rest and not is_flag(rest[0]):
                raise _unknown_sub(verb, rest[0])
            if verb.default_sub:
                sub = next(s for s in verb.subs if s.name == verb.default_sub)
                path = f"{verb.name} {sub.name}"
                verb = sub
                break
            raise Refusal([f"error: {verb.name} needs a command: "
                           + ", ".join(s.name for s in verb.subs),
                           f"  `comms-graph {verb.name} --help` shows them."])
        path = f"{verb.name} {sub.name}"
        verb, rest = sub, rest[1:]
        if verb.expands:
            continue
        break
    if any(a in _HELP for a in rest):
        return Call(verb, path, [], root=root, help=True)
    rest, retired = check_flags(path, verb, rest)
    if verb.raw:
        lead = []
    return Call(verb, path, [*rest, *lead], root=root, retired=retired)
