#!/usr/bin/env python3
"""Register a locally built plugin with Dalamud as a dev plugin, enabled. Game must be closed.

usage: register_dev_plugin.py <path to the built .dll> <InternalName>
Backs up dalamudConfig.json to wedge-watch/backups/<stamp>/ first. Idempotent. An installed copy with the same
InternalName (a fork of a repo plugin) is switched off, since Dalamud loads only one of the two.
"""
import copy, datetime, json, os, re, shutil, subprocess, sys, uuid

BASE = os.path.expanduser("~/Library/Application Support/XIV on Mac")
WINE_CMD_RE = re.compile(r"^[A-Za-z]:\\.*?\.exe(?=\s|$)")


def game_running():
    out = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True).stdout
    return any((m := WINE_CMD_RE.match(l)) and m.group(0).endswith("\\ffxiv_dx11.exe") for l in out.splitlines())


def wine_path(path):
    return "Z:" + os.path.abspath(path).replace("/", "\\")


def register(dll, internal_name):
    if game_running():
        sys.exit("close every game window first")
    if not os.path.exists(dll):
        sys.exit(f"build missing: {dll}")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(BASE, "wedge-watch", "backups", stamp)
    os.makedirs(backup)
    cfg_path = os.path.join(BASE, "dalamudConfig.json")
    shutil.copy2(cfg_path, backup)
    cfg = json.load(open(cfg_path))
    target = wine_path(dll)

    locations = cfg["DevPluginLoadLocations"]["$values"]
    if not any(e["Path"] == target for e in locations):
        entry = copy.deepcopy(locations[0])
        entry.update({"Path": target, "IsEnabled": True, "Nickname": None})
        locations.append(entry)
        print("dev plugin location added")

    settings = cfg["DevPluginSettings"]
    if target not in settings:
        template_key = next(k for k in settings if not k.startswith("$"))
        s = copy.deepcopy(settings[template_key])
        # Rebuilds must never reload a plugin mid-fight on their own; reloads stay explicit.
        s.update({"StartOnBoot": True, "NotifyForErrors": True, "AutomaticReloading": False,
                  "WorkingPluginId": str(uuid.uuid4())})
        if isinstance(s.get("DismissedValidationProblems"), dict):
            s["DismissedValidationProblems"]["$values"] = []
        settings[target] = s
        print("dev plugin settings added")

    # A plugin id Dalamud has never seen lands in the default profile as disabled; enable it up front.
    profile = cfg["DefaultProfile"]["Plugins"]["$values"]
    plugin_id = settings[target]["WorkingPluginId"]
    if not any(e.get("WorkingPluginId") == plugin_id for e in profile):
        template = next(e for e in profile if e.get("InternalName") == "Browsingway")
        profile.append({**template, "InternalName": internal_name, "WorkingPluginId": plugin_id, "IsEnabled": True})
        print("profile entry added (enabled)")
    for e in profile:
        if e.get("InternalName") == internal_name and e.get("WorkingPluginId") != plugin_id and e.get("IsEnabled"):
            e["IsEnabled"] = False
            print(f"installed copy of {internal_name} switched off")

    # Dalamud writes this file with CRLF under Wine; keep whatever the file uses.
    newline = "\r\n" if b"\r\n" in open(cfg_path, "rb").read(4096) else "\n"
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", newline=newline) as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, cfg_path)
    print(f"registered {internal_name} from {target}; backup in {backup}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    register(sys.argv[1], sys.argv[2])
