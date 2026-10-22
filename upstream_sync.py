#!/usr/bin/env python3
"""Keep the plugin forks current with their upstreams, using each fork's upstream-sync workflow.

Once an hour (launchd): if an upstream's default branch moved since the last sync, dispatch the fork's
workflow, wait for it, then download the build it published, check the hash list, install it into the
dev-plugin folder (only while no game window runs), and fast-forward the local clone. A failed run, a
dirty clone or a Dalamud API mismatch is reported and left alone.

The same tick also copies Dalamud's log files aside while no game runs: Dalamud keeps one session back
and only its first 10 MB, so a relaunch before a log was read loses lines. The watchers' captures are
pruned to the newest few of each kind.

Two more chores ride on the tick: after a game patch, once the game-data source has caught up, the
GatherBuddy Reborn lists are regenerated and the settings policy re-applied (game closed); and once a
week Codex's wiki data is rebuilt and handed to the plugin. When GatherBuddy Reborn or Wrath Combo changes
version, its settings policy is checked again and re-applied with the game closed.

By hand, for a change made in a fork itself (pushed first): `--build NAME` runs the fork's workflow and
queues the verified build exactly as a sync would; `--install-now NAME` places a queued build while the game
runs, for the player to switch the plugin off and on.
"""
import datetime, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from attention import ATTENTION, attention_lines, set_attention  # noqa: E402,F401

BASE = os.path.expanduser("~/Library/Application Support/XIV on Mac")
STATE = os.path.join(BASE, "wedge-watch", "upstream-sync-state.json")
LOG = os.path.join(BASE, "wedge-watch", "upstream-sync.log")
WORKFLOW = "upstream-sync.yml"
LOG_DIR = os.path.join(BASE, "logs")
LOG_ARCHIVE = os.path.join(BASE, "wedge-watch", "log-archive")
LOG_ARCHIVE_KEEP = 30
LOG_SOURCES = ("dalamud.log", "dalamud.old.log")
WATCH_DIR = os.path.join(BASE, "wedge-watch")
# the watchers' captures, newest kept per kind (an httpfail event is one report plus up to two samples)
CAPTURE_KEEP = {"boot-*.txt": 30, "wedge-sample-*.txt": 5, "hang-sample-*.txt": 5, "stall-sample-*.txt": 5, "httpfail-*.txt": 15,
                "iinact-stall*.txt": 2, "exit-stuck-*.txt": 10,
                "exit-sample-*.txt": 3}
GAME_VER = os.path.join(BASE, "ffxiv", "game", "ffxivgame.ver")
XIVAPI_PROBE = "https://v2.xivapi.com/api/sheet/Item?limit=1&fields=Name"   # its "version" is the key of the patch it serves
LISTS_TOOL = os.path.expanduser("~/.claude/skills/gbr-lists/gbr_lists.py")
SETTINGS_TOOL = os.path.expanduser("~/.claude/skills/gbr-settings/gbr_check.py")
CODEX_PIPELINE = os.path.expanduser("~/Projects/ffxiv-codex/data/pipeline/run_all.py")
CODEX_DATA = os.path.expanduser("~/Projects/ffxiv-codex/data/codex-data.json")
CODEX_LIVE = os.path.join(BASE, "pluginConfigs", "Codex", "codex-data.json")
CODEX_REFRESH_DAYS = 7
WRATH_TOOL = os.path.expanduser("~/.claude/skills/wrath-settings/wrath_check.py")
GEAR_SOURCE = "Gear plan"
GEAR_MIN_LEVEL = 790
GEAR_SEARCH = ("https://v2.xivapi.com/api/search?sheets=Item&limit=500&fields=Name,LevelItem@as(raw),EquipSlotCategory@as(raw),"
               "BaseParam[].Name,BaseParamValue,BaseParamSpecial[].Name,BaseParamValueSpecial,ItemAction.Data,ClassJobCategory.Name&query=")
ITEM_FOOD = "https://v2.xivapi.com/api/sheet/ItemFood/{}?fields=BaseParam[].Name,IsRelative,Value,Max,ValueHQ,MaxHQ"
MATERIA_SHEET = "https://v2.xivapi.com/api/sheet/Materia?limit=100&fields=BaseParam.Name,Item[].Name,Value"
# the plan's consumables too: a new food or potion tier, or a new materia grade, moves the best melds
GEAR_QUERIES = {"gear": f"+LevelItem>={GEAR_MIN_LEVEL}", "food": "+ItemUICategory=46 +LevelItem>=770",
                "potions": "+ItemUICategory=44 +LevelItem>=770", "materia": "+ItemUICategory=58 +LevelItem>=690"}
