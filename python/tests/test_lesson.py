"""Lessons: the one thing comms stores per user instead of per repository.

Nobody on this machine has written a lesson in six months, which decides what
these tests are about. The read path has to be right on an empty shelf, because
that is the state every agent meets it in; the write path has to refuse rather
than leave a hollow lesson behind, because one empty file in a directory of
curated knowledge is indistinguishable from real knowledge until you open it;
and the files have to be exactly where the Go build looks, because both builds
read this same directory and a lesson only one of them can find is not curated
knowledge, it is a private note.

Each test says what breaks in the real world if it fails.
"""

from __future__ import annotations

import io
import os
import stat
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from comms_graph import lesson as clesson
from comms_graph import lock as clock


def _run(*args, cwd=None):
    """Run the verb the way the CLI would. Returns (exit_code, stdout+stderr).

    Merged streams on purpose: what a user or an agent sees is one terminal,
    and a test that reads only stdout would pass while every refusal went
    unprinted.
    """
    out = io.StringIO()
    was = os.getcwd()
    if cwd is not None:
        os.chdir(cwd)
    try:
        with redirect_stdout(out), redirect_stderr(out):
            try:
                code = clesson.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.chdir(was)
    return code, out.getvalue()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated user home, so no test can read or write the real lessons."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_DATA_HOME", str(h / ".local" / "share"))
    monkeypatch.delenv("COMMS_ACTOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    return h


def _write(slug: str, body: str) -> Path:
    d = clesson.lessons_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slug}.md"
    p.write_text(body, encoding="utf-8")
    return p


def _editor_script(tmp_path, monkeypatch, body: str, name: str = "fake-editor") -> Path:
    """Put a shell-script 'editor' on PATH and point $EDITOR at it."""
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return script


# ---------------------------------------------------------------------------
# The empty shelf: the state this verb is actually met in
# ---------------------------------------------------------------------------


def test_no_lessons_yet_is_an_answer_not_an_error(home):
    """IF THIS FAILS: the first agent ever to run `lesson --list` on a machine
    gets an error or a traceback for the ordinary, expected state of having no
    lessons, and concludes the verb is broken rather than empty. Zero lessons
    have ever been written here, so this is the common path, not the edge."""
    code, out = _run("--list")
    assert code == 0, out
    assert "(no global lessons yet)" in out


def test_listing_survives_a_lessons_dir_that_was_never_created(home):
    """IF THIS FAILS: listing has a side effect: it creates directories in the
    user's data home just to answer a read. A read must not write."""
    _run("--list")
    assert not clesson.lessons_dir().exists()


# ---------------------------------------------------------------------------
# Global, not repo-local: the whole reason the verb exists
# ---------------------------------------------------------------------------


def test_a_lesson_is_readable_from_a_directory_that_is_not_a_repo(home, tmp_path):
    """IF THIS FAILS: lessons became repo-local. Knowledge written while working
    on one project is invisible in the next one, which is the exact opposite of
    what a cross-project lesson is for, and running the verb outside any git
    checkout would fail instead of answering."""
    _write("verify-data-first", "# verify-data-first\n\nCheck API data before UI.\n")
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    code, out = _run("verify-data-first", cwd=elsewhere)
    assert code == 0, out
    assert "Check API data before UI." in out


