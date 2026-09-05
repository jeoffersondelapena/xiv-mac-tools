# xiv-mac-tools

Watchers and one command that keep a two-client FFXIV setup on XIV on Mac honest. Lives at
`~/Library/Application Support/XIV on Mac/wedge-watch` on the machine it watches.

| Piece | Job |
|---|---|
| `portwatch.py` (launchd `xivportwatch`, 4 s) | arms the IINACT port for the next window, classifies each boot, sweeps orphaned renderers, syncs meter settings between windows, detects an IINACT parser stall from its network log and thread-samples the game |
| `xivboot.py` (launchd `xivboot`) | boot monitor: first frame, plugin load, samples of wedged boots |
| `netwatch.py` (launchd `xivnetwatch`) | captures the in-game HTTP failures with thread samples |
| `bin/xivport` | `xivport`, `clean`, `pin`, `unpin`, `urls`, `sync`, `sample` |
| `swap_iinact.py`, `register_dev_plugin.py` | install a locally built plugin as a Dalamud dev plugin |
| `test_portwatch.py` | the suite; runs before every commit |

New machine: clone to the path above, run `./install.sh`. Logs, samples and config backups are
never committed.
