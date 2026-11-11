---
name: using-comms
description: Coordinate with other agents editing the same repository through comms (file claims, tasks, notes). Use when the session briefing says comms is on in this repository, or when the user asks to use, join, check, claim with, or release through comms.
---

# Using comms

comms keeps agents that share one working tree off each other's files. Each repository has one append-only log. `comms` and `comms-graph` are the same command.

## What happens without you

- At session start, in a repository that uses comms, you get a short briefing with your actor name, the files other agents hold, and notes addressed to you.
- Every Edit, Write and NotebookEdit is checked. If another session holds the file, the edit is refused and you are told who holds it. If the file is free, your first edit claims it for your session.
- At session end, the claims made that way are released. One also lapses once its file is committed and someone else wants it.
- Where the commit guard is installed, a commit that would take another agent's claimed file is refused.

You do not need to say hello, and you do not need `--as` on every call. Your session already has a name.

## What is still yours to do

1. **Claim before a shell write.** `sed -i`, a heredoc, a formatter, a code generator, an i18n command: none of these go through the edit check. Claim the paths first.

   ```bash
   comms claim src/auth.ts src/login.ts --intent "rework the session cookie"
   ```

   Several paths in one call are taken together or not at all. Use the same call to reserve a set of files before a change that spans them.

2. **Give subagents their own names and files.** A subagent shares your session, so the edit check cannot tell it from you or from its siblings. When you send out implementers in parallel, give each one a name and a file list, and tell it to pass `--as <name>` on every comms call and to claim before writing.

3. **Release when the work is committed.**

   ```bash
   comms release --all-mine --result "committed 4f2a9c1"
   ```

4. **Stop at a refusal.** A refused edit is the tool working. Do not write the file another way, under another name, or through a subagent. If the refusal prints a `--steal` command, the holder has been silent for over an hour and you may run it. Otherwise leave a note and take other work:

   ```bash
   comms note "@claude-billing I need src/invoice.ts for the rounding fix, tell me when it is free"
   ```

   A note that names an actor is shown to that actor on its next comms command.

5. **Say what comms cannot see.** It tracks files. It does not track migration numbers, ports, dev servers or a GPU. If you take one of those, say so in a note.

## Choosing a name

Your session is named for you, for example `claude-4f9a1c`. To use a role name, run this once:

```bash
comms hello --as claude-billing --label "Billing fixes"
```

Later calls in the session use that name. Pick a name that tells two agents apart. `claude`, `codex`, `agent` and `user` are refused.

## Looking around

```bash
comms status                 # your claims, who else holds what, notes to you
comms board                  # the same, plus what is changed on disk and unclaimed
comms check src/a.ts src/b.ts   # would an edit here be refused?
comms log --since 2h --type note
```

Each prints a short view by default. Add `--all` for everything, or `--json` for a script.

A file that is changed on disk with no claim on it belongs to nobody on record. Ask in a note before you edit it.

## Tasks

Use a task when the work is more than one edit, because the person watching reads the board, and the board shows tasks.

```bash
comms task add stay-signed-in --title "Let people stay signed in after a restart" --check test --review
comms claim src/auth/session.ts --task stay-signed-in --intent "server-side sessions"
comms task done stay-signed-in --check test=pass --note "Cookie is httpOnly, because the security review asks about XSS."
```

- The title is the one line a person outside the code reads. Write it in plain English, say what changes for the user, and leave out file names, function names and abbreviations. The id is only what you type.
- `--note` on `done` records a decision and its reason. The next agent reads it in `comms brief <id>`.
- Add `--review` to `task add` when someone else must check the work. Then `done` does not close the task, and a different agent does:

  ```bash
  comms task review stay-signed-in --pass --evidence "ran the suite, 14 pass; restarted and the session survived"
  comms task review stay-signed-in --fail --evidence "a spent refresh token still works: POST /session/refresh twice returns 200 both times"
  ```

  You cannot review your own work. Give one verdict and say how you checked. Do not negotiate with the author. If you disagree, bring in a third agent or the user.
- `comms next` lists what you could pick up. `comms brief <id>` shows what a task builds on.
- `comms task edge <from> <to> --kind consumes --provides "sessions table, uuid keys"` records that one task uses what another produces. Use `--kind sequence` for order alone.

## Findings

Record a fact that the next session will need, in one line:

```bash
comms find gotcha "META_TOKEN_ENC_KEY cannot change after the first deploy" --ref path:src/crypto.ts
comms find decision "the tracker is the source of truth for leads"
```

The categories are `bug`, `fix`, `ship`, `decision` and `gotcha`. A note is for a message to someone now. A finding is for a fact that stays true.

## The person watching

The user does not run these commands. They read the board in a browser at `http://127.0.0.1:7878`. It shows task titles, who holds which files, and what is changed on disk with no claim. They can release a claim from there. Tell them your actor name once so they can find you on it.

## When something is off

- `operation not permitted` on the working directory: macOS withdrew access to a Desktop or Documents folder. Run from another directory and name the repository: `comms --repo /abs/path status`. Never change `HOME`.
- A comms command behaves differently from this page: run `comms doctor`. It reports a stale install, missing hooks, or an old copy of this skill, with the command that fixes each.
- comms refuses something it should allow, or allows something it should refuse: tell the user the exact command and its output.
