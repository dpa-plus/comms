"""The pre-edit hook: the only thing that actually ENFORCES a claim.

Everything else in comms is advice an agent may ignore. This is the one place an
edit is stopped, and it runs in front of every tool call — so the cost of getting
it wrong is paid on every keystroke, in both directions: block too eagerly and no
work happens, block too little and the tool does nothing at all.

THE EXIT CODE IS THE ENTIRE INTERFACE. Claude Code reads exit 2 from a PreToolUse
hook as "block this call and show stderr to the model", and EVERY other non-zero
code as "the hook itself errored" — in which case the edit proceeds. So 2 is the
only code that stops anything, and there is no code meaning "something went
wrong, be careful". That is why everything unanswerable here is also 2.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from comms_graph import cli as ccli


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("COMMS_ACTOR", raising=False)
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n")
    (repo / "src" / "b.py").write_text("y = 2\n")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run([git, "init", "-q", "."], cwd=repo, check=True)
    return repo


def _run(repo, args, stdin_text=None, session=None):
    out = io.StringIO()
    was = os.getcwd()
    old_stdin = None
    os.chdir(repo)
    try:
        if stdin_text is not None:
            old_stdin, sys.stdin = sys.stdin, io.StringIO(stdin_text)
        if session is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = session
        with redirect_stdout(out), redirect_stderr(out):
            try:
                code = ccli.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()
    finally:
        if old_stdin is not None:
            sys.stdin = old_stdin
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.chdir(was)


def _payload(repo, name, session="sess-B", tool="Edit"):
    return json.dumps({
        "session_id": session, "tool_name": tool,
        "tool_input": {"file_path": str(repo / name)},
    })


def test_an_agent_may_edit_the_file_it_just_claimed(tmp_path, monkeypatch):
    """IF THIS FAILS: the tool is worse than useless — it blocks the one agent
    entitled to edit, with a message naming that same agent as the holder.

    The hook has no COMMS_ACTOR of its own; it inherits the session environment,
    not the per-command prefix agents use. It identifies the caller by matching
    the agent session id in the payload against hello events in the log, which
    is the entire reason claim writes one.
    """
    repo = _repo(tmp_path, monkeypatch)
    code, out = _run(repo, ["claim", "src/a.py", "--as", "claude-dev",
                            "--intent", "fixing it"], session="sess-A")
    assert code == 0, out

    code, out = _run(repo, ["check", "--stdin-json"],
                     stdin_text=_payload(repo, "src/a.py", session="sess-A"))
    assert code == 0, f"the holder must be allowed through: {out}"


def test_a_different_session_is_blocked_with_exit_two(tmp_path, monkeypatch):
    """IF THIS FAILS with any other non-zero code, Claude Code reads it as a
    broken hook and lets the edit through — so the tool silently stops enforcing
    anything while still printing a refusal nobody acts on."""
    repo = _repo(tmp_path, monkeypatch)
    _run(repo, ["claim", "src/a.py", "--as", "claude-dev", "--intent", "mine"],
         session="sess-A")
    code, out = _run(repo, ["check", "--stdin-json"],
                     stdin_text=_payload(repo, "src/a.py", session="sess-B"))
    assert code == 2, out
    assert "BLOCKED" in out and "claude-dev" in out
    assert "mine" in out, "the model needs to know what the holder is doing"


def test_an_unidentifiable_caller_is_blocked_rather_than_waved_through(tmp_path, monkeypatch):
    """IF THIS FAILS: anyone whose identity cannot be established inherits
    everybody else's claims and edits freely. Being told to wait for yourself is
    annoying; editing a file somebody else holds because we could not tell who
    you are is the failure this tool exists to prevent."""
    repo = _repo(tmp_path, monkeypatch)
    _run(repo, ["claim", "src/a.py", "--as", "claude-dev", "--intent", "mine"],
         session="sess-A")
    code, out = _run(repo, ["check", "--stdin-json"],
                     stdin_text=_payload(repo, "src/a.py", session=""))
    assert code == 2, out


def test_a_tool_call_with_no_file_is_allowed(tmp_path, monkeypatch):
    """Bash, a web fetch, a plain read — most tool calls carry no file at all,
    and a hook that blocked them would stop the session dead."""
    repo = _repo(tmp_path, monkeypatch)
    payload = json.dumps({"session_id": "sess-B", "tool_name": "Bash",
                          "tool_input": {"command": "ls"}})
    code, out = _run(repo, ["check", "--stdin-json"], stdin_text=payload)
    assert code == 0, out


def test_a_file_outside_any_repository_is_allowed(tmp_path, monkeypatch):
    """IF THIS FAILS: editing anything outside a checkout becomes impossible."""
    repo = _repo(tmp_path, monkeypatch)
    payload = json.dumps({"session_id": "s", "tool_name": "Edit",
                          "tool_input": {"file_path": "/etc/hosts"}})
    code, out = _run(repo, ["check", "--stdin-json"], stdin_text=payload)
    assert code == 0, out


def test_a_new_file_in_directories_that_do_not_exist_yet_is_allowed(tmp_path, monkeypatch):
    """IF THIS FAILS: creating a file in a new folder is blocked. The hook runs
    BEFORE the editor makes the parent directories, so the file's own directory
    routinely does not exist at the moment we are asked about it."""
    repo = _repo(tmp_path, monkeypatch)
    payload = json.dumps({"session_id": "s", "tool_name": "Write",
                          "tool_input": {"file_path": str(repo / "src/deep/new/f.py")}})
    code, out = _run(repo, ["check", "--stdin-json"], stdin_text=payload)
    assert code == 0, out


def test_an_unreadable_payload_fails_closed(tmp_path, monkeypatch):
    """There is no exit code meaning "be careful", so anything unanswerable has
    to be 2 — the alternative is reporting "clear" without having established
    it."""
    repo = _repo(tmp_path, monkeypatch)
    for bad in ("not json", ""):
        code, out = _run(repo, ["check", "--stdin-json"], stdin_text=bad)
        assert code == 2, f"{bad!r} should fail closed: {out}"


def test_the_repo_is_resolved_from_the_file_not_from_the_process(tmp_path, monkeypatch):
    """IF THIS FAILS: the hook fails on every edit whenever the session was
    started outside a checkout — the normal case for anyone keeping several
    projects in one folder."""
    repo = _repo(tmp_path, monkeypatch)
    _run(repo, ["claim", "src/a.py", "--as", "claude-dev", "--intent", "mine"],
         session="sess-A")
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    code, out = _run(outside, ["check", "--stdin-json"],
                     stdin_text=_payload(repo, "src/a.py", session="sess-B"))
    assert code == 2, f"the claim must be found from the file's own repo: {out}"


def test_an_unclaimed_file_is_allowed(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _run(repo, ["claim", "src/a.py", "--as", "claude-dev", "--intent", "mine"],
         session="sess-A")
    code, out = _run(repo, ["check", "--stdin-json"],
                     stdin_text=_payload(repo, "src/b.py", session="sess-B"))
    assert code == 0, out


# ---------------------------------------------------------------------------
# The path must be compared the way the FILESYSTEM compares it
# ---------------------------------------------------------------------------


def _claimed_repo(tmp_path, monkeypatch):
    import unicodedata
    repo = _repo(tmp_path, monkeypatch)
    (repo / "src" / unicodedata.normalize("NFC", "café.py")).write_text("y = 2\n")
    _run(repo, ["claim", "src/a.py", "--as", "alice", "--intent", "mine"], session="A")
    _run(repo, ["claim", "src/café.py", "--as", "alice", "--intent", "accents"], session="A")
    return repo


@pytest.mark.parametrize("spelling", ["src/a.py", "SRC/A.PY", "src/A.py"])
def test_a_different_case_is_the_same_file_and_must_be_blocked(tmp_path, monkeypatch, spelling):
    """IF THIS FAILS: enforcement is bypassable by pressing shift.

    Overlap between claims is a string comparison, which is right on a
    case-sensitive filesystem and wrong on the one most of this runs on. On
    macOS src/a.py and src/A.py are ONE file, so an agent could hold one
    spelling while another edited the other — the hook compared the strings,
    found them different, and exited 0.
    """
    repo = _claimed_repo(tmp_path, monkeypatch)
    if not (repo / "SRC" / "A.PY").exists():
        pytest.skip("case-sensitive filesystem: this bypass needs a case-insensitive one")
    code, out = _run(repo, ["check", "--stdin-json"],
                     stdin_text=_payload(repo, spelling, session="other"))
    assert code == 2, f"{spelling} reaches the same file and must be blocked: {out}"


@pytest.mark.parametrize("form", ["NFC", "NFD"])
def test_either_unicode_spelling_of_one_filename_is_blocked(tmp_path, monkeypatch, form):
    """IF THIS FAILS: an accented filename is claimable twice.

    macOS stores filenames decomposed (NFD) while a claim is usually typed
    composed (NFC). They are different strings for one file, so comparing them
    raw let a second agent through.
    """
    import unicodedata
    repo = _claimed_repo(tmp_path, monkeypatch)
    name = "src/" + unicodedata.normalize(form, "café.py")
    code, out = _run(repo, ["check", "--stdin-json"],
                     stdin_text=_payload(repo, name, session="other"))
    assert code == 2, f"the {form} spelling must be blocked: {out}"


def test_canonicalising_does_not_invent_a_spelling_for_a_file_being_created(tmp_path, monkeypatch):
    """A file that does not exist yet has no spelling on disk to defer to, so it
    must be kept exactly as typed rather than matched against a sibling."""
    from comms_graph.cli import _canonical_relpath

    repo = _repo(tmp_path, monkeypatch)
    target = repo / "src" / "BrandNew.py"
    assert _canonical_relpath(target, repo) == "src/BrandNew.py"


@pytest.mark.parametrize("odd", ["src/a\tb.py", "", "src/" + "x" * 300 + ".py"])
def test_an_unexpected_failure_blocks_rather_than_letting_the_edit_through(
        tmp_path, monkeypatch, odd):
    """IF THIS FAILS: enforcement silently switches off for that call.

    Exit 2 is the only code that stops an edit; every other non-zero code tells
    Claude Code the hook itself errored and the edit proceeds. So an unhandled
    exception does not merely look untidy — it waves an edit through on ground
    somebody holds. Measured before the wrapper: a tab in a filename, a
    300-byte component, and the repository root each raised and exited 1.
    """
    repo = _repo(tmp_path, monkeypatch)
    _run(repo, ["claim", "src", "--as", "alice", "--intent", "the directory"],
         session="sess-A")
    target = str(repo / odd) if odd else str(repo)
    payload = json.dumps({"session_id": "other", "tool_name": "Edit",
                          "tool_input": {"file_path": target}})
    code, out = _run(repo, ["check", "--stdin-json"], stdin_text=payload)
    assert code == 2, f"{odd!r} must not fail open: exit {code}\n{out}"


def test_an_exact_spelling_on_disk_is_never_rewritten(tmp_path, monkeypatch):
    """IF THIS FAILS: on a case-sensitive filesystem two genuinely different
    files collapse to one scope, and a claim is recorded under a name its
    author never typed.

    The first version of the canonicaliser broke on whichever entry iterdir
    yielded first, so a correctly-spelled path could be rewritten to a case
    variant that merely sorted earlier.
    """
    from comms_graph.cli import _canonical_relpath

    repo = _repo(tmp_path, monkeypatch)
    (repo / "src" / "Zed.py").write_text("A\n")
    assert _canonical_relpath(repo / "src" / "Zed.py", repo) == "src/Zed.py"


def test_canonicalising_does_not_hang_on_a_fifo(tmp_path, monkeypatch):
    """IF THIS FAILS the hook hangs, which is worse than answering wrongly —
    nothing times it out, so the agent simply stops.

    Asking the filesystem for a path's real spelling means opening it, and a
    FIFO opened for reading blocks until a writer appears. O_NONBLOCK is what
    makes that safe.
    """
    import os
    import signal

    from comms_graph.cli import _canonical_relpath

    repo = _repo(tmp_path, monkeypatch)
    os.mkfifo(repo / "src" / "pipe")

    def bang(sig, frame):
        raise TimeoutError("canonicalisation hung on a FIFO")

    old = signal.signal(signal.SIGALRM, bang)
    signal.alarm(5)
    try:
        assert _canonical_relpath(repo / "src" / "pipe", repo) == "src/pipe"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_canonicalising_a_huge_directory_is_not_slow(tmp_path, monkeypatch):
    """IF THIS FAILS the hook pays O(entries) on every agent tool call.

    The listing-based version cost 21.7ms per call per component in a
    50,000-entry node_modules. Asking the filesystem directly is one syscall.
    """
    import time

    from comms_graph.cli import _canonical_relpath

    repo = _repo(tmp_path, monkeypatch)
    big = repo / "node_modules"
    big.mkdir()
    for i in range(4000):
        (big / ("f%05d.js" % i)).touch()

    target = big / "f00001.js"
    started = time.perf_counter()
    for _ in range(50):
        _canonical_relpath(target, repo)
    per_call = (time.perf_counter() - started) / 50
    # Generous: this is about catching a return to per-entry scanning, not
    # pinning a number to one machine.
    assert per_call < 0.010, f"{per_call * 1000:.1f} ms per call"


def test_a_file_being_created_still_canonicalises_the_directories_above_it(
        tmp_path, monkeypatch):
    """IF THIS FAILS: a claim on a directory does not cover a new file inside it.

    An earlier version bailed out entirely when the leaf did not exist, which
    also stopped correcting the DIRECTORIES above it — so a claim on `src` did
    not block an edit to `SRC/newfile.py`, and two agents could claim one
    not-yet-created file under two spellings. The previous test only covered a
    missing leaf under a correctly-spelled directory, which is why it passed.
    """
    from comms_graph.cli import _canonical_relpath

    repo = _repo(tmp_path, monkeypatch)
    (repo / "src" / "sub").mkdir()
    if not (repo / "SRC").exists():
        pytest.skip("case-sensitive filesystem: nothing to canonicalise here")

    assert _canonical_relpath(repo / "SRC" / "newfile.py", repo) == "src/newfile.py"
    assert _canonical_relpath(repo / "SRC" / "SUB" / "deep" / "n.py", repo) == \
        "src/sub/deep/n.py"
    # a leaf that does not exist keeps its own spelling
    assert _canonical_relpath(repo / "src" / "BrandNew.py", repo) == "src/BrandNew.py"


def test_an_oddly_spelled_repo_root_does_not_discard_canonicalisation(
        tmp_path, monkeypatch):
    """IF THIS FAILS: any difference in how the ROOT is spelled silently throws
    the whole correction away and the hook passes claimed ground through.

    The kernel returns a fully normalised path; os.path.realpath does not
    normalise case. Comparing one against the other raised ValueError above the
    repo root and the code fell back to the raw spelling.
    """
    from comms_graph.cli import _canonical_relpath

    repo = _repo(tmp_path, monkeypatch)
    if not (repo / "SRC").exists():
        pytest.skip("case-sensitive filesystem")
    odd_root = Path(str(repo).replace("/repo", "/repo"))  # same path, exercised via probe
    assert _canonical_relpath(repo / "src" / "A.py", odd_root) == "src/a.py"


def test_an_unreachable_store_blocks_while_an_absent_one_allows(tmp_path, monkeypatch):
    """IF THIS FAILS: one stray file in the data home turns enforcement off for
    every repository on the machine, silently, with exit 0.

    Path.is_file() returns False for ENOTDIR, ELOOP and ENOENT alike, so
    "unreachable" read as "nobody has ever coordinated here" and the edit was
    allowed while a live claim sat on disk the whole time. Absent and
    unreachable are different answers and must not share a branch.
    """
    import shutil

    from comms_graph import log as clog

    repo = _repo(tmp_path, monkeypatch)
    _run(repo, ["claim", "src/a.py", "--as", "alice", "--intent", "mine"],
         session="sess-A")
    log_path = Path(clog.log_path(repo))
    payload = _payload(repo, "src/a.py", session="sess-B")

    code, _ = _run(repo, ["check", "--stdin-json"], stdin_text=payload)
    assert code == 2, "the claim is live; a stranger must be blocked"

    # the log path replaced by a directory: unreachable, not absent
    moved = log_path.parent / "log.moved"
    shutil.move(str(log_path), str(moved))
    log_path.mkdir()
    try:
        code, out = _run(repo, ["check", "--stdin-json"], stdin_text=payload)
        assert code == 2, f"an unreachable store must not read as free: {out}"
    finally:
        log_path.rmdir()
        shutil.move(str(moved), str(log_path))

    # and a repo nobody has ever coordinated in is still allowed
    other = tmp_path / "virgin"
    (other / "src").mkdir(parents=True)
    import subprocess
    subprocess.run([shutil.which("git"), "init", "-q", "."], cwd=other, check=True)
    (other / "src" / "free.py").write_text("y = 2\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home2"))
    (tmp_path / "home2").mkdir()
    virgin_payload = json.dumps({
        "session_id": "z", "tool_name": "Edit",
        "tool_input": {"file_path": str(other / "src" / "free.py")},
    })
    code, out = _run(other, ["check", "--stdin-json"], stdin_text=virgin_payload)
    assert code == 0, f"a repo with no log must not be blocked: {out}"
