"""The repo-local wiki: `comms-graph doc`.

These docs are the only place a decision's *reason* survives. The properties
tested here are the ones that decide whether that reason is still there when
somebody comes back for it:

  * a doc prints exactly as it was written, or a quote taken from it is wrong;
  * `--list` reads as a table of contents, or a reader opens all five docs;
  * an editor that dies never damages what was already on disk;
  * two agents cannot edit one doc at once, and the loser is told who has it;
  * the docs are in the repo, not the per-machine store, or the whole point of
    committing them is lost.

Each test says what breaks in the real world if it fails.
"""

from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from comms_graph import doc as cdoc
from comms_graph import lock as clock
from comms_graph import log as clog
from comms_graph import state as cstate


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _cli(repo, *args, session=None):
    """Run `comms doc` in `repo`. Returns (exit_code, stdout+stderr).

    `session` is the host agent's session id, the same way the task-graph tests
    use it: real agents each have their own, and leaving it unset makes every
    actor in a test look like one agent.
    """
    out = io.StringIO()
    was = os.getcwd()
    prior = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.chdir(repo)
    try:
        if session is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = session
        else:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        with redirect_stdout(out), redirect_stderr(out):
            try:
                code = cdoc.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(was)
        if prior is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prior
    return code, out.getvalue()


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=repo, check=True)
    return repo


def _docs(repo) -> Path:
    return repo / ".comms" / "docs"


def _write_doc(repo, slug, body) -> Path:
    path = _docs(repo) / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _editor(tmp_path, body, monkeypatch, name="fake-editor", args=""):
    """Install a shell script as $EDITOR. `$1` is the file to edit."""
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    spec = shlex.quote(str(script)) + (f" {args}" if args else "")
    monkeypatch.setenv("EDITOR", spec)
    monkeypatch.delenv("VISUAL", raising=False)
    return script


def _findings(repo):
    return cstate.fold(clog.read(clog.log_path(repo))).findings


# ---------------------------------------------------------------------------
# Reading — the path that matters most
# ---------------------------------------------------------------------------


def test_a_doc_prints_byte_for_byte(tmp_path, monkeypatch):
    """IF THIS FAILS: a doc read back is not the doc that was written. These are
    design documents somebody quotes from months later; a stripped or added
    trailing newline is the harmless-looking version of the same bug that would
    also drop the last line."""
    repo = _repo(tmp_path, monkeypatch)
    body = "# Kataster\n\nWir nehmen ALKIS, weil WFS die Flurstücke liefert.\n\n  indented\n"
    _write_doc(repo, "kataster-machbarkeit", body)

    code, out = _cli(repo, "kataster-machbarkeit")

    assert code == 0
    assert out == body, "the doc must be copied out unchanged"


def test_a_missing_doc_is_a_refusal_not_a_breakage(tmp_path, monkeypatch):
    """IF THIS FAILS: 'no such doc' is indistinguishable from 'the store is
    broken'. Exit 2 is what a wrapper retries or escalates on; a typo'd slug must
    not look like an outage, and it must say how to find the real name."""
    repo = _repo(tmp_path, monkeypatch)
    _write_doc(repo, "kataster-machbarkeit", "# K\n\nreason\n")

    code, out = _cli(repo, "kataster-machbarkeht")

    assert code == 1
    assert "--list" in out, "tell the reader how to find the name they meant"


def test_list_shows_the_first_real_sentence_not_the_heading(tmp_path, monkeypatch):
    """IF THIS FAILS: --list is a column of slugs. The heading almost always just
    restates the slug, so a reader with five docs open all five to find the one
    that answers their question."""
    repo = _repo(tmp_path, monkeypatch)
    _write_doc(repo, "kartenansicht-entwurf",
               "# Kartenansicht Entwurf\n\nFünf Runden, geprüft gegen Aufgabenpaket 01.\n")
    _write_doc(repo, "merge-develop", "# Merge Develop\n\ndevelop is stale; prod is main.\n")

    code, out = _cli(repo, "--list")

    assert code == 0
    assert "Fünf Runden, geprüft gegen Aufgabenpaket 01." in out
    assert "develop is stale; prod is main." in out
    # Sorted, so the same repo lists the same way for everybody.
    assert out.index("kartenansicht-entwurf") < out.index("merge-develop")


def test_list_falls_back_to_the_heading_when_there_is_no_prose(tmp_path, monkeypatch):
    """IF THIS FAILS: a doc that is still only a heading lists as a bare slug with
    an empty column, which reads as 'unreadable' rather than 'not written yet'."""
    repo = _repo(tmp_path, monkeypatch)
    _write_doc(repo, "stub", "# Naturschutz Machbarkeit\n\n")

    code, out = _cli(repo, "--list")

    assert code == 0
    assert "Naturschutz Machbarkeit" in out


