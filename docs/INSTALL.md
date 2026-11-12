# Install and set up comms

comms runs on macOS and Linux and needs Python 3.11 or newer. The installer puts
it in your home folder and needs no administrator rights.

## Install

Run the installer from the latest release:

```bash
curl -fsSL https://github.com/dpa-plus/comms/releases/latest/download/install.sh | sh
```

To install from a checkout of this repository instead, run this from its root:

```bash
sh install.sh --from python
```

The installer does four things:

1. It creates a Python environment at `~/.local/share/comms-graph`, with `uv` if you
   have it and `python3 -m venv` if not.
2. It installs the package into that environment.
3. It links `comms`, `comms-graph` and `graphify` into `~/.local/bin`. A real file
   already at one of those paths is left alone and reported.
4. It runs `comms-graph doctor --fix`, then prints the installed version.

If the installer says `add ~/.local/bin to your PATH`, do that in your shell
profile. If it says another `comms` on your `PATH` shadows the new one, remove the
other one or put `~/.local/bin` first.

## Check the install with `comms doctor`

`comms doctor` prints one line per check. Each line starts with `ok` or `fix`, and
a `fix` line is followed by the command that repairs it. The exit code is 0 when
every check passes and 1 when something still needs fixing.

| Check | Passes when |
|---|---|
| **comms-graph on PATH**, **comms on PATH** | Both commands are found and report this version. |
| `claude hooks` | `~/.claude/settings.json` has the three comms hooks, pointing at this install. |
| `skill copies` | `~/.claude/skills/using-comms/SKILL.md` and `~/.codex/skills/using-comms/SKILL.md` match the skill inside the package. |
| `board job` | The board runs as a login service and reports this version. Skipped where there is no `launchd`. |
| `data home` | The folder that holds the logs exists and is writable. |
| `commit guard` | The repository you run it in has the commit guard. Skipped outside a git repository. |

`comms doctor --fix` repairs what it can and prints each change:

- It links the two commands into `~/.local/bin`.
- It writes three hooks into `~/.claude/settings.json`. If the hooks in that file
  are not in a shape it can rewrite safely, it says so and leaves the file alone.
  Otherwise it saves a timestamped
  backup first, keeps every hook that is not a comms hook, and replaces old comms
  hooks:
  - `SessionStart` runs `comms-graph hook session-start`.
  - `PreToolUse`, for `Edit|Write|NotebookEdit`, runs `comms-graph hook pre-edit`.
  - `SessionEnd` runs `comms-graph hook session-end`.
- It copies the skill to `~/.claude/skills/using-comms/` and `~/.codex/skills/using-comms/`.
  A copy that is a symbolic link is left alone.
- It creates the data home if it is missing.
- If the board service is installed, it restarts it on the new version.

`--fix` does not install the commit guard, because the guard belongs to one
repository. See [Turn on the commit guard](#turn-on-the-commit-guard).

## Run the board at login (macOS)

```bash
comms doctor --fix --board
```

This writes `~/Library/LaunchAgents/plus.dpa.comms-ui.plist` and loads it with
`launchctl`. The board then starts at login, restarts if it stops, and answers at
`http://127.0.0.1:7878`. Its output goes to `/tmp/comms-ui.log`.
[`DASHBOARD.md`](DASHBOARD.md) describes the board itself.

## Turn on the commit guard

The edit check cannot see a file written from the shell. The commit guard can. It
refuses a commit that includes a file another agent has claimed. Install it once in
each repository you want protected:

```bash
cd /path/to/your-repo
comms guard install
```

If the repository already has a pre-commit hook that comms did not write, the
install refuses. Run `comms guard install --chain` to keep that hook. comms then moves it
aside, runs it first, and keeps its veto. `comms guard status` shows whether the
guard is installed, and `comms guard uninstall` removes it and puts a chained hook
back.

## Set up Codex

Codex has no hooks here, so nothing claims, checks or releases on a Codex agent's
behalf, and comms cannot tell one Codex session from another. Two things make up
for it:

- The installer copies the skill to `~/.codex/skills/using-comms/`.
- Each repository gets instructions at the top of its `AGENTS.md`. Copy
  [`examples/AGENTS.md.template`](../examples/AGENTS.md.template) there. It tells
  the agent to pick a name, pass `--as <name>` on every call, claim before every
  edit, release when done, and run `comms check --staged` before every commit.

## Upgrade

Run the installer again. It reinstalls the package even when the version number
has not changed, relinks the commands, and runs `doctor --fix`, which also updates
the hooks, the skill copies and a running board service.

The log format does not change between versions, so claims, tasks and history
carry over. An agent picks up the new version on its next command.

## Uninstall

1. In each repository with the commit guard, run `comms guard uninstall`.
2. If you installed the board service, stop it and delete its file:

   ```bash
   launchctl bootout "gui/$(id -u)/plus.dpa.comms-ui"
   rm ~/Library/LaunchAgents/plus.dpa.comms-ui.plist
   ```

3. Open `~/.claude/settings.json` and delete the three hook entries whose command
   ends in `hook session-start`, `hook pre-edit` or `hook session-end`.
4. Delete the skill copies, the links and the environment:

   ```bash
   rm -r ~/.claude/skills/using-comms ~/.codex/skills/using-comms
   rm ~/.local/bin/comms ~/.local/bin/comms-graph ~/.local/bin/graphify
   rm -r ~/.local/share/comms-graph
   ```

The logs stay in the data home (`~/Library/Application Support/comms/` on macOS,
`~/.local/share/comms/` on Linux). Delete that folder too if you want the history gone.

## Recover from "operation not permitted" on macOS

An agent may report `operation not permitted`, `uv_cwd operation not permitted` or
`fatal: Unable to read current working directory` in a repository under Desktop,
Documents or Downloads. The repository is fine. macOS has withdrawn the app's access
to that folder, so commands that start from inside it fail.

Run comms from a directory the app can read, and name the repository:

```bash
cd ~
comms --repo /Users/you/code/my-project status
```

`--repo` takes the path you give as the repository root and never reads the current
directory. Put it before the verb. If the app cannot read that path at all, grant it
Full Disk Access in System Settings and restart it, or move the repository out of
the protected folder. Do not change `HOME` to work around this, because comms would then
write to a different log from every other agent.

## Where things live

| Path | What it is |
|---|---|
| `~/.local/share/comms-graph/` | The Python environment comms runs in. |
| `~/.local/bin/comms`, `comms-graph` | Links to the two commands. |
| `~/.claude/settings.json` | The three Claude Code hooks. |
| `~/.claude/skills/using-comms/`, `~/.codex/skills/using-comms/` | The skill copies. |
| `~/Library/LaunchAgents/plus.dpa.comms-ui.plist` | The board service, if installed. |
| `<data home>/comms/<repo-hash>/` | One store per repository: the log, its lock, liveness markers. |
| `<repo>/.git/hooks/pre-commit` | The commit guard, if installed. |
| `<repo>/.comms/docs/` | Notes written with `comms doc`, committed with the repository. |

The data home is `~/Library/Application Support` on macOS and `~/.local/share` (or
`XDG_DATA_HOME`) on Linux. Set `COMMS_HOME` to an absolute path to use another
folder. A repository under the system temp folder keeps its store in
`<temp>/comms-stores/` instead, so scratch repositories do not fill the data home.
