#!/usr/bin/env python3
"""Tests for portwatch's port-arming policy. Run: python3 test_portwatch.py"""
import datetime, importlib.util, json, os, shutil, sys, tempfile, unittest

spec = importlib.util.spec_from_file_location("pw", os.path.join(os.path.dirname(os.path.abspath(__file__)), "portwatch.py"))
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)


class CreditUnowned(unittest.TestCase):
    def test_single_game_gets_the_wineserver_held_listener(self):
        # 10:20 2026-09-05: after `/xldisableplugintemp IINACT` + enable, lsof showed 10501 LISTEN only under wineserver.
        self.assertEqual(pw.credit_unowned({10501}, {46949}, {}), {10501: 46949})

    def test_two_games_stay_unattributed(self):
        self.assertEqual(pw.credit_unowned({10501}, {1, 2}, {}), {})

    def test_never_overrides_a_direct_attribution(self):
        self.assertEqual(pw.credit_unowned({10501}, {7}, {10501: 3}), {10501: 3})



class Orphans(unittest.TestCase):
    def _match(self, games, renderers):
        pw.procs = lambda exe: games if "ffxiv" in exe else (renderers if "Renderer" in exe else [])
        return [r[0] for r in pw.orphan_renderers()]

    def test_a_healthy_pair_is_left_alone(self):
        self.assertEqual(self._match([(1, 100, 0, "")], [(11, 105, 0, "")]), [])

    def test_a_renderer_whose_game_is_gone_is_flagged(self):
        self.assertEqual(self._match([(2, 200, 0, "")], [(11, 105, 0, ""), (12, 205, 0, "")]), [11])

    def test_a_game_whose_renderer_has_not_started_is_not_a_false_positive(self):
        self.assertEqual(self._match([(1, 100, 0, ""), (2, 200, 0, "")], [(11, 105, 0, "")]), [])

    def test_a_restarted_renderer_is_not_flagged(self):
        self.assertEqual(self._match([(1, 100, 0, "")], [(13, 300, 0, "")]), [])


class CrashHandlerOrphans(unittest.TestCase):
    def _match(self, games, handlers):
        pw.procs = lambda exe: games if "ffxiv" in exe else (handlers if "CrashHandler" in exe else [])
        return [h[0] for h in pw.orphans_of("DalamudCrashHandler.exe")]

    def test_a_live_game_keeps_its_handler(self):
        self.assertEqual(self._match([(1, 100, 0, "")], [(11, 103, 0, "")]), [])

    def test_a_handler_with_no_game_at_all_is_an_orphan(self):
        # Seen 2026-09-05 06:00: the handler outlived the game AND the wineserver after a freeze.
        self.assertEqual(self._match([], [(11, 103, 0, "")]), [11])

    def test_two_games_two_handlers_none_flagged(self):
        self.assertEqual(self._match([(1, 100, 0, ""), (2, 200, 0, "")], [(11, 103, 0, ""), (12, 203, 0, "")]), [])

    def test_the_dead_games_handler_is_flagged_but_the_live_ones_is_not(self):
        self.assertEqual(self._match([(2, 200, 0, "")], [(11, 103, 0, ""), (12, 203, 0, "")]), [11])


class WineserverMatch(unittest.TestCase):
    def test_the_real_server_line(self):
        self.assertTrue(pw.is_wineserver_line("/Applications/XIV on Mac.app/Contents/Resources/wine/lib/wine/../../bin/wineserver"))
        self.assertTrue(pw.is_wineserver_line("/opt/wine/bin/wineserver -p0"))

    def test_a_script_mentioning_the_name_is_not_the_server(self):
        # 2026-09-05 06:52: my own tool call's shell text matched, and the verdict stayed silent on a dead server.
        self.assertFalse(pw.is_wineserver_line('bash -c python3 - <<EOF any("bin/wineserver" in line for line in out) EOF'))
        self.assertFalse(pw.is_wineserver_line("grep bin/wineserver | grep -v grep"))


class ExtraServers(unittest.TestCase):
    def test_a_games_own_server_started_before_it_is_kept(self):
        self.assertEqual(pw.extra_servers([100.0], [(1, 98.0)]), [])

    def test_servers_started_after_the_newest_game_are_extra(self):
        # 09:05 2026-09-05: window 1 (08:44:07) on server 08:44:05; servers 08:53:45 and 09:05:56 were cascade leftovers.
        self.assertEqual(pw.extra_servers([100.0], [(1, 98.0), (2, 600.0), (3, 1300.0)]), [2, 3])

    def test_two_games_keep_their_shared_server(self):
        self.assertEqual(pw.extra_servers([100.0, 400.0], [(1, 98.0)]), [])

    def test_a_dead_windows_older_server_is_extra_while_the_live_windows_is_kept(self):
        # 09:30 2026-09-05: window 1 (server 09:17:32) force-quit; window 2 (09:22:53) on server 09:22:51; the old server lingered.
        self.assertEqual(pw.extra_servers([1373.0], [(40739, 1052.0), (42502, 1371.0)]), [40739])

    def test_no_game_means_every_server_is_extra(self):
        self.assertEqual(pw.extra_servers([], [(1, 98.0), (2, 600.0)]), [1, 2])


class SweepPlan(unittest.TestCase):
    def test_nothing_to_sweep_when_a_game_runs(self):
        self.assertIsNone(pw.sweep_plan(1, 1, 30))

    def test_nothing_to_sweep_when_the_prefix_is_empty(self):
        self.assertIsNone(pw.sweep_plan(0, 0, 0))

    def test_lingering_server_after_the_last_game_is_swept(self):
        # 08:08:45 server still alive 4 min after its game exited, with the old session's services attached.
        self.assertIn("1 wineserver", pw.sweep_plan(0, 1, 6))


class SocketVerdict(unittest.TestCase):
    def test_listening_server_is_fine(self):
        self.assertIsNone(pw.socket_verdict(1, True))

    def test_no_server_is_fine(self):
        self.assertIsNone(pw.socket_verdict(0, False))
        self.assertIsNone(pw.socket_verdict(0, None))

    def test_detector_is_disabled_until_the_method_is_validated(self):
        # 09:18 2026-09-05: it fired on a fresh prefix with one healthy server - the netstat check cannot see the socket.
        self.assertIsNone(pw.socket_verdict(1, False))
        self.assertIsNone(pw.socket_verdict(2, False))


class LaunchVerdict(unittest.TestCase):
    def test_clean_state_is_silent(self):
        self.assertIsNone(pw.launch_verdict(0, 0))
        self.assertIsNone(pw.launch_verdict(1, 1))
        self.assertIsNone(pw.launch_verdict(2, 1))

    def test_lingering_server_with_no_game_means_wait(self):
        self.assertIn("WAIT", pw.launch_verdict(0, 1))

    def test_two_servers_beside_a_game_is_flagged(self):
        # 08:11 2026-09-05: relaunch right after EXIT, old server still alive, native crash before first frame.
        self.assertIn("2 wineservers", pw.launch_verdict(1, 2))


class WineserverVerdict(unittest.TestCase):
    def test_silent_when_nothing_is_running(self):
        self.assertIsNone(pw.server_verdict(0, False))

    def test_silent_when_the_server_is_up(self):
        self.assertIsNone(pw.server_verdict(2, True))

    def test_names_the_dead_server_when_windows_are_up(self):
        # 2026-09-05 06:23: two windows, 64/65 threads each spinning in msync waits, no wineserver process.
        self.assertIn("WINESERVER GONE", pw.server_verdict(2, False))