def test_a_long_hint_is_cut_between_characters_not_inside_one(tmp_path, monkeypatch):
    """IF THIS FAILS: the hint for a German doc ends in a replacement glyph. These
    docs are routinely German; cutting a multi-byte character in half corrupts the
    one line whose whole job is to orient the reader."""
    repo = _repo(tmp_path, monkeypatch)
    _write_doc(repo, "lang", "# L\n\n" + "ö" * 80 + "\n")

    code, out = _cli(repo, "--list")

    assert code == 0
    hint = out.split("lang", 1)[1].strip()
    assert hint == "ö" * 67 + "...", "67 characters plus the ellipsis"
    assert "�" not in hint


def test_a_repo_with_no_docs_says_so(tmp_path, monkeypatch):
    """IF THIS FAILS: an empty wiki either crashes or prints nothing, and 'nothing'
    is the same output as 'the command silently did not run'."""
    repo = _repo(tmp_path, monkeypatch)

    code, out = _cli(repo, "--list")

    assert code == 0
    assert "no docs yet" in out
    assert not _docs(repo).exists(), "reading must not create directories in the repo"


def test_list_ignores_the_editor_lock_and_the_install_temp(tmp_path, monkeypatch):
    """IF THIS FAILS: the wiki lists its own plumbing. A sidecar lock or a
    half-installed temp file shown as a doc sends a reader to open a lock file."""
    repo = _repo(tmp_path, monkeypatch)
    _write_doc(repo, "real", "# R\n\nreal content\n")
    (_docs(repo) / ".real.lock").write_text("someone\n2026-08-21T10:00:00Z\n")
    (_docs(repo) / ".real.abc.tmp").write_text("half a doc")
    (_docs(repo) / ".hidden.md").write_text("# hidden\n")

    code, out = _cli(repo, "--list")

    assert code == 0
    assert "real" in out
    assert ".lock" not in out and ".tmp" not in out and "hidden" not in out


def test_a_doc_with_broken_bytes_still_prints_and_says_so(tmp_path, monkeypatch):
    """IF THIS FAILS: one bad byte from a truncated write costs the reader the
    whole document. Printing it is right; printing it silently is not, or a
    mangled line gets quoted back as verbatim."""
    repo = _repo(tmp_path, monkeypatch)
    path = _docs(repo) / "broken.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"# B\n\nreason survives\xff\n")

    code, out = _cli(repo, "broken")

    assert code == 0
    assert "reason survives" in out
    assert "not valid UTF-8" in out


# ---------------------------------------------------------------------------
# Names — the path-safety story
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["../../../etc/passwd", "/etc/passwd", "docs/nested", "Kataster", ".hidden", "with space", ""],
)
def test_a_name_that_could_leave_the_docs_directory_is_refused(bad, tmp_path, monkeypatch):
    """IF THIS FAILS: `doc ../../.ssh/id_rsa` prints a private key, and `--edit`
    with the same argument writes wherever the name points. The grammar is the
    whole containment story — there is no second check behind it."""
    repo = _repo(tmp_path, monkeypatch)

    code, out = _cli(repo, bad)

    assert code == 2, f"{bad!r} must be refused as a name, not opened"
    assert "not a doc name" in out or "exactly one doc name" in out


def test_the_name_grammar_is_the_go_builds_grammar(tmp_path, monkeypatch):
    """IF THIS FAILS: a doc one build can address the other cannot. Dots, dashes
    and underscores are all in real slugs; rejecting any of them splits the wiki
    in two depending on which binary you happen to run."""
    repo = _repo(tmp_path, monkeypatch)
    for slug in ("kandidat-iteration-prioritaet", "v1.2_notes", "a", "0mq"):
        _write_doc(repo, slug, f"# {slug}\n\nbody\n")
        code, _ = _cli(repo, slug)
        assert code == 0, f"{slug!r} is a legal slug and must be readable"


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def test_an_edit_saves_what_the_editor_wrote(tmp_path, monkeypatch):
    """IF THIS FAILS: --edit is decorative. The user spends ten minutes writing a
    decision and the file on disk is unchanged."""
    repo = _repo(tmp_path, monkeypatch)
    _write_doc(repo, "merge-develop", "# Merge Develop\n\nold reason\n")
    _editor(tmp_path, 'printf "%s\\n" "new reason" >> "$1"', monkeypatch)

    code, out = _cli(repo, "merge-develop", "--edit", "--as", "claude-dev")

    assert code == 0, out
    assert "Saved .comms/docs/merge-develop.md" in out
    saved = (_docs(repo) / "merge-develop.md").read_text()
    assert saved == "# Merge Develop\n\nold reason\nnew reason\n"


