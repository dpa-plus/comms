"""A scope is a claim on territory, and overlap is the whole blocking rule.

Every refusal comms ever issues comes out of `overlaps()`. A false NO is two
agents editing the same function; a false YES is an agent blocked on ground
nobody holds, which teaches people to pass --force and stop reading the output.
Both are worse than the tool not existing, so the tests below are about the
answer being right and about it meaning the same thing tomorrow.

`str(scope)` matters just as much: the canonical string is what goes into the
append-only log, so a scope that does not survive its own round trip is a claim
that will be replayed as something else, forever.
"""

from __future__ import annotations

import itertools

import pytest

from comms_graph.scope import AnchorKind, ScopeError, overlaps, parse

# A spread of shapes, deliberately including the awkward ones.
CORPUS = [
    "src/api/server.ts",
    "src/api/server.ts#charge",
    "src/api/server.ts#Charge",
    "src/api/server.ts#L10-40",
    "src/api/server.ts#L41-80",
    "src/api/*.ts",
    "src/**",
    "src/*",
    "src/api",
    "docs/README.md",
    r"src/od\#d.ts#Handler",
]


# ---------------------------------------------------------------------------
# The canonical string is what the log stores
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", CORPUS)
def test_a_scope_survives_the_trip_through_the_log(raw):
    """IF THIS FAILS: the claim written to the append-only log is not the claim
    that was made. The event is immutable, so every future replay reads the
    wrong territory, and the two most likely mistranslations are the dangerous
    ones: an anchored claim widening into a whole-file claim (blocks everyone)
    or a whole-file claim narrowing into a symbol (blocks nobody)."""
    once = parse(raw)
    twice = parse(str(once))
    assert twice.path == once.path
    assert twice.anchor == once.anchor
    assert str(twice) == str(once)


def test_a_path_that_ends_in_a_backslash_does_not_eat_its_anchor():
    """IF THIS FAILS: `src/lib\\` + `#Handler` comes back from the log as a
    WHOLE-FILE claim on a file called "src/lib#Handler" that does not exist: so
    two agents can hold the same symbol and neither is told. On POSIX a
    backslash is an ordinary filename byte, so this is a real path, not a
    hypothetical one."""
    s = parse("src/li\\\\#Handler")  # path "src/li\", anchor "Handler"
    assert s.path.endswith("\\")
    assert s.anchor.kind is AnchorKind.SYMBOL and s.anchor.symbol == "Handler"
    back = parse(str(s))
    assert back.path == s.path and back.anchor == s.anchor


def test_two_spellings_of_one_place_are_one_claim():
    """IF THIS FAILS: `./src/a.ts` and `src/a.ts` are two different claims, so a
    dedupe by scope keeps both and a release that names one leaves the other
    holding the ground."""
    assert parse("./src/a.ts") == parse("src/a.ts")
    assert parse("src//a.ts") == parse("src/a.ts")
    assert hash(parse("./src/a.ts")) == hash(parse("src/a.ts"))


def test_the_words_a_person_typed_are_kept_for_the_error_message():
    """IF THIS FAILS: the refusal tells the user about a normalized path they
    never typed, and they cannot match it against the command they ran."""
    assert parse("./src/a.ts").raw == "./src/a.ts"


# ---------------------------------------------------------------------------
# Anchors mean what they look like
# ---------------------------------------------------------------------------


def test_padding_does_not_turn_a_line_range_into_a_symbol():
    """IF THIS FAILS: `a.ts#L1-5 ` (one stray space, from a shell or a copy) is
    stored as a SYMBOL named "L1-5", and its canonical form re-parses as a LINE
    range. The same claim then means two different territories on two reads,
    which is the one thing an append-only log cannot recover from."""
    padded = parse("a.ts#L1-5 ")
    assert padded.anchor.kind is AnchorKind.LINE
    assert padded.anchor == parse("a.ts#L1-5").anchor