class ProcessScan(unittest.TestCase):
    LINE = "{pid} Tue Sep  8 18:52:49 2026  54.2 {cmd}"

    def rows(self, *cmds):
        listing = "\n".join(self.LINE.format(pid=100 + i, cmd=c) for i, c in enumerate(cmds))
        return pw.parse_procs(listing, "ffxiv_dx11.exe")

    def test_both_launch_shapes_are_the_game(self):
        # With Dalamud the injector spawns the game by its Windows path; without it (patch day,
        # 2026-09-08) XIV on Mac starts it by its Unix path, spaces and all.
        rows = self.rows("C:\\Program Files\\game\\ffxiv_dx11.exe DEV.DataPathType=1",
                         "/Users/x/Library/Application Support/XIV on Mac/ffxiv/game/ffxiv_dx11.exe //**token")
        self.assertEqual([100, 101], [r[0] for r in rows])
        self.assertEqual(datetime.datetime(2026, 9, 8, 18, 52, 49).timestamp(), rows[0][1])
        self.assertEqual(54.2, rows[1][2])

    def test_a_shell_whose_arguments_name_the_game_is_not_the_game(self):
        # the watcher's own maintenance commands looked like a two-second game launch (2026-09-30 23:37)
        rows = self.rows("/bin/zsh -c cd /x && python3 -c print(pw.wine_exe('C:\\game\\ffxiv_dx11.exe DEV.TestSID=1'))",
                         "/bin/zsh -c python3 tool.py C:\\game\\ffxiv_dx11.exe DEV.TestSID=1",
                         "/usr/bin/python3 /x/tool.py /y/game/ffxiv_dx11.exe now")
        self.assertEqual([], rows)

    def test_a_native_program_is_told_by_its_first_word(self):
        self.assertIsNone(pw.wine_exe_path("/bin/sh /x/run.sh /y/ffxiv_dx11.exe now", native=lambda p: p == "/bin/sh"))
        self.assertEqual("/Users/x/Library/Application Support/XIV on Mac/ffxiv/game/ffxiv_dx11.exe",
                         pw.wine_exe_path("/Users/x/Library/Application Support/XIV on Mac/ffxiv/game/ffxiv_dx11.exe //**token", native=lambda p: False))
        self.assertEqual("C:\\Program Files\\game\\ffxiv_dx11.exe", pw.wine_exe_path("C:\\Program Files\\game\\ffxiv_dx11.exe DEV.DataPathType=1"))
        self.assertEqual("/x/out/Browsingway.Renderer.exe", pw.wine_exe_path("/x/out/Browsingway.Renderer.exe --type=gpu-process", native=lambda p: False))
        dashed = "/Volumes/Games/FINAL FANTASY XIV - A Realm Reborn/game/ffxiv_dx11.exe"
        self.assertEqual(dashed, pw.wine_exe_path(dashed + " DEV.TestSID=1", native=lambda p: False))

    def test_processes_that_merely_mention_the_game_are_not_it(self):
        rows = self.rows("C:\\d\\DalamudCrashHandler.exe --game C:\\game\\ffxiv_dx11.exe",
                         "/bin/sh -c 'sample $(pgrep ffxiv_dx11.exe)'",
                         "python3 /x/portwatch.py --watch ffxiv_dx11.exe")
        self.assertEqual([], rows)


class DalamudOffBoots(unittest.TestCase):
    def test_tracked_normally_when_dalamud_is_on(self):
        self.assertEqual(pw.initial_state(True), "pending")

    def test_not_tracked_when_dalamud_is_off(self):
        # 2026-09-05 07:44: two Dalamud-off diagnostic boots were logged as WEDGED / aborted.
        self.assertEqual(pw.initial_state(False), "untracked")
        self.assertEqual(pw.classify_live("untracked", None, 999), ("untracked", None))
        self.assertIsNone(pw.classify_gone("untracked", 999))

    def test_not_tracked_when_iinact_will_not_load(self):
        # 2026-09-05 20:21: the fork was registered but disabled in the profile; the boot was filed as wedged.
        self.assertEqual(pw.initial_state(True, iinact_on=False), "untracked")

    def test_not_tracked_when_dalamud_predates_the_game_patch(self):
        # 2026-09-08 18:49: patch 7.56 took the game to 2026.09.01 while every Dalamud track still
        # supported 2026.08.11, and XIV on Mac started the game bare.
        why = pw.dalamud_mismatch(("15.0.3.2", "2026.08.11.0000.0000"), "2026.09.01.0000.0000")
        self.assertEqual("Dalamud 15.0.3.2 supports game 2026.08.11.0000.0000, the game is 2026.09.01.0000.0000", why)
        self.assertEqual(pw.initial_state(True, iinact_on=True, mismatch=why), "untracked")
        self.assertIsNone(pw.dalamud_mismatch(("15.0.3.2", "2026.09.01.0000.0000"), "2026.09.01.0000.0000"))
        self.assertIsNone(pw.dalamud_mismatch(None, "2026.09.01.0000.0000"))
        self.assertIsNone(pw.dalamud_mismatch(("15.0.3.2", "2026.08.11.0000.0000"), None))

    def test_the_newest_installed_dalamud_is_the_one_compared(self):
        hooks = tempfile.mkdtemp()
        try:
            for name, game, mtime in (("15.0.3.2", "2026.08.11.0000.0000", 800), ("15.0.3.3", "2026.09.01.0000.0000", 900)):
                os.makedirs(os.path.join(hooks, name))
                path = os.path.join(hooks, name, "version.json")
                with open(path, "w") as f:
                    json.dump({"assemblyVersion": name, "supportedGameVer": game}, f)
                os.utime(path, (mtime, mtime))
            self.assertEqual(("15.0.3.3", "2026.09.01.0000.0000"), pw.dalamud_supported_game(hooks))
            self.assertIsNone(pw.dalamud_supported_game(os.path.join(hooks, "missing")))
        finally:
            shutil.rmtree(hooks)

    def cfg(self, enabled_location=True, enabled_profile=True, plugin_id="abc"):
        path = "Z:\\Users\\x\\Projects\\iinact-fork\\IINACT\\bin\\Release\\win-x64\\IINACT.dll"
        return {
            "DevPluginLoadLocations": {"$values": [{"Path": path, "IsEnabled": enabled_location}]},
            "DevPluginSettings": {path: {"WorkingPluginId": plugin_id}},
            "DefaultProfile": {"Plugins": {"$values": [
                {"InternalName": "IINACT", "WorkingPluginId": "old-repo-id", "IsEnabled": True},
                {"InternalName": "IINACT", "WorkingPluginId": plugin_id, "IsEnabled": enabled_profile},
            ]}},
        }

    def test_the_dev_fork_counts_only_when_its_own_profile_entry_is_enabled(self):
        self.assertTrue(pw.iinact_enabled_in(self.cfg(), repo_installed=False))
        self.assertFalse(pw.iinact_enabled_in(self.cfg(enabled_profile=False), repo_installed=False))
        self.assertFalse(pw.iinact_enabled_in(self.cfg(enabled_location=False), repo_installed=False))

    def test_a_repo_install_counts_regardless(self):
        self.assertTrue(pw.iinact_enabled_in(self.cfg(enabled_profile=False), repo_installed=True))


class BootOutcome(unittest.TestCase):
    def test_binding_is_a_clean_boot(self):
        state, msg = pw.classify_live("pending", 10501, 31)
        self.assertEqual(state, "ok")
        self.assertIn("CLEAN", msg)

    def test_no_port_well_past_a_normal_boot_is_wedged(self):
        self.assertEqual(pw.classify_live("pending", None, 151)[0], "wedged")
        self.assertEqual(pw.classify_live("pending", None, 149), ("pending", None))

    def test_a_late_bind_counts_as_recovery_not_a_wedge(self):
        self.assertEqual(pw.classify_live("wedged", 10501, 168)[0], "ok")

    def test_killing_a_hung_window_is_recorded_as_wedged(self):
        self.assertIn("WEDGED", pw.classify_gone("pending", 214))

    def test_closing_during_launch_is_not_called_a_wedge(self):
        self.assertIn("aborted", pw.classify_gone("pending", 12))

    def test_a_healthy_window_closing_is_silent(self):
        self.assertIsNone(pw.classify_gone("ok", 3600))
        self.assertIsNone(pw.classify_gone("prior", 3600))


