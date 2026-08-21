"""The append-only event log: the one piece of comms state that is TRUTH.

Ported from comms (Go) ``internal/event`` — ``event.go`` + ``log.go``.

Every event is a single line of canonical JSON in a JSONL file. The shape is
intentionally narrow: ts + id + actor + type + optional scope + a type-specific
``data`` bag. A reducer interprets the stream to compute active claims and
recent activity; this module only writes lines and hands them back.

The log deliberately does NOT live in ``graph.json``. The map is derived and is
rewritten whole-file on every update (and deleted outright by ``graphify
uninstall --purge``); the log is truth and must survive both. So it gets its own
per-machine store, keyed by a hash of the repo root, exactly where the Go comms
keeps it — which also means this reader can open the log an installed ``comms``
binary has already been writing.

Python 3.11 is a hard floor and not an accident: every ``ts`` in the log is
RFC3339 with a trailing ``Z``, and ``datetime.fromisoformat`` rejects that
before 3.11. As a bonus 3.11 also accepts Go's nanosecond fractions (it
truncates them to microseconds) — pre-3.11 needed hand-rolled parsing for both.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import secrets
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------
# Event types
# --------------------------------------------------------------------------

TYPE_HELLO = "hello"
TYPE_CLAIM = "claim"
TYPE_RELEASE = "release"
TYPE_NOTE = "note"
TYPE_FINDING = "finding"

# The task graph. A task is what should happen; an edge is the order it must
# happen in and what the later task consumes from the earlier one; a state event
# moves a task along its two steps — do it, then a DIFFERENT agent verifies it.
TYPE_TASK = "task"
TYPE_TASK_EDGE = "task_edge"
TYPE_TASK_STATE = "task_state"

# TYPE_BLOCKED records a claim comms REFUSED because someone else held an
# overlapping scope. It is the only moment that proves the tool did its job, and
# for a long time it was the one moment nothing wrote down: the conflict went to
# stderr and the process exited. That is why a store of 4,356 claims reported
# zero collisions ever prevented — not because none were, but because a
# prevented one left no trace.
TYPE_BLOCKED = "blocked"

# Ordered on purpose: this tuple is the single list of types this build
# understands, so flag help, validation errors and the reader's tolerance check
# all read from the same place. The Go port had two hand-maintained whitelists
# in two packages and adding a type meant remembering both.
KNOWN_TYPES: tuple[str, ...] = (
    TYPE_HELLO,
    TYPE_CLAIM,
    TYPE_RELEASE,
    TYPE_NOTE,
    TYPE_FINDING,
    TYPE_TASK,
    TYPE_TASK_EDGE,
    TYPE_TASK_STATE,
    TYPE_BLOCKED,
)

_KNOWN_TYPE_SET = frozenset(KNOWN_TYPES)


def is_known_type(t: object) -> bool:
    """Report whether ``t`` is one of the event types this build understands."""
    return isinstance(t, str) and t in _KNOWN_TYPE_SET


def known_types() -> str:
    """The known types, comma separated, for flag help and error messages."""
    return ",".join(KNOWN_TYPES)


class UnknownEventTypeError(ValueError):
    """A decode failure whose only cause is a type this build does not know.

    The line is well formed; this build is simply older than whatever wrote it.

    READERS skip such a line and keep going (see :func:`read`); WRITERS still
    refuse to emit one (see :meth:`Event.encode`). That asymmetry is the whole
    point: a build must never author a type it cannot fold, but it must survive
    meeting one. Without it, adding a tenth event type would brick every older
    install on the machine — one unrecognised line made the read abort, so
    status, log, claim, note and the pre-edit check hook all failed on that
    repository at once.

    Ship the tolerant reader, let it reach every machine, and only then add the
    type.
    """


class CorruptLogError(Exception):
    """An unrecoverable parse error mid-log. Callers map this to exit code 2.

    Distinct from :class:`UnknownEventTypeError` on purpose: the reader skips
    the one and aborts on the other. Tolerating a newer writer is not the same
    as tolerating a broken file — a log we cannot parse is not a log we should
    silently act on.
    """

    def __init__(self, path: str | os.PathLike[str], line: int, cause: object) -> None:
        self.path = os.fspath(path)
        self.line = line
        self.cause = cause
        super().__init__(f"event log {self.path}: corrupt at line {line}: {cause}")


# --------------------------------------------------------------------------
# Event
# --------------------------------------------------------------------------

# Go unmarshals every JSON number as float64, Python gives int for an integer
# literal. Rather than leave callers guessing which they hold, both boundaries
# normalise the same way: an integral float becomes an int. That direction (and
# not the reverse) is chosen because it is what re-encoding produces anyway —
# Go marshals float64(1) back as `1`, and Python's json writes int 1 as `1`,
# while a float 1.0 would come out as `1.0` and change the bytes of a line that
# round-trips. Only values that survive the trip exactly are converted: beyond
# 2**53 a float64 no longer holds every integer, so converting there would
# invent precision the log never had.
_EXACT_INT_FLOAT = 2**53


def _normalize(value: Any) -> Any:
    """Sort dict keys and normalise numbers so equal events encode to equal bytes.

    Key sorting mirrors Go, whose json.Marshal sorts map keys; without it two
    processes could write byte-different lines for the same event, which makes
    logs pointlessly hard to diff and dedupe by eye.
    """
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    # bool is a subclass of int; leave it alone or True becomes 1.
    if isinstance(value, float) and not isinstance(value, bool):
        if value.is_integer() and abs(value) <= _EXACT_INT_FLOAT:
            return int(value)
    return value


# Go's encoding/json escapes these by default (SetEscapeHTML), so a note whose
# body says "login->dash" lands in the log as "login->dash". We match it
# because both writers append to the SAME file: without this, 377 of the 8,848
# lines in a real store would be byte-different depending on which binary wrote
# them, for no semantic difference at all — and any future dedupe or checksum
# over raw lines would see two spellings of one event. Safe as a post-pass:
# none of these characters is structural in JSON, so they can only occur inside
# string literals. U+2028/U+2029 are in the set because Go escapes them too
# (they terminate a line in JavaScript and would break a JSONL consumer that
# splits on Unicode line boundaries).
_GO_ESCAPES = (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
               ("\u2028", "\\u2028"), ("\u2029", "\\u2029"))


def _escape_like_go(line: str) -> str:
    for raw, esc in _GO_ESCAPES:
        if raw in line:
            line = line.replace(raw, esc)
    return line


# The one actionable sentence for a writer that handed us a naive datetime.
# Kept in a constant so encode() and new_id() cannot drift into saying different
# things about the same mistake.
_NAIVE_TS_HINT = (
    "pass an aware datetime (datetime.now(timezone.utc)); a naive local "
    "datetime.now() would be recorded as if it were UTC and land the event "
    "hours away from every other writer's"
)


def _is_naive(ts: datetime) -> bool:
    """True when ``ts`` names a wall clock rather than an instant.

    ``utcoffset() is None`` counts as naive too: a tzinfo may be attached and
    still decline to resolve an offset for this particular instant, which
    leaves the value exactly as uncomparable as a bare datetime.
    """
    return ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None


def _to_utc(ts: datetime) -> datetime:
    """Convert an AWARE datetime to UTC. A naive one is an error, never a guess.

    The log records INSTANTS that several processes on several machines order
    against each other, so a wall-clock reading with no zone attached is not a
    timestamp — it is a timestamp minus the one fact that makes it comparable.

    WHAT WENT WRONG BEFORE (do not reintroduce): this used to read a naive
    datetime as UTC via ``replace(tzinfo=utc)``. That is the silent-wrong
    default. Every caller reaching for a timestamp writes ``datetime.now()``
    at least once, and ``datetime.now()`` is naive LOCAL time, so the event
    landed in the log shifted by the machine's UTC offset — two hours into the
    future from Berlin, eight hours into the past from California. Nothing
    failed, no line was malformed; claims simply expired early or late and
    events interleaved in the wrong causal order. Refusing the guess turns a
    silent hours-wide skew into an immediate, obvious ValueError at the call
    site that got it wrong.

    The message here is deliberately audience-neutral, because both sides call
    it: writers add the "pass datetime.now(timezone.utc)" hint themselves, and
    that hint would be nonsense wrapped around a bad line read off disk.
    """
    if _is_naive(ts):
        raise ValueError(
            f"timestamp {ts!r} carries no UTC offset; the log stores instants, "
            "not wall clocks"
        )
    try:
        return ts.astimezone(timezone.utc)
    except (OverflowError, OSError) as exc:
        # Shifting to UTC can step off the ends of the datetime range (year 1
        # with a positive offset underflows, year 9999 with a negative one
        # overflows). Raise the same ValueError as every other bad ts so that
        # callers need one except clause, not two.
        raise ValueError(f"timestamp {ts!r} is not representable in UTC: {exc}") from exc


def _format_ts(ts: datetime) -> str:
    """RFC3339 in UTC with trailing Z, trailing zeros trimmed (Go's RFC3339Nano).

    The year is padded EXPLICITLY rather than by ``strftime("%Y")``, which is not
    portable: glibc renders year 1 as ``1`` and BSD libc as ``0001``. That is not
    a cosmetic difference. RFC3339 requires four digits, so on Linux this
    function used to return ``1-01-01T00:00:00Z`` — a string our own reader
    refuses. The zero-instant guard in ``encode`` compares the RENDERED value
    against ``0001-01-01T00:00:00Z`` on purpose (it asks "would our reader accept
    these bytes?"), and an unpadded year walked straight past it, so the one
    value guaranteed to brick a log was writable on Linux and not on macOS.
    """
    ts = _to_utc(ts)
    base = (
        f"{ts.year:04d}-{ts.month:02d}-{ts.day:02d}"
        f"T{ts.hour:02d}:{ts.minute:02d}:{ts.second:02d}"
    )
    if ts.microsecond:
        frac = f"{ts.microsecond:06d}".rstrip("0")
        base = f"{base}.{frac}"
    return base + "Z"


@dataclass
class Event:
    """A single log entry.

    JSON shape::

        {"ts":"2026-05-22T14:30:00Z","id":"01HZ...","actor":"claude-3a1f",
         "type":"claim","scope":["src/foo.ts#bar"],
         "data":{"intent":"fix N+1"}}

    ``scope`` is optional (only set on claim/release events). ``data`` carries
    type-specific fields and is otherwise opaque to the log reader.

    ``ts`` must be a timezone-AWARE datetime and must not be the zero instant;
    :meth:`encode` refuses both rather than writing a line the reader would
    reject or an instant shifted by the writer's local offset.
    """

    ts: datetime
    id: str
    actor: str
    type: str
    scope: list[str] | None = None
    data: dict[str, Any] | None = field(default=None)

    def encode(self) -> bytes:
        """Marshal to one line of canonical JSON terminated with ``\\n``.

        Writers are STRICT: a type this build does not know is refused here,
        even though :func:`read` will happily skip one written by a newer build.
        The same strictness applies to ``ts`` — see below for why that is not
        pedantry but the difference between a log and a brick.
        """
        if not self.id:
            raise ValueError("event: missing id")
        if not self.actor:
            raise ValueError("event: missing actor")
        if not is_known_type(self.type):
            raise ValueError(f"event: invalid type {self.type!r} (known: {known_types()})")

        # WHAT WENT WRONG BEFORE (do not reintroduce): this guard read
        # `if self.ts is None`, a condition no real datetime ever trips. Go
        # guards `e.TS.IsZero()`, and Go's zero time renders as
        # "0001-01-01T00:00:00Z" — the exact string decode() (and Go's Decode)
        # rejects as a MISSING ts. So `Event(ts=datetime.min, ...)` encoded
        # happily, appended a line, and the next read() called that line
        # corruption and aborted: one accidental zero value bricked the whole
        # log from that point on, including every good event appended after it.
        #
        # The check is therefore on the RENDERED value, not on the Python
        # object: it asks the only question that matters — "would our own
        # reader accept the bytes we are about to write?" — and so it catches
        # every spelling of the zero instant that reaches it at once (UTC-aware
        # datetime.min, year 1 at 01:00+01:00), where an `is` or `==` test
        # against one sentinel would catch only the spelling it names. Never
        # write a line we would refuse to read back.
        if not isinstance(self.ts, datetime):
            raise ValueError("event: missing ts")
        if _is_naive(self.ts):
            raise ValueError(f"event: naive ts {self.ts!r}: {_NAIVE_TS_HINT}")
        try:
            ts_text = _format_ts(self.ts)
        except ValueError as exc:
            # Out of range once shifted to UTC: _to_utc has the diagnosis, we
            # add the "event:" prefix every error out of this module carries.
            raise ValueError(f"event: {exc}") from exc
        if ts_text == _GO_ZERO_TS:
            raise ValueError("event: missing ts (zero timestamp)")

        # Field order matches the Go struct so a log written by either side
        # reads the same way to a human: when, who, what.
        obj: dict[str, Any] = {
            "ts": ts_text,
            "id": self.id,
            "actor": self.actor,
            "type": self.type,
        }
        # omitempty: an absent scope and an empty one mean the same thing, and
        # the reducer distinguishes neither.
        if self.scope:
            obj["scope"] = [str(s) for s in self.scope]
        if self.data:
            obj["data"] = _normalize(self.data)
        try:
            # allow_nan=False because NaN/Infinity are not JSON; Python would
            # otherwise emit bare `NaN`, producing a line no conforming parser
            # (including Go's) can read back — corruption authored by us.
            line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"event: marshal: {exc}") from exc
        raw = _escape_like_go(line).encode("utf-8") + b"\n"

        # Same rule as the zero-ts guard above, applied to size: never write a
        # line we would refuse to read back. read() caps a line at
        # MAX_LINE_BYTES and calls anything longer CORRUPTION, not a skippable
        # oddity — so without this check an oversized event was accepted by the
        # writer and then bricked the log the instant it landed. Every later
        # read of that repository aborted, including the pre-edit hook, and the
        # event that did it was already on disk and unreadable. A caller that
        # trips this gets a refusal it can act on (trim the note, drop the
        # payload) instead of a store nothing can open.
        if len(raw) > MAX_LINE_BYTES:
            raise ValueError(
                f"event: encoded event is {len(raw)} bytes, over the "
                f"{MAX_LINE_BYTES}-byte line limit the reader enforces"
            )
        return raw

    @classmethod
    def decode(cls, line: str | bytes) -> "Event":
        """Parse one JSONL line into an Event.

        Raises :class:`UnknownEventTypeError` for a well-formed line of an
        unrecognised type, and plain ``ValueError`` for anything else — the
        reader needs to tell those two apart.
        """
        if isinstance(line, (bytes, bytearray)):
            try:
                line = bytes(line).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"event: unmarshal: invalid UTF-8: {exc}") from exc
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"event: unmarshal: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"event: unmarshal: expected a JSON object, got {type(raw).__name__}")

        # Structural checks first, matching Go, where a shape mismatch fails in
        # json.Unmarshal before any field validation runs.
        scope = raw.get("scope")
        if scope is not None:
            if not isinstance(scope, list) or not all(isinstance(s, str) for s in scope):
                raise ValueError("event: unmarshal: scope must be an array of strings")
            scope = list(scope)
        data = raw.get("data")
        if data is not None and not isinstance(data, dict):
            raise ValueError("event: unmarshal: data must be an object")

        ev_id = raw.get("id")
        if not isinstance(ev_id, str) or not ev_id:
            raise ValueError("event: missing id")
        actor = raw.get("actor")
        if not isinstance(actor, str) or not actor:
            raise ValueError("event: missing actor")
        ev_type = raw.get("type")
        if not is_known_type(ev_type):
            raise UnknownEventTypeError(f"event: unknown type {ev_type!r}")
        ts = _parse_ts(raw.get("ts"))

        return cls(
            ts=ts,
            id=ev_id,
            actor=actor,
            type=ev_type,
            scope=scope or None,
            data=_normalize(data) if data else None,
        )


# Go's zero time.Time serialises to this, and Go's Decode rejects it as a
# missing ts. Keep rejecting it here so a log written by either side is
# validated identically. _ZERO_TS is the same moment as a datetime, for
# comparing instants rather than spellings; encode() checks the rendered string
# (that is what lands on disk) and decode() checks both.
_GO_ZERO_TS = "0001-01-01T00:00:00Z"
_ZERO_TS = datetime(1, 1, 1, tzinfo=timezone.utc)


def _parse_ts(value: object) -> datetime:
    if value is None or value == "":
        raise ValueError("event: missing ts")
    if not isinstance(value, str):
        raise ValueError(f"event: unmarshal: ts must be a string, got {type(value).__name__}")
    if value == _GO_ZERO_TS:
        raise ValueError("event: missing ts")
    try:
        ts = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"event: unmarshal: bad ts {value!r}: {exc}") from exc
    try:
        # A ts with no offset is refused rather than assumed to be UTC: Go's
        # time.RFC3339 requires a zone, so no comms binary and (since the
        # encode() guard above) no build of this port can produce one. A line
        # carrying one came from somewhere else, and reading it as UTC would
        # invent an instant that the file never stated.
        ts = _to_utc(ts)
    except ValueError as exc:
        raise ValueError(f"event: unmarshal: bad ts {value!r}: {exc}") from exc
    # Belt and braces for the zero instant: the literal-string check above only
    # catches Go's exact spelling, while "0001-01-01T01:00:00+01:00" is the same
    # instant written differently. decode() must reject both, or encode()'s
    # guard and decode()'s would disagree about what a valid ts is.
    if ts == _ZERO_TS:
        raise ValueError("event: missing ts")
    return ts


# --------------------------------------------------------------------------
# Monotonic ULID ids
# --------------------------------------------------------------------------

# Crockford base32. The alphabet ascends in ASCII, so lexicographic order over
# the 26-character encoding equals numeric order over the 128-bit value — which
# is the only reason sorting ids by string is meaningful at all.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MAX_MS = (1 << 48) - 1
_MAX_ENTROPY = (1 << 80) - 1
_MAX_INC = 0xFFFFFFFF  # matches oklog/ulid's default monotonic increment bound

_id_lock = threading.Lock()
_last_ms = -1
_last_entropy = -1

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _unix_millis(ts: datetime) -> int:
    # Integer arithmetic rather than int(ts.timestamp() * 1000): the float form
    # is exact enough today but rounds unpredictably at millisecond boundaries,
    # and a rounded-up id would sort into the wrong millisecond.
    delta = _to_utc(ts) - _EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def new_id(now: datetime | None = None) -> str:
    """Return a fresh, monotonic, time-prefixed ULID (26 chars).

    Ids minted in this process in the SAME millisecond are strictly increasing.
    That is the property the reducer leans on: plain randomness is NOT
    monotonic — two same-millisecond ids share the 48-bit timestamp prefix but
    get independent random suffixes, so they sort in random order, which lets
    same-millisecond events (a claim and its steal/release, say) replay out of
    causal order.

    Monotonicity is per-millisecond and per-process only, exactly as in the Go
    original: a clock that steps BACKWARDS starts a fresh random suffix under
    the older timestamp prefix, so the new id sorts before its predecessor.
    That is tolerable because ordering for the reducer is by timestamp, not by
    id; the id only breaks ties within one millisecond.

    ``now`` must be an aware datetime for the same reason ``Event.ts`` must:
    the 48-bit prefix is a UTC instant, and a naive local ``datetime.now()``
    used to be silently shifted into it, minting ids that sorted hours away
    from the events they belong to. It raises ValueError instead now.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if _is_naive(now):
        raise ValueError(f"event: naive ts {now!r}: {_NAIVE_TS_HINT}")
    try:
        ms = _unix_millis(now)
    except ValueError as exc:
        raise ValueError(f"event: {exc}") from exc
    if ms < 0 or ms > _MAX_MS:
        raise ValueError(f"event: timestamp {now!r} does not fit in a ULID's 48 bits")

    global _last_ms, _last_entropy
    with _id_lock:
        if _last_entropy >= 0 and ms == _last_ms:
            entropy = _last_entropy + 1 + secrets.randbelow(_MAX_INC)
            if entropy > _MAX_ENTROPY:
                # ~2**80 ids in one millisecond. Unreachable in practice; fall
                # back to fresh randomness rather than raise, and accept the
                # lost monotonicity for that single id.
                entropy = secrets.randbits(80)
        else:
            entropy = secrets.randbits(80)
        _last_ms = ms
        _last_entropy = entropy

    value = (ms << 80) | entropy
    out = bytearray(26)
    for i in range(25, -1, -1):
        out[i] = ord(_CROCKFORD[value & 0x1F])
        value >>= 5
    return out.decode("ascii")


# --------------------------------------------------------------------------
# Append
# --------------------------------------------------------------------------

# Cap on a single JSONL line. A real event never comes close, but a runaway
# writer must not be able to blow out memory during replay.
MAX_LINE_BYTES = 1 << 20  # 1 MiB

_READ_CHUNK = 64 * 1024


def _repair_torn_tail(fd: int, size: int, target: str) -> int:
    """Drop an unterminated final line before appending after it. Returns the size.

    A writer killed mid-append leaves a half-written last line. read() tolerates
    that — it warns and stops there, and every event before it is still good.
    The danger is what the NEXT append does to it: appending after an
    unterminated line CONCATENATES onto it, so the torn fragment and the new
    event fuse into one unparseable line in the MIDDLE of the file. That is no
    longer the tolerated trailing-garbage case; it is mid-log corruption, and
    read() correctly refuses the whole file from then on. One crash plus one
    ordinary later command was enough to brick a store permanently.

    Discarding the fragment is sound, and only here: the caller holds the repo
    lock, so nobody else is mid-append, and an unterminated line is by
    definition a write that never completed — no command was ever told it
    succeeded, and no reader ever counted it. Truncating to the last newline
    restores exactly the state the interrupted command started from.

    THAT PRECONDITION IS NOT DECORATION, and it is now measured. Run unlocked
    with eight concurrent appenders on Linux, this loses whole events: a process
    reads the size, another appends before the ftruncate lands, and the truncate
    cuts back past complete lines it never saw. 136 of 1200 events, gone, with
    no torn line and no duplicate to show for it — the log simply gets shorter.
    Neutering this function makes that run clean, which is how the mechanism was
    confirmed rather than guessed.

    It does not reproduce on macOS, so it is invisible to anyone developing
    there. Every production path holds the lock (checked call site by call
    site), so this is a hazard for FUTURE callers, not a live fault — which is
    exactly why ``append_batch`` takes ``repair_torn_tail`` rather than leaving
    the requirement in a docstring nothing enforces.
    """
    if not size or os.pread(fd, 1, size - 1) == b"\n":
        return size
    # Scan backwards for the last newline. Bounded work: a torn tail cannot be
    # longer than one line, and a line is capped at MAX_LINE_BYTES.
    pos = size
    keep = 0
    while pos > 0:
        step = min(_READ_CHUNK, pos)
        pos -= step
        chunk = os.pread(fd, step, pos)
        nl = chunk.rfind(b"\n")
        if nl != -1:
            keep = pos + nl + 1
            break
    dropped = size - keep
    os.ftruncate(fd, keep)
    print(
        f"comms: warning: log {target} had an unterminated final line "
        f"({dropped} bytes from an interrupted write); dropped it before appending",
        file=sys.stderr,
    )
    return keep


def append(
    path: str | os.PathLike[str], event: Event, *, repair_torn_tail: bool = True
) -> None:
    """Append one event as a JSONL line. The caller MUST hold the repo lock."""
    append_batch(path, (event,), repair_torn_tail=repair_torn_tail)


def append_batch(
    path: str | os.PathLike[str],
    events: Sequence[Event] | Iterable[Event],
    *,
    repair_torn_tail: bool = True,
) -> None:
    """Append several events as consecutive lines in one open and one write.

    The caller MUST hold the per-repo lock. Two properties are load-bearing:

    * Every event is encoded up front, so a bad event writes nothing at all.
    * On a fault mid-write (ENOSPC, SIGINT) the file is truncated back to its
      pre-write size, so a multi-event command is all-or-nothing on disk too.
      The rollback is only safe because the lock is held — it assumes nobody
      else appended between our open and our failure.

    Note what this deliberately does NOT use: ``open(path, "a")``. That is
    O_APPEND, but it is also an 8 KiB buffered stream, so a batch larger than
    the buffer flushes mid-object and tears a JSON line in half — and a torn
    line is not a lost event, it is a log that stops parsing at that point. Raw
    ``os.write`` on a raw descriptor is what makes each append a single atomic
    ``write(2)`` at the current end of file.
    """
    evs = list(events)
    if not evs:
        return
    buf = b"".join(ev.encode() for ev in evs)

    target = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(target))
    # 0700/0600 throughout: the log carries what an agent is doing and where,
    # and the store is per-user machine state, not shared data.
    os.makedirs(parent, mode=0o700, exist_ok=True)
    created = not os.path.exists(target)

    # O_RDWR, not O_WRONLY: the torn-tail check below has to READ the last byte
    # before writing past it. O_APPEND still pins every write to end of file.
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        start = (
            _repair_torn_tail(fd, os.fstat(fd).st_size, target)
            if repair_torn_tail
            else os.fstat(fd).st_size
        )
        written = 0
        try:
            while written < len(buf):
                n = os.write(fd, buf[written:])
                if n <= 0:
                    raise OSError(f"event log: short write to {target}")
                written += n
        except BaseException:
            if written:
                os.ftruncate(fd, start)
            raise
        # The Go original stops here. We fsync because the log is the only copy
        # of coordination state: O_APPEND makes the write atomic against other
        # appenders, but it says nothing about durability, so a power loss right
        # after a claim can leave the claim in page cache and the agent that
        # took it convinced it holds a scope nothing on disk records.
        os.fsync(fd)
    finally:
        os.close(fd)

    if created:
        # A brand-new file's directory entry needs its own fsync, or the fsync
        # above can survive a crash with no name pointing at the data. Best
        # effort: some filesystems refuse to open a directory for sync.
        try:
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


class _LineTooLong(Exception):
    def __init__(self, line: int) -> None:
        self.line = line
        super().__init__(f"line {line} exceeds {MAX_LINE_BYTES} bytes")


def _iter_lines(fh):
    """Yield ``(line_number, raw_bytes_without_newline, terminated)``.

    Reads bytes, not text, for two reasons: the byte ceiling has to be enforced
    before a runaway line is materialised, and invalid UTF-8 must surface as
    corruption at a known line number rather than as a decode error somewhere
    inside the standard library.
    """
    buf = b""
    num = 0
    eof = False
    while not eof:
        chunk = fh.read(_READ_CHUNK)
        if not chunk:
            eof = True
        else:
            buf += chunk
        start = 0
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            num += 1
            if nl - start + 1 > MAX_LINE_BYTES:
                raise _LineTooLong(num)
            yield num, buf[start:nl], True
            start = nl + 1
        buf = buf[start:]
        if len(buf) > MAX_LINE_BYTES:
            raise _LineTooLong(num + 1)
    if buf:
        yield num + 1, buf, False


def read(path: str | os.PathLike[str]) -> list[Event]:
    """Parse the entire JSONL log at ``path``.

    Recovery policy:

    * Missing file -> empty list, no error. A repo nobody has claimed in yet is
      not an error condition.
    * Blank lines -> silently skipped.
    * Unterminated final line -> one warning on stderr, ignored. That is what a
      writer killed mid-append leaves behind, and the events before it are all
      still good.
    * Unknown event type -> skipped, counted, one warning at the end.
    * Malformed JSON, invalid UTF-8, a ts that is missing/zero/without a UTC
      offset, or an oversized line -> CorruptLogError naming the line number.
    * Duplicate event ids -> first wins, later copies dropped silently.
    """
    target = os.fspath(path)
    events: list[Event] = []
    seen: set[str] = set()
    unknown = 0
    try:
        try:
            fh = open(target, "rb")
        except FileNotFoundError:
            return []
        with fh:
            try:
                for num, raw, terminated in _iter_lines(fh):
                    if not terminated:
                        # Whitespace-only trailing data is just an unterminated
                        # empty line; nothing was lost, so say nothing.
                        if raw.strip(b" \t\r\n"):
                            print(
                                f"comms: warning: log {target} ends with unterminated "
                                f"line {num}, ignored",
                                file=sys.stderr,
                            )
                        break
                    if not raw.strip(b" \t\r\n"):
                        continue
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise CorruptLogError(target, num, "invalid UTF-8") from exc
                    try:
                        ev = Event.decode(text)
                    except UnknownEventTypeError:
                        # A type we do not know is not corruption — it is a
                        # newer writer. Skip it the way a duplicate id is
                        # skipped below; everything else still aborts.
                        unknown += 1
                        continue
                    except ValueError as exc:
                        raise CorruptLogError(target, num, exc) from exc
                    except RecursionError as exc:
                        # A deeply nested JSON value blows the interpreter's
                        # stack inside the parser. RecursionError is not a
                        # ValueError, so it used to escape the reader entirely —
                        # and since every command folds the log, that killed
                        # them all: the pre-edit hook refused every edit in the
                        # repository, with a bare traceback and no line number.
                        # It is a corrupt line like any other; say WHICH line.
                        raise CorruptLogError(
                            target, num, "JSON nested too deeply to parse"
                        ) from exc
                    # De-duplicate by id, exactly as Go's event.Read does with
                    # its seen[ev.ID] set. This is the ONLY place it happens:
                    # state.fold deliberately folds whatever it is handed, so
                    # if the reader stopped filtering here, a line that got
                    # appended twice — a retried write, a log concatenated onto
                    # itself, a copy-paste during recovery — would double-count
                    # every finding and note and replay a claim after its own
                    # release. First occurrence wins, since the log is
                    # append-only and the earliest copy is the original.
                    if ev.id in seen:
                        continue
                    seen.add(ev.id)
                    events.append(ev)
            except _LineTooLong as exc:
                raise CorruptLogError(
                    target, exc.line, f"line exceeds {MAX_LINE_BYTES} bytes"
                ) from exc
    finally:
        # One line at the end rather than one per event: a newer writer emitting
        # a few thousand events of a type we predate should tell the operator
        # once, not bury the real output. Printed even when the read then
        # aborted, because "your build is old" is useful context for the error.
        if unknown:
            print(
                f"comms: warning: log {target}: skipped {unknown} line(s) of an event type "
                "this build does not know; a newer comms wrote them",
                file=sys.stderr,
            )
    return events


# --------------------------------------------------------------------------
# Store location
# --------------------------------------------------------------------------

LOG_FILENAME = "log.jsonl"


def user_data_home() -> Path:
    """The per-user application-data root, matching the Go comms exactly.

    macOS uses ``~/Library/Application Support`` and NOT ``~/Library/Mobile
    Documents``: iCloud Drive resolves concurrent writes by forking the file
    into conflict copies, which for an append-only log means silently splitting
    the truth in two.

    Windows raises rather than guessing a location. The Go original supports
    darwin and linux only, and inventing a third layout here would put the log
    somewhere no ``comms`` binary looks — a store that exists but that nothing
    else on the machine can find is worse than a clear error.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform.startswith(("linux", "freebsd", "netbsd", "openbsd")):
        xdg = os.environ.get("XDG_DATA_HOME", "")
        # Per the XDG spec a relative XDG_DATA_HOME is invalid and MUST be
        # ignored; fall through to the default rather than resolving it against
        # a cwd that varies per invocation.
        if xdg and os.path.isabs(xdg):
            return Path(xdg)
        return Path.home() / ".local" / "share"
    raise RuntimeError(
        f"comms log: unsupported platform {sys.platform!r} (comms stores its log on darwin + linux)"
    )


def coordination_root(repo_root: str | os.PathLike[str]) -> str:
    """The path that identifies ONE repository for coordination purposes.

    For an ordinary checkout this is the repo root. For a **git worktree** it is
    the main repository's root, so every worktree of one repo shares one store.

    THIS IS WHY. Keying the store by the checkout's own path meant two agents in
    two worktrees of the same repository got two disjoint logs and coordinated
    nothing at all: no block, no warning, two empty boards that both looked calm.
    Worktree-per-agent is the standard isolation pattern for running several
    coding agents on one repository — it is this harness's own default — so the
    single most likely deployment was the one where the tool silently did nothing.

    ``git rev-parse --git-common-dir`` is what distinguishes them: in a worktree
    it points at the main repo's ``.git`` while ``--git-dir`` points at the
    worktree's private directory. Its parent is the shared root.

    Falls back to the given path when git is unavailable or this is not a
    repository — coordination inside a plain directory is still better than an
    exception, and callers that need to know use ``is_worktree``.
    """
    root = os.path.realpath(os.path.abspath(os.fspath(repo_root)))
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=root, capture_output=True, text=True, timeout=5, check=False,
        )
        common = (out.stdout or "").strip()
        if out.returncode == 0 and common:
            # <main repo>/.git -> <main repo>. A bare repo has no worktree parent
            # worth using, so keep the reported path itself in that case.
            parent = os.path.dirname(os.path.realpath(common))
            if parent and os.path.isdir(parent):
                return parent
    except (OSError, subprocess.SubprocessError):
        pass
    return root


def repo_hash(repo_root: str | os.PathLike[str]) -> str:
    """First 12 hex chars of sha256 over the coordination root.

    Symlinks are resolved first so that ``/tmp/x`` and ``/private/tmp/x`` — the
    same directory on macOS — key the same store. Renaming or moving the repo
    changes the hash and orphans the old log; that is the Go behaviour and it is
    accepted, because the alternative (an id file inside the repo) gets copied
    along with the repo and makes two checkouts share one log.

    Note this hashes the COORDINATION root, not the path handed in, so all
    worktrees of a repository agree. See ``coordination_root``.
    """
    return hashlib.sha256(coordination_root(repo_root).encode("utf-8")).hexdigest()[:12]


def store_dir(repo_root: str | os.PathLike[str]) -> Path:
    """The per-machine, NOT-committed store for this repo.

    ``<data home>/comms/<repo hash>/`` — the same directory the Go comms uses,
    so an existing store is found rather than a second one started beside it.
    The lock file lives here too, next to the log it guards.
    """
    return user_data_home() / "comms" / repo_hash(repo_root)


def log_path(repo_root: str | os.PathLike[str]) -> Path:
    """Absolute path to this repo's event log."""
    return store_dir(repo_root) / LOG_FILENAME


#: Written beside the log so a store can name the repo it belongs to. The Go
#: build has always written this; the directory name is a hash and carries no
#: name of its own.
REPO_PATH_FILENAME = "repo-path.txt"


def write_repo_path(store: Path, repo_root: str | os.PathLike[str]) -> None:
    """Record which repo this store serves. Best effort, never fatal.

    Failing to write a label must never stop a claim from being recorded —
    coordination is the job, the label is a convenience for whoever reads the
    board later.
    """
    try:
        target = store / REPO_PATH_FILENAME
        want = coordination_root(repo_root)
        # Only write when it would change, so a busy repo does not rewrite the
        # same bytes on every single command.
        if target.exists() and target.read_text(encoding="utf-8").strip() == want:
            return
        target.write_text(want + "\n", encoding="utf-8")
    except OSError:
        pass


def known_stores(data_home: Path | None = None) -> list[dict]:
    """Every comms store on this machine, newest activity first.

    A store is one repository's log. They are keyed by a hash of the repo path,
    so the only way to name one is the label written beside it — a store with
    no label is reported with an empty ``root`` rather than being hidden, since
    an unnamed project that is busy is exactly the one somebody wants to find.

    Reads only the filesystem: size and mtime, never the log itself. A board
    lists every project on the machine and folding 188 logs to draw a sidebar
    would cost seconds.
    """
    home = data_home or (user_data_home() / "comms")
    out: list[dict] = []
    try:
        entries = sorted(home.iterdir())
    except OSError:
        return out
    for d in entries:
        log = d / LOG_FILENAME
        try:
            if not log.is_file():
                continue
            st = log.stat()
        except OSError:
            continue
        root = ""
        try:
            root = (d / REPO_PATH_FILENAME).read_text(encoding="utf-8").strip()
        except OSError:
            pass
        out.append({
            "key": d.name,
            "root": root,
            "name": os.path.basename(root) if root else "",
            "bytes": st.st_size,
            "modified": st.st_mtime,
            "exists": bool(root) and os.path.isdir(root),
        })
    out.sort(key=lambda s: s["modified"], reverse=True)
    return out


_EPHEMERAL_ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")


def is_ephemeral_store(path: str | os.PathLike[str]) -> bool:
    """Report whether the store resolved under a throwaway temp root.

    That almost always means ``$HOME`` was overridden, so the events land in a
    private log that a normally-launched agent never sees. Callers should warn
    loudly: the failure mode is not an error, it is two agents coordinating
    through logs that cannot see each other.
    """
    clean = os.path.normpath(os.fspath(path)) + os.sep
    return clean.startswith(_EPHEMERAL_ROOTS)
