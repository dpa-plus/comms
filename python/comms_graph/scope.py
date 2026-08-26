"""Claim scopes and overlap arithmetic: ported from comms/internal/overlap.

Grammar::

    scope  := path ('#' anchor)?
    path   := POSIX path, optionally globbed with * or **
    anchor := L<n>-<m>          (line range, inclusive, n <= m, both >= 1)
            | <symbol-name>      (opaque identifier)

A literal ``#`` in a filename is escaped as ``\\#``, and a literal backslash
as ``\\\\``. The backslash must be escaped too, or the escape is not
invertible: a path ending in a backslash would otherwise eat the ``#`` that
separates it from its anchor when the canonical form is parsed back.

Globs are never expanded against the real filesystem. Overlap is a pure
string operation on the patterns, for two reasons: a claim may name a file
that does not exist yet, and the answer must not change under us because
someone else's working tree changed between the check and the write.

This module deliberately knows nothing about the graph. It runs before the
map exists (that is the whole point of a claim), so it may not resolve a
symbol to a line range or a directory to its contents.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "ScopeError",
    "AnchorKind",
    "Anchor",
    "Scope",
    "parse",
    "normalize_path",
    "overlaps",
    "paths_overlap",
    "pattern_matches_path",
]


class ScopeError(ValueError):
    """A scope string violates the grammar.

    Go returns errors; we raise. Callers surface this text to a human at the
    CLI boundary, so the messages stay user-facing rather than internal.
    """


# Matches the line-range anchor shape and nothing else. Anchors that merely
# start with 'L' and contain '-' ("List-impl", "Loader-2", "L-value") must NOT
# be read as ranges: they are legitimate symbol names and fall through to the
# symbol branch. [0-9] rather than \d on purpose: \d in Python also matches
# non-ASCII digits (٣, ３), and int() would happily parse them.
_LINE_RANGE_RE = re.compile(r"L([0-9]+)-([0-9]+)")

# The cutset of Go's strings.TrimSpace (unicode.IsSpace), spelled out.
#
# NOT str.strip(): Python additionally treats U+001C-U+001F (the C0 file,
# group, record and unit separators) as whitespace. str.strip() therefore
# trimmed those bytes off the symbol, the symbol then passed the
# control-character check below, and the control byte survived in Scope.raw:
# which is echoed back to a terminal in conflict messages. Trimming exactly
# Go's set leaves the control byte on the symbol, where the check rejects it
# and nothing reaches the log. Do not simplify this back to str.strip().
#
# Every code point here is also whitespace to Python, so this only ever trims
# less, never more.
_GO_SPACE = (
    "\t\n\v\f\r "  # U+0009-U+000D and space
    "\x85\xa0"  # NEL, NBSP
    "\u1680"  # OGHAM SPACE MARK
    "\u2000\u2001\u2002\u2003\u2004\u2005"  # EN QUAD .. FOUR-PER-EM SPACE
    "\u2006\u2007\u2008\u2009\u200a"  # SIX-PER-EM .. HAIR SPACE
    "\u2028\u2029"  # LINE / PARAGRAPH SEPARATOR
    "\u202f\u205f\u3000"  # NARROW NBSP, MEDIUM MATH SPACE, IDEOGRAPHIC SPACE
)

# Go's int is 64-bit, so its strconv.Atoi rejects anything past this. Python
# ints are unbounded and would silently accept a 200-digit line number into
# the append-only log, where it lives forever. Reject at the boundary instead.
_MAX_LINE = (1 << 63) - 1


class AnchorKind(Enum):
    """The three legal anchor shapes (plus whole-file)."""

    WHOLE = "whole"  # no `#` anchor: claims the entire file
    LINE = "line"  # L<n>-<m>
    SYMBOL = "symbol"  # symbol-name (opaque string)


@dataclass(frozen=True, slots=True)
class Anchor:
    """The per-file claim refinement.

    Exactly one of (line_start, line_end) or symbol carries meaning, selected
    by kind. Frozen so a claim can be used as a dict key and can never be
    mutated after the event that created it was written to the log.
    """

    kind: AnchorKind = AnchorKind.WHOLE
    line_start: int = 0
    line_end: int = 0
    symbol: str = ""


@dataclass(frozen=True, slots=True)
class Scope:
    """The parsed form of a ``path[#anchor]`` string."""

    # The original user-supplied string, preserved so conflict messages can
    # echo back exactly what was typed. Excluded from equality/hash: two
    # scopes that normalize to the same territory ARE the same claim, and
    # "./src/a.ts" vs "src/a.ts" must not defeat a dedup.
    raw: str = field(compare=False)

    # Normalized, repo-relative POSIX path or glob pattern.
    path: str

    anchor: Anchor = Anchor()

    def __str__(self) -> str:
        """Canonical (post-normalization) form; round-trips through parse()."""
        # Backslash BEFORE hash, or the backslashes we just introduced by
        # escaping a hash would be escaped a second time.
        #
        # Escaping the backslash is not cosmetic. It was missing here (and is
        # missing in Go), and a path whose normalized form ends in `\` then
        # swallowed the anchor separator on re-parse: "src/lib\" + "#Handler"
        # came back as a WHOLE-file claim on the nonexistent "src/lib#Handler"
        # (two agents could hold the same symbol with no conflict reported)
        # and "a\" + "#" came back as a ScopeError, which state.py catches
        # broadly, silently dropping a claim whose event lives in the log
        # forever. Do not drop this replace: the round-trip contract in the
        # docstring above is what the append-only log relies on.
        path = self.path.replace("\\", "\\\\").replace("#", r"\#")
        if self.anchor.kind is AnchorKind.LINE:
            return f"{path}#L{self.anchor.line_start}-{self.anchor.line_end}"
        if self.anchor.kind is AnchorKind.SYMBOL:
            return f"{path}#{self.anchor.symbol}"
        return path

    def overlaps(self, other: "Scope") -> bool:
        return overlaps(self, other)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse(raw: str) -> Scope:
    """Interpret a raw scope string. Raises ScopeError on grammar violations."""
    if raw == "":
        raise ScopeError("scope: empty")
    # The splitter unescapes as it goes. It used to hand back a still-escaped
    # path that was unescaped here with a blind str.replace, which mangled
    # `\\#` (an escaped backslash followed by the anchor separator) into a
    # literal `#`.
    path_part, anchor_part, has_anchor = _split_on_unescaped_hash(raw)

    scope_path = normalize_path(path_part)
    if not has_anchor:
        return Scope(raw=raw, path=scope_path, anchor=Anchor(AnchorKind.WHOLE))
    return Scope(raw=raw, path=scope_path, anchor=_parse_anchor(anchor_part))


def _split_on_unescaped_hash(raw: str) -> tuple[str, str, bool]:
    """Split on the first unescaped ``#``, unescaping the path part as it goes.

    Two escapes are recognized, exactly the two ``Scope.__str__`` writes:
    ``\\#`` -> ``#`` and ``\\\\`` -> ``\\``. Together they make the encoding
    invertible, which is what the round-trip contract needs.

    A backslash before anything else: including at the very end of the
    string: is an ordinary filename byte, not a broken escape. That
    leniency is deliberate: on POSIX a backslash is a legal character in a
    name, and the append-only log already holds entries written before the
    backslash was escaped at all. Rejecting them would make old logs
    unreplayable; reading them as literals reproduces what they used to mean.

    The anchor part is returned raw and is never unescaped: the split happens
    at the FIRST unescaped ``#``, so everything after it is the anchor
    verbatim, ``#`` and backslashes included, and __str__ writes it back
    verbatim too.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n and raw[i + 1] in "#\\":
            out.append(raw[i + 1])
            i += 2
            continue
        if ch == "#":
            return "".join(out), raw[i + 1 :], True
        out.append(ch)
        i += 1
    return "".join(out), "", False


def _is_control(ch: str) -> bool:
    """C0 control, DEL, or C1 control.

    These code points can carry terminal-escape sequences: notably C1 CSI
    (U+009B), which many terminals interpret exactly like the two-byte ESC[
    introducer, so no scope containing one may ever reach the log, which
    later gets printed back to a terminal.

    Checked per code point, not per byte, so that ordinary printable Unicode
    survives: Café / файл / 日本語 decode to code points >= 0x100. A C1 control
    is a single code point in 0x80-0x9F and is caught however it was encoded.
    """
    o = ord(ch)
    return o < 0x20 or o == 0x7F or 0x80 <= o <= 0x9F


def _reject_unencodable(s: str, what: str) -> None:
    """Reject lone surrogates.

    Go checks utf8.ValidString on symbols; Python strings are already valid
    Unicode except for surrogates smuggled in by surrogateescape decoding:
    which is exactly how sys.argv arrives on macOS when a filename holds an
    undecodable byte. Such a string cannot be UTF-8 encoded, so it would blow
    up at json.dump time, i.e. while appending to the log. Fail here instead,
    where the error still has a user-facing home.
    """
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise ScopeError(f"scope: {what} is not valid UTF-8") from None


def normalize_path(raw: str) -> str:
    """Normalize a repo-relative path. Also used by the policy loader.

    Rules: reject absolute paths, reject anything that escapes the repo root,
    clean redundant separators and ``.`` segments.
    """
    if raw == "":
        raise ScopeError("scope: empty path")
    # Control characters are rejected before any other processing, so a scope
    # carrying terminal-escape bytes can never be normalized or persisted.
    for ch in raw:
        if _is_control(ch):
            raise ScopeError("scope: path contains control character")
    _reject_unencodable(raw, "path")
    if raw.startswith("/"):
        raise ScopeError(f"scope: absolute paths not allowed: {raw!r}")

    # posixpath, not os.path: repo paths are POSIX on every platform because
    # that is how git reports them. Note we do NOT translate backslashes to
    # slashes: on POSIX a backslash is an ordinary filename byte, and Go's
    # filepath.ToSlash is likewise a no-op there, so translating would be a
    # divergence, not a port.
    cleaned = posixpath.normpath(raw)
    cleaned = cleaned.removeprefix("./")  # normpath already does this; belt and braces
    if cleaned in (".", ""):
        raise ScopeError("scope: empty path after normalization")
    if ".." in cleaned.split("/"):
        raise ScopeError(f"scope: path escapes repo root: {raw!r}")
    return cleaned


def _parse_anchor(s: str) -> Anchor:
    if s == "":
        raise ScopeError("scope: anchor after `#` is empty")

    # Trim BEFORE the shape test, not only on the symbol branch below.
    # Trimming late made the canonical form non-injective: "L1-1 " missed the
    # range shape, became a SYMBOL named "L1-1", and __str__ wrote it as
    # "…#L1-1", which parses back as a LINE range. A symbol claim replayed
    # from the log as a line claim. Both spellings must land on the same
    # anchor, so the whitespace goes first.
    #
    # Go's cutset, not str.strip(): see _GO_SPACE.
    s = s.strip(_GO_SPACE)

    # fullmatch, not match: the pattern has no anchors, so re.match would
    # accept "L1-10junk" as a range on the strength of its prefix. (`$` would
    # not do either: it also matches before a trailing newline.)
    m = _LINE_RANGE_RE.fullmatch(s)
    if m is not None:
        # Once a string DOES have the range shape we validate the numbers, so
        # genuinely malformed ranges (L0-10, L10-5) stay hard errors instead
        # of quietly degrading into symbol names.
        start, end = int(m.group(1)), int(m.group(2))
        if start < 1:
            raise ScopeError(f"scope: line numbers must be >= 1, got L{start}-{end}")
        if start > end:
            raise ScopeError(f"scope: inverted line range L{start}-{end}")
        if end > _MAX_LINE:
            raise ScopeError(f"scope: line number out of range in {s!r}")
        return Anchor(kind=AnchorKind.LINE, line_start=start, line_end=end)

    # Already trimmed above; the emptiness check stays because an anchor of
    # nothing but whitespace reaches here as "".
    symbol = s
    if symbol == "":
        raise ScopeError("scope: symbol is empty after trimming whitespace")
    for ch in symbol:
        if _is_control(ch):
            raise ScopeError("scope: symbol contains control character")
    _reject_unencodable(symbol, "symbol")
    return Anchor(kind=AnchorKind.SYMBOL, symbol=symbol)


# --------------------------------------------------------------------------
# Overlap
# --------------------------------------------------------------------------


def overlaps(a: Scope, b: Scope) -> bool:
    """Do two scopes claim overlapping territory?

    Path intersection first; anchors only refine. Anchor refinement can never
    expand an overlap, only shrink one, so a path miss is a definitive no.
    """
    if not paths_overlap(a.path, b.path):
        return False
    return _anchors_overlap(a.anchor, b.anchor)


def _anchors_overlap(a: Anchor, b: Anchor) -> bool:
    if a.kind is AnchorKind.WHOLE or b.kind is AnchorKind.WHOLE:
        return True
    if a.kind is not b.kind:
        # Mixed line-range vs symbol. Pessimistic on purpose: this module has
        # no symbol table, so it cannot tell whether "#parse" lives inside
        # "#L10-40". The map can answer that later; a claim check runs before
        # the map exists and must fail safe.
        return True
    if a.kind is AnchorKind.LINE:
        # Closed intervals intersect iff each starts no later than the other ends.
        return a.line_start <= b.line_end and b.line_start <= a.line_end
    return a.symbol == b.symbol  # case-sensitive: Parse and parse are different symbols


# --------------------------------------------------------------------------
# Glob intersection
# --------------------------------------------------------------------------


def paths_overlap(a: str, b: str) -> bool:
    """Can two POSIX-style glob patterns match at least one path in common?

    ``**`` matches zero or more segments, ``*`` matches any run of characters
    within one segment, and every other glob character (?, [, ]) is a literal.
    Supporting full glob syntax buys nothing: claims are written by hand.

    A pattern whose final segment is a plain literal is ALSO tried as
    ``pattern/**``: see _may_name_a_directory for why.

    KNOWN HOLE, accepted deliberately. Directory promotion is withheld from a
    final segment containing ``*``, so::

        paths_overlap("src/foo", "src/foo/bar.ts")  -> True   (promoted)
        paths_overlap("src/*",   "src/foo/bar.ts")  -> False  (not promoted)

    A claim on ``src/*`` therefore does NOT conflict with a claim on
    ``src/foo/bar.ts``, even though ``src/*`` may well have been meant to
    cover the directory ``src/foo`` and everything under it. This is a real
    missed conflict, not an oversight: the alternative: promoting starred
    segments too: makes ``src/*`` and ``src/**`` synonyms and destroys the
    only way to claim just the files directly inside ``src``. A user who
    wants the recursive meaning has a spelling for it (``src/**``); a user
    who wants the shallow meaning would have none. Write ``src/**`` when you
    mean the subtree.
    """
    a_seg = _split(a)
    b_seg = _split(b)
    if _segments_overlap(a_seg, b_seg):
        return True
    if _may_name_a_directory(a_seg) and _segments_overlap(a_seg + ["**"], b_seg):
        return True
    if _may_name_a_directory(b_seg) and _segments_overlap(a_seg, b_seg + ["**"]):
        return True
    return False


def pattern_matches_path(pattern: str, path: str) -> bool:
    """Does a claim pattern cover one concrete repository path?

    Only ``pattern`` is interpreted; stars in ``path`` are ordinary filename
    bytes. Use this for paths that came from git or the filesystem: where a
    literal ``*`` in a filename is legal and must not be read as a wildcard:
    and paths_overlap for claim-against-claim.
    """
    pat = _split(pattern)
    tgt = _split(path)
    if _pattern_matches_segments(pat, tgt):
        return True
    # Only the pattern side is promoted: `path` is a real file, and a real
    # file has nothing underneath it.
    return _may_name_a_directory(pat) and _pattern_matches_segments(pat + ["**"], tgt)


def _split(p: str) -> list[str]:
    # Empty segments ("a//b", a trailing slash) carry no meaning and would
    # otherwise have to literally match something. normalize_path removes them
    # already; dropping them here keeps the function total on raw input too.
    return [seg for seg in p.split("/") if seg != ""]


def _may_name_a_directory(segs: Sequence[str]) -> bool:
    """Should this pattern also be tried as ``pattern/**``?

    Go answers no, and that is a bug: a claim on "src/lib/banking" does not
    conflict with a claim on "src/lib/banking/webhook.ts", so two agents can
    both believe they own the webhook file. We fix it here rather than porting
    it, because the fix is free of false conflicts: promotion only ever adds
    overlap between a path P and something UNDER P, and if P were really a
    file then nothing could exist under it. So the only pairs it newly
    conflicts are pairs that cannot both exist anyway.

    Promotion is withheld when the final segment contains a ``*``, which is
    what keeps ``src/*`` and ``src/**`` distinguishable: a user who writes
    ``src/*`` chose the non-recursive wildcard deliberately, and promoting it
    would erase the only way to say "just the files directly in src". A final
    LITERAL segment has no such alternative spelling: "src/lib" cannot mean
    anything but that file or that directory. The missed conflict this leaves
    behind is spelled out in paths_overlap under KNOWN HOLE.
    """
    return bool(segs) and "*" not in segs[-1]


def _segments_overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    """Intersection-emptiness over segment lists, with ``**`` as the wildcard.

    dp[i][j] is true when the unconsumed suffixes a[i:] and b[j:] can be made
    to name a common path. Built bottom-up from the accept state dp[la][lb]
    so every cell only reads cells with a larger i or j.

    This is deliberately the same recurrence as _globs_can_intersect one level
    down: segments here, characters there, ``**`` here, ``*`` there. The Go
    original recurses instead, which re-derives the same subproblem
    exponentially often for patterns like "**/**/**/x"; the table costs
    O(len(a) * len(b)) once.
    """
    la, lb = len(a), len(b)
    dp = [[False] * (lb + 1) for _ in range(la + 1)]
    dp[la][lb] = True

    # A trailing run of `**` can still match the empty remainder of the other
    # side, so propagate the accept state back along the last row and column.
    for i in range(la - 1, -1, -1):
        if a[i] == "**":
            dp[i][lb] = dp[i + 1][lb]
    for j in range(lb - 1, -1, -1):
        if b[j] == "**":
            dp[la][j] = dp[la][j + 1]

    for i in range(la - 1, -1, -1):
        ai = a[i]
        row, nxt = dp[i], dp[i + 1]
        for j in range(lb - 1, -1, -1):
            if ai == "**":
                # Skip the `**` (consume zero of b) or let it eat one segment.
                row[j] = nxt[j] or row[j + 1]
            elif b[j] == "**":
                row[j] = row[j + 1] or nxt[j]
            else:
                row[j] = _single_segment_overlap(ai, b[j]) and nxt[j + 1]
    return dp[0][0]


def _single_segment_overlap(a: str, b: str) -> bool:
    """Can two single-segment patterns match the same segment name?"""
    if "*" not in a and "*" not in b:
        return a == b
    return _globs_can_intersect(a, b)


def _globs_can_intersect(a: str, b: str) -> bool:
    """Is some concrete string matched by BOTH single-segment patterns?

    An intersection-emptiness test, NOT a "does a match b" test. Comparing
    only the leading and trailing literal anchors: the obvious shortcut:
    produces false positives whenever interior literals impose required
    characters or a minimum length: "a*a" vs "a" (no room for the trailing
    'a'), or "a*b*c" vs "axc" (the 'b' is mandatory but absent).

    Transitions on dp[i][j]:
      - a[i] == '*': the star eats one character of b (advance j, star stays)
        or is skipped, having eaten zero (advance i).
      - b[j] == '*': symmetric.
      - both literal: they must be equal; advance both.

    Go compares bytes; we compare code points. The verdict is identical for
    valid UTF-8: equal literals are equal either way, and a star that spans a
    multi-byte rune must span all of it to reach an accept state, and code
    points keep us out of encode/decode territory entirely.
    """
    la, lb = len(a), len(b)
    dp = [[False] * (lb + 1) for _ in range(la + 1)]
    dp[la][lb] = True

    for i in range(la - 1, -1, -1):
        if a[i] == "*":
            dp[i][lb] = dp[i + 1][lb]
    for j in range(lb - 1, -1, -1):
        if b[j] == "*":
            dp[la][j] = dp[la][j + 1]

    for i in range(la - 1, -1, -1):
        ai = a[i]
        row, nxt = dp[i], dp[i + 1]
        for j in range(lb - 1, -1, -1):
            if ai == "*":
                row[j] = nxt[j] or row[j + 1]
            elif b[j] == "*":
                row[j] = row[j + 1] or nxt[j]
            else:
                row[j] = ai == b[j] and nxt[j + 1]
    return dp[0][0]


def _pattern_matches_segments(pat: Sequence[str], tgt: Sequence[str]) -> bool:
    """One-directional match: pat is a pattern, tgt is a concrete path."""
    lp, lt = len(pat), len(tgt)
    dp = [[False] * (lt + 1) for _ in range(lp + 1)]
    dp[lp][lt] = True
    for i in range(lp - 1, -1, -1):
        if pat[i] == "**":
            dp[i][lt] = dp[i + 1][lt]
    # No dp[lp][j < lt] row: a concrete path has no wildcards, so leftover
    # target segments can never be absorbed by an exhausted pattern.
    for i in range(lp - 1, -1, -1):
        pi = pat[i]
        row, nxt = dp[i], dp[i + 1]
        for j in range(lt - 1, -1, -1):
            if pi == "**":
                row[j] = nxt[j] or row[j + 1]
            else:
                row[j] = _segment_pattern_matches(pi, tgt[j]) and nxt[j + 1]
    return dp[0][0]


def _segment_pattern_matches(pattern: str, value: str) -> bool:
    """Within one segment: `*` is a wildcard in pattern, a literal in value."""
    lp, lv = len(pattern), len(value)
    dp = [[False] * (lv + 1) for _ in range(lp + 1)]
    dp[lp][lv] = True
    for i in range(lp - 1, -1, -1):
        if pattern[i] == "*":
            dp[i][lv] = dp[i + 1][lv]
    for i in range(lp - 1, -1, -1):
        pi = pattern[i]
        row, nxt = dp[i], dp[i + 1]
        for j in range(lv - 1, -1, -1):
            if pi == "*":
                row[j] = nxt[j] or row[j + 1]
            else:
                row[j] = pi == value[j] and nxt[j + 1]
    return dp[0][0]
