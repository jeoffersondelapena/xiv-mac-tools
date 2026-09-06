#!/usr/bin/env python3
"""Keep the plugin forks current with their upstreams, using each fork's upstream-sync workflow.

Once an hour (launchd): if an upstream's default branch moved since the last sync, dispatch the fork's
workflow, wait for it, then download the build it published, check the hash list, install it into the
dev-plugin folder (only while no game window runs), and fast-forward the local clone. A failed run, a
dirty clone or a Dalamud API mismatch is reported and left alone.
"""
import datetime, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time

BASE = os.path.expanduser("~/Library/Application Support/XIV on Mac")
STATE = os.path.join(BASE, "wedge-watch", "upstream-sync-state.json")
ATTENTION = os.path.join(BASE, "pluginConfigs", "OverlayDoctor", "attention.txt")
LOG = os.path.join(BASE, "wedge-watch", "upstream-sync.log")
WORKFLOW = "upstream-sync.yml"
RUN_TIMEOUT = 25 * 60
WINE_CMD_RE = re.compile(r"^[A-Za-z]:\\.*?\.exe(?=\s|$)")

PLUGINS = [
    {"name": "IINACT", "fork": "jeoffersondelapena/IINACT", "upstream": "marzent/IINACT", "upstream_branch": "main",
     "clone": os.path.expanduser("~/Projects/iinact-fork"), "branch": "macos",
     "install_dir": os.path.expanduser("~/Projects/iinact-fork/IINACT/bin/Release/win-x64"), "manifest": "IINACT.json"},
    {"name": "Browsingway", "fork": "jeoffersondelapena/Browsingway", "upstream": "Styr1x/Browsingway", "upstream_branch": "main",
     "clone": os.path.expanduser("~/Projects/browsingway-fork"), "branch": "macos",
     "install_dir": os.path.expanduser("~/Projects/browsingway-fork/out"), "manifest": "Browsingway.json"},
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

def needs_sync(state, upstream_head, local_api_level=None):
    """A new upstream head means work. A head that already failed is retried only when the failure was a
    Dalamud API mismatch and this Mac's Dalamud has changed since."""
    if upstream_head == state.get("synced_upstream"):
        return False
    if upstream_head == state.get("failed_upstream"):
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
    if not needs_sync(st, head, local_api_level()):
        return
    st.pop("failed_api_level", None)
    log(f"{name}: upstream {plugin['upstream']} moved to {head[:7]}; running the fork's workflow")
    conclusion, run_id = dispatch_and_wait(plugin)
    if conclusion != "success":
        st["failed_upstream"] = head
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
        log(f"{name}: artifact hash mismatch on {bad}; not installed")
        notify(f"{name}: build rejected", "Downloaded files did not match their hash list.")
        set_attention(name, "a downloaded build failed its hash check and was not installed")
        return
    level, local = artifact_api_level(dest, plugin["manifest"]), local_api_level()
    if not api_level_compatible(level, local):
        st["failed_upstream"] = head
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
    set_attention(name, None)
    log(f"{name}: installed build {pending['sha'][:7]} (upstream {pending['upstream'][:7]}); {note}")
    notify(f"{name} updated", f"Rebased on upstream {pending['upstream'][:7]}; loads at the next launch. {note}.")


def main():
    state = load_state()
    for plugin in PLUGINS:
        try:
            sync_one(plugin, state)
        except Exception as ex:
            log(f"{plugin['name']}: {type(ex).__name__}: {ex}")
        save_state(state)


if __name__ == "__main__":
    main()
