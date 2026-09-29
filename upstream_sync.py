#!/usr/bin/env python3
"""Keep the plugin forks current with their upstreams, using each fork's upstream-sync workflow.

Once an hour (launchd): if an upstream's default branch moved since the last sync, dispatch the fork's
workflow, wait for it, then download the build it published, check the hash list, install it into the
dev-plugin folder (only while no game window runs), and fast-forward the local clone. A failed run, a
dirty clone or a Dalamud API mismatch is reported and left alone.

The same tick also copies Dalamud's log files aside while no game runs: Dalamud keeps one session back
and only its first 10 MB, so a relaunch before a log was read loses lines.

Two more chores ride on the tick: after a game patch, once the game-data source has caught up, the
GatherBuddy Reborn lists are regenerated and the settings policy re-applied (game closed); and once a
week Codex's wiki data is rebuilt and handed to the plugin. When GatherBuddy Reborn or Wrath Combo changes
version, its settings policy is checked again and re-applied with the game closed.
"""
import datetime, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request

BASE = os.path.expanduser("~/Library/Application Support/XIV on Mac")
STATE = os.path.join(BASE, "wedge-watch", "upstream-sync-state.json")
ATTENTION = os.path.join(BASE, "pluginConfigs", "OverlayDoctor", "attention.txt")
LOG = os.path.join(BASE, "wedge-watch", "upstream-sync.log")
WORKFLOW = "upstream-sync.yml"
LOG_DIR = os.path.join(BASE, "logs")
LOG_ARCHIVE = os.path.join(BASE, "wedge-watch", "log-archive")
LOG_ARCHIVE_KEEP = 30
LOG_SOURCES = ("dalamud.log", "dalamud.old.log")
GAME_VER = os.path.join(BASE, "ffxiv", "game", "ffxivgame.ver")
XIVAPI_PROBE = "https://v2.xivapi.com/api/sheet/Item?limit=1&fields=Name"   # its "version" is the key of the patch it serves
LISTS_TOOL = os.path.expanduser("~/.claude/skills/gbr-lists/gbr_lists.py")
SETTINGS_TOOL = os.path.expanduser("~/.claude/skills/gbr-settings/gbr_check.py")
CODEX_PIPELINE = os.path.expanduser("~/Projects/ffxiv-codex/data/pipeline/run_all.py")
CODEX_DATA = os.path.expanduser("~/Projects/ffxiv-codex/data/codex-data.json")
CODEX_LIVE = os.path.join(BASE, "pluginConfigs", "Codex", "codex-data.json")
CODEX_REFRESH_DAYS = 7
WRATH_TOOL = os.path.expanduser("~/.claude/skills/wrath-settings/wrath_check.py")
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


def attention_lines(existing, plugin, note):
    """The attention file, one line per plugin: replace this plugin's line, drop it when note is None."""
    kept = [l for l in existing.splitlines() if l.strip() and not l.startswith(plugin + ":")]
    if note:
        kept.append(f"{plugin}: {note}")
    return "".join(l + "\n" for l in kept)


def artifact_name(plugin, sha):
    return f"{plugin}-{sha}"


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
    """Copy the build over the dev folder; extra files there (the parser DLLs IINACT fetched) are kept."""
    os.makedirs(plugin["install_dir"], exist_ok=True)
    for root, _, files in os.walk(dest):
        for name in files:
            if name in ("SHA256SUMS", "COMMIT", "UPSTREAM"):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, dest)
            target = os.path.join(plugin["install_dir"], rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)


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

def set_attention(plugin, note):
    """Leave a note Overlay Doctor reads out at login; None clears this plugin's note."""
    try:
        existing = open(ATTENTION).read() if os.path.exists(ATTENTION) else ""
        text = attention_lines(existing, plugin, note)
        os.makedirs(os.path.dirname(ATTENTION), exist_ok=True)
        if text:
            with open(ATTENTION, "w") as f:
                f.write(text)
        elif os.path.exists(ATTENTION):
            os.remove(ATTENTION)
    except OSError as ex:
        log(f"attention file: {ex}")


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
            set_attention(name, f"the settings policy needs a look after the update to {version}")
    else:
        outcome = f"needs a hand ({result or err})"
        notify(f"{name} settings need a hand", f"After the update to {version}: {result or err}"[:200])
        set_attention(name, f"the settings policy needs a look after the update to {version}")
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
    conclusion, run_id = dispatch_and_wait(plugin)
    if conclusion != "success":
        st["failed_upstream"] = head
        st["failed_branch"] = branch_head(plugin)
        log(f"{name}: workflow run {run_id} ended with {conclusion}; the branch was left alone")
        notify(f"{name}: upstream sync needs a hand", f"The rebase or build failed (run {run_id}).")
        set_attention(name, f"upstream sync needs a hand (workflow run {run_id} {conclusion})")
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
        set_attention(name, "a downloaded build failed its hash check and was not installed")
        return
    level, local = artifact_api_level(dest, plugin["manifest"]), local_api_level()
    if not api_level_compatible(level, local):
        st["failed_upstream"] = head
        st["failed_branch"] = branch_head(plugin)
        st["failed_api_level"] = local
        log(f"{name}: build targets Dalamud API {level}, this Mac runs {local}; not installed")
        notify(f"{name}: build not installed", f"Built for Dalamud API {level}; this Mac runs {local}.")
        set_attention(name, f"an upstream build for Dalamud API {level} is waiting; this Mac runs API {local}")
        return
    st["pending_install"] = {"dest": dest, "upstream": head, "sha": open(os.path.join(dest, "COMMIT")).read().strip()}
    return finish_install(plugin, st, st["pending_install"])


def finish_install(plugin, st, pending):
    name = plugin["name"]
    if game_running():
        log(f"{name}: build {pending['sha'][:7]} ready; waiting for the game to close before installing")
        return
    install(plugin, pending["dest"])
    note = sync_clone(plugin, pending["sha"])
    shutil.rmtree(pending["dest"], ignore_errors=True)
    st["synced_upstream"] = pending["upstream"]
    st["installed_sha"] = pending["sha"]
    st.pop("pending_install", None)
    st.pop("failed_upstream", None)
    st.pop("failed_api_level", None)
    st.pop("failed_branch", None)
    set_attention(name, None)
    log(f"{name}: installed build {pending['sha'][:7]} (upstream {pending['upstream'][:7]}); {note}")
    notify(f"{name} updated", f"Rebased on upstream {pending['upstream'][:7]}; loads at the next launch. {note}.")


def main():
    state = load_state()
    try:
        for name in snapshot_logs(state):
            log(f"dalamud log copied to log-archive/{name}")
    except Exception as ex:
        log(f"log snapshot: {type(ex).__name__}: {ex}")
    save_state(state)
    now = datetime.datetime.now()
    version, key = game_version(), xivapi_key()
    plugin_versions = [(pol["name"], plugin_version(pol["manifest"])) for pol in POLICIES]
    seed(state, version, key, now, plugin_versions)
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
    save_state(state)
    for plugin in PLUGINS:
        try:
            sync_one(plugin, state)
        except Exception as ex:
            log(f"{plugin['name']}: {type(ex).__name__}: {ex}")
        save_state(state)


if __name__ == "__main__":
    main()