@pytest.mark.parametrize("name", ["List-impl", "L-value", "Loader-2", "L10-x"])
def test_a_symbol_that_merely_looks_like_a_range_stays_a_symbol(name):
    """IF THIS FAILS: an ordinary identifier is silently reinterpreted as a span
    of lines. The claim then covers lines the author never asked for and, worse,
    stops conflicting with the agent who really holds that symbol."""
    s = parse(f"a.ts#{name}")
    assert s.anchor.kind is AnchorKind.SYMBOL
    assert s.anchor.symbol == name


@pytest.mark.parametrize("bad", ["a.ts#L0-10", "a.ts#L10-5", "a.ts#"])
def test_a_malformed_range_is_refused_not_quietly_downgraded(bad):
    """IF THIS FAILS: a typo in a line range becomes a symbol name that matches
    nothing, so the claim protects nothing and the agent is told everything is
    fine."""
    with pytest.raises(ScopeError):
        parse(bad)


# ---------------------------------------------------------------------------
# What may never reach the log
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "src/\x1b[31mred.ts",       # ESC: colour/cursor control
        "src/a.ts#\x9bfoo",          # C1 CSI, one code point, same effect as ESC[
        "src/a\x00b.ts",
        "src/a.ts#drop\x07",
    ],
)
def test_terminal_escape_sequences_never_get_into_a_claim(hostile):
    """IF THIS FAILS: a scope string carrying terminal control bytes lands in an
    append-only log and is then echoed back: to every agent and every human who
    runs `status`: for the life of the repository, with no way to remove it."""
    with pytest.raises(ScopeError):
        parse(hostile)


@pytest.mark.parametrize("outside", ["/etc/passwd", "../other-repo/src/a.ts", "src/../../x"])
def test_a_claim_cannot_name_ground_outside_the_repo(outside):
    """IF THIS FAILS: claims stop being comparable. The store is keyed per
    repository, so a path that escapes the root either collides with an
    unrelated project's territory or names something no other agent can resolve
   : either way the overlap answer is meaningless."""
    with pytest.raises(ScopeError):
        parse(outside)


def test_ordinary_unicode_filenames_still_work():
    """IF THIS FAILS: the control-character defence has been written as a
    printability test and everyone with a non-ASCII filename is locked out of
    claiming their own files."""
    for name in ("src/café.ts", "src/файл.ts", "src/日本語.ts"):
        assert parse(name).path == name


# ---------------------------------------------------------------------------
# Overlap is an answer two agents must agree on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("a,b", list(itertools.combinations(CORPUS, 2)))
def test_the_answer_does_not_depend_on_who_asks_first(a, b):
    """IF THIS FAILS: whether a collision is caught depends on which agent
    happened to run the check, so the same pair of claims is a conflict in one
    direction and clear in the other, and both agents proceed."""
    assert overlaps(parse(a), parse(b)) == overlaps(parse(b), parse(a))


@pytest.mark.parametrize("raw", CORPUS)
def test_a_claim_always_conflicts_with_itself(raw):
    """IF THIS FAILS: the exact same scope, claimed twice, is not detected: the
    single most common collision there is, and the one users will assume works
    before they trust anything subtler."""
    assert overlaps(parse(raw), parse(raw))


def test_a_directory_claim_covers_the_files_under_it():
    """IF THIS FAILS: one agent claims `src/lib/banking` (the natural way to say
    "I'm reworking this area") and another claims `src/lib/banking/webhook.ts`,
    and both are told they own the webhook file."""
    assert overlaps(parse("src/lib/banking"), parse("src/lib/banking/webhook.ts"))
    assert overlaps(parse("src/lib/banking"), parse("src/lib/banking/deep/nested/x.ts"))


def test_a_file_claim_does_not_leak_onto_its_neighbours():
    """IF THIS FAILS: claiming one file blocks work on files that merely share a
    name prefix: `server.ts` blocking `server.test.ts`, so agents are refused
    ground nobody holds and stop believing refusals."""
    assert not overlaps(parse("src/api/server.ts"), parse("src/api/server.test.ts"))
    assert not overlaps(parse("src/api"), parse("src/api-v2/x.ts"))


