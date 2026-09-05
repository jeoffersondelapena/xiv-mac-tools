#!/usr/bin/env python3
"""XIV on Mac boot monitor.  --watch: run forever, write one report per boot (and sample a stalled game);
--last N: print the last N boot reports from the logs.  Reports go next to this script."""
import os, re, sys, time, glob, subprocess, datetime
BASE = os.path.expanduser("~/Library/Application Support/XIV on Mac")
LOGS = os.path.join(BASE, "logs"); HERE = os.path.dirname(os.path.abspath(__file__))
FIRST_FRAME_LIMIT, PLUGIN_LIMIT = 75, 150   # seconds after boot start before we call it stalled

def read_log():
    out = []
    for f in ("dalamud.old.log", "dalamud.log"):
        try:
            with open(os.path.join(LOGS, f), encoding="utf-8", errors="ignore") as fh: out += fh.read().splitlines()
        except FileNotFoundError: pass
    return out

def ts(line):
    try: return datetime.datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S.%f")
    except Exception: return None

def boots(lines):
    idx = [i for i, l in enumerate(lines) if "Initializing VFS database" in l or "[PluginManager] Boot load started" in l]
    # One boot writes both markers seconds apart; separate launches are minutes apart. A wedged boot
    # logs almost nothing, so line distance merges it with the relaunch that follows.
    kept = []
    for i in idx:
        if not kept:
            kept.append(i); continue
        a, b = ts(lines[kept[-1]]), ts(lines[i])
        same_boot = (b - a).total_seconds() < 60 if a and b else i - kept[-1] <= 200
        if not same_boot:
            kept.append(i)
    idx = kept
    return [(i, idx[n + 1] if n + 1 < len(idx) else len(lines)) for n, i in enumerate(idx)]

def analyse(seg):
    t0 = ts(seg[0]); r = {"start": t0, "first_frame": None, "plugins_done": None, "ended": False, "loads": {}, "unfinished": [], "errors": [], "hitches": 0}
    for l in seg:
        t = ts(l)
        if "FrameworkTickAsync) START" in l and not r["first_frame"]: r["first_frame"] = t
        elif "Loaded plugins on boot" in l: r["plugins_done"] = t
        elif "Session has ended" in l: r["ended"] = True
        elif "[PluginManager] Loading plugin " in l or "Loading dev plugin " in l:
            name = l.split("plugin ")[-1].strip(); r["loads"].setdefault(name, [t, None])
        elif "[LocalPlugin] Finished loading " in l:
            name = l.split("Finished loading ")[-1].strip()
            if name in r["loads"]: r["loads"][name][1] = t
        elif "[ERR]" in l or "[FTL]" in l: r["errors"].append(l[11:19] + " " + re.sub(r".*\] ", "", l)[:110])
        elif "[HITCH]" in l: r["hitches"] += 1
    r["unfinished"] = [n for n, (a, b) in r["loads"].items() if b is None]
    return r

def crash_after(t0):
    for p in sorted(glob.glob(os.path.join(LOGS, "dalamud_appcrash_*.log"))):
        m = re.search(r"appcrash_(\d{8})_(\d{6})", p)
        if m and datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") >= t0: return p
    return None