# xivgear's job sims decide the gear skill's rankings, so a change to them is as good as a patch
XIVGEAR_SIMS = "https://api.github.com/repos/xiv-gear-planner/gear-planner/commits?path=packages/sims/src&per_page=1"
XIVGEAR_COMMIT = "https://api.github.com/repos/xiv-gear-planner/gear-planner/commits/{}"
LODESTONE = "https://na.finalfantasyxiv.com"
LODESTONE_LOG = LODESTONE + "/lodestone/special/patchnote_log/"
GEAR_TERMS = ("bygone brass", "thundersteep", "universal tomestone", "mnemonics", "treno rain", "courtly lover",
              "everkeep certificate", "phantom vision", "occultum", "eclipticum", r"sacks? of nuts",
              r"(ordelle|montiont|ranperre) coin", "heavy holoblade", "final final fixative", "enlightenment silver",
              r"materia xii", r"materia xiii", "gemdraught")
# settings policies re-checked when their plugin's installed version changes
POLICIES = [
    {"name": "GatherBuddyReborn", "tool": SETTINGS_TOOL,
     "manifest": os.path.expanduser("~/Projects/gbr-fork/GatherBuddy/bin/Release/GatherBuddyReborn.json")},
    {"name": "WrathCombo", "tool": WRATH_TOOL,
     "manifest": os.path.join(BASE, "installedPlugins", "WrathCombo", "*", "WrathCombo.json")},
]
TOOL_TIMEOUT = 40 * 60
RUN_TIMEOUT = 25 * 60
WINE_CMD_RE = re.compile(r"^[A-Za-z]:\\.*?\.exe(?=\s|$)")

PLUGINS = [
    {"name": "IINACT", "fork": "jeoffersondelapena/IINACT", "upstream": "marzent/IINACT", "upstream_branch": "main",
     "clone": os.path.expanduser("~/Projects/iinact-fork"), "branch": "macos",
     "install_dir": os.path.expanduser("~/Projects/iinact-fork/IINACT/bin/Release/win-x64"), "manifest": "IINACT.json"},
    {"name": "Browsingway", "fork": "jeoffersondelapena/Browsingway", "upstream": "Styr1x/Browsingway", "upstream_branch": "main",
     "clone": os.path.expanduser("~/Projects/browsingway-fork"), "branch": "macos",
     "install_dir": os.path.expanduser("~/Projects/browsingway-fork/out"), "manifest": "Browsingway.json"},
    {"name": "GatherBuddyReborn", "fork": "jeoffersondelapena/GatherBuddyReborn", "upstream": "FFXIV-CombatReborn/GatherBuddyReborn", "upstream_branch": "main",
     "clone": os.path.expanduser("~/Projects/gbr-fork"), "branch": "patches",
     "install_dir": os.path.expanduser("~/Projects/gbr-fork/GatherBuddy/bin/Release"), "manifest": "GatherBuddyReborn.json"},
    {"name": "ICE", "fork": "jeoffersondelapena/Ices-Cosmic-Exploration", "upstream": "LeontopodiumNivale14/Ices-Cosmic-Exploration", "upstream_branch": "Main-Branch",
     "clone": os.path.expanduser("~/Projects/ice-fork"), "branch": "patches",
     "install_dir": os.path.expanduser("~/Projects/ice-fork/ICE/bin/Release"), "manifest": "ICE.json"},
]


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def notify(title, text):
    subprocess.run(["osascript", "-e", f'display notification "{text}" with title "{title}"'], capture_output=True)


def sh(args, cwd=None, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def game_running():
    out = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True).stdout
    return any((m := WINE_CMD_RE.match(l)) and m.group(0).endswith("\\ffxiv_dx11.exe") for l in out.splitlines())


# --- pure decisions -------------------------------------------------------------------------------

def needs_sync(state, upstream_head, local_api_level=None, branch_head=None):
    """A new upstream head means work. A head that already failed is retried when the fork's branch has
    been pushed since (a conflict resolved by hand) or, for an API mismatch, when this Mac's Dalamud changed."""
    if upstream_head == state.get("synced_upstream"):
        return False
    if upstream_head == state.get("failed_upstream"):
        if branch_head is not None and branch_head != state.get("failed_branch"):
            return True
        failed_level = state.get("failed_api_level")
        return failed_level is not None and failed_level != local_api_level
    return True


def artifact_name(plugin, sha):
    return f"{plugin}-{sha}"


SOLVER = os.path.expanduser("~/Projects/gbr-fork/GatherBuddy/bin/Release/raphael-cli.exe")
SOLVER_SOURCE = "GatherBuddyReborn solver"


