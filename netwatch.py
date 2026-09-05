#!/usr/bin/env python3
"""Logs network-path changes (tunnel interfaces, default route, the route to the JP game servers)
and Dalamud's plugin-repo fetch outcomes, so a wedged plugin installer can be correlated with a
tunnel rebuild. Read-only. --watch runs forever; --last N prints the last N events."""
import os, re, time, re, subprocess, sys, time, datetime

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netwatch.log")
DALAMUD = os.path.expanduser("~/Library/Application Support/XIV on Mac/logs/dalamud.log")
GAME_IP = "124.150.157.1"   # inside SE's JP game range

def sh(*a):
    try: return subprocess.run(a, capture_output=True, text=True, timeout=10).stdout
    except Exception: return ""

def state():
    tun = sorted(set(re.findall(r"^(utun\d+)", sh("ifconfig"), re.M)))
    routes = sh("netstat", "-rn", "-f", "inet")
    default = next((l.split()[-1] for l in routes.splitlines() if l.startswith("default")), "?")
    game = next((l.split()[-1] for l in sh("route", "-n", "get", GAME_IP).splitlines() if "interface:" in l), "?")
    mudfish = "up" if sh("pgrep", "-f", "Mudfish Cloud VPN").strip() else "down"
    return {"tunnels": ",".join(tun), "default": default, "game_route": game, "mudfish": mudfish}

def note(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    with open(LOG, "a") as f: f.write(line + "\n")
    print(line, flush=True)

_WINE_EXE = re.compile(r"^[A-Za-z]:\\.*?\.exe(?=\s|$)")

def game_pids():
    """macOS pids of the game itself. pgrep -f also matched DalamudCrashHandler, whose command line
    names the game exe, and the Wine paths hold spaces so argv[0] cannot be split off."""
    out = sh("ps", "-Ao", "pid=,command=")
    pids = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        m = _WINE_EXE.match(cmd)
        if m and m.group(0).endswith("\\ffxiv_dx11.exe"):
            pids.append(pid)
    return pids


def snapshot(state):
    """First failures in a session: capture what the game's network looks like right then, and
    whether the same requests work from macOS, so the next occurrence explains itself."""
    global snapped
    if snapped:
        return
    snapped = True

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"httpfail-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt")
    pid = game_pids()
    with open(out, "w") as f:
        f.write("state: " + " | ".join(f"{k}={v}" for k, v in state.items()) + "\n\n")
        for p in pid[:2]:
            f.write(f"=== game pid {p}: sockets by state ===\n")
            f.write(sh("lsof", "-nP", "-p", p, "-iTCP") + "\n")
            f.write(f"=== game pid {p}: open handle count ===\n")
            f.write(str(len(sh("lsof", "-nP", "-p", p).splitlines())) + "\n\n")
        f.write("=== same requests from macOS ===\n")
        for url in ("https://kamori.goats.dev/Plugin/PluginMaster", "https://raw.githubusercontent.com/marzent/IINACT/main/repo.json"):
            f.write(url + "\n" + sh("curl", "-sS", "-o", "/dev/null", "-m", "15", "-w",
                                     "  http=%{http_code} dns=%{time_namelookup}s connect=%{time_connect}s total=%{time_total}s\n", url) + "\n")
        f.write("=== routes ===\n" + sh("netstat", "-rn", "-f", "inet")[:4000])
    note(f"captured the failing state to {os.path.basename(out)}")

    # Where the stalled request is sitting only shows in a thread sample taken while it stalls.
    # Requests time out after ~20 s and the poll runs every 15 s, so sample now and again shortly after.
    for n in (1, 2):
        for p in pid[:2]:
            sh("sample", p, "3", "-file", out.replace(".txt", f"-sample{n}-{p}.txt"))
        if n == 1:
            time.sleep(8)
    note("thread samples taken")


snapped = False


def watch():
    global snapped
    prev = state(); note("start: " + " | ".join(f"{k}={v}" for k, v in prev.items()))
    seen_fail = seen_ok = 0
    primed = False
    while True:
        time.sleep(15)
        cur = state()
        for k in cur:
            if cur[k] != prev[k]: note(f"CHANGE {k}: {prev[k]} -> {cur[k]}")
        if not game_pids(): snapped = False
        prev = cur
        try:
            with open(DALAMUD, encoding="utf-8", errors="ignore") as f: text = f.read()
        except FileNotFoundError:
            continue
        fails, oks = text.count("PluginMaster failed"), text.count("Successfully fetched repo")
        if fails > seen_fail and primed:
            note(f"dalamud: {fails - seen_fail} repo fetch failure(s) | " + " | ".join(f"{k}={v}" for k, v in cur.items()))
            snapshot(cur)
        if oks > seen_ok and primed:
            note(f"dalamud: {oks - seen_ok} repo fetch success(es)")
        seen_fail, seen_ok, primed = fails, oks, True

if __name__ == "__main__":
    if "--watch" in sys.argv: watch()
    else:
        n = int(sys.argv[sys.argv.index("--last") + 1]) if "--last" in sys.argv else 20
        print("".join(open(LOG).readlines()[-n:]) if os.path.exists(LOG) else "(nothing logged yet)")
        print("now: " + " | ".join(f"{k}={v}" for k, v in state().items()))