class OverlaySync(unittest.TestCase):
    def test_nothing_to_do_with_a_single_slot(self):
        self.assertIsNone(pw.choose_sync_source([("/a", 100.0)]))

    def test_nothing_to_do_with_no_slots(self):
        self.assertIsNone(pw.choose_sync_source([]))

    def test_the_most_recently_written_profile_wins(self):
        self.assertEqual(pw.choose_sync_source([("/a", 100.0), ("/b", 200.0)]), "/b")
        self.assertEqual(pw.choose_sync_source([("/a", 300.0), ("/b", 200.0)]), "/a")

    def test_profiles_already_in_step_are_left_alone(self):
        # Copying every time would churn the store and its backup for no reason.
        self.assertIsNone(pw.choose_sync_source([("/a", 100.0), ("/b", 100.4)]))

    def test_three_slots_pick_the_newest(self):
        self.assertEqual(pw.choose_sync_source([("/a", 100.0), ("/b", 500.0), ("/c", 300.0)]), "/b")

    def test_one_window_edited_since_the_last_sync_syncs_normally(self):
        baseline = {"/a": 100.0, "/b": 100.0}
        self.assertEqual(pw.choose_sync_source([("/a", 100.0), ("/b", 200.0)], baseline), "/b")

    def test_both_windows_edited_still_takes_the_newest(self):
        # Matches every other shared config here: the last window to save wins.
        baseline = {"/a": 100.0, "/b": 100.0}
        self.assertEqual(pw.choose_sync_source([("/a", 300.0), ("/b", 200.0)], baseline), "/a")

    def test_both_edited_is_still_detected_so_it_can_be_logged(self):
        baseline = {"/a": 100.0, "/b": 100.0}
        self.assertEqual(len(pw.changed_since_sync([("/a", 300.0), ("/b", 200.0)], baseline)), 2)

    def test_a_missing_baseline_still_syncs_rather_than_stalling(self):
        self.assertEqual(pw.choose_sync_source([("/a", 100.0), ("/b", 200.0)], {}), "/b")

    def test_changed_since_sync_ignores_sub_second_jitter(self):
        baseline = {"/a": 100.0, "/b": 100.0}
        self.assertEqual(pw.changed_since_sync([("/a", 100.5), ("/b", 100.2)], baseline), [])


class SyncTrigger(unittest.TestCase):
    def test_fires_when_the_last_process_goes_away(self):
        self.assertTrue(pw.sync_due(True, False))

    def test_does_not_fire_while_a_process_is_still_shutting_down(self):
        # The bug: the port had closed but the process was still there, so the sync aborted.
        self.assertFalse(pw.sync_due(True, True))

    def test_does_not_fire_on_startup_with_nothing_running(self):
        self.assertFalse(pw.sync_due(None, False))
        self.assertFalse(pw.sync_due(False, False))




class SamplePlan(unittest.TestCase):
    def test_no_game_means_nothing_to_sample(self):
        self.assertEqual(pw.sample_plan([], datetime.datetime(2026, 9, 5, 10, 3, 7)), [])

    def test_one_file_per_window_named_by_time_and_pid(self):
        games = [(45777, 1.0, 0.5, "cmd"), (46949, 2.0, 0.5, "cmd")]
        plan = pw.sample_plan(games, datetime.datetime(2026, 9, 5, 10, 3, 7))
        self.assertEqual([pid for pid, _ in plan], [45777, 46949])
        self.assertTrue(all(out.endswith(f"stall-sample-100307-{pid}.txt") for pid, out in plan))
        self.assertEqual(len({out for _, out in plan}), 2)


class NetLogLines(unittest.TestCase):
    def test_chat_line_yields_its_code(self):
        self.assertEqual(pw.classify_netlog_line("00|2026-09-05T10:05:01.0+08:00|0029||You hit it.|abc"), ("00", 0x29))

    def test_system_chat_code_is_masked_to_the_log_kind(self):
        self.assertEqual(pw.classify_netlog_line("00|2026-09-05T10:05:01.0+08:00|0839||The duty has begun.|abc"), ("00", 0x39))

    def test_parser_lines_carry_no_code(self):
        self.assertEqual(pw.classify_netlog_line("21|2026-09-05T10:05:01.0+08:00|10001234|Name|..."), ("21", None))
        self.assertEqual(pw.classify_netlog_line("261|2026-09-05T10:05:01.0+08:00|Change|..."), ("261", None))

    def test_garbage_is_ignored(self):
        self.assertIsNone(pw.classify_netlog_line("not a log line"))
        self.assertIsNone(pw.classify_netlog_line(""))


class StallVerdict(unittest.TestCase):
    def test_combat_chat_without_parser_lines_is_a_stall(self):
        self.assertTrue(pw.stall_verdict(combat_chat=20, parser_lines=0))

    def test_combat_with_parser_lines_is_healthy(self):
        self.assertFalse(pw.stall_verdict(combat_chat=20, parser_lines=1))

    def test_idle_is_not_a_stall(self):
        self.assertFalse(pw.stall_verdict(combat_chat=0, parser_lines=0))
        self.assertFalse(pw.stall_verdict(combat_chat=pw.STALL_MIN_CHAT - 1, parser_lines=0))


class NetLogTailing(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "Network_30208_20260905.log")
        # The watch writes to the real portwatch.log through log(); keep test chatter out of it.
        self._log, pw.log = pw.log, lambda msg: None

    def tearDown(self):
        pw.log = self._log

    def test_existing_history_is_skipped_and_new_lines_are_returned_once(self):
        with open(self.path, "w") as f:
            f.write("00|old|0029||x|h\n" * 400)
        tail = pw.NetLogTail(self.dir)
        self.assertEqual(tail.read_new(), [])
        with open(self.path, "a") as f:
            f.write("21|t|a|b\n00|t|002B||y|h\npartial")
        self.assertEqual(tail.read_new(), ["21|t|a|b", "00|t|002B||y|h"])
        self.assertEqual(tail.read_new(), [])
        with open(self.path, "a") as f:
            f.write(" line\n")
        self.assertEqual(tail.read_new(), ["partial line"])

    def test_stall_watch_fires_once_and_notes_recovery(self):
        with open(self.path, "w") as f:
            f.write("")
        watch = pw.StallWatch(self.dir)
        fired = []
        watch.on_stall = lambda now, games, chat: fired.append((now, chat))
        with open(self.path, "a") as f:
            f.write("00|t|0029||hit|h\n" * 10)
        watch.tick(1000.0, games=[])
        watch.tick(1004.0, games=[])
        self.assertEqual(len(fired), 1)
        with open(self.path, "a") as f:
            f.write("21|t|a|b\n")
        watch.tick(1008.0, games=[])
        self.assertFalse(watch.stalled)


class WinePaths(unittest.TestCase):
    def setUp(self):
        self.prefix = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.prefix, "drive_c", "users", "u"))
        os.makedirs(os.path.join(self.prefix, "dosdevices"))
        os.symlink("/", os.path.join(self.prefix, "dosdevices", "z:"))

    def tearDown(self):
        shutil.rmtree(self.prefix)

    def test_c_is_the_prefix_and_z_is_the_mac_root(self):
        want = os.path.join(os.path.realpath(self.prefix), "drive_c", "users", "u", "Documents", "IINACT")
        self.assertEqual(want, pw.wine_to_mac_path("C:\\users\\u\\Documents\\IINACT", self.prefix))
        self.assertEqual(want, pw.wine_to_mac_path("c:/users/u/Documents/IINACT", self.prefix))
        self.assertEqual("/Users/u/Library/x", pw.wine_to_mac_path("Z:\\Users\\u\\Library\\x", self.prefix))

    def test_unknown_drives_and_spellings_resolve_to_nothing(self):
        self.assertIsNone(pw.wine_to_mac_path("Q:\\foo", self.prefix))
        self.assertIsNone(pw.wine_to_mac_path("not a path", self.prefix))
        self.assertIsNone(pw.wine_to_mac_path(None, self.prefix))

    def test_mac_paths_spell_back_through_the_right_drive(self):
        self.assertEqual("C:\\users\\u", pw.mac_to_wine_path(os.path.join(self.prefix, "drive_c", "users", "u"), self.prefix))
        self.assertEqual("Z:\\Users\\u\\Library\\x", pw.mac_to_wine_path("/Users/u/Library/x", self.prefix))