def sha256_of(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def solver_sha_from_sums(sums_text):
    for line in sums_text.splitlines():
        digest, _, name = line.partition("  ")
        if name.strip().lstrip("./") == "raphael-cli.exe":
            return digest.strip()
    return None


def check_solver(state, path=SOLVER, attention_path=None):
    """GatherBuddy's quality solver is a separate binary beside the plugin; without it, or with one that is not what the
    last installed build shipped, the recipes that need it are skipped with only a passing chat line. Returns the problem."""
    expected = (state.get("GatherBuddyReborn") or {}).get("solver_sha256")
    if not os.path.exists(path):
        problem = "the Raphael solver (raphael-cli.exe) is missing next to GatherBuddy Reborn; recipes that need it are skipped"
    elif expected and sha256_of(path) != expected:
        problem = "the Raphael solver beside GatherBuddy Reborn is not the one its last installed build shipped; recipes that need it may fail"
    else:
        problem = None
    st = state.setdefault(SOLVER_SOURCE, {})
    if problem:
        st["note"] = problem
    else:
        st.pop("note", None)
    set_attention(SOLVER_SOURCE, problem, *([attention_path] if attention_path else []))
    return problem


def verify_hashes(sums_text, read_file):
    """Every line of SHA256SUMS must match the file it names; returns the list of mismatches."""
    bad = []
    for line in sums_text.splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        name = name.strip().lstrip("./")
        data = read_file(name)
        if data is None or hashlib.sha256(data).hexdigest() != digest.strip():
            bad.append(name)
    return bad


def api_level_compatible(manifest_level, local_level):
    return manifest_level is None or local_level is None or manifest_level == local_level


def clone_is_clean(status_short, unpushed):
    return status_short.strip() == "" and unpushed == 0


# --- github --------------------------------------------------------------------------------------

def upstream_head(plugin):
    return sh(["gh", "api", f"repos/{plugin['upstream']}/commits/{plugin['upstream_branch']}", "--jq", ".sha"])


def branch_head(plugin):
    return sh(["gh", "api", f"repos/{plugin['fork']}/commits/{plugin['branch']}", "--jq", ".sha"])


def dispatch_and_wait(plugin):
    """Run the fork's workflow and return (conclusion, run id, head sha of the run)."""
    sh(["gh", "workflow", "run", WORKFLOW, "-R", plugin["fork"], "--ref", plugin["branch"]])
    time.sleep(10)
    run_id = None
    deadline = time.time() + RUN_TIMEOUT
    while time.time() < deadline:
        runs = json.loads(sh(["gh", "run", "list", "-R", plugin["fork"], "--workflow", WORKFLOW, "-L", "1",
                              "--json", "databaseId,status,conclusion"]))
        if runs:
            run_id = runs[0]["databaseId"]
            if runs[0]["status"] == "completed":
                return runs[0]["conclusion"], run_id
        time.sleep(30)
    return "timeout", run_id


def download_artifact(plugin, run_id, dest):
    """The single artifact of the run; its COMMIT file names the rebased head."""
    arts = json.loads(sh(["gh", "api", f"repos/{plugin['fork']}/actions/runs/{run_id}/artifacts", "--jq", ".artifacts"]))
    names = [a["name"] for a in arts if a["name"].startswith(plugin["name"] + "-")]
    if len(names) != 1:
        raise RuntimeError(f"expected one artifact, found {names}")
    sh(["gh", "run", "download", str(run_id), "-R", plugin["fork"], "-n", names[0], "-D", dest])
    return names[0]


def local_api_level():
    try:
        version = json.load(open(os.path.join(BASE, "logs", "dalamud.troubleshooting.json")))["DalamudVersion"]
        return int(version.split(".")[0])
    except (OSError, ValueError, KeyError):
        return None


def artifact_api_level(dest, manifest):
    try:
        return int(json.load(open(os.path.join(dest, manifest)))["DalamudApiLevel"])
    except (OSError, ValueError, KeyError):
        return None


def install(plugin, dest):
    """Copy the build over the dev folder; extra files there (the parser DLLs IINACT fetched) are kept.
    Each file is swapped in whole under its name: a process that has the old one mapped (Browsingway's
    renderer runs from this folder) keeps it, where writing in place would change it underneath."""
    os.makedirs(plugin["install_dir"], exist_ok=True)
    for root, _, files in os.walk(dest):
        for name in files:
            if name in ("SHA256SUMS", "COMMIT", "UPSTREAM"):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, dest)
            target = os.path.join(plugin["install_dir"], rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            staged = target + ".installing"
            shutil.copy2(src, staged)
            os.replace(staged, target)


def sync_clone(plugin, sha):
    """Fast-forward the local clone to the rebased branch, only when nothing local would be lost."""
    clone, branch = plugin["clone"], plugin["branch"]
    sh(["git", "fetch", "fork", branch], cwd=clone)
    status = sh(["git", "status", "--short"], cwd=clone)
    # A rebase rewrites every commit, so compare patches, not hashes: only work that is truly absent upstream counts.
    unpushed = sum(1 for l in sh(["git", "cherry", f"fork/{branch}", branch], cwd=clone, check=False).splitlines() if l.startswith("+"))
    if not clone_is_clean(status, unpushed):
        return f"clone not synced: {'uncommitted changes' if status.strip() else f'{unpushed} unpushed commit(s)'}"
    sh(["git", "checkout", "-q", branch], cwd=clone)
    sh(["git", "reset", "-q", "--hard", f"fork/{branch}"], cwd=clone)
    head = sh(["git", "rev-parse", "HEAD"], cwd=clone)
    return "clone synced" if head == sha else f"clone at {head[:7]}, build is {sha[:7]}"


# --- main ----------------------------------------------------------------------------------------

def snapshot_name(source, mtime):
    """dalamud-<last write>.log; -old marks the file Dalamud rolled over at a launch."""
    stamp = datetime.datetime.fromtimestamp(mtime).strftime("%Y%m%d-%H%M%S")
    return f"dalamud-{stamp}{'-old' if source.endswith('.old.log') else ''}.log"


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prune_archive(archive, keep):
    files = sorted((os.path.getmtime(p), p) for p in glob.glob(os.path.join(archive, "dalamud-*.log")))
    for _, path in files[:max(0, len(files) - keep)]:
        os.remove(path)


def prune_captures(folder=WATCH_DIR, rules=CAPTURE_KEEP):
    """Delete the older captures beyond each kind's keep count; returns the names removed."""
    removed = []
    for pattern, keep in rules.items():
        files = sorted(glob.glob(os.path.join(folder, pattern)), key=os.path.getmtime)
        for path in files[:max(0, len(files) - keep)]:
            os.remove(path)
            removed.append(os.path.basename(path))
    return removed


def snapshot_logs(state, running=None, log_dir=LOG_DIR, archive=LOG_ARCHIVE, keep=LOG_ARCHIVE_KEEP):
    """Copy each changed Dalamud log file into the archive while no game runs; returns the names written.
    A rolled-over .old.log that repeats an earlier copy byte for byte is skipped."""
    if (running or game_running)():
        return []
    st = state.setdefault("log_snapshots", {})
    digests, seen = st.setdefault("digests", []), st.setdefault("seen", {})
    written = []
    for source in LOG_SOURCES:
        path = os.path.join(log_dir, source)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            continue
        stat = os.stat(path)
        if seen.get(source) == [stat.st_mtime, stat.st_size]:
            continue
        digest = file_digest(path)
        if digest not in digests:
            os.makedirs(archive, exist_ok=True)
            name = snapshot_name(source, stat.st_mtime)
            shutil.copy2(path, os.path.join(archive, name))
            digests.append(digest)
            del digests[:-2 * keep]
            written.append(name)
        seen[source] = [stat.st_mtime, stat.st_size]
    if written:
        prune_archive(archive, keep)
    return written


def game_version():
    try:
        return open(GAME_VER).read().strip() or None
    except OSError:
        return None


def xivapi_key():
    try:
        req = urllib.request.Request(XIVAPI_PROBE, headers={"User-Agent": "xiv-mac-tools/1"})
        return json.load(urllib.request.urlopen(req, timeout=30)).get("version") or None
    except Exception:
        return None


def seed(state, version, api_key, now, plugin_versions=()):
    """First tick with this feature: the present is the baseline, nothing is regenerated or re-checked for it."""
    st = state.setdefault("patch", {})
    if version and "game_version" not in st:
        st["game_version"], st["api_key"] = version, api_key
    state.setdefault("codex_refreshed", now.isoformat(timespec="seconds"))
    policies = state.setdefault("policies", {})
    for name, pv in plugin_versions:
        if pv and name not in policies:
            policies[name] = pv


def plugin_version(manifest_glob):
    """AssemblyVersion of the newest manifest the pattern matches; Dalamud keeps old plugin folders around."""
    vers = []
    for path in glob.glob(manifest_glob):
        try:
            with open(path) as f:
                v = json.load(f).get("AssemblyVersion")
        except (OSError, ValueError):
            continue
        if v:
            vers.append(v)
    return max(vers, key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v))) if vers else None


