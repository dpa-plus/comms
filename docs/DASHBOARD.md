# The board

The README has the short version. This is the rest of it.


```bash
COMMS_ACTOR=human-you comms-graph ui   # http://127.0.0.1:7878, every project in one tab
```

It opens on **the work**: a graph of open tasks, labelled by their names rather
than technical IDs. **Show completed** adds finished work without hiding open
tasks; **List** provides a simpler alternative. The **Team** panel shows who has reported recently
and who holds files; the **Projects** rail scopes the view to one project.
Last-reported times are evidence of an update, not proof an agent is still running.

Click a node to focus its connections, then **Details** for its checks, files
and history. Each node names its owner and hold count. **History** also opens
the project timeline; agent history buttons filter it to that person. Older
events remain available through **Load earlier**, including after an agent leaves
the current Team panel. Technical details stay collapsed until needed.

**It reads. It does not write**, with exactly one exception. The log is appended
under a lock, through a fold that enforces the rules, and a dashboard writing
around either would be a second writer with none of those guarantees. So the only
button that changes anything is **Release**, which frees a claim somebody else is
holding, and it goes through the same lock and appends the same event the CLI
does. It asks for a reason and refuses without one, because the release is
recorded under your name permanently and "who freed this and why" is the only
question anybody asks afterwards.

> **Who signs the release.** The inline confirmation shows the exact held scope
> and holder, and requires your operator name and a reason. The operator field
> is prefilled from `?actor=name`, a previously successful release in this browser,
> or the server's `COMMS_ACTOR`, and remains editable. A successful release records
> that name as `arbitrator` and the holder as `original_actor`. Cancel changes
> nothing. Releasing a hold does **not** stop the agent or discard its edits.

It is **unified by default**: one window for every comms project on the machine. The **Projects** rail lists them and clicking one scopes the whole view. It lists real projects only. A store whose directory has been deleted, or which lives in a temp folder, is not a project, and before that filter existed two real projects sat among 213 that were not.

The **Team** panel includes agents that reported within the last hour, plus anyone
holding a claim whatever its age. Quiet holds are advisory: nothing expires
automatically. The panel also distinguishes unclaimed changes on disk from
claimed work, so an empty hold list does not imply nobody is working.

Run it **once** and watch every repo. Agents never open anything. They write to their logs, which this board already sees.

The main graph distinguishes two kinds of connection: solid arrows run from a
prerequisite to the task it unlocks; dashed lines indicate related code, not a
dependency. Code-related lines appear around the selected task, with up to twelve
visible links initially and an explicit option to show more. Connections to
hidden completed tasks are counted separately. A missing code map is labelled
as unavailable, not mistaken for evidence that tasks are unrelated.

Search, selection, zoom and scroll remain in place through live updates within
one project. Switching projects resets those controls. Titles wrap rather than
being cut off, and keyboard focus stays on the search or selected node after an
update. The layout is static, with no moving physics simulation.

**Connections** still opens the selected project's secondary **Task graph** or
**Code map** for a deeper look. The code map describes code relationships, not
an inferred order of work. Recorded dependency kinds and notes remain available
inside a task's collapsed technical details.

The browser receives snapshots through **Server-Sent Events**. The server checks
for changes every half second and also sends a fresh snapshot after roughly ten
seconds without a change. Snapshot work takes additional time; updates are not
instantaneous. **Live** appears only after a valid snapshot has rendered, not
merely when a connection opens. Failed updates preserve the previous view and
show a warning; a delayed stream is labelled rather than presented as current.

Every snapshot carries the server's **front-end build fingerprint**, and the page remembers the one it loaded with. The server also fingerprints the installed `comms_graph` Python sources. Once a changed installation has stayed stable for a short debounce, the server closes cleanly and refreshes itself: launchd starts a fresh process, while a manually started `comms-graph ui` replaces itself directly. Every open tab reconnects, notices the new front-end build on the next push, and **reloads itself**, so no stale UI is lingering after an upgrade.

It **opens your browser automatically** when run interactively (`--no-open` to
suppress). On macOS you can also use a **Comms Dashboard** launcher. The main
heading identifies the selected project.

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

So the dashboard is always up. It survives reboots, and is restarted automatically
if it exits. Install it as a per-user `launchd` agent. The template's `COMMS_ACTOR`
prefills the release dialog; change it from `operator` to your own name first:

```bash
# Set your operator name (and point at your binary if it is not Homebrew, `which comms`):
#   sed -i '' "s#<string>operator</string>#<string>human-you</string>#" contrib/launchd/plus.dpa.comms-ui.plist
install -m644 contrib/launchd/plus.dpa.comms-ui.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/plus.dpa.comms-ui.plist
```

The first time you install the version that introduced automatic refresh, restart the already-running service once:

```bash
launchctl kickstart -k "gui/$(id -u)/plus.dpa.comms-ui"
```

That one restart is unavoidable: a process started from an older installation has no source watcher in memory yet. Later Python-package reinstalls are detected automatically; Comms data remains in the append-only log, and open tabs reconnect and reload themselves.

To remove it: `launchctl bootout "gui/$(id -u)/plus.dpa.comms-ui"` then delete the plist.

---

---

## Upgrading, and what happens to a running session

Because `comms` is just a binary that runs fresh on every command, upgrading is painless and **never disturbs an in-flight session**:

- **The session lives in the log file, not in the binary.** Claims, findings, and notes are on disk. Replacing the binary doesn't touch them.
- **CLI commands pick up the new version instantly**: the *next* `comms …` an agent runs uses the new binary. No restart, no re-join.
- **The dashboard refreshes itself after its installed Python source changes.** `comms ui` is the one long-running process, so it fingerprints the installed `comms_graph` `.py` files and waits for a stable change before refreshing. launchd's `KeepAlive` supplies the fresh service process; a manually started board replaces itself directly. Every open tab reconnects, sees the new build fingerprint on the next push, and **reloads itself** (see [The live dashboard](#the-live-dashboard)). Refreshing loses nothing. It just re-reads the same log.
- **One initial nudge is still required when adopting this version.** A dashboard already running older code cannot gain a watcher retroactively. Restart it once after that first install; subsequent Python-package installs refresh automatically.

```bash
go install github.com/dpa-plus/comms/cmd/comms@latest   # agents use it on their next command
# Once, when first adopting automatic dashboard refresh:
launchctl kickstart -k "gui/$(id -u)/plus.dpa.comms-ui"
# Later comms-graph package installs refresh the running dashboard themselves.
```

---
