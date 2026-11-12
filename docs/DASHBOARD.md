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

It opens on **who holds which files**. Each agent that holds something gets one
group: its name, its label and model when its hello recorded them, and how long
ago it was last active. An agent silent for over an hour is marked, and the board
puts it first, because a silent holder is the one thing on the board that may
need a person. Under each agent, every file it holds shows the agent's intent in
its own words and how long the file has been held. A file the edit check claimed
on first touch reads as "editing". Each file has a **Release** button.

Below the holds are the changed files nobody holds and the **Recent activity**
feed of claims, releases, notes and refusals. comms reads `git status` for the
changed files and never guesses who made an unclaimed change, so an empty list of
holds does not suggest that nobody is working.

**History** opens the project timeline, and the history button next to an agent
filters it to that agent. **Load earlier** fetches older events. **Code map**
opens the project's code map. Build it with `graphify extract . --code-only` in
the repository.

The **Team** panel lists agents that reported within the last hour, plus anyone
holding a file, however old. A last-reported time shows that an agent sent an
update, not that it is still running, and nothing expires on its own.

The **Projects** rail lists every comms project on the machine and scopes the
view to one. Run as a login service, the board has no project of its
own, so it opens the most recently active one. It leaves out stores whose repository folder was deleted and stores
that live in a temp folder, because nobody works in those.

Task and finding events in an old log stay out of the feed and out of History.

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

Upgrading never disturbs an agent's work. Claims and notes live in the log,
not in the program, and the log format does not change between versions. Each comms
command starts fresh, so an agent uses the new version on its next command with no
restart. The board is the one long-running process, and it refreshes itself as
described above.