def policy_check_needed(state, name, version):
    seen = (state.get("policies") or {}).get(name)
    return bool(version) and seen is not None and seen != version


def check_policy(state, policy, version):
    name, tool = policy["name"], policy["tool"]
    code, result, err = run_tool([tool])
    if result.startswith("RESULT: matches policy"):
        outcome = "matches the policy"
    elif "DRIFT" in result or "differ" in result:
        if game_running():
            log(f"{name} {version}: settings drifted; waiting for the game to close to re-apply the policy")
            return
        run_tool([tool, "--write"])
        code, again, err = run_tool([tool])
        if again.startswith("RESULT: matches policy"):
            outcome = "re-applied"
        else:
            outcome = f"re-apply did not stick ({again or err})"
            notify(f"{name} settings need a hand", f"After the update to {version}: {again or err}"[:200])
            state.setdefault("policy_notes", {})[name] = f"the settings policy needs a look after the update to {version}"
    else:
        outcome = f"needs a hand ({result or err})"
        notify(f"{name} settings need a hand", f"After the update to {version}: {result or err}"[:200])
        state.setdefault("policy_notes", {})[name] = f"the settings policy needs a look after the update to {version}"
    if outcome in ("matches the policy", "re-applied"):
        (state.get("policy_notes") or {}).pop(name, None)
        set_attention(name, None)
    state.setdefault("policies", {})[name] = version
    log(f"{name} {version}: policy check: {outcome}")


def patch_regen_needed(state, version, api_key, running):
    """The game moved to a new version and the data source moved since the lists were built, while no game runs."""
    st = state.get("patch") or {}
    if running or not version or not api_key or "game_version" not in st:
        return False
    return version != st["game_version"] and api_key != st.get("api_key")


def codex_refresh_due(state, now, days=CODEX_REFRESH_DAYS):
    last = state.get("codex_refreshed")
    if not last:
        return False
    return (now - datetime.datetime.fromisoformat(last)).days >= days


