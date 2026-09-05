#!/usr/bin/env python3
"""Tests for portwatch's port-arming policy. Run: python3 test_portwatch.py"""
import datetime, importlib.util, os, sys, unittest

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


if __name__ == "__main__":
    unittest.main(verbosity=1)


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