class NetlogLocation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = os.path.join(self.dir, "IINACT.json")
        self.home = os.path.join(self.dir, "home")
        os.makedirs(os.path.join(self.home, "Documents", "IINACT"))
        os.makedirs(os.path.join(self.home, "Library"))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_the_configured_folder_wins_and_the_default_covers_the_rest(self):
        with open(self.cfg, "w") as f:
            json.dump({"LogFilePath": "C:\\users\\u\\AppData\\IINACT", "WriteLogFile": True}, f)
        self.assertEqual("C:\\users\\u\\AppData\\IINACT", pw.netlog_setting(self.cfg))
        with open(self.cfg, "w") as f:
            json.dump({"WriteLogFile": True}, f)
        self.assertEqual(pw.DEFAULT_NETLOG_WIN, pw.netlog_setting(self.cfg))
        with open(self.cfg, "w") as f:
            f.write("{not json")
        self.assertEqual(pw.DEFAULT_NETLOG_WIN, pw.netlog_setting(self.cfg))
        self.assertEqual(pw.DEFAULT_NETLOG_WIN, pw.netlog_setting(os.path.join(self.dir, "missing.json")))

    def test_gated_folders_are_recognised_through_symlinks(self):
        self.assertTrue(pw.in_gated_folder(os.path.join(self.home, "Documents", "IINACT"), self.home))
        self.assertTrue(pw.in_gated_folder(os.path.join(self.home, "Downloads"), self.home))
        self.assertFalse(pw.in_gated_folder(os.path.join(self.home, "Library", "logs"), self.home))
        link = os.path.join(self.dir, "wine-documents")
        os.symlink(os.path.join(self.home, "Documents"), link)
        self.assertTrue(pw.in_gated_folder(os.path.join(link, "IINACT"), self.home))


class NetlogRelocation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home = os.path.join(self.dir, "home")
        self.docs = os.path.join(self.home, "Documents", "IINACT")
        os.makedirs(self.docs)
        self.cfg = os.path.join(self.dir, "IINACT.json")
        with open(self.cfg, "w") as f:
            json.dump({"LogFilePath": "C:\\users\\u\\Documents\\IINACT", "WriteLogFile": True}, f)
        self.new = os.path.join(self.home, "Library", "Application Support", "XIV on Mac", "iinact-logs")
        self.game = [(1, 0.0, 0.0, "C:\\game\\ffxiv_dx11.exe")]
        self.said = []
        self._log, pw.log = pw.log, self.said.append

    def tearDown(self):
        pw.log = self._log
        os.chmod(self.docs, 0o700)
        shutil.rmtree(self.dir)

    def setting(self):
        with open(self.cfg) as f:
            return json.load(f)["LogFilePath"]

    def test_nothing_moves_while_a_game_runs(self):
        self.assertFalse(pw.relocate_netlog(self.cfg, self.new, self.game))
        self.assertEqual("C:\\users\\u\\Documents\\IINACT", self.setting())
        self.assertFalse(os.path.isdir(self.new))

    def test_the_move_creates_the_folder_and_keeps_the_rest_of_the_config(self):
        self.assertTrue(pw.relocate_netlog(self.cfg, self.new, []))
        with open(self.cfg) as f:
            cfg = json.load(f)
        self.assertEqual(pw.mac_to_wine_path(self.new), cfg["LogFilePath"])
        self.assertTrue(cfg["LogFilePath"].startswith("Z:\\"))
        self.assertTrue(cfg["WriteLogFile"])
        self.assertTrue(os.path.isdir(self.new))

    def test_a_missing_config_is_reported_not_raised(self):
        self.assertFalse(pw.relocate_netlog(os.path.join(self.dir, "none.json"), self.new, []))
        self.assertTrue(any("could not move" in s for s in self.said))

    def test_a_refused_listing_is_told_apart_from_an_empty_folder(self):
        tail = pw.NetLogTail(self.docs)
        self.assertIsNone(tail.newest())
        self.assertFalse(tail.denied)
        os.chmod(self.docs, 0)
        self.assertIsNone(tail.newest())
        self.assertTrue(tail.denied)
        os.chmod(self.docs, 0o700)
        tail.newest()
        self.assertFalse(tail.denied)

    def test_the_watch_warns_once_then_moves_the_log_when_the_game_is_gone(self):
        os.chmod(self.docs, 0)
        watch = pw.StallWatch(self.docs, config_path=self.cfg, new_home=self.new, home=self.home)
        watch.tick(1000.0, self.game)
        watch.tick(1004.0, self.game)
        self.assertEqual(1, sum("unreadable" in s for s in self.said))
        self.assertIn("Files and Folders", self.said[0])
        self.assertEqual("C:\\users\\u\\Documents\\IINACT", self.setting())
        watch.tick(1008.0, [])
        self.assertEqual(self.new, watch.tail.directory)
        self.assertEqual(pw.mac_to_wine_path(self.new), self.setting())
        self.assertTrue(any("moved to" in s for s in self.said))
        watch.tick(1012.0, [])
        self.assertEqual(1, sum("unreadable" in s for s in self.said))