def run_tool(args, cwd=None):
    """(exit code, the tool's RESULT line, last stderr line)."""
    r = subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True, timeout=TOOL_TIMEOUT)
    result = next((l for l in r.stdout.splitlines() if l.startswith("RESULT")), "")
    return r.returncode, result, (r.stderr.strip().splitlines() or [""])[-1]


def regenerate_after_patch(state, version, api_key):
    log(f"game moved to {version}; regenerating the GatherBuddy Reborn lists")
    code, result, err = run_tool([LISTS_TOOL, "--write"])
    if code != 0 or not result.startswith("RESULT: written"):
        log(f"list regeneration failed: {result or err}")
        notify("GatherBuddy Reborn lists need a hand", f"Regeneration for game {version} failed: {result or err}"[:200])
        set_attention("GatherBuddyReborn", f"the lists could not be regenerated for game {version}")
        return
    # an earlier failed try's note must not outlive the success; a note the state still holds stays
    set_attention("GatherBuddyReborn", standing_note(state, "GatherBuddyReborn"))
    code, settings, err = run_tool([SETTINGS_TOOL])
    if "differ" in settings:
        code, settings, err = run_tool([SETTINGS_TOOL, "--write"])
    if settings.startswith("RESULT: BLOCKED") or not settings:
        notify("GatherBuddy Reborn settings need a hand", f"After game {version}: {settings or err}"[:200])
        set_attention("GatherBuddyReborn", f"the settings policy needs a look after game {version}")
    state["patch"] = {"game_version": version, "api_key": api_key, "at": datetime.datetime.now().isoformat(timespec="seconds")}
    log(f"lists regenerated for game {version}; settings: {settings}")
    notify("Lists regenerated for the new patch", f"Game {version}. {result[8:120]}")


def refresh_codex(state, now):
    log("refreshing Codex's data from the wiki")
    r = subprocess.run([sys.executable, CODEX_PIPELINE], capture_output=True, text=True, timeout=TOOL_TIMEOUT)
    if r.returncode != 0 or not os.path.exists(CODEX_DATA):
        tail = (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or [""])[-1]
        log(f"Codex data refresh failed: {tail}")
        notify("Codex data refresh failed", tail[:200])
        # try again tomorrow rather than every hour
        state["codex_refreshed"] = (now - datetime.timedelta(days=CODEX_REFRESH_DAYS - 1)).isoformat(timespec="seconds")
        return
    os.makedirs(os.path.dirname(CODEX_LIVE), exist_ok=True)
    tmp = CODEX_LIVE + ".tmp"
    shutil.copyfile(CODEX_DATA, tmp)
    os.replace(tmp, CODEX_LIVE)
    state["codex_refreshed"] = now.isoformat(timespec="seconds")
    log("Codex data refreshed; the plugin picks it up at the next launch or /codex reload")


def xivapi_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "xiv-mac-tools/1"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def raw(fields, name):
    """A column asked for @as(raw) comes back under that suffixed key; a plain one as a dict with a value."""
    value = fields.get(name + "@as(raw)", fields.get(name))
    return value.get("value") if isinstance(value, dict) else value


def param_text(names, values):
    return ",".join(f"{n['fields']['Name'] if isinstance(n, dict) else n}:{v}" for n, v in zip(names, values) if v)


def food_fingerprint(fetch, fields):
    """The meal's or potion's bonuses, read from the ItemFood row its action points at."""
    data = (fields.get("ItemAction") or {}).get("fields", {}).get("Data") or []
    if len(data) < 2 or not data[1]:
        return ""
    f = fetch(ITEM_FOOD.format(data[1])).get("fields", {})
    return ",".join(f"{p['fields'].get('Name')}:{'%' if rel else ''}{v}/{m}/{vh}/{mh}"
                    for p, rel, v, m, vh, mh in zip(f.get("BaseParam", []), f.get("IsRelative", []), f.get("Value", []),
                                                     f.get("Max", []), f.get("ValueHQ", []), f.get("MaxHQ", [])) if p.get("fields", {}).get("Name"))


def materia_values(fetch):
    """{materia item name: stat value} from the Materia sheet."""
    out = {}
    for row in fetch(MATERIA_SHEET).get("rows", []):
        f = row["fields"]
        for item, value in zip(f.get("Item", []), f.get("Value", [])):
            name = item.get("fields", {}).get("Name") if isinstance(item, dict) else None
            if name:
                out[name] = f"{f.get('BaseParam', {}).get('fields', {}).get('Name')}:{value}"
    return out