def test_lessons_live_where_the_go_build_looks(home):
    """IF THIS FAILS: the two builds keep separate lesson shelves. A lesson
    written through one is missing from the other's `--list`, so agents get
    different curated knowledge depending on which binary they happen to run."""
    assert clesson.lessons_dir() == (
        Path(os.environ["XDG_DATA_HOME"] if sys.platform.startswith("linux")
             else str(Path.home() / "Library" / "Application Support"))
        / "comms" / "global" / "lessons"
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_the_list_shows_what_each_lesson_says_not_just_its_name(home):
    """IF THIS FAILS: the listing is a column of slugs. Choosing which lesson to
    read means opening all of them, so nobody reads any of them."""
    _write("verify-data-first", "# verify-data-first\n\nCheck API data before UI.\n")
    code, out = _run("--list")
    assert code == 0, out
    assert "verify-data-first" in out
    assert "Check API data before UI." in out


def test_a_lesson_with_only_headings_still_gets_a_hint(home):
    """IF THIS FAILS: a freshly stubbed lesson lists as a bare slug with a blank
    column, which reads like a corrupt entry rather than an unfinished one."""
    _write("half-written", "# half-written\n\n")
    code, out = _run("--list")
    assert code == 0, out
    assert "half-written" in out


def test_the_listing_ignores_sidecars_and_non_lessons(home):
    """IF THIS FAILS: the editor's own lock files and stray notes show up as
    lessons. The sidecar for `foo` would list as a lesson named `.foo`, and
    `lesson .foo` then refuses it as an invalid slug: a listed entry that
    cannot be opened."""
    _write("real-lesson", "# real-lesson\n\nBody.\n")
    d = clesson.lessons_dir()
    (d / ".real-lesson.lock").write_text("someone\n2026-01-01T00:00:00Z\n")
    (d / "notes.txt").write_text("not a lesson\n")
    (d / "subdir.md").mkdir()
    code, out = _run("--list")
    assert code == 0, out
    listed = [line.split()[0] for line in out.strip().splitlines()]
    assert listed == ["real-lesson"]


# ---------------------------------------------------------------------------
# Refusals: 1 means "no", 2 means "that did not work"
# ---------------------------------------------------------------------------


def test_a_lesson_that_does_not_exist_is_a_no_not_a_breakage(home):
    """IF THIS FAILS: a typo'd slug exits with the same code as an unreadable
    data home. A wrapper that retries on failure retries a name that will never
    exist, and one that alerts on breakage alerts on a typo."""
    code, out = _run("no-such-lesson")
    assert code == 1, out
    assert "no-such-lesson" in out


def test_a_slug_cannot_walk_out_of_the_lessons_directory(home, tmp_path):
    """IF THIS FAILS: `lesson` becomes a file reader for the whole disk. The
    slug is pasted straight into a path, so a traversal prints private files:
    and with --edit it would open $EDITOR on one."""
    secret = home / "secret.md"
    secret.write_text("private\n", encoding="utf-8")
    for bad in ("../secret", "../../etc/passwd", "/etc/passwd", "Upper", "has space"):
        code, out = _run(bad)
        assert code == 2, f"{bad!r} -> {code}: {out}"
        assert "private" not in out


def test_a_slug_the_go_build_accepts_is_not_refused_here(home):
    """IF THIS FAILS: this build's stricter task-id slug rule leaks in and
    refuses lessons the Go build wrote into the same directory: dots and
    underscores included. The file lists, and then will not open."""
    _write("verify.data_first-2", "# verify.data_first-2\n\nStill a lesson.\n")
    code, out = _run("verify.data_first-2")
    assert code == 0, out
    assert "Still a lesson." in out


def test_list_with_a_slug_is_refused_rather_than_guessed(home):
    """IF THIS FAILS: `lesson --list foo` silently does one of the two things
    the user might have meant, and the other one silently does not happen."""
    _write("foo", "# foo\n\nBody.\n")
    code, out = _run("--list", "foo")
    assert code == 2, out
    assert "--list" in out


def test_a_misspelled_flag_does_not_quietly_become_a_read(home, monkeypatch):
    """IF THIS FAILS: `lesson foo --edt` prints the lesson and exits 0. The user
    believes they edited it; nothing was written and nothing said so."""
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    _write("foo", "# foo\n\nBody.\n")
    code, out = _run("foo", "--edt")
    assert code == 2, out
    assert "--edt" in out


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def test_edit_creates_the_stub_that_makes_a_lesson_worth_writing(home, monkeypatch):
    """IF THIS FAILS: $EDITOR opens on an empty buffer, and what gets written is
    a paragraph with no "Avoid" and no "Evidence": the two sections that make a
    lesson checkable instead of an opinion."""
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    monkeypatch.setenv("EDITOR", "true")
    code, out = _run("claim-smallest-scope", "--edit")
    assert code == 0, out
    body = (clesson.lessons_dir() / "claim-smallest-scope.md").read_text()
    assert "# claim-smallest-scope" in body
    assert "Effective pattern:" in body
    assert "Avoid:" in body
    assert "Evidence:" in body


def test_edit_opens_the_existing_lesson_instead_of_overwriting_it(home, monkeypatch, tmp_path):
    """IF THIS FAILS: editing a lesson resets it to the stub. Months of curated
    knowledge are replaced by four empty headings, and the only copy is gone."""
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    _write("keep-me", "# keep-me\n\nHard-won detail.\n")
    _editor_script(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("EDITOR", "fake-editor")
    code, out = _run("keep-me", "--edit")
    assert code == 0, out
    assert "Hard-won detail." in (clesson.lessons_dir() / "keep-me.md").read_text()


def test_the_editor_gets_its_arguments_and_the_path(home, monkeypatch, tmp_path):
    """IF THIS FAILS: an $EDITOR with flags: `code --wait`, `subl -w`: is run
    as a binary literally named "code --wait", which does not exist. Editing
    fails for everyone whose editor needs a wait flag, and for those where it
    does not, the editor returns immediately and the lesson is saved unedited."""
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    _editor_script(
        tmp_path, monkeypatch,
        '#!/bin/sh\n'
        'if [ "$1" != "--append" ]; then echo "missing --append" >&2; exit 7; fi\n'
        'printf "\\nEdited through argument-aware editor.\\n" >> "$2"\n',
    )
    monkeypatch.setenv("EDITOR", "fake-editor --append")
    code, out = _run("argument-editor", "--edit")
    assert code == 0, out
    body = (clesson.lessons_dir() / "argument-editor.md").read_text()
    assert "Edited through argument-aware editor." in body


def test_an_editor_path_with_spaces_survives_quoting(home, monkeypatch, tmp_path):
    """IF THIS FAILS: `EDITOR='"/Applications/Visual Studio Code.app/.../code"
    --wait'` splits into three nonexistent binaries. That spelling is the normal
    one on macOS, so editing is simply unavailable there."""
    editor_dir = tmp_path / "an app dir"
    editor_dir.mkdir()
    _editor_script(editor_dir, monkeypatch, "#!/bin/sh\nexit 0\n", name="spaced-editor")
    monkeypatch.setenv("EDITOR", f'"{editor_dir / "spaced-editor"}" --wait')
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    code, out = _run("spaced-path", "--edit")
    assert code == 0, out
    assert (clesson.lessons_dir() / "spaced-path.md").exists()


def test_a_missing_editor_leaves_no_hollow_lesson_behind(home, monkeypatch):
    """IF THIS FAILS: every failed edit deposits a stub. `--list` then shows
    slugs that look like curated knowledge and contain nothing but headings, and
    there is no way to tell those from lessons somebody meant to leave short."""
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    monkeypatch.setenv("EDITOR", "comms-definitely-not-an-editor")
    code, out = _run("missing-editor", "--edit")
    assert code == 2, out
    assert not (clesson.lessons_dir() / "missing-editor.md").exists()


def test_an_editor_that_fails_does_not_report_a_save(home, monkeypatch):
    """IF THIS FAILS: comms prints "Saved global lesson ..." after the editor
    crashed. The agent moves on believing the lesson is recorded."""
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    monkeypatch.setenv("EDITOR", "false")
    code, out = _run("editor-fails", "--edit")
    assert code == 2, out
    assert "Saved" not in out


def test_editing_anonymously_is_refused_before_anything_is_created(home, monkeypatch):
    """IF THIS FAILS: the sidecar records no useful holder, so the next agent to
    collide is told "another editor" and has nobody to ask. Worse, the refusal
    lands after the stub is written, leaving an empty lesson from a command that
    failed."""
    monkeypatch.setenv("EDITOR", "true")
    code, out = _run("anonymous", "--edit")
    assert code == 2, out
    assert not (clesson.lessons_dir() / "anonymous.md").exists()


def test_visual_wins_over_editor(home, monkeypatch, tmp_path):
    """IF THIS FAILS: the POSIX convention is inverted and a user who sets
    $VISUAL to a real editor gets their terminal fallback instead."""
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    _editor_script(
        tmp_path, monkeypatch,
        '#!/bin/sh\nprintf "\\nfrom VISUAL\\n" >> "$1"\n', name="visual-editor",
    )
    monkeypatch.setenv("VISUAL", "visual-editor")
    monkeypatch.setenv("EDITOR", "false")
    code, out = _run("visual-first", "--edit")
    assert code == 0, out
    assert "from VISUAL" in (clesson.lessons_dir() / "visual-first.md").read_text()


# ---------------------------------------------------------------------------
# Two editors at once
# ---------------------------------------------------------------------------


def test_a_second_editor_is_told_who_holds_the_lesson(home, monkeypatch):
    """IF THIS FAILS: two agents open the same lesson, both save, and the second
    write silently discards the first: the failure mode comms exists to
    prevent, in the one directory whose content is irreplaceable."""
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    monkeypatch.setenv("EDITOR", "true")
    assert _run("contested", "--edit")[0] == 0

    sidecar = clesson.lessons_dir() / ".contested.lock"
    handle = clock.try_acquire(sidecar)
    assert handle is not None
    try:
        code, out = _run("contested", "--edit")
    finally:
        handle.release()
    assert code == 1, out
    assert "human-eli" in out, "the holder's name is the point of the message"
    assert "retry later" in out


def test_the_lesson_is_editable_again_once_the_holder_leaves(home, monkeypatch):
    """IF THIS FAILS: the sidecar is never released, so one finished edit locks
    that lesson out of every future edit, and the lock file is never removed,
    so there is nothing to clear."""
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    monkeypatch.setenv("EDITOR", "true")
    assert _run("twice", "--edit")[0] == 0
    code, out = _run("twice", "--edit")
    assert code == 0, out


def test_one_lesson_being_edited_does_not_lock_the_others(home, monkeypatch):
    """IF THIS FAILS: the sidecar is shared across lessons, so an agent with vi
    open on one lesson blocks every other agent from writing an unrelated one."""
    monkeypatch.setenv("COMMS_ACTOR", "human-eli")
    monkeypatch.setenv("EDITOR", "true")
    assert _run("first", "--edit")[0] == 0
    handle = clock.try_acquire(clesson.lessons_dir() / ".first.lock")
    assert handle is not None
    try:
        code, out = _run("second", "--edit")
    finally:
        handle.release()
    assert code == 0, out


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_asking_how_it_works_has_no_side_effect(home):
    """IF THIS FAILS: `lesson --help` is read as a slug or a bad flag. For a
    tool whose users are agents, the help text is the whole discovery path."""
    for flag in ("-h", "--help"):
        code, out = _run(flag)
        assert code == 0, out
        assert "--list" in out and "--edit" in out