class HangDetection(unittest.TestCase):
    def test_the_diag_name_carries_the_start_time(self):
        self.assertEqual(datetime.datetime(2026, 9, 6, 0, 25, 25).timestamp(), pw.diag_start_time("iinact-20260906-002525-364.log"))
        self.assertEqual(datetime.datetime(2026, 9, 6, 0, 25, 25).timestamp(), pw.diag_start_time("doctor-20260906-002525-364.log"))
        self.assertIsNone(pw.diag_start_time("notes-20260906-002525-364.log"))

    def test_a_diag_file_is_matched_to_the_game_that_started_with_it(self):
        starts = {26844: 1000.0, 28015: 1240.0}
        self.assertEqual(26844, pw.match_game(1005.0, starts))
        self.assertEqual(28015, pw.match_game(1230.0, starts))
        self.assertIsNone(pw.match_game(1120.0, starts))

    def test_silence_after_heartbeats_is_a_hang(self):
        self.assertTrue(pw.hang_verdict(file_age=200, has_heartbeat=True, last_line="[00:32:01.952] heartbeat: ..."))

    def test_recent_writes_or_no_heartbeat_yet_are_not(self):
        self.assertFalse(pw.hang_verdict(file_age=40, has_heartbeat=True, last_line="heartbeat"))
        self.assertFalse(pw.hang_verdict(file_age=500, has_heartbeat=False, last_line="[00:25:41] unscrambler: ..."))

    def test_the_threshold_follows_the_plugins_own_beat_spacing(self):
        five = "[14:00:00.001] heartbeat: a\n[14:00:05.002] heartbeat: b\n"
        minute = "[14:00:00.001] heartbeat: a\n[14:01:00.002] heartbeat: b\n"
        self.assertEqual(20, pw.beat_threshold(five))
        self.assertEqual(150, pw.beat_threshold(minute))
        self.assertEqual(150, pw.beat_threshold("[14:00:00.001] heartbeat: only one\n"))
        self.assertEqual(20, pw.beat_threshold("[23:59:58.000] heartbeat: a\n[00:00:03.000] heartbeat: b\n"))

    def test_a_fast_beat_makes_a_short_silence_a_hang(self):
        self.assertTrue(pw.hang_verdict(file_age=25, has_heartbeat=True, last_line="[14:00:05.002] heartbeat: b", threshold=20))
        self.assertFalse(pw.hang_verdict(file_age=12, has_heartbeat=True, last_line="[14:00:05.002] heartbeat: b", threshold=20))

    def test_the_timer_threads_stall_lines_count_even_though_they_keep_the_file_fresh(self):
        self.assertFalse(pw.hang_verdict(file_age=1, has_heartbeat=True, last_line="[14:00:17.0] frame loop stalled 17s; gc 1/1/1", threshold=20))
        self.assertTrue(pw.hang_verdict(file_age=1, has_heartbeat=True, last_line="[14:00:26.0] frame loop stalled 26s; gc 1/1/1", threshold=20))
        self.assertFalse(pw.hang_verdict(file_age=1, has_heartbeat=True, last_line="[14:00:40.0] frame loop resumed", threshold=20))

    def test_stall_reports_that_stop_mean_the_whole_process_is_held(self):
        last = "[10:53:38.601] frame loop stalled 6s; gc 411/117/14, pool busy 6, queued 0"
        self.assertFalse(pw.hang_verdict(file_age=4, has_heartbeat=False, last_line=last, threshold=150))
        self.assertFalse(pw.hang_verdict(file_age=19, has_heartbeat=False, last_line=last, threshold=150))
        self.assertTrue(pw.hang_verdict(file_age=20, has_heartbeat=False, last_line=last, threshold=150))
        self.assertFalse(pw.hang_verdict(file_age=500, has_heartbeat=False, last_line="[10:53:32.635] XIV Doctor 0.1.0.0 loaded, pid 492", threshold=150))

    def test_a_launch_frozen_during_plugin_loading_is_reported_with_the_whole_stall(self):
        import tempfile, time
        d = tempfile.mkdtemp()
        started = time.time() - 60
        path = os.path.join(d, "doctor-%s-492.log" % datetime.datetime.fromtimestamp(started).strftime("%Y%m%d-%H%M%S"))
        with open(path, "w") as f:
            f.write("[10:53:32.635] XIV Doctor 0.1.0.0 loaded, pid 492\n[10:53:34.825] frame loop stalled 2s; gc 267/58/13, pool busy 28, queued 0\n"
                    "[10:53:38.601] frame loop stalled 6s; gc 411/117/14, pool busy 6, queued 0\n")
        now = time.time()
        os.utime(path, (now - 30, now - 30))
        watch = pw.HangWatch(d)
        hangs = []
        watch.on_hang = lambda pid, age, name: hangs.append((pid, round(age)))
        watch.tick(now, [(9930, started, 99.0, "cmd")])
        watch.tick(now + 4, [(9930, started, 99.0, "cmd")])
        self.assertEqual([(9930, 36)], hangs)

    def test_a_stall_that_ends_on_its_own_takes_the_report_back(self):
        import tempfile, time
        d = tempfile.mkdtemp()
        started = time.time() - 600
        path = os.path.join(d, "doctor-%s-364.log" % datetime.datetime.fromtimestamp(started).strftime("%Y%m%d-%H%M%S"))
        with open(path, "w") as f:
            f.write("[14:00:00.0] heartbeat: a\n[14:00:05.0] heartbeat: b\n[14:00:31.0] frame loop stalled 26s; gc 1/1/1\n")
        watch = pw.HangWatch(d)
        hangs, recovered = [], []
        watch.on_hang = lambda pid, age, name: hangs.append(pid)
        watch.on_recover = lambda pid, name: recovered.append(pid)
        games = [(26844, started, 99.0, "cmd")]
        watch.tick(time.time(), games)
        with open(path, "a") as f:
            f.write("[14:00:12.0] frame loop resumed\n[14:00:15.0] heartbeat: c\n")
        watch.tick(time.time(), games)
        self.assertEqual(([26844], [26844]), (hangs, recovered))
        self.assertEqual(set(), watch.reported)

    def test_a_plugin_switched_off_on_purpose_is_not_a_hang(self):
        self.assertFalse(pw.hang_verdict(file_age=500, has_heartbeat=True, last_line="[23:41:56.973] unloading"))
        self.assertFalse(pw.hang_verdict(file_age=500, has_heartbeat=True, last_line="[23:41:56.973] plugin unloading"))

    def test_the_watch_reports_a_frozen_window_once_and_leaves_the_healthy_one_alone(self):
        import tempfile, time
        d = tempfile.mkdtemp()
        started = time.time() - 600
        frozen = os.path.join(d, "doctor-%s-364.log" % datetime.datetime.fromtimestamp(started).strftime("%Y%m%d-%H%M%S"))
        with open(frozen, "w") as f:
            f.write("[x] XIV Doctor 0.1.0.0 loaded, pid 364\n[x] heartbeat: logged in True; territory 130\n")
        os.utime(frozen, (started + 60, started + 60))
        healthy = os.path.join(d, "doctor-%s-2632.log" % datetime.datetime.fromtimestamp(started + 100).strftime("%Y%m%d-%H%M%S"))
        with open(healthy, "w") as f:
            f.write("[x] heartbeat: logged in True; territory 130\n")
        watch = pw.HangWatch(d)
        hangs = []
        watch.on_hang = lambda pid, age, name: hangs.append(pid)
        games = [(26844, started, 99.0, "cmd"), (28015, started + 100, 40.0, "cmd")]
        watch.tick(time.time(), games)
        watch.tick(time.time(), games)
        self.assertEqual([26844], hangs)


class WaitingBuild(unittest.TestCase):
    def test_only_plugins_with_a_build_waiting_are_named(self):
        text = '{"IINACT": {"installed_sha": "a"}, "Browsingway": {"pending_install": {"sha": "b"}}, "seeded": true}'
        self.assertEqual(["Browsingway"], pw.pending_builds(text))
        self.assertEqual([], pw.pending_builds("not json"))
        self.assertEqual([], pw.pending_builds("[1, 2]"))

    def test_the_sync_runs_once_the_game_has_been_gone_a_few_seconds(self):
        self.assertFalse(pw.nudge_due(1, 60, ["Browsingway"], None))
        self.assertFalse(pw.nudge_due(0, 3, ["Browsingway"], None))
        self.assertFalse(pw.nudge_due(0, 60, [], None))
        self.assertTrue(pw.nudge_due(0, 8, ["Browsingway"], None))

    def test_a_build_that_still_waits_is_not_retried_every_tick(self):
        self.assertFalse(pw.nudge_due(0, 60, ["Browsingway"], 30))
        self.assertTrue(pw.nudge_due(0, 700, ["Browsingway"], 600))

    def test_the_watch_nudges_once_and_never_while_a_game_runs(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "state.json")
        with open(path, "w") as f:
            f.write('{"Browsingway": {"pending_install": {"sha": "b"}}}')
        nudge = pw.InstallNudge(path)
        ran = []
        nudge.run = lambda pending: ran.append(pending)
        game = [(26630, 0.0, 50.0, "cmd")]
        nudge.tick(1000.0, game)
        nudge.tick(1004.0, [])
        nudge.tick(1008.0, [])
        self.assertEqual([], ran)
        nudge.tick(1012.0, [])
        nudge.tick(1016.0, [])
        self.assertEqual([["Browsingway"]], ran)
        nudge.tick(1020.0, game)
        nudge.tick(1024.0, [])
        nudge.tick(1040.0, [])
        self.assertEqual([["Browsingway"]], ran)


class StaleServer(unittest.TestCase):
    def test_a_running_game_is_never_touched(self):
        self.assertFalse(pw.stale_server_verdict(1, 500, True, [900], 400))

    def test_a_normal_exit_gets_time_to_take_its_own_server_down(self):
        self.assertFalse(pw.stale_server_verdict(0, 25, False, [900], 400))
        self.assertTrue(pw.stale_server_verdict(0, 31, False, [900], 400))

    def test_a_force_quit_is_swept_sooner(self):
        self.assertFalse(pw.stale_server_verdict(0, 3, True, [900], 400))
        self.assertTrue(pw.stale_server_verdict(0, 5, True, [900], 400))

    def test_a_launch_in_progress_is_left_alone(self):
        self.assertFalse(pw.stale_server_verdict(0, 60, True, [900], 5))
        self.assertFalse(pw.stale_server_verdict(0, 60, True, [20], 20))

    def test_nothing_to_sweep_without_a_server(self):
        self.assertFalse(pw.stale_server_verdict(0, 60, True, [], None))

    def test_a_log_cut_off_mid_session_means_the_game_was_killed(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "doctor-20260930-140841-492.log")
        with open(path, "w") as f:
            f.write("[14:46:12.0] heartbeat: logged in True; territory 974\n")
        self.assertTrue(pw.last_exit_was_kill(d))
        with open(path, "a") as f:
            f.write("[14:47:00.0] unloading\n")
        self.assertFalse(pw.last_exit_was_kill(d))
        self.assertFalse(pw.last_exit_was_kill(tempfile.mkdtemp()))

    def test_only_the_real_server_counts_not_the_launchers_waiter(self):
        real = "/Applications/XIV on Mac.app/Contents/Resources/wine/lib/wine/../../bin/wineserver"
        self.assertTrue(pw.REAL_SERVER_RE.search(real))
        self.assertFalse(pw.REAL_SERVER_RE.search("/Applications/XIV on Mac.app/Contents/Resources/wine/bin/wineserver -w"))


