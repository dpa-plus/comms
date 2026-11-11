"""The verb table is the one source for dispatch, usage, help and hints."""

from __future__ import annotations

import pytest

from comms_graph import cli, verbs


def _help_for(path: str) -> str:
    call = verbs.prepare([*path.split(), "--help"])
    return verbs.verb_help(call.verb, call.path)


LEAVES = verbs.leaves()


def test_every_verb_that_runs_has_a_handler():
    for path, verb in LEAVES:
        if not verb.expands:
            assert path in cli._HANDLERS, f"{path} is in the table but nothing runs it"
    table = {p for p, v in LEAVES if not v.expands}
    assert set(cli._HANDLERS) == table, "a handler the table does not list"


@pytest.mark.parametrize("path,verb", LEAVES, ids=[p for p, _ in LEAVES])
def test_help_names_every_flag_the_verb_accepts(path, verb):
    text = _help_for(path)
    assert text.startswith("Usage: comms-graph ")
    for flag in verb.flags:
        assert f"--{flag.name}" in text, f"{path} --help does not name --{flag.name}"
        for alias in flag.aliases:
            assert f"--{alias}" in text
    if not verb.raw:
        for flag in verbs.GLOBAL_FLAGS:
            assert f"--{flag.name}" in text


@pytest.mark.parametrize("path,verb", LEAVES, ids=[p for p, _ in LEAVES])
def test_every_flag_alias_resolves_to_its_flag(path, verb):
    for flag in verb.flags:
        for alias in flag.aliases:
            args = [f"--{alias}", "v"] if flag.arg else [f"--{alias}"]
            assert verbs.check_flags(path, verb, args)[0] == f"--{flag.name}"


def test_every_verb_alias_resolves():
    for verb in verbs.VERBS:
        assert verbs.lookup(verb.name) is verb
        for alias in verb.aliases:
            assert verbs.lookup(alias) is verb


def test_the_expanding_aliases_land_on_a_real_verb():
    for _, verb in LEAVES:
        if verb.expands:
            target = verbs.lookup(verb.expands[0])
            assert target is not None and target.name in cli._HANDLERS or target.subs


def test_global_flags_are_accepted_by_every_verb():
    for path, verb in LEAVES:
        if verb.raw:
            continue
        out = verbs.check_flags(path, verb, ["--as", "x", "--json", "--root", "r", "--graph", "g"])
        assert out == ["--as", "x", "--json", "--root", "r", "--graph", "g"]


def test_a_global_flag_before_the_verb_moves_behind_it():
    call = verbs.prepare(["--as", "a1", "--json", "claim", "x.py"])
    assert call.path == "claim" and call.argv == ["x.py", "--as", "a1", "--json"]


def test_an_unknown_flag_is_three_lines_and_names_the_closest_form():
    with pytest.raises(verbs.Refusal) as caught:
        verbs.prepare(["claim", "x.py", "--intnt", "y"])
    lines = caught.value.lines
    assert len(lines) <= 3 and "--intent" in lines[1]


def test_a_flag_that_needs_another_is_refused_with_the_right_one_named():
    with pytest.raises(verbs.Refusal) as caught:
        verbs.prepare(["claim", "x.py", "--reason", "y"])
    assert "--intent" in " ".join(caught.value.lines)
    verbs.prepare(["claim", "x.py", "--steal", "ID", "--reason", "y"])


def test_an_unknown_verb_is_three_lines_at_most_and_guesses():
    with pytest.raises(verbs.Refusal) as caught:
        verbs.prepare(["claimm"])
    assert len(caught.value.lines) <= 3 and "claim" in caught.value.lines[1]
    with pytest.raises(verbs.Refusal) as caught:
        verbs.prepare(["task", "shw"])
    assert "task show" in caught.value.lines[1]


def test_a_boolean_flag_does_not_swallow_the_positional_after_it():
    cli._BOOLEANS = verbs.booleans(verbs.lookup("release"))
    try:
        positional, flags = cli._parse_flags(["--all-mine", "a.txt", "--result", "ok"])
    finally:
        cli._BOOLEANS = frozenset()
    assert positional == ["a.txt"] and flags["result"] == "ok"


def test_text_that_merely_starts_with_dashes_is_a_positional():
    assert not verbs.is_flag("--this is prose, not a flag")
    assert verbs.is_flag("--intent") and verbs.is_flag("--intent=x")


def test_the_flags_each_verb_reads_are_all_in_the_table():
    import re
    from pathlib import Path

    here = Path(cli.__file__).parent
    src = "".join((here / f"{m}.py").read_text(encoding="utf-8")
                  for m in ("cli", "status", "hello", "session", "compact", "doc"))
    named = {f.name for v in verbs.VERBS for s in (v, *v.subs) for f in s.flags}
    named |= {f.name for f in verbs.GLOBAL_FLAGS} | {a for f in verbs.GLOBAL_FLAGS for a in f.aliases}
    named |= {a for v in verbs.VERBS for s in (v, *v.subs) for f in s.flags for a in f.aliases}
    read = set(re.findall(r'flags\.get\("([a-z-]+)"', src))
    read |= set(re.findall(r'"([a-z-]+)" (?:not )?in flags', src))
    assert read <= named, f"read by the code but not in the table: {sorted(read - named)}"
