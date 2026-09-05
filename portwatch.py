#!/usr/bin/env python3
"""Watches the two-client FFXIV setup from outside the game.

Classifies each boot per window (the IINACT port binding is the proof a boot finished), reports and
sweeps Browsingway renderers no live game accounts for, syncs the meter's settings between windows,
and detects an IINACT parser stall from its network log. Ports are no longer arranged here: each
window's Browsingway derives its port from its cache slot and IINACT in that window follows it.
"""
import os, re, sys, json, time, base64, shutil, subprocess, datetime

BASE = os.path.expanduser("~/Library/Application Support/XIV on Mac")
CFG = os.path.join(BASE, "pluginConfigs")
LOG = os.path.join(BASE, "wedge-watch", "portwatch.log")
BOOTLOG = os.path.join(BASE, "wedge-watch", "boots-per-window.log")
SYNCSTATE = os.path.join(BASE, "wedge-watch", "overlay-sync-state.json")
WEDGE_AFTER = 150   # a clean boot binds its port in well under a minute
KILLED_WEDGE_AFTER = 90   # closed before binding, but long past a normal boot: a wedge the user killed
PORTS = [10501, 10502]   # the fork derives its own port as 10500 + cache slot; keep these in step
SLOT_PREFIX = "cef-cache"
# Cactbot's settings live in IINACT's shared config, but kagerou keeps its own in browser
# localStorage, which is per CEF profile, so only the meter drifts between windows.
DRIFTING_OVERLAY = "DPS"

# A Wine command line starts with a drive-letter path; anything else merely quoting an .exe name
# (a shell running a script that mentions one, say) must not be mistaken for the process itself.
WINE_CMD_RE = re.compile(r'^[A-Za-z]:\\.*?\.exe(?=\s|$)')
PS_RE = re.compile(r'^\s*(\d+)\s+(\w{3} \w{3}\s+\d+ \d{2}:\d{2}:\d{2} \d{4})\s+([\d.]+)\s+(.*)$')


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def dalamud_enabled():
    """XIV on Mac's Dalamud toggle. With it off no IINACT loads, so no port ever binds and the boot
    classifier would call a perfectly good boot wedged."""
    out = subprocess.run(["defaults", "read", "dezent.XIV-on-Mac", "DalamudEnabled"], capture_output=True, text=True).stdout.strip()
    return out != "0"


def iinact_set_to_load():
    """Whether Dalamud will load IINACT at all: the dev build registered and enabled in the default
    profile, or the repo install present. With neither, no port binds and a good boot looks wedged."""
    try:
        cfg = json.load(open(os.path.join(BASE, "dalamudConfig.json")))
    except (OSError, ValueError):
        return True
    return iinact_enabled_in(cfg, os.path.isdir(os.path.join(BASE, "installedPlugins", "IINACT")))


def iinact_enabled_in(cfg, repo_installed):
    if repo_installed:
        return True
    locations = cfg.get("DevPluginLoadLocations", {}).get("$values", [])
    settings = cfg.get("DevPluginSettings", {})
    profile = cfg.get("DefaultProfile", {}).get("Plugins", {}).get("$values", [])
    for entry in locations:
        if not entry.get("Path", "").endswith("\\IINACT.dll") or not entry.get("IsEnabled"):
            continue
        plugin_id = settings.get(entry["Path"], {}).get("WorkingPluginId")
        if any(e.get("WorkingPluginId") == plugin_id and e.get("IsEnabled") for e in profile):
            return True
    return False


def initial_state(dalamud_on, iinact_on=True):
    return "pending" if dalamud_on and iinact_on else "untracked"


def classify_live(state, bound, age):
    """(new state, message or None) for a window still running."""
    if state == "pending" and bound is not None:
        return "ok", f"CLEAN boot, bound {bound} after {age:.0f}s"
    if state == "pending" and age > WEDGE_AFTER:
        return "wedged", f"WEDGED - no port after {age:.0f}s"
    if state == "wedged" and bound is not None:
        return "ok", f"recovered late, bound {bound} after {age:.0f}s"
    return state, None