class StuckClosing(unittest.TestCase):
    CLOSING = "[22:44:32.376] unloading; game closing"

    def test_a_game_still_there_two_minutes_after_it_began_to_close_is_stuck(self):
        self.assertTrue(pw.exit_stuck_verdict(self.CLOSING, 120))
        self.assertTrue(pw.exit_stuck_verdict(self.CLOSING, 900))

    def test_a_normal_close_gets_its_time(self):
        self.assertFalse(pw.exit_stuck_verdict(self.CLOSING, 22))
        self.assertFalse(pw.exit_stuck_verdict(self.CLOSING, 119))

    def test_a_plugin_switched_off_while_the_game_runs_is_never_a_stuck_close(self):
        self.assertFalse(pw.exit_stuck_verdict("[23:41:56.973] unloading", 5000))
        self.assertFalse(pw.exit_stuck_verdict("[23:41:56.973] heartbeat: logged in True; territory 131", 5000))
        self.assertFalse(pw.exit_stuck_verdict("", 5000))

    def diag(self, d, started, last_line, age, pid_suffix=492):
        import time
        path = os.path.join(d, "doctor-%s-%d.log" % (datetime.datetime.fromtimestamp(started).strftime("%Y%m%d-%H%M%S"), pid_suffix))
        with open(path, "w") as f:
            f.write("[x] heartbeat: logged in True; territory 131\n" + last_line + "\n")
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def watch(self, d):
        watch = pw.ExitWatch(d)
        stuck, clean = [], []
        watch.on_stuck = lambda pid, age, path: stuck.append(pid)
        watch.on_clean_exit = lambda pid: clean.append(pid)
        return watch, stuck, clean

    def test_the_watch_ends_a_stuck_close_once_and_does_not_call_it_clean(self):
        import tempfile, time
        d = tempfile.mkdtemp()
        started = time.time() - 9000
        self.diag(d, started, self.CLOSING, age=130)
        watch, stuck, clean = self.watch(d)
        games = [(26630, started, 205.0, "cmd")]
        watch.tick(time.time(), games)
        watch.tick(time.time(), games)
        watch.tick(time.time(), [])
        self.assertEqual(([26630], []), (stuck, clean))
        self.assertEqual((set(), set()), (watch.closing, watch.ended))

    def test_a_close_that_finishes_by_itself_is_clean(self):
        import tempfile, time
        d = tempfile.mkdtemp()
        started = time.time() - 9000
        self.diag(d, started, self.CLOSING, age=6)
        watch, stuck, clean = self.watch(d)
        watch.tick(time.time(), [(26630, started, 40.0, "cmd")])
        self.assertEqual(([], []), (stuck, clean))
        watch.tick(time.time(), [])
        watch.tick(time.time(), [])
        self.assertEqual(([], [26630]), (stuck, clean))

    def test_only_the_closing_window_is_touched(self):
        import tempfile, time
        d = tempfile.mkdtemp()
        first, second = time.time() - 9000, time.time() - 5000
        self.diag(d, first, self.CLOSING, age=500)
        self.diag(d, second, "[22:50:00.000] unloading", age=500, pid_suffix=3012)
        watch, stuck, clean = self.watch(d)
        watch.tick(time.time(), [(26630, first, 205.0, "cmd"), (41798, second, 30.0, "cmd")])
        self.assertEqual([26630], stuck)

    def test_the_report_names_where_the_shutdown_stopped(self):
        dalamud = ("2026-09-30 22:44:20.957 +08:00 [INF] Framework::Destroy!\n"
                   "2026-09-30 22:44:39.381 +08:00 [INF] [LocalPlugin] Finished unloading Glamourer\n"
                   "2026-09-30 22:44:39.381 +08:00 [DBG] [Glamourer] Disposed all services.\n"
                   "2026-09-30 22:44:39.381 +08:00 [INF] [LocalPlugin] Unloading vnavmesh\n")
        text = pw.exit_report(26630, 121, "memory pressure normal, swap 2.2 of 3.0 GB used", "[x] heartbeat: a\n" + self.CLOSING + "\n", dalamud)
        self.assertIn("pid 26630 was still running 121 s after the game began to close", text)
        self.assertTrue(text.rstrip().endswith("[LocalPlugin] Unloading vnavmesh"))
        self.assertIn("unloading; game closing", text)
        self.assertNotIn("Disposed all services", text)

    def test_the_report_says_when_dalamud_had_finished_and_names_the_sample(self):
        dalamud = ("2026-10-01 12:58:36.392 +08:00 [INF] [LocalPlugin] Finished unloading WrathCombo\n"
                   "2026-10-01 12:58:38.564 +08:00 [DBG] [ServiceManager] Service<Dalamud>: Unset\n"
                   "2026-10-01 12:58:38.567 +08:00 [INF] Session has ended.\n")
        text = pw.exit_report(10993, 120, "memory pressure normal", self.CLOSING + "\n", dalamud, "/x/exit-sample-130032-10993.txt")
        self.assertIn("Dalamud finished shutting down (2026-10-01 12:58:38.567); the game process did not exit after that.", text)
        self.assertIn("thread sample of the stuck process: exit-sample-130032-10993.txt", text)
        self.assertTrue(text.rstrip().endswith("Session has ended."))
        older = pw.exit_report(26630, 121, "memory pressure normal", self.CLOSING + "\n", "2026-09-30 22:44:39.381 +08:00 [INF] [LocalPlugin] Unloading vnavmesh\n")
        self.assertNotIn("Dalamud finished", older)
        self.assertNotIn("thread sample", older)

    def test_a_stuck_close_is_ended_reported_and_noted(self):
        import subprocess, tempfile, time
        base = tempfile.mkdtemp()
        os.makedirs(os.path.join(base, "wedge-watch"))
        d = os.path.join(base, "diag")
        os.makedirs(d)
        pid = int(subprocess.run(["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"], capture_output=True, text=True).stdout)
        started = time.time() - 9000
        self.diag(d, started, self.CLOSING, age=125)
        dalamud = os.path.join(base, "dalamud.log")
        with open(dalamud, "w") as f:
            f.write("2026-09-30 22:44:39.381 +08:00 [INF] [LocalPlugin] Unloading vnavmesh\n")
        notes, notices, lines = [], [], []
        saved = (pw.BASE, pw.DALAMUD_LOG, pw.set_attention, pw.notify, pw.boot_note, pw.memory_facts)
        pw.BASE, pw.DALAMUD_LOG = base, dalamud
        pw.set_attention = lambda source, note: notes.append((source, note))
        pw.notify = lambda title, text: notices.append(title)
        pw.boot_note = lines.append
        pw.memory_facts = lambda: (2, "memory pressure warning")
        try:
            pw.ExitWatch(d).tick(time.time(), [(pid, started, 205.0, "cmd")])
        finally:
            pw.BASE, pw.DALAMUD_LOG, pw.set_attention, pw.notify, pw.boot_note, pw.memory_facts = saved
        self.assertFalse(pw.process_alive(pid))
        reports = [n for n in os.listdir(os.path.join(base, "wedge-watch")) if n.startswith("exit-stuck-")]
        self.assertEqual(1, len(reports))
        with open(os.path.join(base, "wedge-watch", reports[0])) as f:
            self.assertIn("Unloading vnavmesh", f.read())
        self.assertEqual([], [n for n in os.listdir(os.path.join(base, "wedge-watch")) if n.startswith("exit-sample-")])
        self.assertEqual(["Game stuck closing"], notices)
        self.assertEqual([("GameExit", None)], notes)
        self.assertIn("STUCK CLOSING", lines[0])
        self.assertIn("ended", lines[0])

    def test_only_a_stuck_window_that_could_not_be_ended_leaves_a_note(self):
        self.assertIsNone(pw.exit_note(125.0, True, "/tmp/exit-stuck-101500-7.txt"))
        self.assertRegex(pw.exit_note(125.0, False, "/tmp/exit-stuck-101500-7.txt"),
                         r"^a game window was stuck closing for 125 s and could not be ended at \d\d:\d\d; capture exit-stuck-101500-7\.txt$")

    def test_a_process_is_ended_and_then_no_longer_alive(self):
        import subprocess
        pid = int(subprocess.run(["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"], capture_output=True, text=True).stdout)
        self.assertTrue(pw.process_alive(pid))
        self.assertTrue(pw.end_process(pid, wait=2))
        self.assertFalse(pw.process_alive(pid))

    def test_a_process_that_ignores_the_polite_signal_is_killed(self):
        import subprocess
        pid = int(subprocess.run(["sh", "-c", "(trap '' TERM; sleep 30) >/dev/null 2>&1 & echo $!"], capture_output=True, text=True).stdout)
        self.assertTrue(pw.end_process(pid, wait=1))
        self.assertFalse(pw.process_alive(pid))

    def test_an_exited_child_nobody_collected_does_not_count_as_alive(self):
        import subprocess, time
        child = subprocess.Popen(["true"])
        time.sleep(0.3)
        self.assertFalse(pw.process_alive(child.pid))
        child.wait()