def gear_items(fetch=xivapi_json):
    """{name: [item level, stat fingerprint]} for every equippable item at GEAR_MIN_LEVEL or above and the current food, potion
    and materia tiers; the fingerprint is how a stat change to an item the plan already knows is noticed."""
    items, materia = {}, None
    for kind, query in GEAR_QUERIES.items():
        base = GEAR_SEARCH + urllib.parse.quote(query)
        url = base
        for _ in range(10):
            page = fetch(url)
            for row in page.get("results", []):
                f = row["fields"]
                if kind == "gear" and not raw(f, "EquipSlotCategory"):
                    continue
                if kind == "gear":
                    print_ = param_text(f.get("BaseParam", []), f.get("BaseParamValue", [])) + "|" + param_text(f.get("BaseParamSpecial", []), f.get("BaseParamValueSpecial", []))
                elif kind == "materia":
                    materia = materia_values(fetch) if materia is None else materia
                    print_ = materia.get(f["Name"], "")
                else:
                    print_ = food_fingerprint(fetch, f)
                entry = [raw(f, "LevelItem"), print_]
                if kind == "gear":
                    entry.append(((f.get("ClassJobCategory") or {}).get("fields") or {}).get("Name", ""))
                items[f["Name"]] = entry
            if not page.get("next"):
                break
            url = base + "&cursor=" + urllib.parse.quote(str(page["next"]))
    return items


def level_of(entry):
    return entry[0] if isinstance(entry, list) else entry


def jobs_text(categories):
    """'every job', or the job codes a set of ClassJobCategory names spells out."""
    if any(c.startswith(("All Classes", "Disciples")) for c in categories):
        return "every job"
    jobs = sorted({j for c in categories for j in c.split() if j.isupper()})
    return ", ".join(jobs) if jobs else "the affected jobs"


def gear_note(version, known, items):
    """The note for items the data source gained or changed since the snapshot, or None."""
    new = sorted(n for n in items if n not in known)
    changed = sorted(n for n in items if n in known and isinstance(known[n], list) and known[n][1] != items[n][1])
    if not new and not changed:
        return None
    parts = []
    if new:
        parts.append(f"added {len(new)} (highest item level {max(level_of(items[n]) for n in new)}, e.g. {new[0]})")
    if changed:
        parts.append(f"changed the stats of {len(changed)} (e.g. {changed[0]})")
    # food, potions and materia carry no job category: a change to them concerns every job
    jobs = "every job" if any(len(items[n]) < 3 for n in new + changed) else jobs_text({items[n][2] for n in new + changed if items[n][2]})
    return (f"the game data for {version or 'the new patch'} {' and '.join(parts)} among the items the gear plan cares about, gear at item level "
            f"{GEAR_MIN_LEVEL} or above, food, potions and materia; the gear plan page may be out of date; re-run the gear skill for {jobs}")


def gear_check(state, version, api_key, fetch=xivapi_json):
    """Each new data-source version: compare the high-level items with the snapshot; the first look is the baseline."""
    st = state.setdefault(GEAR_SOURCE, {})
    if not api_key or api_key == st.get("api_key"):
        return None
    items = gear_items(fetch)
    if not items:
        return None
    known = st.get("items")
    st["api_key"], st["items"] = api_key, items
    note = gear_note(version, known, items) if known is not None else None
    if note:
        remember_note(st, GEAR_SOURCE, note)
    return note


def http_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 xiv-mac-tools/1"})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")


def newest_patch_notes(log_html):
    """(patch, url) of the highest-numbered 'Patch X.Y Notes' link; the log page is not in date order."""
    best = None
    for href, text in re.findall(r'<a[^>]+href="(/lodestone/topics/detail/[0-9a-f]+/)"[^>]*>(.*?)</a>', log_html, re.S):
        m = re.search(r"Patch (\d+\.\d+)", re.sub(r"<[^>]+>", "", text))
        if m and (best is None or float(m.group(1)) > float(best[0])):
            best = (m.group(1), LODESTONE + href)
    return best


def notes_terms(html, names=()):
    """The plan's currencies and mechanics, and every item name the snapshot holds, that the notes mention."""
    text = re.sub(r"<[^>]+>", " ", html).lower()
    patterns = list(GEAR_TERMS) + [re.escape(n.lower()) for n in names]
    found = []
    for m in (re.search(t, text) for t in patterns):
        if m and m.group(0) not in found:
            found.append(m.group(0))
    return found


def gear_notes_check(state, version, fetch_text=http_text):
    """Each new game version: scan the newest patch notes for the plan's names; the first look is the baseline."""
    st = state.setdefault(GEAR_SOURCE, {})
    if not version or version == st.get("notes_version"):
        return None
    newest = newest_patch_notes(fetch_text(LODESTONE_LOG))
    if not newest:
        return None
    patch, url = newest
    first, previous = "notes_url" not in st, st.get("notes_url")
    st["notes_version"], st["notes_url"] = version, url
    if first or url == previous:
        return None
    hits = notes_terms(fetch_text(url), st.get("items", {}).keys())
    if not hits:
        return None
    note = f"the patch {patch} notes mention {', '.join(hits[:4])}; the gear plan page may be out of date; re-run the gear skill for the jobs that use them"
    remember_note(st, GEAR_SOURCE, note)
    return note


def sim_jobs(files):
    """The jobs whose sims a xivgear commit touched; anything shared counts for every job."""
    jobs, shared = set(), False
    for path in files:
        m = re.match(r"packages/sims/src/(?:tank|healer|melee|ranged|caster|blu)/([a-z]{3})", path)
        if m:
            jobs.add(m.group(1).upper())
        elif path.startswith("packages/sims/src/"):
            shared = True
    return "every job" if shared or not jobs else ", ".join(sorted(jobs))


