#!/usr/bin/env python3
"""Watches the two-client FFXIV setup from outside the game.

Classifies each boot per window (the IINACT port binding is the proof a boot finished), reports and
sweeps Browsingway renderers no live game accounts for, syncs the meter's settings between windows,
and detects an IINACT parser stall from its network log. Ports are no longer arranged here: each
window's Browsingway derives its port from its cache slot and IINACT in that window follows it.
"""
import os, re, sys, json, time, base64, shutil, subprocess, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from attention import set_attention, event_note  # noqa: E402

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

# A Wine command line starts with the executable's path: drive-letter style when the Dalamud
# injector spawns the game, plain Unix style when XIV on Mac starts it bare. Anything else merely
# quoting an .exe name (a shell running a script that mentions one, say) is not the process.
WINE_CMD_RE = re.compile(r'^(?:[A-Za-z]:\\|/).*?\.exe(?=\s|$)')
NOT_A_PATH_RE = re.compile(r'''["']|\s--?[A-Za-z]|[A-Za-z]:\\''')   # quotes, an option, or a drive path inside a Unix one


def wine_exe_path(cmd, native=os.path.isfile):
    """The executable path a Wine command line starts with, else None. A shell or interpreter whose arguments
    mention an .exe starts with a Unix path too: a zsh line quoting the game's Windows path was taken for a game
    launch on 2026-09-30, and the watcher ends processes it takes for games."""
    m = WINE_CMD_RE.match(cmd)
    if not m:
        return None
    path = m.group(0)
    if path.startswith("/"):
        first = path.split(None, 1)[0]
        if not first.lower().endswith(".exe") and native(first):
            return None
        if NOT_A_PATH_RE.search(path):
            return None
    return path


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


def game_version():
    try:
        with open(os.path.join(BASE, "ffxiv", "game", "ffxivgame.ver")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def dalamud_supported_game(hooks=None):
    """(assembly version, game version it was built for) of the newest Dalamud XIV on Mac holds."""
    hooks = hooks or os.path.join(BASE, "dalamud", "Hooks")
    try:
        files = [p for p in (os.path.join(hooks, n, "version.json") for n in os.listdir(hooks)) if os.path.isfile(p)]
        if not files:
            return None
        with open(max(files, key=os.path.getmtime)) as f:
            info = json.load(f)
        return info.get("assemblyVersion"), info.get("supportedGameVer")
    except (OSError, ValueError, AttributeError):
        return None


def dalamud_mismatch(installed, game):
    """Why no plugin will load: XIV on Mac refuses to inject a Dalamud built for another game
    version and starts the game bare, so no port binds and a normal boot looks wedged."""
    if not installed or not game or not installed[1] or installed[1] == game:
        return None
    return f"Dalamud {installed[0]} supports game {installed[1]}, the game is {game}"


def initial_state(dalamud_on, iinact_on=True, mismatch=None):
    return "pending" if dalamud_on and iinact_on and not mismatch else "untracked"


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
    """Live Wine processes whose executable is `exe`, as parse_procs rows."""
    out = subprocess.run(["ps", "-Ao", "pid=,lstart=,%cpu=,command="], capture_output=True, text=True).stdout
    return parse_procs(out, exe)


def parse_procs(out, exe):
    """(pid, start epoch, %cpu, command) rows of a `ps -Ao pid=,lstart=,%cpu=,command=` listing
    whose executable is `exe`, oldest first."""
    found = []
    for line in out.splitlines():
        m = PS_RE.match(line)
        if not m:
            continue
        pid, when, cpu, cmd = m.groups()
        path = wine_exe_path(cmd)
        if not path or not path.endswith(("\\" + exe, "/" + exe)):
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
# The log goes where IINACT's config says, by default Documents\IINACT, which Wine links to the
# real ~/Documents: a folder macOS gates behind a Files and Folders consent that apps get asked
# for and a launchd agent never does, so listing it fails there, quietly.
HOME = os.path.expanduser("~")
WINEPREFIX = os.path.join(BASE, "wineprefix")
IINACT_CONFIG = os.path.join(CFG, "IINACT.json")
DEFAULT_NETLOG_WIN = "C:\\users\\" + os.path.basename(HOME) + "\\Documents\\IINACT"
NETLOG_HOME = os.path.join(BASE, "iinact-logs")
GATED_FOLDERS = ("Desktop", "Documents", "Downloads")
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


def wine_to_mac_path(win_path, prefix=WINEPREFIX):
    """Mac path behind a Wine one: C: is the prefix's drive_c, any other letter is whatever its
    dosdevices link points at; None for an unknown drive or spelling."""
    m = re.match(r"^([A-Za-z]):[\\/]?(.*)$", win_path or "")
    if not m:
        return None
    letter, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
    root = os.path.join(prefix, "drive_c" if letter == "c" else os.path.join("dosdevices", letter + ":"))
    if not os.path.exists(root):
        return None
    return os.path.normpath(os.path.join(os.path.realpath(root), rest))


def mac_to_wine_path(mac_path, prefix=WINEPREFIX):
    """Wine spelling of a Mac path: under drive_c as C:, anywhere else through Z:, Wine's root drive."""
    real = os.path.realpath(mac_path)
    drive_c = os.path.realpath(os.path.join(prefix, "drive_c"))
    if real == drive_c or real.startswith(drive_c + os.sep):
        return "C:\\" + real[len(drive_c) + 1:].replace("/", "\\")
    return "Z:" + real.replace("/", "\\")


def netlog_setting(config_path=IINACT_CONFIG):
    """The folder IINACT is set to log into (Wine spelling), or its default when unset or unreadable."""
    try:
        with open(config_path) as f:
            path = json.load(f).get("LogFilePath")
    except (OSError, ValueError, AttributeError):
        path = None
    return path or DEFAULT_NETLOG_WIN


def netlog_dir(config_path=IINACT_CONFIG, prefix=WINEPREFIX):
    return wine_to_mac_path(netlog_setting(config_path), prefix) or wine_to_mac_path(DEFAULT_NETLOG_WIN, prefix)


def in_gated_folder(path, home=HOME):
    """Whether reading `path` needs the Files and Folders consent macOS asks apps for (Desktop,
    Documents, Downloads); symlinks are followed because Wine's Documents is one."""
    real, home = os.path.realpath(path), os.path.realpath(home)
    return any(real == os.path.join(home, n) or real.startswith(os.path.join(home, n) + os.sep)
               for n in GATED_FOLDERS)


def relocate_netlog(config_path, new_dir, games, prefix=WINEPREFIX, say=None):
    """Points IINACT's log folder at `new_dir` while no game runs, creating the folder first:
    IINACT falls back to Documents when the configured folder does not exist."""
    say = say or log
    if games:
        return False
    try:
        os.makedirs(new_dir, exist_ok=True)
        with open(config_path) as f:
            cfg = json.load(f)
        cfg["LogFilePath"] = mac_to_wine_path(new_dir, prefix)
        tmp = config_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, config_path)
    except (OSError, ValueError, AttributeError, TypeError) as e:
        say(f"could not move IINACT's network log: {e}")
        return False
    say(f"IINACT network log moved to {new_dir}; earlier logs stay where they were")
    return True