def test_editing_an_unknown_name_creates_the_doc(tmp_path, monkeypatch):
    """IF THIS FAILS: writing the first doc in a repo needs a mkdir and a touch
    first, which nothing tells an agent to do — so the first doc never gets
    written."""
    repo = _repo(tmp_path, monkeypatch)
    _editor(tmp_path, 'printf "%s\\n" "we chose ALKIS" >> "$1"', monkeypatch)

    code, out = _cli(repo, "kataster-machbarkeit", "--edit", "--as", "claude-dev")

    assert code == 0, out
    text = (_docs(repo) / "kataster-machbarkeit.md").read_text()
    assert text == "# kataster-machbarkeit\n\nwe chose ALKIS\n"


def test_an_editor_that_fails_leaves_the_original_exactly_as_it_was(tmp_path, monkeypatch):
    """IF THIS FAILS: a crashed editor eats a 5KB design document. The editor
    never touches the real file, so a session that ends badly cannot half-write
    over what was already there."""
    repo = _repo(tmp_path, monkeypatch)
    original = "# Kartenansicht\n\nFünf Runden, geprüft von Codex.\n"
    _write_doc(repo, "kartenansicht-entwurf", original)
    _editor(tmp_path, 'printf "%s" "TRUNCATED" > "$1"; exit 3', monkeypatch)

    code, out = _cli(repo, "kartenansicht-entwurf", "--edit", "--as", "claude-dev")

    assert code == 2, "an editor that failed is 'it did not work', not a refusal"
    assert (_docs(repo) / "kartenansicht-entwurf.md").read_text() == original
    assert "unchanged" in out
    assert not _findings(repo), "nothing was decided, so nothing may be recorded"


def test_an_abandoned_first_edit_leaves_no_empty_doc_behind(tmp_path, monkeypatch):
    """IF THIS FAILS: every abandoned `--edit` adds a phantom doc to the wiki. An
    empty stub is worse than no doc: it lists as if somebody had written
    something, and the next reader opens it to find nothing."""
    repo = _repo(tmp_path, monkeypatch)
    _editor(tmp_path, "exit 1", monkeypatch)

    code, _ = _cli(repo, "naturschutz-machbarkeit", "--edit", "--as", "claude-dev")

    assert code == 2
    assert not (_docs(repo) / "naturschutz-machbarkeit.md").exists()
    _, out = _cli(repo, "--list")
    assert "no docs yet" in out


def test_no_editor_configured_is_said_plainly_and_writes_nothing(tmp_path, monkeypatch):
    """IF THIS FAILS: an agent with no $EDITOR either gets a traceback or gets a
    stubbed empty doc it never asked for. The check happens before anything is
    created, so a machine without an editor cannot litter the repo."""
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    code, out = _cli(repo, "merge-develop", "--edit", "--as", "claude-dev")

    assert code == 2
    assert "EDITOR" in out
    assert not _docs(repo).exists(), "a failed edit must not create the docs tree"


def test_an_editor_setting_with_a_quoted_path_and_a_flag_is_honoured(tmp_path, monkeypatch):
    """IF THIS FAILS: `EDITOR="/Applications/Visual Studio Code.app/.../code"
    --wait` tries to run "/Applications/Visual". Splitting on plain whitespace
    breaks the single commonest real-world $EDITOR value, and dropping --wait is
    worse than failing: the command returns before the user has typed anything."""
    repo = _repo(tmp_path, monkeypatch)
    spaced = tmp_path / "Code App With Spaces"
    spaced.mkdir()
    script = spaced / "code"
    script.write_text('#!/bin/sh\nprintf "%s\\n" "flag=$1" >> "$2"\n')
    script.chmod(0o755)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", f'"{script}" --wait')

    code, out = _cli(repo, "notes", "--edit", "--as", "claude-dev")

    assert code == 0, out
    assert "flag=--wait" in (_docs(repo) / "notes.md").read_text()


def test_two_agents_cannot_edit_one_doc_and_the_loser_is_told_who_has_it(tmp_path, monkeypatch):
    """IF THIS FAILS: two agents open the same doc and the second save silently
    discards the first one's work. 'Busy' alone is not enough either — the agent
    that is refused needs a name to go and talk to."""
    repo = _repo(tmp_path, monkeypatch)
    _write_doc(repo, "merge-develop", "# M\n\nreason\n")
    _editor(tmp_path, 'printf "%s\\n" "second writer" >> "$1"', monkeypatch)

    sidecar = _docs(repo) / ".merge-develop.lock"
    held = clock.try_acquire(sidecar)
    assert held is not None
    sidecar.write_text("codex-dev\n2026-08-21T09:00:00Z\n")
    try:
        code, out = _cli(repo, "merge-develop", "--edit", "--as", "claude-dev")
    finally:
        held.release()

    assert code == 1, "somebody else holds it: a refusal, not a failure"
    assert "codex-dev" in out
    assert "second writer" not in (_docs(repo) / "merge-develop.md").read_text()