class MemoryFacts(unittest.TestCase):
    SWAP = "total = 3072.00M  used = 2263.31M  free = 808.69M  (encrypted)"

    def test_pressure_and_swap_read_as_one_line(self):
        self.assertEqual((1, "memory pressure normal, swap 2.2 of 3.0 GB used"), pw.describe_memory("1\n", self.SWAP))
        self.assertEqual((4, "memory pressure critical, swap 2.2 of 3.0 GB used"), pw.describe_memory("4", self.SWAP))

    def test_missing_values_do_not_break_the_report(self):
        self.assertEqual((None, "memory pressure unknown"), pw.describe_memory("", ""))
        self.assertEqual((2, "memory pressure warning"), pw.describe_memory("2", None))

    def test_no_thread_sample_is_taken_while_the_machine_is_short_of_memory(self):
        self.assertTrue(pw.sample_worthwhile(1))
        self.assertTrue(pw.sample_worthwhile(None))
        self.assertFalse(pw.sample_worthwhile(2))
        self.assertFalse(pw.sample_worthwhile(4))


class MemoryRecord(unittest.TestCase):
    MB = 1048576

    def report(self):
        def part(dirty, swapped=0):
            return {"dirty": dirty * self.MB, "swapped": swapped * self.MB, "clean": 0, "reclaimable": 0, "wired": 0, "regions": 1}
        return {"bytes per unit": 1, "processes": [
            {"pid": 26630, "name": "ffxiv_dx11.exe", "footprint": 11700 * self.MB,
             "categories": {"untagged (VM_ALLOCATE)": part(6100, 2500), "IOAccelerator (graphics)": part(5200, 100), "MALLOC_LARGE": part(400), "__TEXT": part(0)}},
            {"pid": 27559, "name": "Browsingway.Renderer.exe", "footprint": 310 * self.MB, "categories": {}},
            {"pid": 27565, "name": "Browsingway.Renderer.exe", "footprint": 290 * self.MB, "categories": {}},
            {"pid": 26575, "name": "services.exe", "footprint": 12 * self.MB, "categories": {}},
            {"pid": 999, "name": "Finder", "footprint": 400 * self.MB, "categories": {}}]}

    EXES = {26630: "ffxiv_dx11.exe", 27559: "Browsingway.Renderer.exe", 27565: "Browsingway.Renderer.exe", 26575: "services.exe"}

    def test_the_executable_name_is_read_off_a_wine_command_line(self):
        self.assertEqual("ffxiv_dx11.exe", pw.wine_exe("C:\\Program Files (x86)\\SquareEnix\\game\\ffxiv_dx11.exe DEV.TestSID=abc"))
        self.assertEqual("Browsingway.Renderer.exe", pw.wine_exe("/Users/x/Projects/browsingway-fork/out/renderer/Browsingway.Renderer.exe --type=gpu-process"))
        self.assertIsNone(pw.wine_exe("/usr/bin/python3 portwatch.py --watch"))

    def test_a_window_is_split_into_its_largest_parts_and_the_rest_summed_per_executable(self):
        windows, helpers = pw.summarise_footprint(self.report(), self.EXES)
        self.assertEqual(1, len(windows))
        w = windows[0]
        self.assertEqual((26630, 11700, 2600), (w["pid"], w["footprint"], w["swapped"]))
        self.assertEqual(["untagged (VM_ALLOCATE)", "IOAccelerator (graphics)", "MALLOC_LARGE"], list(w["parts"]))
        self.assertEqual(6100, w["parts"]["untagged (VM_ALLOCATE)"])
        self.assertEqual({"Browsingway.Renderer.exe": 600, "services.exe": 12}, helpers)

    def test_the_plugins_own_memory_line_is_read(self):
        tail = ("[21:58:07.001] heartbeat: a\n[21:58:07.002] memory: managed 700 MB, committed 900 MB, resident 9000 MB; players 3; territory 129\n"
                "[21:59:07.004] memory: managed 812 MB, committed 1490 MB, resident 9800 MB; players 23; territory 131; longest frame 412 ms at 21:58:40.3\n")
        self.assertEqual({"managed": 812, "committed": 1490, "resident": 9800, "players": 23, "territory": 131,
                          "line_at": 21 * 3600 + 59 * 60 + 7, "longest_ms": 412, "longest_at": 21 * 3600 + 58 * 60 + 40.3}, pw.doctor_memory(tail))
        older = pw.doctor_memory("[21:58:07.002] memory: managed 700 MB, committed 900 MB, resident 9000 MB; players 3; territory 129\n")
        self.assertEqual((700, 3), (older["managed"], older["players"]))
        self.assertNotIn("longest_ms", older)
        self.assertIsNone(pw.doctor_memory("[21:58:07.001] heartbeat: a\n"))

    def at(self, h, m, s):
        return h * 3600 + m * 60 + s

    def doctor(self, line_at, longest_ms, longest_at):
        return {"line_at": line_at, "longest_ms": longest_ms, "longest_at": longest_at}

    def test_a_long_frame_inside_a_reading_counts_against_it(self):
        reading = (self.at(21, 58, 40), self.at(21, 58, 41.2))
        self.assertTrue(pw.reading_disturbed(reading, self.doctor(self.at(21, 59, 7), 412, self.at(21, 58, 40.6))))
        self.assertTrue(pw.reading_disturbed(reading, self.doctor(self.at(21, 59, 7), 412, self.at(21, 58, 41.4))))

    def test_a_long_frame_elsewhere_in_the_minute_or_a_short_one_does_not(self):
        reading = (self.at(21, 58, 40), self.at(21, 58, 41.2))
        self.assertFalse(pw.reading_disturbed(reading, self.doctor(self.at(21, 59, 7), 3000, self.at(21, 58, 55))))
        self.assertFalse(pw.reading_disturbed(reading, self.doctor(self.at(21, 59, 7), 40, self.at(21, 58, 40.6))))

    def test_a_reading_is_not_judged_before_the_plugin_has_reported_its_minute(self):
        reading = (self.at(21, 58, 40), self.at(21, 58, 41.2))
        self.assertIsNone(pw.reading_disturbed(reading, self.doctor(self.at(21, 58, 7), 900, self.at(21, 57, 50))))
        self.assertIsNone(pw.reading_disturbed(reading, self.doctor(self.at(22, 3, 7), 900, self.at(22, 2, 50))))
        self.assertIsNone(pw.reading_disturbed(reading, None))
        self.assertIsNone(pw.reading_disturbed(reading, {"line_at": self.at(21, 59, 7), "managed": 1}))

    def test_midnight_does_not_confuse_the_clock(self):
        reading = (self.at(23, 59, 50), self.at(23, 59, 51))
        self.assertTrue(pw.reading_disturbed(reading, self.doctor(self.at(0, 0, 20), 800, self.at(23, 59, 50.5))))
        self.assertEqual(30, pw.clock_gap(self.at(0, 0, 20), self.at(23, 59, 50)))

    def judged(self, verdicts):
        """A log fed one reading per verdict, each judged at once."""
        import tempfile
        log = pw.MemoryLog(os.path.join(tempfile.mkdtemp(), "memory.log"))
        said = []
        saved = (pw.log, pw.reading_disturbed)
        pw.log = said.append
        try:
            for i, verdict in enumerate(verdicts):
                pw.reading_disturbed = lambda window, doctor, v=verdict: v
                log.pending.append((1000.0 + i, 1001.0 + i, 5000.0 + 60 * i))
                log.doctor = lambda started: {"line_at": 0}
                log.judge(5000.0 + 60 * i + 30, [(26630, 0.0, 50.0, "cmd")])
        finally:
            pw.log, pw.reading_disturbed = saved
        return log, said

    def test_readings_that_keep_holding_the_longest_frame_are_cut_down_to_totals(self):
        log, said = self.judged([False, True, False, True, True])
        self.assertTrue(log.light)
        self.assertEqual((3, 5), (log.disturbed, log.checked))
        self.assertEqual(["3 of 5 memory readings held the minute's longest frame; reading totals only from here"], said)

    def test_a_few_coincidences_among_many_readings_change_nothing(self):
        log, said = self.judged([False] * 30 + [True, True, True])
        self.assertFalse(log.light)
        self.assertEqual([], said)

    def test_a_reading_nobody_reports_on_is_dropped_after_a_while(self):
        import tempfile
        log = pw.MemoryLog(os.path.join(tempfile.mkdtemp(), "memory.log"))
        log.doctor = lambda started: None
        log.pending.append((1000.0, 1001.0, 5000.0))
        log.judge(5060.0, [(26630, 0.0, 50.0, "cmd")])
        self.assertEqual(1, len(log.pending))
        log.judge(5200.0, [(26630, 0.0, 50.0, "cmd")])
        self.assertEqual(([], 0), (log.pending, log.checked))

    def records(self):
        first = {"t": "2026-10-01 19:00:00", "pressure": 1, "swap_mb": 500, "took": 0.4, "helpers": {"Browsingway.Renderer.exe": 500},
                 "windows": [{"pid": 26630, "footprint": 6000, "swapped": 0, "up_min": 2, "doctor": {"managed": 600, "committed": 800, "resident": 5000, "players": 4, "territory": 129},
                              "parts": {"untagged (VM_ALLOCATE)": 3500, "IOAccelerator (graphics)": 2000, "MALLOC_LARGE": 400}}]}
        last = {"t": "2026-10-01 22:00:00", "pressure": 2, "swap_mb": 2900, "took": 0.6, "helpers": {"Browsingway.Renderer.exe": 640, "services.exe": 12},
                "windows": [{"pid": 26630, "footprint": 11700, "swapped": 2600, "up_min": 182, "doctor": {"managed": 650, "committed": 900, "resident": 9000, "players": 41, "territory": 131},
                             "parts": {"untagged (VM_ALLOCATE)": 4100, "IOAccelerator (graphics)": 7000, "MALLOC_LARGE": 380, "stack": 20}}]}
        return [first, last]

    def test_parts_are_ordered_by_how_much_they_grew(self):
        first, last = (r["windows"][0] for r in self.records())
        self.assertEqual([("IOAccelerator (graphics)", 2000, 7000), ("untagged (VM_ALLOCATE)", 3500, 4100), ("stack", 0, 20), ("MALLOC_LARGE", 400, 380)],
                         pw.growth(first, last))

    def test_the_report_says_what_grew(self):
        text = pw.memory_report(self.records())
        self.assertIn("footprint 5.9 GB -> 11.4 GB (peak 11.4)", text)
        self.assertIn("IOAccelerator (graphics): 2000 MB -> 7000 MB (+5000)", text)
        self.assertLess(text.index("IOAccelerator"), text.index("untagged"))
        self.assertIn("plugins' managed heap 600 MB -> 650 MB; players in view 4 -> 41", text)
        self.assertIn("Browsingway.Renderer.exe 640 MB", text)
        self.assertIn("highest memory pressure seen: warning, swap 2.8 GB used, at 2026-10-01 22:00:00", text)
        self.assertNotIn("readings held against", text)
        judged = self.records()
        judged[-1].update(judged=170, felt=1, light=False)
        self.assertIn("readings held against the game's own frames: 170, of which 1 held the minute's longest frame\n", pw.memory_report(judged))
        self.assertEqual("no game window has been recorded yet\n", pw.memory_report([]))

    def test_a_reading_is_taken_once_a_minute_and_only_while_a_game_runs(self):
        import tempfile
        log = pw.MemoryLog(os.path.join(tempfile.mkdtemp(), "memory.log"))
        taken = []
        log.measure = lambda now, games: taken.append(now) or {"t": "x", "took": 0.2, "windows": [], "helpers": {}}
        games = [(26630, 0.0, 50.0, "cmd")]
        log.tick(1000.0, [])
        log.tick(1000.0, games)
        log.tick(1030.0, games)
        log.tick(1061.0, games)
        self.assertEqual([1000.0, 1061.0], taken)
        self.assertEqual(2, len(pw.read_memory_log(log.path)))

    def test_a_slow_reading_makes_the_next_one_wait_longer(self):
        import tempfile
        log = pw.MemoryLog(os.path.join(tempfile.mkdtemp(), "memory.log"))
        log.measure = lambda now, games: {"t": "x", "took": 4.5, "windows": [], "helpers": {}}
        said = []
        saved, pw.log = pw.log, said.append
        try:
            log.tick(1000.0, [(26630, 0.0, 50.0, "cmd")])
        finally:
            pw.log = saved
        self.assertEqual(120, log.every)
        self.assertIn("next one in 120s", said[0])

    def test_a_failed_reading_is_logged_and_nothing_is_written(self):
        import tempfile
        log = pw.MemoryLog(os.path.join(tempfile.mkdtemp(), "memory.log"))
        def broken(now, games):
            raise OSError("footprint not found")
        log.measure = broken
        said = []
        saved, pw.log = pw.log, said.append
        try:
            log.tick(1000.0, [(26630, 0.0, 50.0, "cmd")])
        finally:
            pw.log = saved
        self.assertEqual(["memory reading failed: footprint not found"], said)
        self.assertEqual([], pw.read_memory_log(log.path))

    def test_a_damaged_line_in_the_log_is_skipped(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "memory.log")
        with open(path, "w") as f:
            f.write(json.dumps(self.records()[0]) + "\n{ cut off\n" + json.dumps(self.records()[1]) + "\n")
        self.assertEqual(2, len(pw.read_memory_log(path)))

    def test_a_real_reading_of_this_process_has_the_expected_shape(self):
        import tempfile
        me = os.getpid()
        log = pw.MemoryLog(os.path.join(tempfile.mkdtemp(), "memory.log"), tempfile.mkdtemp())
        saved = pw.wine_procs
        pw.wine_procs = lambda: [(me, "C:\\game\\ffxiv_dx11.exe")]
        try:
            record = log.measure(1000.0, [(me, 940.0, 1.0, "cmd")])
        finally:
            pw.wine_procs = saved
        self.assertEqual(me, record["windows"][0]["pid"])
        self.assertGreater(record["windows"][0]["footprint"], 0)
        self.assertTrue(record["windows"][0]["parts"])
        self.assertEqual(1, record["windows"][0]["up_min"])
        self.assertFalse(os.path.exists(log.path + ".footprint.json"))
        self.assertFalse(record["light"])
        log.light = True
        pw.wine_procs = lambda: [(me, "C:\\game\\ffxiv_dx11.exe")]
        try:
            totals = log.measure(1000.0, [(me, 940.0, 1.0, "cmd")])
        finally:
            pw.wine_procs = saved
        self.assertTrue(totals["light"])
        self.assertGreater(totals["windows"][0]["footprint"], 0)
        self.assertEqual({}, totals["windows"][0]["parts"])


class TeardownTiming(unittest.TestCase):
    def test_reports_how_long_the_server_outlived_the_last_window(self):
        self.assertEqual("wineserver exited 7s after the last window", pw.teardown_note(1000.0, 1007.4))

    def test_nothing_to_report_without_a_recorded_exit(self):
        self.assertIsNone(pw.teardown_note(None, 1007.4))


if __name__ == "__main__":
    unittest.main(verbosity=1)