class NetLogTail:
    """Yields new lines of the newest IINACT network log, starting from its current end."""

    def __init__(self, directory):
        self.directory = directory
        self.path = None
        self.offset = 0
        self.denied = False   # the last listing was refused, as opposed to finding nothing

    def newest(self):
        try:
            names = os.listdir(self.directory) if self.directory else []
        except PermissionError:
            self.denied = True
            return None
        except OSError:
            names = []
        self.denied = False
        files = [os.path.join(self.directory, n) for n in names
                 if n.startswith("Network_") and n.endswith(".log")]
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

    def __init__(self, directory=None, config_path=IINACT_CONFIG, new_home=NETLOG_HOME, home=HOME):
        self.config_path = config_path
        self.new_home = new_home
        self.home = home
        self.tail = NetLogTail(directory or netlog_dir(config_path))
        self.recent = []          # (arrival time, kind, chat code)
        self.stalled = False
        self.last_sample = 0
        self.denied_noted = False

    def counts(self, now):
        self.recent = [r for r in self.recent if now - r[0] <= STALL_WINDOW]
        chat = sum(1 for _, k, c in self.recent if k == "00" and c in COMBAT_CHAT)
        parser = sum(1 for _, k, _ in self.recent if k in PARSER_TYPES)
        return chat, parser

    def tick(self, now, games):
        lines = self.tail.read_new()
        if self.tail.denied:
            self.on_denied(games)
        elif self.denied_noted:
            log("IINACT network log readable again")
            self.denied_noted = False
        for line in lines:
            kind = classify_netlog_line(line)
            if kind:
                self.recent.append((now, kind[0], kind[1]))
        chat, parser = self.counts(now)
        stalled = stall_verdict(chat, parser)
        if stalled and not self.stalled:
            self.on_stall(now, games, chat)
        elif self.stalled and parser > 0:
            log("IINACT parser lines resumed")
            set_attention("IINACT", None)
        self.stalled = stalled

    def on_denied(self, games):
        """No consent prompt ever reaches a launchd agent, so the cures are moving the log out of the
        gated folder, done here once no game runs because IINACT writes its config back from memory,
        or a grant made by hand."""
        gated = in_gated_folder(self.tail.directory, self.home)
        if not self.denied_noted:
            cure = ("it moves out of there (automatic once no game runs) or Python is allowed into that "
                    "folder under System Settings > Privacy & Security > Files and Folders"
                    if gated else "its permissions let this user read it")
            log(f"IINACT network log unreadable at {self.tail.directory}; the stall detector is blind until {cure}")
            self.denied_noted = True
        if gated and relocate_netlog(self.config_path, self.new_home, games):
            self.tail = NetLogTail(self.new_home)
            self.denied_noted = False

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
        set_attention("IINACT", event_note("the parser stalled", files[0] if files else None))