def xivgear_sims_check(state, fetch_text=http_text):
    """Every tick: has xivgear committed a sim change since the last look? The first look is the baseline."""
    st = state.setdefault(GEAR_SOURCE, {})
    latest = json.loads(fetch_text(XIVGEAR_SIMS))
    if not latest:
        return None
    sha, date = latest[0]["sha"], latest[0]["commit"]["committer"]["date"][:10]
    previous = st.get("sims_sha")
    if sha == previous:
        return None
    st["sims_sha"] = sha
    if previous is None:
        return None
    files = [f["filename"] for f in json.loads(fetch_text(XIVGEAR_COMMIT.format(sha))).get("files", [])]
    jobs = sim_jobs(files)
    note = f"xivgear changed its {jobs} sim on {date} (commit {sha[:7]}); re-run the gear skill for {jobs}"
    remember_note(st, GEAR_SOURCE, note)
    return note


def gear_plan_updated(state, api_key, version, fetch=xivapi_json, fetch_text=http_text):
    """The plan page was re-checked: the present becomes the baseline and the note goes."""
    st = state.setdefault(GEAR_SOURCE, {})
    items = gear_items(fetch)
    if items:
        st["api_key"], st["items"] = api_key, items
    newest = newest_patch_notes(fetch_text(LODESTONE_LOG))
    if newest:
        st["notes_version"], st["notes_url"] = version, newest[1]
    try:
        latest = json.loads(fetch_text(XIVGEAR_SIMS))
        if latest:
            st["sims_sha"] = latest[0]["sha"]
    except Exception as ex:
        log(f"xivgear sims baseline: {type(ex).__name__}: {ex}")
    st.pop("note", None)
    set_attention(GEAR_SOURCE, None)


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE)


def standing_note(state, source):
    """The note the state would re-assert for `source` at the next tick, if any."""
    return (state.get(source) or {}).get("note") or (state.get("policy_notes") or {}).get(source)


def remember_note(st, source, note):
    """A standing problem's note is kept in state and re-asserted every tick; an in-game ack only hides it until then."""
    st["note"] = note
    set_attention(source, note)


def reassert_notes(state):
    """Every note still standing, as (source, note); called each tick before the chores."""
    notes = [(name, st["note"]) for name, st in state.items() if isinstance(st, dict) and st.get("note")]
    notes += [(name, note) for name, note in (state.get("policy_notes") or {}).items() if note]
    return notes


def sync_one(plugin, state):
    name = plugin["name"]
    st = state.setdefault(name, {})
    pending = st.get("pending_install")
    if pending:
        return finish_install(plugin, st, pending)
    head = upstream_head(plugin)
    if not needs_sync(st, head, local_api_level(), branch_head(plugin)):
        return
    st.pop("failed_api_level", None)
    log(f"{name}: upstream {plugin['upstream']} moved to {head[:7]}; running the fork's workflow")
    return build_and_queue(plugin, st, head)


def build_and_queue(plugin, st, head):
    """Run the fork's workflow, check what it published and queue it for install; `head` is the upstream head it sits on."""
    name = plugin["name"]
    conclusion, run_id = dispatch_and_wait(plugin)
    if conclusion != "success":
        st["failed_upstream"] = head
        st["failed_branch"] = branch_head(plugin)
        log(f"{name}: workflow run {run_id} ended with {conclusion}; the branch was left alone")
        notify(f"{name}: upstream sync needs a hand", f"The rebase or build failed (run {run_id}).")
        remember_note(st, name, f"upstream sync needs a hand (workflow run {run_id} {conclusion})")
        return
    dest = tempfile.mkdtemp(prefix=f"{name}-")
    download_artifact(plugin, run_id, dest)
    bad = verify_hashes(open(os.path.join(dest, "SHA256SUMS")).read(),
                        lambda rel: open(os.path.join(dest, rel), "rb").read() if os.path.exists(os.path.join(dest, rel)) else None)
    if bad:
        st["failed_upstream"] = head
        st["failed_branch"] = branch_head(plugin)
        log(f"{name}: artifact hash mismatch on {bad}; not installed")
        notify(f"{name}: build rejected", "Downloaded files did not match their hash list.")
        remember_note(st, name, "a downloaded build failed its hash check and was not installed")
        return
    level, local = artifact_api_level(dest, plugin["manifest"]), local_api_level()
    if not api_level_compatible(level, local):
        st["failed_upstream"] = head
        st["failed_branch"] = branch_head(plugin)
        st["failed_api_level"] = local
        log(f"{name}: build targets Dalamud API {level}, this Mac runs {local}; not installed")
        notify(f"{name}: build not installed", f"Built for Dalamud API {level}; this Mac runs {local}.")
        remember_note(st, name, f"an upstream build for Dalamud API {level} is waiting; this Mac runs API {local}")
        return
    st["pending_install"] = {"dest": dest, "upstream": head, "sha": open(os.path.join(dest, "COMMIT")).read().strip()}
    return finish_install(plugin, st, st["pending_install"])