LAST_SEG = None
def report(seg, live=False):
    r = analyse(seg); t0 = r["start"]; f = lambda t: t.strftime("%H:%M:%S") if t else "never"
    d = lambda t: f"+{int((t - t0).total_seconds())}s" if t else "never"
    lines = [f"BOOT {f(t0)}", f"  first frame: {d(r['first_frame'])} | all plugins loaded: {d(r['plugins_done'])}"]
    slow = sorted(((b - a).total_seconds(), n) for n, (a, b) in r["loads"].items() if b)[-5:]
    if slow: lines.append("  slowest plugin loads: " + ", ".join(f"{n} {s:.1f}s" for s, n in reversed(slow)))
    if r["unfinished"]:
        lines.append("  plugins that STARTED loading but never finished: " + ", ".join(r["unfinished"]))
        # Three stalls of this batch went unattributed because the log had rotated by the time anyone
        # looked; keep what was happening right before the silence, and the last constructor entered.
        noise = ("Finished loading", "Loading plugin", "Hook verification", "Loading dev plugin")
        tail = [re.sub(r".*\] ", "", l)[:70] for l in seg[-40:] if not any(n in l for n in noise)][-5:]
        lines.append("  last log lines before the stall: " + " | ".join(tail))
        created = [l for l in seg if "Creating plugin instance for" in l]
        if created: lines.append("  last plugin instance created: " + re.sub(r".*instance for ", "", created[-1]).strip()[:60])
    crash = crash_after(t0)
    if crash:
        top = [l.strip() for l in open(crash, encoding="utf-8", errors="ignore").read().splitlines() if re.match(r"\s*\[\d+\]", l)][:6]
        lines.append(f"  CRASH log: {os.path.basename(crash)}; top frames: " + " > ".join(x.split("]")[-1].strip() for x in top))
        lines.append("  last log lines before the crash: " + " | ".join(re.sub(r".*\] ", "", l)[:60] for l in seg[-4:]))
    if r["errors"]: lines.append(f"  errors ({len(r['errors'])}): " + " | ".join(r["errors"][:4]))
    if r["hitches"]: lines.append(f"  hitches: {r['hitches']}")
    if "Browsingway" not in r["loads"]: lines.append("  Browsingway (dev plugin) did NOT load this boot; if a game patch just landed, rebase and rebuild the fork")
    lines += fork_status()
    if not live:
        if seg is LAST_SEG and game_pid(): lines.append("  verdict: still running"); return "\n".join(lines)
        verdict = "crashed" if crash else ("never drew a frame (black screen)" if not r["first_frame"] else
                  ("plugin loading stalled" if not r["plugins_done"] else ("ok, quit normally" if r["ended"] else "ok, but ended without a normal quit (killed?)")))
        lines.append(f"  verdict: {verdict}")
    return "\n".join(lines)

FORK = os.path.expanduser("~/Projects/browsingway-fork")
def fork_status():
    if not os.path.isdir(FORK): return []
    try:
        subprocess.run(["git", "fetch", "origin", "-q"], cwd=FORK, capture_output=True, timeout=30)
        n = subprocess.run(["git", "rev-list", "--count", "HEAD..origin/main"], cwd=FORK, capture_output=True, text=True).stdout.strip()
        if n and n != "0":
            last = subprocess.run(["git", "log", "-1", "--format=%cs %s", "origin/main"], cwd=FORK, capture_output=True, text=True).stdout.strip()
            return [f"  upstream Browsingway has {n} new commit(s) not in your local branch (latest: {last[:70]}); consider rebase + rebuild"]
    except Exception: pass
    return []

def _game_pids():
    # The path holds spaces, so argv[0] cannot be split off; instead take the first ".exe"
    # in the command, which is the crash handler's own name when it is the crash handler.
    out = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        end = cmd.find(".exe")
        if end != -1 and cmd[:end + 4].endswith("ffxiv_dx11.exe"):
            pids.append(int(pid))
    return pids


def game_pid():
    pids = _game_pids()
    return str(pids[0]) if pids else None


def watch():
    seen = set(); sampled = set()
    while True:
        lines = read_log(); bl = boots(lines)
        if bl:
            i, j = bl[-1]; seg = lines[i:j]; r = analyse(seg); key = seg[0][:23]
            age = (datetime.datetime.now() - r["start"]).total_seconds(); pid = game_pid()
            stalled = pid and ((not r["first_frame"] and age > FIRST_FRAME_LIMIT) or (r["first_frame"] and not r["plugins_done"] and age > PLUGIN_LIMIT))
            if stalled and key not in sampled:
                sampled.add(key); out = os.path.join(HERE, f"wedge-sample-{r['start'].strftime('%H%M%S')}.txt")
                subprocess.run(["sample", pid, "8", "-file", out], capture_output=True)
                open(os.path.join(HERE, f"boot-{r['start'].strftime('%H%M%S')}.txt"), "a").write(report(seg, live=True) + f"\n  STALL detected at +{int(age)}s; thread sample: {out}\n")
            done = (r["plugins_done"] or r["ended"] or crash_after(r["start"])) and not pid
            if (done or (r["plugins_done"] and age > 300)) and key not in seen:
                seen.add(key); open(os.path.join(HERE, f"boot-{r['start'].strftime('%H%M%S')}.txt"), "a").write(report(seg) + "\n")
        time.sleep(10)

if __name__ == "__main__":
    if "--watch" in sys.argv: watch()
    else:
        n = int(sys.argv[sys.argv.index("--last") + 1]) if "--last" in sys.argv else 3
        lines = read_log()
        bl = boots(lines)
        for i, j in bl[-n:]:
            seg = lines[i:j]
            if (i, j) == bl[-1]: LAST_SEG = seg
            print(report(seg), "\n")