# XIV Doctor's per-window diag log carries a heartbeat every minute while the game's frame loop runs, which
# makes a silent file the cheapest external sign that a live window has frozen (2026-09-06 00:32: one
# thread spinning in Rosetta's exception server, heartbeats simply stopped). IINACT carried the beat until
# 2026-09-30; a parser that failed to load switched freeze detection off, and XIV Doctor is always on.
DOCTOR_DIAG_DIR = os.path.join(CFG, "XIVDoctor", "diag")
HANG_AFTER = 150          # the ceiling: two missed beats of the one-minute cadence older plugin builds keep
HANG_FLOOR = 20           # four missed beats of the five-second cadence: a hang in a duty gets force-quit within a minute
STALL_AFTER = 25          # plugin loading stalls the frame loop 10-17 s on every boot; sampling then hit a healthy, busy game (2026-09-30 18:18)
START_MATCH_SLACK = 20    # the diag name carries Wine's idea of the start time


def diag_start_time(name):
    """Process start time encoded in a per-window diag name, `doctor-YYYYMMDD-HHMMSS-<pid>.log` or IINACT's, else None."""
    m = re.match(r"(?:doctor|iinact)-(\d{8})-(\d{6})-\d+\.log$", name)
    if not m:
        return None
    return datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()


def match_game(diag_started, game_starts):
    """The live game whose start time is closest to the diag file's, within the slack; else None."""
    best = None
    for pid, started in game_starts.items():
        gap = abs(started - diag_started)
        if gap <= START_MATCH_SLACK and (best is None or gap < best[1]):
            best = (pid, gap)
    return best[0] if best else None


def teardown_note(last_exit_at, now):
    """How long the wineserver outlived the last window; the WAIT verdict depends on this being short."""
    return None if last_exit_at is None else f"wineserver exited {now - last_exit_at:.0f}s after the last window"


def beat_threshold(tail):
    """Seconds of silence that mean a freeze, read off the plugin's own beat spacing: four missed beats,
    never under the floor nor over the ceiling. One beat or none says nothing about the spacing."""
    stamps = re.findall(r"^\[(\d\d):(\d\d):(\d\d)\.\d+\] heartbeat:", tail, re.M)
    if len(stamps) < 2:
        return HANG_AFTER
    a, b = (int(h) * 3600 + int(m) * 60 + int(s) for h, m, s in stamps[-2:])
    return min(HANG_AFTER, max(HANG_FLOOR, 4 * ((b - a) % 86400)))


def stalled_seconds(last_line):
    """The plugin's timer thread reports a frame loop that stopped ticking; None when the last line is not such a report."""
    m = re.search(r"frame loop stalled (\d+)s", last_line)
    return int(m.group(1)) if m else None


def hang_verdict(file_age, has_heartbeat, last_line, threshold=HANG_AFTER):
    """A window that has produced heartbeats, then nothing for the threshold, has frozen; so has one whose timer
    thread says the frame loop has been stalled that long, even though that keeps the file fresh. Those reports
    come every three seconds (4.1 s at most over seven launches), so silence after one is the whole process held,
    heartbeat or not: on 2026-10-01 a launch froze 6 s into plugin loading and went unreported for two minutes.
    A file ending in 'unloading' is the plugin switched off on purpose, not a hang."""
    if "unloading" in last_line:
        return False
    stalled = stalled_seconds(last_line)
    if stalled is not None:
        return stalled >= STALL_AFTER or file_age >= HANG_FLOOR
    return has_heartbeat and file_age > threshold


class HangWatch:
    def __init__(self, directory=DOCTOR_DIAG_DIR):
        self.directory = directory
        self.reported = set()

    def tick(self, now, games):
        starts = {pid: st for pid, st, _, _ in games}
        gone = [pid for pid in self.reported if pid not in starts]
        if gone:
            self.reported.difference_update(gone)
            if not self.reported:
                set_attention("GameWindow", None)
        try:
            names = [n for n in os.listdir(self.directory) if n.startswith("doctor-")]
        except OSError:
            return
        for name in names:
            started = diag_start_time(name)
            if started is None or now - started > 12 * 3600:
                continue
            pid = match_game(started, starts)
            if pid is None:
                continue
            path = os.path.join(self.directory, name)
            try:
                age = now - os.path.getmtime(path)
                with open(path, "rb") as f:
                    tail = f.read()[-4096:].decode("utf-8", "replace")
            except OSError:
                continue
            lines = tail.splitlines()
            last = lines[-1] if lines else ""
            frozen = hang_verdict(age, "heartbeat:" in tail, last, beat_threshold(tail))
            if pid in self.reported:
                # a stall that ended on its own was a long hitch, not a hang: take the note back
                if not frozen and stalled_seconds(last) is None and age < HANG_FLOOR:
                    self.reported.discard(pid)
                    self.on_recover(pid, name)
                continue
            if frozen:
                self.reported.add(pid)
                self.on_hang(pid, age + (stalled_seconds(last) or 0), name)

    def on_recover(self, pid, name):
        boot_note(f"pid {pid}: frame loop resumed; that freeze ended on its own ({name})")
        if not self.reported:
            set_attention("GameWindow", None)

    def on_hang(self, pid, age, name):
        level, memory = memory_facts()
        boot_note(f"pid {pid}: HANG - no plugin heartbeat for {age:.0f}s while the process lives ({name}); {memory}")
        out = None
        if sample_worthwhile(level):
            out = os.path.join(BASE, "wedge-watch", f"hang-sample-{datetime.datetime.now():%H%M%S}-{pid}.txt")
            subprocess.run(["sample", str(pid), "3", "-file", out], capture_output=True)
            log(f"thread sample: {out}")
        else:
            log("no thread sample: the machine is short of memory, and sampling would hold the game still for longer")
        notify("Game window frozen", f"pid {pid}: no plugin heartbeat for {age:.0f}s; {memory}. Force Quit it; leftovers are cleared for you.")
        set_attention("GameWindow", event_note(f"a game window froze (no plugin heartbeat for {age:.0f} s; {memory})", out))


