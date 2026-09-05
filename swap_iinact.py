#!/usr/bin/env python3
"""Replace the repo-installed IINACT with the local fork as a Dalamud dev plugin. Game must be closed.

Everything it touches is backed up or parked, never deleted:
  installedPlugins/IINACT      -> parked-plugins/IINACT-repo-<stamp>
  pluginConfigs/IINACT.json    -> wedge-watch/backups/<stamp>/ (copy)
  pluginConfigs/IINACT/        -> wedge-watch/backups/<stamp>/ (copy)
  dalamudConfig.json           -> wedge-watch/backups/<stamp>/ (copy), then edited in place
"""
import copy, datetime, json, os, re, shutil, subprocess, sys, uuid

BASE = os.path.expanduser("~/Library/Application Support/XIV on Mac")
FORK_DLL = os.path.expanduser("~/Projects/iinact-fork/IINACT/bin/Release/win-x64/IINACT.dll")
WINE_DLL = "Z:" + FORK_DLL.replace("/", "\\")
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
BACKUP = os.path.join(BASE, "wedge-watch", "backups", STAMP)
WINE_CMD_RE = re.compile(r"^[A-Za-z]:\\.*?\.exe(?=\s|$)")


def game_running():
    out = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True).stdout
    return any((m := WINE_CMD_RE.match(l)) and m.group(0).endswith("\\ffxiv_dx11.exe") for l in out.splitlines())


def main(undo=None):
    if undo:
        return revert(undo)
    if game_running():
        sys.exit("close every game window first")
    if not os.path.exists(FORK_DLL):
        sys.exit(f"fork build missing: {FORK_DLL}")
    os.makedirs(BACKUP)
    for name in ("pluginConfigs/IINACT.json", "pluginConfigs/IINACT", "dalamudConfig.json"):
        src = os.path.join(BASE, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(BACKUP, os.path.basename(name)))
        elif os.path.exists(src):
            shutil.copy2(src, BACKUP)
    repo = os.path.join(BASE, "installedPlugins", "IINACT")
    parked = None
    if os.path.isdir(repo):
        parked = os.path.join(BASE, "parked-plugins", f"IINACT-repo-{STAMP}")
        os.makedirs(os.path.dirname(parked), exist_ok=True)
        shutil.move(repo, parked)
        print(f"repo IINACT parked at {parked}")
    seed_parser_dlls(parked)

    cfg_path = os.path.join(BASE, "dalamudConfig.json")
    cfg = json.load(open(cfg_path))
    locations = cfg["DevPluginLoadLocations"]["$values"]
    if not any(e["Path"] == WINE_DLL for e in locations):
        template = next(e for e in locations)
        entry = copy.deepcopy(template)
        entry.update({"Path": WINE_DLL, "IsEnabled": True, "Nickname": None})
        locations.append(entry)
        print("dev plugin location added")
    settings = cfg["DevPluginSettings"]
    if WINE_DLL not in settings:
        template_key = next(k for k in settings if not k.startswith("$"))
        s = copy.deepcopy(settings[template_key])
        # A rebuild must never reload the parser mid-fight on its own; reloads are explicit.
        s.update({"StartOnBoot": True, "NotifyForErrors": True, "AutomaticReloading": False,
                  "WorkingPluginId": str(uuid.uuid4())})
        if isinstance(s.get("DismissedValidationProblems"), dict):
            s["DismissedValidationProblems"]["$values"] = []
        settings[WINE_DLL] = s
        print("dev plugin settings added")
    # A plugin id Dalamud has never seen lands in the default profile as disabled; enable it up front.
    profile = cfg["DefaultProfile"]["Plugins"]["$values"]
    plugin_id = settings[WINE_DLL]["WorkingPluginId"]
    if not any(e.get("WorkingPluginId") == plugin_id for e in profile):
        template = next(e for e in profile if e.get("InternalName") == "Browsingway")
        profile.append({**template, "InternalName": "IINACT", "WorkingPluginId": plugin_id, "IsEnabled": True})
        print("profile entry added (enabled)")
    tmp = cfg_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, cfg_path)
    print(f"done; backups in {BACKUP}\nrevert with: python3 swap_iinact.py --undo {STAMP}")


def seed_parser_dlls(source_dir):
    """Copy the parser DLLs IINACT downloads at first load, so the fork's first start needs no download."""
    if source_dir is None:
        return
    versions = sorted(os.listdir(source_dir))
    src = os.path.join(source_dir, versions[-1]) if versions else source_dir
    out = os.path.dirname(FORK_DLL)
    copied = 0
    for name in os.listdir(src):
        if name.startswith("FFXIV_ACT_Plugin") and name.endswith(".dll") and not os.path.exists(os.path.join(out, name)):
            shutil.copy2(os.path.join(src, name), out)
            copied += 1
    print(f"parser DLLs seeded: {copied}")


def revert(stamp):
    if game_running():
        sys.exit("close every game window first")
    backup = os.path.join(BASE, "wedge-watch", "backups", stamp)
    shutil.copy2(os.path.join(backup, "dalamudConfig.json"), os.path.join(BASE, "dalamudConfig.json"))
    parked = os.path.join(BASE, "parked-plugins", f"IINACT-repo-{stamp}")
    repo = os.path.join(BASE, "installedPlugins", "IINACT")
    if os.path.isdir(parked) and not os.path.exists(repo):
        shutil.move(parked, repo)
    print(f"reverted to the state saved at {stamp}")


if __name__ == "__main__":
    main(undo=sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--undo" else None)
