---
name: using-comms
description: Keep off other agents' files in a shared repository through comms (file claims and notes). Use when the session briefing says comms is on in this repository, or when the user asks to use, join, check, claim with, or release through comms.
---

# Using comms

comms does one thing. It keeps agents that share one working tree off each other's files. It does not ask you to plan, review, record or report anything. How the work is planned and checked is decided by you and whoever you work for.

`comms` and `comms-graph` are the same command.

## What happens without you

- At session start, in a repository that uses comms, you get a short briefing with your actor name, the files other agents hold, and notes addressed to you.
- Every Edit, Write and NotebookEdit is checked. If another session holds the file, the edit is refused and you are told who holds it. If the file is free, your first edit claims it for your session.
- At session end, the claims made that way are released. One also lapses when someone else wants the file, the claim is over five minutes old, and the file has nothing uncommitted.
- Where the commit guard is installed, a commit that would take another agent's claimed file is refused.

You do not need to say hello, and you do not need `--as` on every call. Your session already has a name.

## What is still yours to do

1. **Claim before a shell write.** `sed -i`, a heredoc, a formatter, a code generator, an i18n command: none of these go through the edit check. Claim the paths first.

   ```bash
   comms claim src/auth.ts src/login.ts --intent "rework the session cookie"
   ```

   Several paths in one call are taken together or not at all.

2. **Give parallel subagents their own names.** A subagent shares your session, so the edit check cannot tell it from you or from its siblings. When you send out subagents that write files in parallel, give each one a name, and tell it to pass `--as <name>` on every comms call and to claim before writing. A claim under one name blocks a claim under another.

3. **Release when the work is committed.**

   ```bash
   comms release --all-mine --result "committed 4f2a9c1"
   ```

4. **Stop at a refusal.** A refused edit is the tool working. Do not write the file another way, under another name, or through a subagent. If the refusal prints a `--steal` command, the holder has been silent for over an hour and you may run it. Otherwise leave a note and take other work:

   ```bash
   comms note "@claude-billing I need src/invoice.ts for the rounding fix, tell me when it is free"
   ```

   A note that names an actor is shown to that actor on its next comms command.

comms tracks files only. It cannot see a migration number, a port, a dev server or a GPU. If another agent could collide with you on one of those, a note is the way to say so.

## Names

Your session is named for you, for example `claude-4f9a1c`, and a call with no `--as` uses that name. To work under a role name, pass `--as claude-billing` on each call. A name holds only the claims made under it, so release with the name you claimed with. `claude`, `codex`, `agent` and `user` are refused, because they do not tell two agents apart.

## Looking around

```bash
comms status                    # your claims, who else holds what, notes to you
comms board                     # the same, plus what is changed on disk and unclaimed
comms check src/a.ts src/b.ts   # would an edit here be refused?
```

Each prints a short view. Add `--all` for everything.

A file that is changed on disk with no claim on it belongs to nobody on record. Ask in a note before you edit it.

## Tasks are optional

comms never requires a task. If the user or your lead wants the work to show on the board under a title, add one and tag your claims with it:

```bash
comms task add stay-signed-in --title "Let people stay signed in after a restart"
comms claim src/auth/session.ts --task stay-signed-in --intent "server-side sessions"
comms task done stay-signed-in
```

Write the title in plain English for a person who does not read code.

## When something is off

- `operation not permitted` on the working directory: macOS withdrew access to a Desktop or Documents folder. Run from another directory and name the repository: `comms --repo /abs/path status`. Never change `HOME`.
- A comms command behaves differently from this page: run `comms doctor`. It reports a stale install, missing hooks, or an old copy of this skill, with the command that fixes each.
- comms refuses something it should allow, or allows something it should refuse: tell the user the exact command and its output.