# A machine out of memory holds a game still for minutes and looks the same from outside as a hang. On 2026-09-30
# a window at 12 GB ran beside a second one on 18 GB, fell silent for 136 s and lost its connection. `sample` got
# 106-200 of its 3000 samples from it, and a further 15 s stall fell inside the second capture; under pressure the
# two numbers below are the capture.
PRESSURE = {1: "normal", 2: "warning", 4: "critical"}


def describe_memory(level_text, swap_text):
    """(pressure level or None, 'memory pressure <name>, swap X of Y GB used') from the two sysctl values."""
    try:
        level = int(level_text.strip())
    except (ValueError, AttributeError):
        level = None
    parts = [f"memory pressure {PRESSURE.get(level, 'unknown')}"]
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", swap_text or "")
    if m:
        parts.append(f"swap {float(m.group(2)) / 1024:.1f} of {float(m.group(1)) / 1024:.1f} GB used")
    return level, ", ".join(parts)


def memory_facts():
    def read(name):
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True).stdout
    return describe_memory(read("kern.memorystatus_vm_pressure_level"), read("vm.swapusage"))


def sample_worthwhile(level):
    return level not in (2, 4)


# A game that began to close and never finished. XIV Doctor's last line says the game itself is closing (a plugin
# switched off writes plain 'unloading'), nothing follows it, and the process is still there. 58 normal exits in
# September took at most 22 s. On 2026-09-30 22:44 one stopped while unloading a plugin and sat at two cores for
# fifteen minutes; nothing noticed, because HangWatch reads 'unloading' as intentional.
EXIT_STUCK_AFTER = 120
DALAMUD_LOG = os.path.join(BASE, "logs", "dalamud.log")


def exit_stuck_verdict(last_line, file_age, threshold=EXIT_STUCK_AFTER):
    return "game closing" in last_line and file_age >= threshold


def exit_report(pid, age, memory, diag_tail, dalamud_tail, sample=None):
    """What a stuck close leaves to look at: the last plugin lines say where the shutdown stopped. When Dalamud's own
    shutdown finished ("Session has ended."), the hang was in the game's exit after it, and only a thread sample shows where."""
    lines = dalamud_tail.splitlines()
    unload = [l for l in lines if "[LocalPlugin]" in l or "Framework::Destroy" in l][-6:]
    ended = [l for l in lines if "Session has ended." in l][-1:]
    where = ([f"Dalamud finished shutting down ({ended[0][:23]}); the game process did not exit after that."] if ended else []) \
        + ([f"thread sample of the stuck process: {os.path.basename(sample)}"] if sample else [])
    return "\n".join([f"pid {pid} was still running {age:.0f} s after the game began to close, and was ended.", memory, *where, "",
                      "last lines of the window's XIV Doctor log:", *diag_tail.splitlines()[-6:], "",
                      "last plugin load and unload lines of dalamud.log (shared by all windows):", *unload, *ended]) + "\n"


def process_alive(pid):
    """A process that has exited but was not yet collected by its parent still has a pid; it is not alive."""
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def end_process(pid, wait=3):
    """TERM, then KILL: the game stuck on 2026-09-30 ignored TERM for eight seconds."""
    subprocess.run(["kill", "-TERM", str(pid)], capture_output=True)
    deadline = time.time() + wait
    while time.time() < deadline and process_alive(pid):
        time.sleep(0.5)
    if process_alive(pid):
        subprocess.run(["kill", "-9", str(pid)], capture_output=True)
        time.sleep(1)
    return not process_alive(pid)


def tail_of(path, size=8192):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