def test_the_repo_lock_is_free_while_the_editor_is_open(tmp_path, monkeypatch):
    """IF THIS FAILS: one open editor blocks every claim and release in the repo
    for as long as the human takes. An editor session has no deadline, so holding
    the coordination lock across it turns comms into the thing being waited on."""
    repo = _repo(tmp_path, monkeypatch)
    lock_file = clog.store_dir(repo) / ".lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import fcntl, sys\n"
        "lock_file, doc = sys.argv[1], sys.argv[2]\n"
        "fh = open(lock_file, 'a+')\n"
        "try:\n"
        "    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    verdict = 'repo lock was FREE'\n"
        "except OSError:\n"
        "    verdict = 'repo lock was HELD'\n"
        "open(doc, 'a').write(verdict + chr(10))\n"
    )
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv(
        "EDITOR",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))} {shlex.quote(str(lock_file))}",
    )

    code, out = _cli(repo, "merge-develop", "--edit", "--as", "claude-dev")

    assert code == 0, out
    assert "repo lock was FREE" in (_docs(repo) / "merge-develop.md").read_text()


# ---------------------------------------------------------------------------
# What the shared log sees
# ---------------------------------------------------------------------------


def test_a_saved_edit_is_recorded_the_way_the_go_build_records_it(tmp_path, monkeypatch):
    """IF THIS FAILS: doc edits are invisible to the other build. Both builds read
    one log, so a different category, summary or ref spelling for the same fact is
    dropped silently by the reader that did not write it — no error, just a wiki
    that appears never to change."""
    repo = _repo(tmp_path, monkeypatch)
    _editor(tmp_path, 'printf "%s\\n" "ALKIS via WFS" >> "$1"', monkeypatch)

    code, out = _cli(repo, "kataster-machbarkeit", "--edit", "--as", "claude-dev",
                     session="agent-A")

    assert code == 0, out
    findings = _findings(repo)
    assert len(findings) == 1
    recorded = findings[0]
    assert recorded.actor == "claude-dev"
    assert recorded.category == "decision"
    assert recorded.summary == "updated doc:kataster-machbarkeit"
    assert [(r.kind, r.value) for r in recorded.refs] == [("doc", "kataster-machbarkeit")]


def test_the_docs_are_in_the_repo_not_the_per_machine_store(tmp_path, monkeypatch):
    """IF THIS FAILS: the reason a decision was made is only on the machine that
    typed it. Docs are committed with the code on purpose; the event log is not,
    and putting a doc in the store hides it from everybody who would clone."""
    repo = _repo(tmp_path, monkeypatch)
    _editor(tmp_path, 'printf "%s\\n" "decided" >> "$1"', monkeypatch)

    code, out = _cli(repo, "merge-develop", "--edit", "--as", "claude-dev")

    assert code == 0, out
    assert (repo / ".comms" / "docs" / "merge-develop.md").is_file()
    store = clog.store_dir(repo)
    assert not list(store.rglob("*.md")), "no doc may land in the per-machine store"


def test_reading_a_doc_writes_nothing_at_all(tmp_path, monkeypatch):
    """IF THIS FAILS: `doc --list` in a hook or a loop appends an event every time.
    Reading is not a decision, and a log full of 'somebody looked' buries the
    findings that matter."""
    repo = _repo(tmp_path, monkeypatch)
    _write_doc(repo, "merge-develop", "# M\n\nreason\n")

    assert _cli(repo, "--list")[0] == 0
    assert _cli(repo, "merge-develop")[0] == 0

    log = clog.log_path(repo)
    assert not log.exists() or clog.read(log) == []


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def test_asking_how_it_works_never_edits_anything(tmp_path, monkeypatch):
    """IF THIS FAILS: `doc --help` opens an editor, or errors. For a tool whose
    users are agents, --help is the whole discovery path, and it must be the one
    thing that can never have a side effect."""
    repo = _repo(tmp_path, monkeypatch)
    _editor(tmp_path, 'printf "%s\\n" "should never run" >> "$1"', monkeypatch)

    code, out = _cli(repo, "merge-develop", "--edit", "--help", "--as", "claude-dev")

    assert code == 0
    assert "--list" in out and "--edit" in out
    assert not _docs(repo).exists()


def test_list_plus_a_name_is_refused_rather_than_guessed(tmp_path, monkeypatch):
    """IF THIS FAILS: `doc --list merge-develop` quietly does one of the two
    things the user might have meant. Guessing between 'show me everything' and
    'show me this one' is how somebody ends up reading the wrong document."""
    repo = _repo(tmp_path, monkeypatch)

    assert _cli(repo, "--list", "merge-develop")[0] == 2
    assert _cli(repo, "--list", "--edit")[0] == 2
    assert _cli(repo)[0] == 2