def test_the_shallow_and_recursive_globs_stay_different_claims():
    """IF THIS FAILS: `src/*` and `src/**` become synonyms, and there is then no
    way at all to claim only the files sitting directly in a directory: every
    such claim silently escalates to the whole subtree and blocks work that was
    never meant to be blocked."""
    assert overlaps(parse("src/*"), parse("src/a.ts"))
    assert not overlaps(parse("src/*"), parse("src/deep/a.ts"))
    assert overlaps(parse("src/**"), parse("src/a.ts"))
    assert overlaps(parse("src/**"), parse("src/deep/a.ts"))


def test_two_globs_that_can_never_match_the_same_file_do_not_conflict():
    """IF THIS FAILS: agents working on different languages or different layers
    block each other: the "interior literal" cases are exactly the ones a naive
    prefix/suffix comparison gets wrong."""
    assert not overlaps(parse("src/**/*.ts"), parse("src/**/*.py"))
    assert overlaps(parse("src/**/*.ts"), parse("src/api/server.ts"))
    assert not overlaps(parse("a*a.ts"), parse("a.ts"))       # no room for the trailing a
    assert not overlaps(parse("a*b*c.ts"), parse("axc.ts"))   # the b is mandatory


# ---------------------------------------------------------------------------
# Anchors refine, and refine safely
# ---------------------------------------------------------------------------


def test_line_ranges_are_inclusive_and_touch_at_the_edges():
    """IF THIS FAILS: two agents editing lines 1-10 and 10-20 are told they are
    independent, and line 10 gets written twice."""
    assert overlaps(parse("a.ts#L1-10"), parse("a.ts#L10-20"))
    assert not overlaps(parse("a.ts#L1-10"), parse("a.ts#L11-20"))


def test_a_whole_file_claim_swallows_every_anchor_in_that_file():
    """IF THIS FAILS: "I am rewriting this file" fails to block someone editing
    one function inside it: the coarse claim must dominate the fine one, or
    coarse claims are worthless."""
    whole = parse("a.ts")
    for finer in ("a.ts#charge", "a.ts#L1-10", "a.ts#L900-901"):
        assert overlaps(whole, parse(finer))


def test_a_symbol_and_a_line_range_are_treated_as_possibly_the_same_place():
    """IF THIS FAILS: the check fails OPEN. This module runs before the map
    exists: that is the point of claiming, so it cannot know whether `#charge`
    lives inside lines 10-40. The only safe answer is "maybe", and the cost of
    that pessimism is one unnecessary refusal, against the cost of two agents
    editing one function."""
    assert overlaps(parse("a.ts#charge"), parse("a.ts#L10-40"))


def test_different_symbols_in_one_file_are_different_ground():
    """IF THIS FAILS: symbol-level claims are pointless: two agents cannot work
    on two functions in one file, which is the normal case in any real codebase
    and the main reason anchors exist."""
    assert not overlaps(parse("a.ts#charge"), parse("a.ts#refund"))


def test_case_distinguishes_two_symbols():
    """IF THIS FAILS: in Go (and anywhere case marks visibility) `Parse` and
    `parse` are genuinely different functions, and one agent's claim silently
    covers the other's. The map-side resolver matches case-sensitively too; the
    two layers have to agree or they contradict each other about one pair."""
    assert not overlaps(parse("a.ts#Parse"), parse("a.ts#parse"))


def test_anchors_never_reach_across_files():
    """IF THIS FAILS: identically-named symbols (every `main`, every `handler`)
    collide across the entire repository and nobody can claim anything."""
    assert not overlaps(parse("a.ts#charge"), parse("b.ts#charge"))
    assert not overlaps(parse("a.ts#L1-10"), parse("b.ts#L1-10"))