class ExitWatch:
    def __init__(self, directory=DOCTOR_DIAG_DIR):
        self.directory = directory
        self.closing = set()
        self.ended = set()

    def tick(self, now, games):
        starts = {pid: st for pid, st, _, _ in games}
        for pid in [p for p in self.closing if p not in starts]:
            self.closing.discard(pid)
            if pid in self.ended:
                self.ended.discard(pid)
            else:
                self.on_clean_exit(pid)
        try:
            names = [n for n in os.listdir(self.directory) if n.startswith("doctor-")]
        except OSError:
            return
        for name in names:
            started = diag_start_time(name)
            if started is None or now - started > 7 * 86400:
                continue
            pid = match_game(started, starts)
            if pid is None or pid in self.ended:
                continue
            path = os.path.join(self.directory, name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            lines = tail_of(path, 2048).splitlines()
            last = lines[-1] if lines else ""
            if "game closing" not in last:
                continue
            self.closing.add(pid)
            if exit_stuck_verdict(last, age):
                self.ended.add(pid)
                self.on_stuck(pid, age, path)

    def on_clean_exit(self, pid):
        set_attention("GameExit", None)

    def on_stuck(self, pid, age, diag_path):
        level, memory = memory_facts()
        stamp = f"{datetime.datetime.now():%H%M%S}-{pid}"
        out = os.path.join(BASE, "wedge-watch", f"exit-stuck-{stamp}.txt")
        sample = None
        # nobody plays a window that is closing, so holding it still for a sample costs nothing, unless memory is short
        if sample_worthwhile(level):
            sample = os.path.join(BASE, "wedge-watch", f"exit-sample-{stamp}.txt")
            subprocess.run(["sample", str(pid), "3", "-file", sample], capture_output=True)
            if not os.path.exists(sample):
                sample = None
        try:
            with open(out, "w") as f:
                f.write(exit_report(pid, age, memory, tail_of(diag_path, 2048), tail_of(DALAMUD_LOG, 65536), sample))
        except OSError:
            out = None
        gone = end_process(pid)
        boot_note(f"pid {pid}: STUCK CLOSING - still running {age:.0f}s after the game began to close; "
                  f"{'ended' if gone else 'could not be ended'}; {memory}")
        notify("Game stuck closing", f"A game window had been closing for {age:.0f}s and "
               + ("was ended. Nothing else to do." if gone else "could not be ended."))
        set_attention("GameExit", exit_note(age, gone, out))


def exit_note(age, gone, capture):
    """An ended window leaves the player nothing to do and its capture has never shown a cause, so only a window that
    is still there gets a note; the report and the boot log keep the record either way."""
    if gone:
        return None
    return event_note(f"a game window was stuck closing for {age:.0f} s and could not be ended", capture)


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


def sweep_orphan_renderers(kill, say=print):
    """Renderers, their browser processes and crash handlers whose game is gone. A game that no longer
    exists is the whole test, so the watcher runs this on its own; returns how many orphans it saw."""
    orphans = orphan_renderers()
    handlers = orphans_of("DalamudCrashHandler.exe")
    orphan_pids = {o[0] for o in orphans}
    live_slots = {renderer_slot(cmd) for pid, _, _, cmd in procs("Browsingway.Renderer.exe")
                  if pid not in orphan_pids}
    children = cef_children()

    for pid, started, cpu, cmd in orphans:
        age = int((time.time() - started) / 60)
        slot = renderer_slot(cmd)
        # Only sweep a slot's browser processes when no surviving renderer is still using it.
        strays = [c for c, cslot in children if slot and cslot == slot and cslot not in live_slots]
        say(f"orphaned renderer pid {pid}: {cpu}% CPU, started {age} min ago, "
            f"slot {slot or 'unknown'}, {len(strays)} browser process(es)")
        if kill:
            for c in strays:
                subprocess.run(["kill", "-9", str(c)])
            subprocess.run(["kill", "-9", str(pid)])
            say(f"  killed renderer {pid} and {len(strays)} browser process(es)")

    # Dalamud's crash handler is one-per-game too, and a frozen game's can outlive the whole Wine
    # tree, idle and deaf to SIGTERM.
    for pid, started, cpu, _ in handlers:
        age = int((time.time() - started) / 60)
        say(f"orphaned crash handler pid {pid}: {cpu}% CPU, started {age} min ago")
        if kill:
            subprocess.run(["kill", "-9", str(pid)])
            say(f"  killed crash handler {pid}")
    return len(orphans) + len(handlers)


def report_orphans(kill=False):
    if not orphan_renderers() and not orphans_of("DalamudCrashHandler.exe") and (wineserver_alive() or not wine_procs()) \
            and not sweep_plan(len(game_pids()), wineserver_count(), len(wine_procs())) \
            :
        print("no orphaned renderers")
        return

    sweep_orphan_renderers(kill)

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


REAL_SERVER_RE = re.compile(r"XIV on Mac\.app/.*/bin/wineserver\s*$")   # not the launcher's `wineserver -w` waiters
EXIT_WAIT = 30            # 58 normal exits logged in September: 50 within 5 s, the slowest 22 s
KILL_WAIT = 4             # a force-quit never does; the plugin's log not ending in 'unloading' tells the two apart
SERVER_MIN_AGE = 60
LAUNCH_QUIET = 30         # anything Wine-side younger than this is a launch in progress


def stale_server_verdict(n_games, gone_for, killed, server_ages, youngest_wine_age):
    """With no game at all, a server that outlives its session is left over from a force-quit, and the next launch
    would join it. Never while a game runs (2026-09-05: the server a live window was attached to was removed and the
    window died) and never during a launch."""
    if n_games or gone_for is None or not server_ages:
        return False
    if gone_for < (KILL_WAIT if killed else EXIT_WAIT):
        return False
    if max(server_ages) < SERVER_MIN_AGE:
        return False
    return youngest_wine_age is None or youngest_wine_age >= LAUNCH_QUIET


def last_exit_was_kill(directory):
    """The plugin writes 'unloading' when the game shuts down by itself; a log that ends on anything else was cut off."""
    try:
        names = [n for n in os.listdir(directory) if n.startswith("doctor-")]
        if not names:
            return False
        newest = max(names, key=lambda n: os.path.getmtime(os.path.join(directory, n)))
        with open(os.path.join(directory, newest), "rb") as f:
            lines = f.read()[-2048:].decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    return bool(lines) and "unloading" not in lines[-1]


def wine_ages(now):
    """(real server ages, age of the youngest Wine-side process) from one listing."""
    out = subprocess.run(["ps", "-Ao", "pid=,lstart=,command="], capture_output=True, text=True).stdout
    servers, youngest = [], None
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\w{3} \w{3}\s+\d+ \d{2}:\d{2}:\d{2} \d{4})\s+(.*)$", line)
        if not m:
            continue
        cmd = m.group(3)
        real = bool(REAL_SERVER_RE.search(cmd))
        if not real and not wine_exe_path(cmd) and not is_wineserver_line(cmd):
            continue
        try:
            age = now - datetime.datetime.strptime(m.group(2), "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            continue
        if real:
            servers.append((int(m.group(1)), age))
        youngest = age if youngest is None else min(youngest, age)
    return servers, youngest


class ServerSweep:
    def __init__(self, directory=None):
        self.directory = directory or DOCTOR_DIAG_DIR
        self.gone_at = None

    def tick(self, now, games):
        if games:
            self.gone_at = None
            return
        if self.gone_at is None:
            self.gone_at = now
        servers, youngest = wine_ages(now)
        if stale_server_verdict(0, now - self.gone_at, last_exit_was_kill(self.directory), [a for _, a in servers], youngest):
            self.sweep(servers, now - self.gone_at)

    def sweep(self, servers, gone_for):
        for pid, _ in servers:
            subprocess.run(["kill", "-TERM", str(pid)], capture_output=True)
        time.sleep(3)
        for pid, _ in wine_ages(time.time())[0]:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
        dead = wine_procs()
        for pid, _ in dead:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
        log(f"cleared the Wine server a session left behind ({gone_for:.0f}s after it ended; {len(servers)} server(s), "
            f"{len(dead)} leftover process(es)); the next launch starts fresh")


# The hourly sync installs a verified build only at its own tick, so closing the game and starting it again
# inside the hour kept running the old build. With no game left, a waiting build is installed at once.
SYNC_STATE = os.path.join(BASE, "wedge-watch", "upstream-sync-state.json")
SYNC_AGENT = "com.jeoffersondelapena.xivupstream"
NUDGE_AFTER = 8
NUDGE_EVERY = 600


def pending_builds(state_text):
    """Plugins with a build waiting for the game to close, from the sync's state file."""
    try:
        state = json.loads(state_text)
    except ValueError:
        return []
    if not isinstance(state, dict):
        return []
    return sorted(name for name, st in state.items() if isinstance(st, dict) and st.get("pending_install"))


def nudge_due(n_games, gone_for, pending, since_last):
    if n_games or gone_for is None or gone_for < NUDGE_AFTER or not pending:
        return False
    return since_last is None or since_last >= NUDGE_EVERY


class InstallNudge:
    def __init__(self, state_path=SYNC_STATE):
        self.state_path = state_path
        self.gone_at = None
        self.nudged_at = None

    def tick(self, now, games):
        if games:
            self.gone_at = None
            return
        if self.gone_at is None:
            self.gone_at = now
        try:
            with open(self.state_path) as f:
                pending = pending_builds(f.read())
        except OSError:
            return
        if nudge_due(0, now - self.gone_at, pending, None if self.nudged_at is None else now - self.nudged_at):
            self.nudged_at = now
            self.run(pending)

    def run(self, pending):
        log(f"the game is closed and a build is waiting ({', '.join(pending)}); running the sync now")
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{SYNC_AGENT}"], capture_output=True)


# per-window memory breakdown, once a minute while a game runs; the 2026-09-30 freezes left no such data
MEMORY_LOG = os.path.join(BASE, "wedge-watch", "memory.log")
MEMORY_EVERY = 60
MEMORY_SLOW = 3.0          # a breakdown that takes this long is a load on the game itself: back off
MEMORY_LOG_MAX = 5 * 1024 * 1024
GAME_EXE = "ffxiv_dx11.exe"


def wine_exe(cmd):
    """The executable's own name from a Wine command line, else None."""
    path = wine_exe_path(cmd)
    return re.split(r"[\\/]", path)[-1] if path else None


def summarise_footprint(report, exes, keep=8):
    """From `footprint --swapped -j`: each game window's footprint, how much of it is swapped or compressed, and its
    largest parts; every other Wine-side process summed per executable. MB throughout. A part's 'dirty' figure
    already includes what is swapped."""
    unit = report.get("bytes per unit", 1)

    def mb(n):
        return round(n * unit / 1048576)

    windows, helpers = [], {}
    for proc in report.get("processes", []):
        exe = exes.get(proc.get("pid"))
        if exe is None:
            continue
        if exe.lower() != GAME_EXE:
            helpers[exe] = helpers.get(exe, 0) + mb(proc.get("footprint", 0))
            continue
        parts = {name: mb(c.get("dirty", 0)) for name, c in proc.get("categories", {}).items()}
        windows.append({"pid": proc["pid"], "footprint": mb(proc.get("footprint", 0)),
                        "swapped": mb(sum(c.get("swapped", 0) for c in proc.get("categories", {}).values())),
                        "parts": dict(sorted(((n, v) for n, v in parts.items() if v > 0), key=lambda kv: -kv[1])[:keep])})
    return windows, helpers


DISTURB_MS = 250          # a frame this long inside a reading counts against the reading
DISTURB_MIN = 3
DISTURB_SHARE = 0.2       # by chance a reading holds the minute's longest frame about one time in forty
MEMORY_LINE_RE = re.compile(r"\[(\d\d):(\d\d):(\d\d)\.\d+\] memory: managed (\d+) MB, committed (\d+) MB, resident (\d+) MB; players (\d+); territory (\d+)"
                            r"(?:; longest frame (\d+) ms at (\d\d):(\d\d):(\d\d)\.(\d))?")


def second_of_day(epoch):
    t = time.localtime(epoch)
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec + (epoch % 1)


def clock_gap(a, b):
    """a minus b in seconds for two times of day, across midnight."""
    return ((a - b + 43200) % 86400) - 43200


def doctor_memory(tail):
    """The newest 'memory:' line XIV Doctor wrote, as numbers; None without one. Times are seconds of the day."""
    found = MEMORY_LINE_RE.findall(tail)
    if not found:
        return None
    h, m, s, managed, committed, resident, players, territory, longest, lh, lm, ls, lt = found[-1]
    out = {"managed": int(managed), "committed": int(committed), "resident": int(resident), "players": int(players), "territory": int(territory),
           "line_at": int(h) * 3600 + int(m) * 60 + int(s)}
    if longest:
        out["longest_ms"] = int(longest)
        out["longest_at"] = int(lh) * 3600 + int(lm) * 60 + int(ls) + int(lt) / 10
    return out


def reading_disturbed(window, doctor):
    """Whether the longest frame of the minute XIV Doctor just reported fell inside a reading: True or False once
    that minute covers the reading, None while it does not. `window` is (start, end) in seconds of the day."""
    if not doctor or "longest_ms" not in doctor:
        return None
    start, end = window
    if clock_gap(doctor["line_at"], end) < 0 or clock_gap(doctor["line_at"], start) > 61:
        return None
    inside = clock_gap(doctor["longest_at"], start) >= -0.3 and clock_gap(doctor["longest_at"], end) <= 0.3
    return inside and doctor["longest_ms"] >= DISTURB_MS


def growth(first, last):
    """Parts of a window ordered by how much they grew between two records, largest first."""
    names = set(first.get("parts", {})) | set(last.get("parts", {}))
    moved = [(n, first.get("parts", {}).get(n, 0), last.get("parts", {}).get(n, 0)) for n in names]
    return sorted(moved, key=lambda m: m[1] - m[2])


def memory_report(records):
    """Readable account of memory.log: per window, what it started at, what it ended at and which parts grew."""
    by_pid = {}
    for rec in records:
        for w in rec.get("windows", []):
            by_pid.setdefault(w["pid"], []).append((rec, w))
    if not by_pid:
        return "no game window has been recorded yet\n"
    out = []
    for pid, rows in by_pid.items():
        (r0, w0), (r1, w1) = rows[0], rows[-1]
        peak = max(w["footprint"] for _, w in rows)
        out.append(f"window pid {pid}: {r0['t']} to {r1['t']}, {len(rows)} readings")
        out.append(f"  footprint {w0['footprint'] / 1024:.1f} GB -> {w1['footprint'] / 1024:.1f} GB (peak {peak / 1024:.1f}), "
                   f"swapped or compressed at the end {w1['swapped'] / 1024:.1f} GB")
        for name, a, b in growth(w0, w1)[:6]:
            out.append(f"    {name}: {a} MB -> {b} MB ({b - a:+d})")
        d0, d1 = w0.get("doctor"), w1.get("doctor")
        if d0 and d1:
            out.append(f"  plugins' managed heap {d0['managed']} MB -> {d1['managed']} MB; players in view {d0['players']} -> {d1['players']}")
    helpers = records[-1].get("helpers", {})
    if helpers:
        top = sorted(helpers.items(), key=lambda kv: -kv[1])[:6]
        out.append("other processes at the last reading: " + ", ".join(f"{n} {v} MB" for n, v in top)
                   + f" (all {sum(helpers.values())} MB)")
    last = records[-1]
    if "judged" in last:
        out.append(f"readings held against the game's own frames: {last['judged']}, of which {last['felt']} held the minute's longest frame"
                   + (" (totals only since)" if last.get("light") else ""))
    worst = max(records, key=lambda r: (r.get("pressure") or 0, r.get("swap_mb") or 0))
    out.append(f"highest memory pressure seen: {PRESSURE.get(worst.get('pressure'), 'unknown')}, swap {worst.get('swap_mb', 0) / 1024:.1f} GB used, at {worst['t']}")
    return "\n".join(out) + "\n"


class MemoryLog:
    def __init__(self, path=MEMORY_LOG, directory=DOCTOR_DIAG_DIR):
        self.path = path
        self.directory = directory
        self.every = MEMORY_EVERY
        self.last = 0.0
        self.pending = []
        self.checked = 0
        self.disturbed = 0
        self.light = False

    def tick(self, now, games):
        if not games:
            return
        self.judge(now, games)
        if now - self.last < self.every:
            return
        self.last = now
        try:
            record = self.measure(now, games)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            log(f"memory reading failed: {e}")
            return
        if record["took"] > MEMORY_SLOW:
            self.every = min(self.every * 2, 900)
            log(f"a memory reading took {record['took']:.1f}s; next one in {self.every}s")
        if not self.light:
            self.pending.append((second_of_day(now), second_of_day(now) + record["took"], now))
        record["judged"], record["felt"] = self.checked, self.disturbed
        self.write(record)

    def judge(self, now, games):
        """The readings must not be felt in the game. Each one is held against the longest frame XIV Doctor reports
        for that minute; when readings keep holding it, only totals are read from then on."""
        if not self.pending:
            return
        doctors = [self.doctor(started) for _, started, _, _ in games]
        for reading in list(self.pending):
            verdicts = [reading_disturbed(reading[:2], d) for d in doctors]
            if any(v is True for v in verdicts):
                self.disturbed += 1
            elif not any(v is False for v in verdicts):
                if now - reading[2] > 150:
                    self.pending.remove(reading)
                continue
            self.checked += 1
            self.pending.remove(reading)
        if not self.light and self.disturbed >= DISTURB_MIN and self.disturbed >= DISTURB_SHARE * self.checked:
            self.light = True
            self.pending.clear()
            log(f"{self.disturbed} of {self.checked} memory readings held the minute's longest frame; reading totals only from here")

    def measure(self, now, games):
        exes = {pid: wine_exe(cmd) for pid, cmd in wine_procs()}
        exes = {pid: exe for pid, exe in exes.items() if exe}
        began = time.time()
        out = self.path + ".footprint.json"
        args = [a for pid in exes for a in ("-p", str(pid))]
        mode = ["--noCategories"] if self.light else ["--swapped"]
        subprocess.run(["footprint", *mode, "-j", out, *args], capture_output=True, timeout=60)
        with open(out) as f:
            report = json.load(f)
        os.remove(out)
        windows, helpers = summarise_footprint(report, exes)
        starts = {pid: st for pid, st, _, _ in games}
        for w in windows:
            w["up_min"] = round((now - starts[w["pid"]]) / 60) if w["pid"] in starts else None
            w["doctor"] = self.doctor(starts.get(w["pid"]))
        level, _ = memory_facts()
        swap = re.search(r"used = ([\d.]+)M", subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout)
        return {"t": datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"), "pressure": level,
                "swap_mb": round(float(swap.group(1))) if swap else None, "windows": windows, "helpers": helpers,
                "took": round(time.time() - began, 2), "light": self.light}

    def doctor(self, started):
        if started is None:
            return None
        try:
            for name in os.listdir(self.directory):
                at = diag_start_time(name) if name.startswith("doctor-") else None
                if at is not None and abs(at - started) <= START_MATCH_SLACK:
                    return doctor_memory(tail_of(os.path.join(self.directory, name), 8192))
        except OSError:
            pass
        return None

    def write(self, record):
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > MEMORY_LOG_MAX:
                os.replace(self.path, self.path.replace(".log", ".old.log"))
            with open(self.path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass


def read_memory_log(path=MEMORY_LOG):
    records = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return records


def wineserver_alive():
    out = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True).stdout
    return any(is_wineserver_line(line) for line in out.splitlines())


def wine_procs():
    """Every Wine process: game, renderer, and the prefix's own services."""
    out = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if wine_exe_path(cmd):
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
    mismatch = dalamud_mismatch(dalamud_supported_game(), game_version())
    for n, pid in enumerate(live, 1):
        if pid in port_of:
            print(f"window {n} (pid {pid}): listening on {port_of[pid]}")
        elif mismatch:
            print(f"window {n} (pid {pid}): running without plugins")
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

    where = netlog_dir()
    gated = " - inside a folder launchd agents cannot read; the watcher moves it once no game runs" if where and in_gated_folder(where) else ""
    print(f"network log: {where or 'no wineprefix found'}{gated}")
    if mismatch:
        print(f"{mismatch} - XIV on Mac starts the game without plugins until Dalamud updates")

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
    hang_watch = HangWatch()
    exit_watch = ExitWatch()
    memory_log = MemoryLog()
    server_sweep = ServerSweep()
    install_nudge = InstallNudge()
    last_exit_at = None
    while True:
        live = game_pids()
        running_now = procs("ffxiv_dx11.exe")
        stall_watch.tick(time.time(), running_now)
        hang_watch.tick(time.time(), running_now)
        exit_watch.tick(time.time(), running_now)
        memory_log.tick(time.time(), running_now)
        sweep_orphan_renderers(kill=True, say=log)
        server_sweep.tick(time.time(), running_now)
        install_nudge.tick(time.time(), running_now)
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
        if was_running and not live:
            last_exit_at = time.time()
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
                mismatch = dalamud_mismatch(dalamud_supported_game(), game_version())
                seen[pid] = initial_state(dalamud_on, iinact_on, mismatch)
                if seen[pid] == "untracked":
                    why = "Dalamud is off" if not dalamud_on else mismatch or "IINACT is not set to load"
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
        elif not live and server_seen and not alive:
            note = teardown_note(last_exit_at, time.time())
            if note:
                log(note)
            server_seen = False
            last_exit_at = None
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
    elif "--memory" in sys.argv:
        print(memory_report(read_memory_log()), end="")
    elif "--orphans" in sys.argv or "--kill-orphans" in sys.argv:
        status()
        print()
        report_orphans(kill="--kill-orphans" in sys.argv)
    else:
        status()
        print()
        report_orphans()
