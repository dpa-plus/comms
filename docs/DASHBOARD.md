# The board

The README has the short version. This is the rest of it.


```bash
COMMS_ACTOR=human-you comms-graph ui   # http://127.0.0.1:7878, every project in one tab
```

It opens on **what just happened**, newest first, with the things that need
somebody pulled to the top. Around it: who is holding which files right now, the
roster of who is actually here, the tasks with the files each one touches, and
every project on this machine down the left side.

**It reads. It does not write**, with exactly one exception. The log is appended
under a lock, through a fold that enforces the rules, and a dashboard writing
around either would be a second writer with none of those guarantees. So the only
button that changes anything is **Release**, which frees a claim somebody else is
holding, and it goes through the same lock and appends the same event the CLI
does. It asks for a reason and refuses without one, because the release is
recorded under your name permanently and "who freed this and why" is the only
question anybody asks afterwards.

> **Who signs the release.** The first time you free something, the board asks
> for your name (e.g. `human-you`) and remembers it in that browser; `?actor=name`
> in the address bar sets or changes it. The release is recorded under that name,
> with the holder as `original_actor` and you as `arbitrator`, the same way
> `release --force` records it. A board started with `COMMS_ACTOR` set signs as
> that name instead and never asks. With neither, it refuses: a release with no
> author is worse than no release, because the ground is gone and the log cannot
> say who took it.

It is **unified by default**: one window for every comms project on the machine. The **Projects** rail lists them and clicking one scopes the whole view. It lists real projects only. A store whose directory has been deleted, or which lives in a temp folder, is not a project, and before that filter existed two real projects sat among 213 that were not.

The **Roster** shows who is here, meaning the last hour, plus anyone holding a claim whatever their age, because a stale claim is the one thing on the board that needs a person, and hiding its holder would hide the only name that can free it. Agents take a fresh name each session, so everyone who has ever said hello is a much longer and much less useful list; it is one click away.

Run it **once** and watch every repo. Agents never open anything. They write to their logs, which this board already sees.

The **work graph** shows the tasks in the selected project: an arrow means the
task it points at comes afterwards, and tasks joined to nothing sit apart from
the rest, because they are. Finished work compresses to a dashed outline so the
board shrinks as the project progresses; work waiting for a verifier is the
loudest thing on it, because it is finished *and* holding up everything
downstream. Layout is computed on the server, so an arrow can only point
rightward, so there is no geometry to get wrong in the browser.

It updates by **push, not polling.** A file watcher inside `comms ui` is notified by the operating system the instant any project's `log.jsonl` changes; it rebuilds the snapshot once and streams it to every open browser tab over [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events). So when any agent anywhere appends an event, the right project lights up in the sidebar **immediately**, and your laptop isn't burning cycles re-reading logs on a timer.

Every snapshot carries the server's **front-end build fingerprint**, and the page remembers the one it loaded with. So when you replace the binary and restart `comms ui`, every open tab notices the new build on the next push and **reloads itself** to the new dashboard, so no stale UI is lingering after an upgrade.

It **opens your browser automatically** when run interactively (`--no-open` to suppress). On macOS you can also double-click a **Comms Dashboard** launcher instead of using the terminal. The header shows the active **session name** (the name agents use, e.g. `acme-build`) next to the repo.

**One dashboard, two entry points.** `comms ui` and `comms-graph ui` are the
same board: the Go build no longer serves a dashboard of its own and hands the
job straight to the Python build, replacing its own process so Ctrl-C and any
supervisor still work.

There were two once, reading the same log and drawing different pictures. The
second fell behind and nobody noticed until it was opened, by which point it was
listing 176 projects by hash while the other had had real names for a week.
Building every improvement twice is a cost that gets paid in exactly that way.

```bash
comms ui                       # the board, on 127.0.0.1:7878
comms ui --addr 0.0.0.0:9000   # split into --host and --port for you
comms --repo /path/to/repo ui  # scope it to one repository
comms-graph ui --graph out/graph.json   # every flag the board itself takes
```

`--demo`, `--all`, `--open` and `--stale-after` are accepted and ignored. They
live in muscle memory and in a committed launchd template, and refusing them
would turn a rename into an outage for whoever least expected one.

### Run the dashboard as a login service (macOS)

So the dashboard is always up. It survives reboots, and is restarted automatically if it ever exits. Install it as a per-user `launchd` agent. The template sets `COMMS_ACTOR`, so releases from the board are signed without the page asking (see the note above); change it from `operator` to your own name first:

```bash
# Set your operator name (and point at your binary if it is not Homebrew, `which comms`):
#   sed -i '' "s#<string>operator</string>#<string>human-you</string>#" contrib/launchd/plus.dpa.comms-ui.plist
install -m644 contrib/launchd/plus.dpa.comms-ui.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/plus.dpa.comms-ui.plist
```

After installing a new binary, restart the service to pick it up (open tabs then auto-reload, see above):

```bash
launchctl kickstart -k "gui/$(id -u)/plus.dpa.comms-ui"
```

To remove it: `launchctl bootout "gui/$(id -u)/plus.dpa.comms-ui"` then delete the plist.

---

---

## Upgrading, and what happens to a running session

Because `comms` is just a binary that runs fresh on every command, upgrading is painless and **never disturbs an in-flight session**:

- **The session lives in the log file, not in the binary.** Claims, findings, and notes are on disk. Replacing the binary doesn't touch them.
- **CLI commands pick up the new version instantly**: the *next* `comms …` an agent runs uses the new binary. No restart, no re-join.
- **Only the dashboard's *process* needs a nudge.** `comms ui` is the one long-running process; it holds the old binary until you restart it. But once you do, the browser doesn't: every open tab sees the new build fingerprint on the next push and **reloads itself** (see [The live dashboard](#the-live-dashboard)). Restarting loses nothing. It just re-reads the same log.

```bash
go install github.com/dpa-plus/comms/cmd/comms@latest   # agents use it on their next command
# then restart the one long-running dashboard process; open tabs auto-reload:
launchctl kickstart -k "gui/$(id -u)/plus.dpa.comms-ui"  # if installed as a login service
# (otherwise: stop your `comms ui` and run it again)
```

---