def classify_gone(state, age):
    """(message or None) for a window that has disappeared."""
    if state == "pending":
        # Quitting a hung window is the commonest way a wedge ends, so age at close decides:
        # past a normal boot it counts as wedged, not merely abandoned.
        if age > KILLED_WEDGE_AFTER:
            return f"WEDGED - closed after {age:.0f}s without ever binding"
        return f"launch aborted after {age:.0f}s (before a boot would finish)"
    if state == "wedged":
        return "wedged window closed"
    return None


def boot_note(msg):
    """Per-window boot outcomes. xivboot can only see whichever instance wins the shared
    dalamud.log, so a second window's boots go unrecorded there entirely."""
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        with open(BOOTLOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    log(msg)


def procs(exe):
    """(pid, start epoch, %cpu, command) for Wine processes whose executable is `exe`."""
    out = subprocess.run(["ps", "-Ao", "pid=,lstart=,%cpu=,command="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        m = PS_RE.match(line)
        if not m:
            continue
        pid, when, cpu, cmd = m.groups()
        exe_match = WINE_CMD_RE.match(cmd)
        if not exe_match or not exe_match.group(0).endswith("\\" + exe):
            continue
        try:
            started = datetime.datetime.strptime(when, "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            continue
        found.append((int(pid), started, float(cpu), cmd))
    return sorted(found, key=lambda r: r[1])


def game_pids():
    return {r[0] for r in procs("ffxiv_dx11.exe")}


def sample_plan(games, now):
    """One thread-sample file per running game window, named so several stalls on one day sort."""
    stamp = now.strftime("%H%M%S")
    return [(pid, os.path.join(BASE, "wedge-watch", f"stall-sample-{stamp}-{pid}.txt")) for pid, *_ in games]


def sample_games():
    plan = sample_plan(procs("ffxiv_dx11.exe"), datetime.datetime.now())
    if not plan:
        print("no game window running - nothing to sample")
        return
    for pid, out in plan:
        print(f"sampling pid {pid} for 5 s ...", flush=True)
        subprocess.run(["sample", str(pid), "5", "-file", out], capture_output=True)
        print(f"  {out}")
    log("stall sample taken: " + ", ".join(str(pid) for pid, _ in plan))


# IINACT's network log is the one place a parser stall shows while the game runs: chat lines are
# fed straight from Dalamud's chat hook and keep coming, while everything the parser produces
# (ability lines, combatant-memory lines) stops. Combat chat with no parser lines is that stall.
NETLOG_DIR = os.path.join(BASE, "wineprefix", "drive_c", "users",
                          os.path.basename(os.path.expanduser("~")), "Documents", "IINACT")
STALL_LOG = os.path.join(BASE, "wedge-watch", "iinact-stalls.log")
COMBAT_CHAT = set(range(0x29, 0x34))   # damage, actions, healing, effects gained and lost
PARSER_TYPES = {"21", "22", "261"}
STALL_WINDOW = 60
STALL_MIN_CHAT = 8
STALL_SAMPLE_COOLDOWN = 600

NETLOG_LINE_RE = re.compile(r"^(\d{2,3})\|[^|]*\|([0-9A-Fa-f]{4})?")


def classify_netlog_line(line):
    """(type, chat code or None) for a network-log line; None for anything else."""
    m = NETLOG_LINE_RE.match(line)
    if not m:
        return None
    kind, code = m.group(1), m.group(2)
    if kind == "00":
        return (kind, int(code, 16) & 0x7F) if code else None
    return (kind, None)


def stall_verdict(combat_chat, parser_lines):
    """True when the game is clearly in combat but the parser has written nothing."""
    return combat_chat >= STALL_MIN_CHAT and parser_lines == 0


class NetLogTail:
    """Yields new lines of the newest IINACT network log, starting from its current end."""

    def __init__(self, directory):
        self.directory = directory
        self.path = None
        self.offset = 0

    def newest(self):
        try:
            files = [os.path.join(self.directory, n) for n in os.listdir(self.directory)
                     if n.startswith("Network_") and n.endswith(".log")]
        except OSError:
            return None
        return max(files, key=os.path.getmtime) if files else None

    def read_new(self):
        path = self.newest()
        if path is None:
            return []
        if path != self.path:
            # A file seen for the first time is read from its end, so history is never replayed;
            # a file that has just been created (a new day) is small and read whole.
            self.path = path
            self.offset = 0 if os.path.getsize(path) < 4096 else os.path.getsize(path)
        try:
            with open(path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        if not data:
            return []
        cut = data.rfind(b"\n")
        if cut < 0:
            return []
        self.offset += cut + 1
        return data[:cut].decode("utf-8", "replace").splitlines()


class StallWatch:
    """Keeps a one-minute window of network-log line kinds and samples the game once per stall."""

    def __init__(self, directory=NETLOG_DIR):
        self.tail = NetLogTail(directory)
        self.recent = []          # (arrival time, kind, chat code)
        self.stalled = False
        self.last_sample = 0

    def counts(self, now):
        self.recent = [r for r in self.recent if now - r[0] <= STALL_WINDOW]
        chat = sum(1 for _, k, c in self.recent if k == "00" and c in COMBAT_CHAT)
        parser = sum(1 for _, k, _ in self.recent if k in PARSER_TYPES)
        return chat, parser

    def tick(self, now, games):
        for line in self.tail.read_new():
            kind = classify_netlog_line(line)
            if kind:
                self.recent.append((now, kind[0], kind[1]))
        chat, parser = self.counts(now)
        stalled = stall_verdict(chat, parser)
        if stalled and not self.stalled:
            self.on_stall(now, games, chat)
        elif self.stalled and parser > 0:
            log("IINACT parser lines resumed")
        self.stalled = stalled

    def on_stall(self, now, games, chat):
        msg = f"IINACT STALL: {chat} combat chat lines in {STALL_WINDOW}s but no parser lines"
        log(msg)
        files = []
        if games and now - self.last_sample > STALL_SAMPLE_COOLDOWN:
            self.last_sample = now
            for pid, out in sample_plan(games, datetime.datetime.now()):
                out = out.replace("stall-sample-", "stall-sample-auto-")
                subprocess.run(["sample", str(pid), "5", "-file", out], capture_output=True)
                files.append(out)
            log("thread sample(s): " + ", ".join(files))
        try:
            with open(STALL_LOG, "a") as f:
                f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}; samples: {files}\n")
        except OSError:
            pass
        notify("IINACT stalled", "Thread sample taken. Restart overlays when you can.")


def notify(title, text):
    subprocess.run(["osascript", "-e", f'display notification "{text}" with title "{title}"'],
                   capture_output=True)


_attribution = {"key": None, "held": {}}


def overlay_profiles():
    """(path, mtime) of each slot's copy of the drifting overlay's browser storage."""
    root = os.path.join(CFG, "Browsingway")
    found = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return found
    for name in entries:
        if not name.startswith(SLOT_PREFIX):
            continue
        store = os.path.join(root, name, DRIFTING_OVERLAY, "Local Storage")
        if os.path.isdir(store):
            found.append((store, newest_write(store)))
    return found


def newest_write(directory):
    """A directory's own mtime only moves when entries appear or vanish, not when leveldb writes
    into an existing file, so ask the files themselves."""
    newest = 0.0
    for root, _, files in os.walk(directory):
        for name in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                pass
    return newest


def changed_since_sync(profiles, baseline):
    """Profiles written since the last reconcile. Two means both windows were edited."""
    return [path for path, mtime in profiles if mtime > baseline.get(path, 0) + 1]


def choose_sync_source(profiles, baseline=None):
    """Which profile the others copy from, or None when there is nothing to do.

    Newest wins even when both windows were edited, matching every other shared config here: the
    last window to save overwrites. The replaced copy is kept as .bak.
    """
    if len(profiles) < 2:
        return None

    newest = max(profiles, key=lambda pair: pair[1])
    if all(abs(mtime - newest[1]) < 1 for _, mtime in profiles):
        return None

    return newest[0]


def read_sync_state():
    try:
        with open(SYNCSTATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_sync_state(profiles):
    try:
        with open(SYNCSTATE, "w") as f:
            json.dump({path: mtime for path, mtime in profiles}, f)
    except OSError:
        pass


def sync_due(was_running, running):
    """Only on the transition to no game process at all; a port closing is too early."""
    return bool(was_running) and not running


def sync_overlay_profiles(source_override=None):
    """Reconcile the meter's settings across slots. Only safe with no game running: leveldb is
    locked while a renderer holds it."""
    if game_pids():
        return

    profiles = overlay_profiles()
    source = choose_sync_source(profiles) if source_override is None else source_override
    if source is None:
        return

    changed = changed_since_sync(profiles, read_sync_state())
    if len(changed) > 1:
        # Recorded so a setting that reappears as the other window's has an explanation.
        log(f"meter settings were changed in {len(changed)} windows; keeping the most recent")

    for target, _ in profiles:
        if target == source:
            continue
        try:
            backup = target + ".bak"
            if os.path.isdir(backup):
                shutil.rmtree(backup)
            shutil.copytree(target, backup)
            shutil.rmtree(target)
            shutil.copytree(source, target)
            log(f"synced meter settings from {os.path.basename(os.path.dirname(os.path.dirname(source)))} "
                f"to {os.path.basename(os.path.dirname(os.path.dirname(target)))} (previous kept as .bak)")
        except OSError as e:
            log(f"could not sync meter settings to {target}: {e}")

    write_sync_state(overlay_profiles())




def listening_now():
    """Which of our ports are listening, without walking every process's file descriptors."""
    try:
        out = subprocess.run(["netstat", "-an", "-p", "tcp"],
                             capture_output=True, text=True, timeout=10).stdout
    except (subprocess.TimeoutExpired, OSError):
        return set()
    found = set()
    for line in out.splitlines():
        if "LISTEN" not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        m = re.search(r"\.(\d+)$", parts[3])
        if m and int(m.group(1)) in PORTS:
            found.add(int(m.group(1)))
    return found


def credit_unowned(unowned_ports, live, held):
    """A listener lsof shows only under the wineserver (as it does after an IINACT plugin restart)
    still belongs to some game; with exactly one game running there is no ambiguity."""
    if len(live) == 1:
        (only,) = tuple(live)
        for port in unowned_ports:
            held.setdefault(port, only)
    return held


def attribute(live):
    """port -> pid, via lsof. Roughly 10x the cost of netstat, so call it only on a change."""
    held = {}
    unowned = set()
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=15).stdout
    except (subprocess.TimeoutExpired, OSError):
        return held
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        m = re.search(r":(\d+)$", parts[8])
        if not m or int(m.group(1)) not in PORTS:
            continue
        if pid in live:
            held[int(m.group(1))] = pid
        elif is_wineserver_line(" ".join(parts[8:])) or parts[0].startswith("wineserve"):
            unowned.add(int(m.group(1)))
    return credit_unowned(unowned, live, held)


def bound_ports(live):
    """port -> pid for ports held by a live game.

    Attribution needs lsof, but nothing changes between transitions, so the expensive call is made
    only when the listening set or the set of running games moves. During a boot that turns one scan
    every tick into two for the whole launch.
    """
    listening = listening_now()
    key = (tuple(sorted(listening)), tuple(sorted(live)))
    if _attribution["key"] != key:
        _attribution["held"] = attribute(live)
        _attribution["key"] = key
    return {port: pid for port, pid in _attribution["held"].items()
            if pid in live and port in listening}


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def write(path, text):
    tmp = path + ".portwatch"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)




def renderer_slot(cmd):
    """The cache slot a renderer was started against, from its serialised arguments."""
    _, _, arg = cmd.partition("Browsingway.Renderer.exe ")
    arg = arg.strip()
    if not arg:
        return None
    try:
        raw = base64.b64decode(arg + "=" * (-len(arg) % 4)).decode("utf-8", "ignore")
    except Exception:
        return None
    for seg in raw.split("\x00"):
        if SLOT_PREFIX in seg and "\\" in seg:
            return seg.rsplit("\\", 1)[1]
    return None


def cef_children():
    """(pid, slot) for CEF browser processes, attributed by the --user-data-dir they were given."""
    found = []
    for pid, _, _, cmd in procs("CefSharp.BrowserSubprocess.exe"):
        m = re.search(r"--user-data-dir=(.*?)(?= --|$)", cmd)
        if m:
            found.append((pid, m.group(1).rstrip().rsplit("\\", 1)[1]))
    return found


def orphans_of(exe):
    """Processes of `exe` no live game accounts for; each game starts exactly one, after itself."""
    games = procs("ffxiv_dx11.exe")
    others = procs(exe)
    claimed = set()
    for _, gstart, _, _ in games:
        for pid, ostart, _, _ in others:
            if pid not in claimed and ostart > gstart:
                claimed.add(pid)
                break
    return [o for o in others if o[0] not in claimed]


def orphan_renderers():
    """Renderers no live game can account for.

    Wine gives every process ppid 1 and the plugin's owner.pid records Wine's own pids, so neither
    the process tree nor those records can be matched from macOS. Each game has exactly one renderer
    and starts before it, so giving every live game its earliest unclaimed renderer leaves only the
    renderers whose game is gone.
    """
    games = procs("ffxiv_dx11.exe")
    renderers = procs("Browsingway.Renderer.exe")
    claimed = set()
    for _, gstart, _, _ in games:
        for pid, rstart, _, _ in renderers:
            if pid not in claimed and rstart > gstart:
                claimed.add(pid)
                break
    return [r for r in renderers if r[0] not in claimed]


def report_orphans(kill=False):
    orphans = orphan_renderers()
    if not orphans and not orphans_of("DalamudCrashHandler.exe") and (wineserver_alive() or not wine_procs()) \
            and not sweep_plan(len(game_pids()), wineserver_count(), len(wine_procs())) \
            :
        print("no orphaned renderers")
        return

    orphan_pids = {o[0] for o in orphans}
    live_slots = {renderer_slot(cmd) for pid, _, _, cmd in procs("Browsingway.Renderer.exe")
                  if pid not in orphan_pids}
    children = cef_children()

    for pid, started, cpu, cmd in orphans:
        age = int((time.time() - started) / 60)
        slot = renderer_slot(cmd)
        # Only sweep a slot's browser processes when no surviving renderer is still using it.
        strays = [c for c, cslot in children if slot and cslot == slot and cslot not in live_slots]
        print(f"orphaned renderer pid {pid}: {cpu}% CPU, started {age} min ago, "
              f"slot {slot or 'unknown'}, {len(strays)} browser process(es)")
        if kill:
            for c in strays:
                subprocess.run(["kill", "-9", str(c)])
            subprocess.run(["kill", "-9", str(pid)])
            print(f"  killed renderer {pid} and {len(strays)} browser process(es)")

    # Dalamud's crash handler is one-per-game too, and a frozen game's can outlive the whole Wine
    # tree, idle and deaf to SIGTERM.
    for pid, started, cpu, _ in orphans_of("DalamudCrashHandler.exe"):
        age = int((time.time() - started) / 60)
        print(f"orphaned crash handler pid {pid}: {cpu}% CPU, started {age} min ago")
        if kill:
            subprocess.run(["kill", "-9", str(pid)])
            print(f"  killed crash handler {pid}")

    plan = sweep_plan(len(game_pids()), wineserver_count(), len(wine_procs()))
    if plan and wineserver_alive():
        print(plan)
        if kill:
            for pid in wineserver_pids():
                subprocess.run(["kill", "-TERM", str(pid)])
            time.sleep(3)
            for pid in wineserver_pids():
                subprocess.run(["kill", "-9", str(pid)])
            for pid, _ in wine_procs():
                subprocess.run(["kill", "-9", str(pid)])
            print("  previous session cleared; the next launch starts a fresh wineserver")

    # Disabled while a game runs: on 2026-09-05 09:31 the "extra server" rule removed the server a
    # running window was actually attached to, and the window died. Attachment cannot be read from
    # outside; servers are only swept when no game exists at all.
    games = procs("ffxiv_dx11.exe")
    extras = []
    if extras:
        print(f"extra wineserver(s) beside the running window: {extras} - started after it, so not its own")
        if kill:
            for pid in extras:
                subprocess.run(["kill", "-TERM", str(pid)])
            time.sleep(3)
            for pid in extras:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
            print(f"  removed {len(extras)} extra server(s); the running window's server was left alone")

    # No server means nothing in the prefix can make progress: the games spin, the services idle.
    if not wineserver_alive():
        dead = wine_procs()
        if dead:
            print(f"dead prefix (no wineserver): {len(dead)} Wine process(es) that can never recover")
            for pid, cmd in dead:
                print(f"    {pid}  {cmd.rsplit(chr(92), 1)[-1][:40]}")
            if kill:
                for pid, _ in dead:
                    subprocess.run(["kill", "-9", str(pid)])
                print(f"  killed {len(dead)} process(es); relaunch starts a fresh wineserver")

    if not kill:
        print("\nre-run with --kill-orphans to stop them")



BUILT_PLUGIN = os.path.expanduser("~/Projects/browsingway-fork/out/Browsingway.dll")


def restart_path_report():
    """Whether the renderer's crash-and-restart path has run on the current build, and how it went."""
    try:
        since = os.path.getmtime(BUILT_PLUGIN)
    except OSError:
        return ["renderer restart path: built plugin not found"]

    logs = os.path.join(CFG, "Browsingway", "logs")
    lines = []
    for name in sorted(os.listdir(logs)) if os.path.isdir(logs) else []:
        m = re.match(r"bw-(\d{8}-\d{6})-\d+\.log$", name)
        if not m:
            continue
        booted = datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
        if booted < since:
            continue
        try:
            text = open(os.path.join(logs, name), errors="ignore").read()
        except OSError:
            continue
        if "ipc channel rebuilt" not in text:
            continue
        after = text.split("ipc channel rebuilt", 1)[1]
        came_back = "Notifying on ready state" in after
        lines.append(f"renderer restart path: RAN in {name}; renderer "
                     f"{'came back' if came_back else 'did NOT come back'} - "
                     f"{'check that window shows overlay data' if came_back else 'Fix crash macro needed'}")
    return lines or ["renderer restart path: not yet exercised on this build"]


# The path holds spaces ("XIV on Mac.app"), so anchor on how the line ends, not on a substring: a
# shell running a script that merely mentions the name would otherwise count as the server.
WINESERVER_RE = re.compile(r"/bin/wineserver(\s+-\S+)*\s*$")


def is_wineserver_line(line):
    return bool(WINESERVER_RE.search(line))


def wineserver_count():
    out = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True).stdout
    return sum(is_wineserver_line(line) for line in out.splitlines())


def server_socket_listening():
    """True if some process is bound at the Wine server socket path. macOS lsof cannot answer this by
    path, but netstat -f unix prints bound paths. None when there is no server dir to check."""
    import glob
    dirs = glob.glob(os.path.expanduser(f"/tmp/.wine-{os.getuid()}/server-*"))
    if not dirs:
        return None
    out = subprocess.run(["netstat", "-f", "unix"], capture_output=True, text=True).stdout
    key = os.path.basename(dirs[0])
    return any(key in line for line in out.splitlines())


def socket_verdict(n_servers, listening):
    """Disabled: netstat -f unix does not list the wineserver's listening socket by path on this
    macOS, so the check fired on a healthy single server (2026-09-05 09:18). Kept as a no-op until a
    method that can tell a stale path from a live one is verified against a known-good server."""
    return None


def launch_verdict(n_games, n_servers):
    """A relaunch straight after a quit starts a second wineserver beside the one still tearing the
    old session down (08:11 on 2026-09-05: native crash before the first frame)."""
    if n_games == 0 and n_servers > 0:
        return "WAIT - the previous session's wineserver is still shutting down; launching now starts a second one"
    if n_games > 0 and n_servers > 1:
        return f"{n_servers} wineservers for one prefix - the newest window runs on its own server (works, but each server writes the prefix registry on exit; prefer a fresh start when convenient)"
    return None


def wineserver_pids():
    out = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True).stdout
    return [int(l.split(None, 1)[0]) for l in out.splitlines() if is_wineserver_line(l.split(None, 1)[1] if " " in l.strip() else "")]


def wineserver_starts():
    """(pid, start epoch) per wineserver."""
    out = subprocess.run(["ps", "-Ao", "pid=,lstart=,command="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        m = PS_RE.match(line.replace("  ", " ", 1)) if False else None
        m = re.match(r"\s*(\d+)\s+(\w{3} \w{3}\s+\d+ \d{2}:\d{2}:\d{2} \d{4})\s+(.*)$", line)
        if not m or not is_wineserver_line(m.group(3)):
            continue
        try:
            found.append((int(m.group(1)), datetime.datetime.strptime(m.group(2), "%a %b %d %H:%M:%S %Y").timestamp()))
        except ValueError:
            pass
    return found


def extra_servers(game_starts, server_starts):
    """Each running game is served by the server that started closest before it (the loader starts
    the server ~2 s ahead of the game); every other server is a leftover. With no game, all are."""
    keep = set()
    for g in game_starts:
        before = [(st, pid) for pid, st in server_starts if st <= g]
        if before:
            keep.add(max(before)[1])
    return [pid for pid, st in server_starts if pid not in keep]


def sweep_plan(n_games, n_servers, n_procs):
    """With no game running, every Wine process and every wineserver is leftover; a server that is
    still tearing an old session down makes the next launch start a second one beside it."""
    if n_games:
        return None
    if n_servers or n_procs:
        return f"previous session still shutting down: {n_servers} wineserver(s), {n_procs} Wine process(es)"
    return None


def wineserver_alive():
    out = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True).stdout
    return any(is_wineserver_line(line) for line in out.splitlines())


def wine_procs():
    """Every Wine process: game, renderer, and the prefix's own services."""
    out = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if WINE_CMD_RE.match(cmd):
            found.append((int(pid), cmd))
    return found


def server_verdict(n_games, alive):
    """Clients of a dead wineserver spin forever on waits nobody will signal; nothing inside them recovers."""
    if n_games == 0 or alive:
        return None
    return f"WINESERVER GONE - {n_games} window(s) cannot recover; close them and relaunch"


def status():
    running = procs("ffxiv_dx11.exe")
    live = sorted(r[0] for r in running)
    starts = {pid: st for pid, st, _, _ in running}
    held = bound_ports(set(live))
    port_of = {pid: port for port, pid in held.items()}

    if not live:
        print("no game windows running")
    for n, pid in enumerate(live, 1):
        if pid in port_of:
            print(f"window {n} (pid {pid}): listening on {port_of[pid]}")
        else:
            up = int(time.time() - starts.get(pid, time.time()))
            print(f"window {n} (pid {pid}): still loading ({up}s since launch), has not claimed a port yet")

    verdict = server_verdict(len(live), wineserver_alive())
    if verdict:
        print(verdict)
    launch = launch_verdict(len(live), wineserver_count())
    if launch:
        print(launch)
    stale = socket_verdict(wineserver_count(), server_socket_listening())
    if stale:
        print(stale)

    if not launch and not stale:
        print("\nSAFE - launch the next window whenever you like.")

    for line in restart_path_report():
        print(line)


def watch():
    log("portwatch started")
    last = None
    seen = {}
    last_age = {}
    first_pass = True
    was_running = None
    server_seen = False
    two_servers_noted = False
    stall_watch = StallWatch()
    while True:
        live = game_pids()
        stall_watch.tick(time.time(), procs("ffxiv_dx11.exe"))
        held = bound_ports(live)
        state = tuple(sorted(held.items()))
        if state != last:
            starts = {pid: st for pid, st, _, _ in procs("ffxiv_dx11.exe")}
            new = [(p, q) for p, q in sorted(held.items()) if (p, q) not in (last if last else ())]
            # On the first pass everything already bound would report its process age, not a bind time.
            for port, pid in (new if last is not None else []):
                if pid in starts:
                    log(f"pid {pid} bound {port} {time.time() - starts[pid]:.0f}s after launch")
            if held:
                log("listening: " + ", ".join(f"{p} <- pid {q}" for p, q in sorted(held.items())))
            elif last is not None:
                log("no game windows listening")
            last = state

        # The last window's port closes seconds before its process exits, so keying the sync on
        # the ports made it run while a game still held the store and silently do nothing.
        if sync_due(was_running, bool(live)):
            sync_overlay_profiles()
        was_running = bool(live)

        starts_all = {pid: st for pid, st, _, _ in procs("ffxiv_dx11.exe")}
        if first_pass:
            # Windows already running were not observed from launch; timing them would be fiction.
            seen.update({pid: "prior" for pid in live})
            first_pass = False
        for pid in list(seen):
            if pid not in live:
                msg = classify_gone(seen[pid], last_age.get(pid, 0))
                if msg:
                    boot_note(f"pid {pid}: {msg}")
                del seen[pid]
                last_age.pop(pid, None)
        for pid in sorted(live):
            bound = next((p for p, q in held.items() if q == pid), None)
            age = time.time() - starts_all.get(pid, time.time())
            last_age[pid] = age
            if pid not in seen:
                dalamud_on, iinact_on = dalamud_enabled(), iinact_set_to_load()
                seen[pid] = initial_state(dalamud_on, iinact_on)
                if seen[pid] == "untracked":
                    why = "Dalamud is off" if not dalamud_on else "IINACT is not set to load"
                    boot_note(f"pid {pid}: {why}; boot not tracked (no port will bind)")
            seen[pid], msg = classify_live(seen[pid], bound, age)
            if msg:
                boot_note(f"pid {pid}: {msg}")

        # Both windows froze at once on 2026-09-05 06:23 when the shared wineserver died; the moment
        # it vanishes is the fact worth having, so watch for the transition.
        alive = wineserver_alive()
        n_servers = wineserver_count()
        if live and n_servers > 1 and not two_servers_noted:
            boot_note(f"{n_servers} wineservers while {len(live)} window(s) run - the newest window did not join the running server")
            two_servers_noted = True
        elif n_servers <= 1:
            two_servers_noted = False
        if live and server_seen and not alive:
            boot_note(f"wineserver vanished while {len(live)} window(s) were running - they cannot recover")
            server_seen = False
        elif alive:
            server_seen = True

        time.sleep(4)


if __name__ == "__main__":
    if "--watch" in sys.argv:
        watch()
    elif "--sync" in sys.argv:
        if game_pids():
            print("close every game window first; the meter's storage is locked while one is open")
        else:
            override = None
            if "--from" in sys.argv:
                slot = sys.argv[sys.argv.index("--from") + 1]
                override = next((p for p, _ in overlay_profiles() if f"/{slot}/" in p), None)
                if override is None:
                    sys.exit(f"no slot named {slot}; try one of "
                             + ", ".join(p.split("Browsingway/")[1].split("/")[0] for p, _ in overlay_profiles()))
            sync_overlay_profiles(override)
            print("meter settings reconciled across slots")
    elif "--sample" in sys.argv:
        sample_games()
    elif "--orphans" in sys.argv or "--kill-orphans" in sys.argv:
        status()
        print()
        report_orphans(kill="--kill-orphans" in sys.argv)
    else:
        status()
        print()
        report_orphans()