def finish_install(plugin, st, pending, while_running=False):
    name = plugin["name"]
    running = game_running()
    if running and not while_running:
        log(f"{name}: build {pending['sha'][:7]} ready; waiting for the game to close before installing")
        return
    install(plugin, pending["dest"])
    sums = os.path.join(pending["dest"], "SHA256SUMS")
    if os.path.exists(sums) and solver_sha_from_sums(open(sums).read()) is not None:
        st["solver_sha256"] = solver_sha_from_sums(open(sums).read())
    note = sync_clone(plugin, pending["sha"])
    shutil.rmtree(pending["dest"], ignore_errors=True)
    st["synced_upstream"] = pending["upstream"]
    st["installed_sha"] = pending["sha"]
    st.pop("pending_install", None)
    st.pop("failed_upstream", None)
    st.pop("failed_api_level", None)
    st.pop("failed_branch", None)
    st.pop("note", None)
    set_attention(name, None)
    when = "while the game runs; it loads when the plugin is switched off and on" if running else "loads at the next launch"
    log(f"{name}: installed build {pending['sha'][:7]} (upstream {pending['upstream'][:7]}), {when}; {note}")
    notify(f"{name} updated", f"Build {pending['sha'][:7]} on upstream {pending['upstream'][:7]}; {when}. {note}.")


def main():
    state = load_state()
    for source, note in reassert_notes(state):
        set_attention(source, note)
    try:
        for name in snapshot_logs(state):
            log(f"dalamud log copied to log-archive/{name}")
    except Exception as ex:
        log(f"log snapshot: {type(ex).__name__}: {ex}")
    try:
        gone = prune_captures()
        if gone:
            log(f"pruned {len(gone)} old capture(s): {', '.join(gone[:6])}{'...' if len(gone) > 6 else ''}")
    except Exception as ex:
        log(f"capture pruning: {type(ex).__name__}: {ex}")
    save_state(state)
    now = datetime.datetime.now()
    version, key = game_version(), xivapi_key()
    plugin_versions = [(pol["name"], plugin_version(pol["manifest"])) for pol in POLICIES]
    seed(state, version, key, now, plugin_versions)
    try:
        note = gear_check(state, version, key)
        if note:
            log(f"gear plan: {note}")
            notify("Gear plan may be out of date", note[:200])
    except Exception as ex:
        log(f"gear check: {type(ex).__name__}: {ex}")
    try:
        note = gear_notes_check(state, version)
        if note:
            log(f"gear plan: {note}")
            notify("Gear plan may be out of date", note[:200])
    except Exception as ex:
        log(f"patch notes check: {type(ex).__name__}: {ex}")
    try:
        note = xivgear_sims_check(state)
        if note:
            log(f"gear plan: {note}")
            notify("Gear plan may be out of date", note[:200])
    except Exception as ex:
        log(f"xivgear sims check: {type(ex).__name__}: {ex}")
    try:
        if patch_regen_needed(state, version, key, game_running()):
            regenerate_after_patch(state, version, key)
    except Exception as ex:
        log(f"patch regeneration: {type(ex).__name__}: {ex}")
    try:
        if codex_refresh_due(state, now):
            refresh_codex(state, now)
    except Exception as ex:
        log(f"Codex refresh: {type(ex).__name__}: {ex}")
    for pol, (_, pv) in zip(POLICIES, plugin_versions):
        try:
            if policy_check_needed(state, pol["name"], pv):
                check_policy(state, pol, pv)
        except Exception as ex:
            log(f"{pol['name']} policy check: {type(ex).__name__}: {ex}")
    try:
        if check_solver(state):
            log(f"solver check: {state[SOLVER_SOURCE]['note']}")
    except Exception as ex:
        log(f"solver check: {type(ex).__name__}: {ex}")
    save_state(state)
    for plugin in PLUGINS:
        try:
            sync_one(plugin, state)
        except Exception as ex:
            log(f"{plugin['name']}: {type(ex).__name__}: {ex}")
        save_state(state)


def by_hand(action, name):
    plugin = next((p for p in PLUGINS if p["name"] == name), None)
    if plugin is None:
        sys.exit(f"unknown plugin {name}; known: {', '.join(p['name'] for p in PLUGINS)}")
    state = load_state()
    st = state.setdefault(name, {})
    if action == "--build":
        log(f"{name}: running the fork's workflow for {branch_head(plugin)[:7]}, a change made in the fork itself")
        build_and_queue(plugin, st, upstream_head(plugin))
    elif st.get("pending_install"):
        finish_install(plugin, st, st["pending_install"], while_running=True)
    else:
        print(f"{name}: no build is waiting")
    save_state(state)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in ("--build", "--install-now"):
        by_hand(sys.argv[1], sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "--gear-plan-updated":
        state = load_state()
        gear_plan_updated(state, xivapi_key(), game_version())
        save_state(state)
        print(f"{GEAR_SOURCE}: baseline of {len(state[GEAR_SOURCE].get('items', {}))} items taken, note cleared")
    else:
        main()
