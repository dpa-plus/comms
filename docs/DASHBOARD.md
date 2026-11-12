# The board

The board is a web page for the person watching the agents. The README has the
short version. This page has the rest.

```bash
comms ui                  # http://127.0.0.1:7878, every project in one tab
comms ui --port 7879      # another port
comms --repo /path/to/repo ui
```

`comms ui` and `comms-graph ui` are the same board. To keep it running all the
time on macOS, see [Run it at login](#run-it-at-login-macos).

## What it shows

It opens on **the work**: a graph of open tasks, labelled by their titles rather
than their ids. **Show completed** adds finished work without hiding open tasks,
and **List** shows the same tasks as a list. The **Team** panel shows who has
reported recently and who holds files. The **Projects** rail scopes the view to one
project. A last-reported time shows that an agent sent an update, not that it is
still running.

Click a node to focus its connections, then **Details** for its checks, files and
history. Each node names its owner and how many files it holds. **History** opens
the project timeline, and the history button next to an agent filters it to that
agent. **Load earlier** fetches older events, including those of agents that have
left the Team panel. Technical details stay collapsed until you open them.

The board lists every comms project on the machine. It leaves out stores whose
repository folder was deleted and stores that live in a temp folder, because those
are not projects anyone is working on.

The **Team** panel includes agents that reported within the last hour, plus anyone
holding a claim, however old. A quiet claim is only a hint, and nothing expires on its
own. The panel also separates changed files nobody claimed from claimed work, so an
empty list of claims does not suggest that nobody is working.

## Arrows and lines

Solid arrows run from a task to the task it unlocks. Dashed lines connect tasks
whose code is related in the code map, and say nothing about order. Code lines
appear around the selected task, up to twelve at first, with an option to show
more. Links to hidden completed tasks are counted separately. When there is no code
map, the board says so instead of showing the tasks as unrelated.

**Connections** opens the selected project's **Task graph** or **Code map** for a
closer look. Build the code map with `graphify extract . --code-only` in the
repository.

## It reads, with one exception

The log is written under a lock, through a fold that enforces the rules. A board
that wrote around both would be a second writer with none of those guarantees. So
the only button that changes anything is **Release**, which frees a claim somebody
else holds. It takes the same lock and appends the same event that
`comms release <claim-id> --force` does.

The confirmation shows the exact file and its holder, and asks for your name and a
reason. The name field is filled from `?actor=name` in the URL, from your last
successful release in this browser, or from the board's `COMMS_ACTOR`, and you can
change it. The release is recorded with your name as `arbitrator` and the holder as
`original_actor`. **Cancel** changes nothing. Releasing a claim does not stop the
agent and does not undo its edits.

## Live updates

The browser receives snapshots over Server-Sent Events. The server checks for a
change every half second, and sends a fresh snapshot after about ten seconds
without one. **Live** appears only after a valid snapshot has rendered. A failed
update keeps the previous view and shows a warning, and a delayed stream is labelled
as delayed.

Search, selection, zoom and scroll stay in place through live updates within one
project. Switching projects resets them. Titles wrap instead of being cut off, and
the layout does not move on its own.

## Upgrades refresh it

Every snapshot carries a fingerprint of the page, and the server also fingerprints
the installed comms source files. When the installed files change and stay stable
for a moment, the board restarts itself on the new code. Under `launchd`, the
service starts a fresh process. A board started by hand replaces its own process.
Every open tab reconnects, sees the new fingerprint, and reloads. Nothing is lost,
because the board only reads the log again.

`comms doctor --fix`, which the installer runs, also restarts the board service when
it reports an older version than the one installed.

## Run it at login (macOS)

```bash
comms doctor --fix --board
```

This writes `~/Library/LaunchAgents/plus.dpa.comms-ui.plist`, which runs
`comms-graph ui` from your install, and loads it with `launchctl`. The board then
starts at login and restarts if it stops. Its output goes to `/tmp/comms-ui.log`.
`comms doctor` reports whether the service is loaded and on the current version.

To stop it and remove it:

```bash
launchctl bootout "gui/$(id -u)/plus.dpa.comms-ui"
rm ~/Library/LaunchAgents/plus.dpa.comms-ui.plist
```

## Upgrades and running agents

Upgrading never disturbs an agent's work. Claims, tasks and notes live in the log,
not in the program, and the log format does not change between versions. Each comms
command starts fresh, so an agent uses the new version on its next command with no
restart. The board is the one long-running process, and it refreshes itself as
described above.
