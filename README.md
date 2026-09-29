# xiv-mac-tools

Watchers and one command that keep a two-client FFXIV setup on XIV on Mac honest. Lives at
`~/Library/Application Support/XIV on Mac/wedge-watch` on the machine it watches.

| Piece | Job |
|---|---|
| `portwatch.py` (launchd `xivportwatch`, 4 s) | classifies each boot, sweeps orphaned renderers, syncs meter settings between windows, detects an IINACT parser stall from its network log and thread-samples the game |
| `xivboot.py` (launchd `xivboot`) | boot monitor: first frame, plugin load, samples of wedged boots |
| `netwatch.py` (launchd `xivnetwatch`) | captures the in-game HTTP failures with thread samples |
| `bin/xivport` | `xivport`, `clean`, `sync`, `sample` |
| `register_dev_plugin.py` | register a locally built plugin as a Dalamud dev plugin |
| `upstream_sync.py` (launchd `xivupstream`, hourly) | when a plugin's upstream moves, runs the fork's `upstream-sync` workflow on GitHub, then downloads the build it published, checks its hashes and Dalamud API level, installs it while no game runs, and fast-forwards the local clone; the same tick copies `dalamud.log` / `dalamud.old.log` into `log-archive/` while no game runs (one copy per session, newest 30 kept), since Dalamud keeps only one session back and only its first 10 MB; after a game patch, once the game-data source has caught up and no game runs, regenerates the GatherBuddy Reborn lists and re-applies the settings policy; once a week rebuilds Codex's wiki data and hands it to the plugin; when GatherBuddy Reborn or Wrath Combo changes version, re-checks its settings policy and re-applies it with the game closed |
| `attention.py` | the watchers' notes to the player: one line per source in Overlay Doctor's attention file, shown at login and repeated in chat every half hour until cleared (`/overlays ack` in game, or the source clears it) |
| `test_portwatch.py`, `test_upstream_sync.py` | the suites; run before every commit |

New machine: clone to the path above, run `./install.sh`. The overlay port rule (10500 + the Browsingway cache slot)
is shared with the Browsingway fork; change it in both or not at all. IINACT's network log is kept in
`~/Library/Application Support/XIV on Mac/iinact-logs`, not in Documents: a launchd agent cannot read the
folders macOS gates (Desktop, Documents, Downloads), so the watcher moves the log there the first time it
finds it unreadable while no game runs. Logs, samples and config backups are never committed.
