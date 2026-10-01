#!/usr/bin/env python3
"""Tests for the upstream sync decisions. Run: python3 -m unittest test_upstream_sync"""
import datetime, hashlib, importlib.util, os, tempfile, unittest

spec = importlib.util.spec_from_file_location("us", os.path.join(os.path.dirname(os.path.abspath(__file__)), "upstream_sync.py"))
us = importlib.util.module_from_spec(spec)
spec.loader.exec_module(us)


class WhenToSync(unittest.TestCase):
    def test_a_new_upstream_head_means_work(self):
        self.assertTrue(us.needs_sync({"synced_upstream": "aaa"}, "bbb"))
        self.assertTrue(us.needs_sync({}, "bbb"))

    def test_the_head_already_synced_or_already_failed_is_left_alone(self):
        self.assertFalse(us.needs_sync({"synced_upstream": "aaa"}, "aaa"))
        self.assertFalse(us.needs_sync({"failed_upstream": "bbb"}, "bbb"))

    def test_a_failed_head_is_retried_once_the_fork_branch_was_pushed_by_hand(self):
        state = {"failed_upstream": "bbb", "failed_branch": "f1"}
        self.assertFalse(us.needs_sync(state, "bbb", branch_head="f1"))
        self.assertTrue(us.needs_sync(state, "bbb", branch_head="f2"))

    def test_an_api_mismatch_is_retried_once_this_macs_dalamud_changed(self):
        state = {"failed_upstream": "bbb", "failed_api_level": 15}
        self.assertFalse(us.needs_sync(state, "bbb", local_api_level=15))
        self.assertTrue(us.needs_sync(state, "bbb", local_api_level=16))


class AttentionNotes(unittest.TestCase):
    def test_each_plugin_keeps_one_line(self):
        text = us.attention_lines("", "IINACT", "needs a hand")
        text = us.attention_lines(text, "Browsingway", "build waiting")
        text = us.attention_lines(text, "IINACT", "still needs a hand")
        self.assertEqual("Browsingway: build waiting\nIINACT: still needs a hand\n", text)

    def test_clearing_removes_only_that_plugin(self):
        text = "IINACT: a\nBrowsingway: b\n"
        self.assertEqual("Browsingway: b\n", us.attention_lines(text, "IINACT", None))
        self.assertEqual("", us.attention_lines("IINACT: a\n", "IINACT", None))


class Artifacts(unittest.TestCase):
    def test_artifact_names_carry_the_plugin_and_commit(self):
        self.assertEqual("IINACT-abc123", us.artifact_name("IINACT", "abc123"))

    def test_every_listed_file_must_match_its_hash(self):
        files = {"IINACT.dll": b"one", "sub/Other.dll": b"two"}
        sums = "\n".join(f"{hashlib.sha256(data).hexdigest()}  ./{name}" for name, data in files.items()) + "\n"
        self.assertEqual([], us.verify_hashes(sums, files.get))
        files["IINACT.dll"] = b"tampered"
        self.assertEqual(["IINACT.dll"], us.verify_hashes(sums, files.get))

    def test_a_missing_file_is_a_mismatch(self):
        sums = f"{hashlib.sha256(b'x').hexdigest()}  ./gone.dll\n"
        self.assertEqual(["gone.dll"], us.verify_hashes(sums, lambda name: None))


class Compatibility(unittest.TestCase):
    def test_same_api_level_installs(self):
        self.assertTrue(us.api_level_compatible(15, 15))

    def test_a_different_api_level_does_not(self):
        self.assertFalse(us.api_level_compatible(16, 15))

    def test_unknown_levels_do_not_block(self):
        self.assertTrue(us.api_level_compatible(None, 15))
        self.assertTrue(us.api_level_compatible(15, None))


class CloneSafety(unittest.TestCase):
    def test_a_clean_pushed_clone_may_be_reset(self):
        self.assertTrue(us.clone_is_clean("", 0))

    def test_local_work_blocks_the_reset(self):
        self.assertFalse(us.clone_is_clean(" M portwatch.py\n", 0))
        self.assertFalse(us.clone_is_clean("", 2))


class LogSnapshots(unittest.TestCase):
    T0 = datetime.datetime(2026, 9, 28, 16, 0, 0).timestamp()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, "logs")
        self.archive = os.path.join(self.tmp.name, "archive")
        os.makedirs(self.logs)
        self.state = {}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text, mtime):
        path = os.path.join(self.logs, name)
        with open(path, "w") as f:
            f.write(text)
        os.utime(path, (mtime, mtime))

    def snap(self, running=False):
        return us.snapshot_logs(self.state, running=lambda: running, log_dir=self.logs, archive=self.archive, keep=3)

    def archived(self):
        return sorted(os.listdir(self.archive)) if os.path.isdir(self.archive) else []

    def test_a_new_log_is_copied_and_named_by_its_last_write(self):
        self.write("dalamud.log", "line 1\n", self.T0)
        self.assertEqual(["dalamud-20260928-160000.log"], self.snap())
        with open(os.path.join(self.archive, "dalamud-20260928-160000.log")) as f:
            self.assertEqual("line 1\n", f.read())

    def test_an_unchanged_log_is_not_copied_twice(self):
        self.write("dalamud.log", "line 1\n", self.T0)
        self.snap()
        self.assertEqual([], self.snap())
        self.assertEqual(1, len(self.archived()))

    def test_a_grown_log_is_copied_again(self):
        self.write("dalamud.log", "line 1\n", self.T0)
        self.snap()
        self.write("dalamud.log", "line 1\nline 2\n", self.T0 + 3600)
        self.assertEqual(["dalamud-20260928-170000.log"], self.snap())
        self.assertEqual(2, len(self.archived()))

    def test_the_rolled_over_copy_of_a_known_session_is_skipped(self):
        self.write("dalamud.log", "session A\n", self.T0)
        self.snap()
        # a relaunch moved the same bytes into .old.log and started a fresh .log
        self.write("dalamud.old.log", "session A\n", self.T0 + 60)
        self.write("dalamud.log", "session B\n", self.T0 + 120)
        self.assertEqual(["dalamud-20260928-160200.log"], self.snap())

    def test_an_old_log_with_unseen_content_is_kept_with_a_marker(self):
        self.write("dalamud.old.log", "older session\n", self.T0)
        self.assertEqual(["dalamud-20260928-160000-old.log"], self.snap())

    def test_nothing_happens_while_the_game_runs(self):
        self.write("dalamud.log", "live\n", self.T0)
        self.assertEqual([], self.snap(running=True))
        self.assertEqual([], self.archived())
        self.assertEqual({}, self.state)

    def test_only_the_newest_copies_are_kept(self):
        for i in range(5):
            self.write("dalamud.log", f"session {i}\n", self.T0 + i * 3600)
            self.snap()
        self.assertEqual(["dalamud-20260928-180000.log", "dalamud-20260928-190000.log", "dalamud-20260928-200000.log"],
                         self.archived())

    def test_captures_are_pruned_to_the_newest_of_each_kind(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(4):
                for kind in ("boot-%d.txt", "hang-sample-%d.txt", "notes-%d.txt"):
                    path = os.path.join(d, kind % i)
                    with open(path, "w") as f:
                        f.write("x")
                    os.utime(path, (self.T0 + i, self.T0 + i))
            removed = us.prune_captures(d, {"boot-*.txt": 2, "hang-sample-*.txt": 1})
            self.assertEqual(["boot-0.txt", "boot-1.txt", "hang-sample-0.txt", "hang-sample-1.txt", "hang-sample-2.txt"], sorted(removed))
            self.assertEqual(["boot-2.txt", "boot-3.txt", "hang-sample-3.txt", "notes-0.txt", "notes-1.txt", "notes-2.txt", "notes-3.txt"],
                             sorted(os.listdir(d)))

    def test_snapshot_names(self):
        self.assertEqual("dalamud-20260928-160000.log", us.snapshot_name("dalamud.log", self.T0))
        self.assertEqual("dalamud-20260928-160000-old.log", us.snapshot_name("dalamud.old.log", self.T0))


class PatchAndWikiChores(unittest.TestCase):
    NOW = datetime.datetime(2026, 9, 29, 12, 0, 0)

    def test_the_first_tick_records_the_present_and_regenerates_nothing(self):
        state = {}
        us.seed(state, "2026.09.15.0000.0000", "key-a", self.NOW)
        self.assertEqual({"game_version": "2026.09.15.0000.0000", "api_key": "key-a"}, state["patch"])
        self.assertEqual("2026-09-29T12:00:00", state["codex_refreshed"])
        self.assertFalse(us.patch_regen_needed(state, "2026.09.15.0000.0000", "key-a", running=False))

    def test_a_patch_waits_for_the_data_source_and_a_closed_game(self):
        state = {"patch": {"game_version": "2026.09.15.0000.0000", "api_key": "key-a"}}
        self.assertFalse(us.patch_regen_needed(state, "2026.10.20.0000.0000", "key-a", running=False))
        self.assertFalse(us.patch_regen_needed(state, "2026.10.20.0000.0000", "key-b", running=True))
        self.assertTrue(us.patch_regen_needed(state, "2026.10.20.0000.0000", "key-b", running=False))

    def test_a_data_source_update_alone_or_unknown_values_change_nothing(self):
        state = {"patch": {"game_version": "2026.09.15.0000.0000", "api_key": "key-a"}}
        self.assertFalse(us.patch_regen_needed(state, "2026.09.15.0000.0000", "key-b", running=False))
        self.assertFalse(us.patch_regen_needed(state, None, "key-b", running=False))
        self.assertFalse(us.patch_regen_needed(state, "2026.10.20.0000.0000", None, running=False))
        self.assertFalse(us.patch_regen_needed({}, "2026.10.20.0000.0000", "key-b", running=False))

    def test_a_policy_is_rechecked_only_when_its_plugin_changed_version(self):
        state = {}
        us.seed(state, "2026.09.15.0000.0000", "key-a", self.NOW, [("WrathCombo", "1.0.4.26"), ("GatherBuddyReborn", None)])
        self.assertEqual({"WrathCombo": "1.0.4.26"}, state["policies"])
        self.assertFalse(us.policy_check_needed(state, "WrathCombo", "1.0.4.26"))
        self.assertTrue(us.policy_check_needed(state, "WrathCombo", "1.0.4.27"))
        self.assertFalse(us.policy_check_needed(state, "WrathCombo", None))
        self.assertFalse(us.policy_check_needed(state, "GatherBuddyReborn", "7.5.6.1"))

    def test_the_newest_installed_manifest_wins_and_broken_ones_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            for ver, text in (("1.0.4.25", '{"AssemblyVersion": "1.0.4.25"}'), ("1.0.4.26", '{"AssemblyVersion": "1.0.4.26"}'), ("junk", "{")):
                os.makedirs(os.path.join(d, ver))
                with open(os.path.join(d, ver, "WrathCombo.json"), "w") as f:
                    f.write(text)
            self.assertEqual("1.0.4.26", us.plugin_version(os.path.join(d, "*", "WrathCombo.json")))
            self.assertIsNone(us.plugin_version(os.path.join(d, "*", "Other.json")))

    def test_the_wiki_refresh_is_weekly_from_the_last_one(self):
        self.assertFalse(us.codex_refresh_due({}, self.NOW))
        self.assertFalse(us.codex_refresh_due({"codex_refreshed": "2026-09-23T12:00:01"}, self.NOW))
        self.assertTrue(us.codex_refresh_due({"codex_refreshed": "2026-09-22T12:00:00"}, self.NOW))


class AttentionNotes(unittest.TestCase):
    def test_a_note_is_written_replaced_and_cleared_per_source(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("attention", os.path.join(os.path.dirname(os.path.abspath(__file__)), "attention.py"))
        att = importlib.util.module_from_spec(spec); spec.loader.exec_module(att)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "attention.txt")
            self.assertTrue(att.set_attention("Boot", "a boot wedged on a black screen at 10:02; capture wedge-sample-100200.txt", path))
            self.assertTrue(att.set_attention("IINACT", "the parser stalled at 10:05", path))
            self.assertTrue(att.set_attention("Boot", "a boot wedged on a black screen at 10:09", path))
            with open(path) as f:
                self.assertEqual(["IINACT: the parser stalled at 10:05", "Boot: a boot wedged on a black screen at 10:09"], f.read().splitlines())
            att.set_attention("IINACT", None, path); att.set_attention("Boot", None, path)
            self.assertFalse(os.path.exists(path))
        when = datetime.datetime(2026, 9, 29, 10, 2)
        self.assertEqual("a boot wedged on a black screen at 10:02; capture wedge-sample-100200.txt",
                         att.event_note("a boot wedged on a black screen", "/x/wedge-sample-100200.txt", when))
        self.assertEqual("the parser stalled at 10:02", att.event_note("the parser stalled", None, when))


class SolverCheck(unittest.TestCase):
    def test_a_missing_or_foreign_solver_leaves_a_note_and_a_matching_one_clears_it(self):
        with tempfile.TemporaryDirectory() as d:
            exe, att = os.path.join(d, "raphael-cli.exe"), os.path.join(d, "attention.txt")
            state = {"GatherBuddyReborn": {}}
            self.assertIn("missing", us.check_solver(state, exe, att))
            self.assertIn("missing", state[us.SOLVER_SOURCE]["note"])
            with open(exe, "wb") as f:
                f.write(b"solver")
            self.assertIsNone(us.check_solver(state, exe, att))
            self.assertNotIn("note", state[us.SOLVER_SOURCE])
            self.assertFalse(os.path.exists(att))
            state["GatherBuddyReborn"]["solver_sha256"] = "0" * 64
            self.assertIn("not the one", us.check_solver(state, exe, att))
            state["GatherBuddyReborn"]["solver_sha256"] = us.sha256_of(exe)
            self.assertIsNone(us.check_solver(state, exe, att))
        self.assertEqual("abc", us.solver_sha_from_sums("abc  ./raphael-cli.exe\ndef  ./GatherBuddyReborn.dll\n"))
        self.assertIsNone(us.solver_sha_from_sums("def  ./GatherBuddyReborn.dll\n"))


class StandingNotes(unittest.TestCase):
    def test_notes_kept_in_state_are_reasserted_and_dropped_when_solved(self):
        state = {"IINACT": {"failed_upstream": "abc", "note": "upstream sync needs a hand (workflow run 1 failure)"},
                 "Browsingway": {"synced_upstream": "def"},
                 "policy_notes": {"WrathCombo": "the settings policy needs a look after the update to 1.0.4.27"}}
        self.assertEqual([("IINACT", "upstream sync needs a hand (workflow run 1 failure)"),
                          ("WrathCombo", "the settings policy needs a look after the update to 1.0.4.27")], us.reassert_notes(state))
        state["IINACT"].pop("note"); state["policy_notes"].pop("WrathCombo")
        self.assertEqual([], us.reassert_notes(state))


class Installing(unittest.TestCase):
    def build(self):
        dest, out = tempfile.mkdtemp(), tempfile.mkdtemp()
        os.makedirs(os.path.join(dest, "renderer"))
        os.makedirs(os.path.join(out, "renderer"))
        for folder, files in ((dest, {"Plugin.dll": "new", "renderer/Renderer.exe": "new exe", "SHA256SUMS": "x", "COMMIT": "abc", "UPSTREAM": "def"}),
                              (out, {"Plugin.dll": "old", "renderer/Renderer.exe": "old exe", "fetched.dll": "kept"})):
            for rel, text in files.items():
                with open(os.path.join(folder, rel), "w") as f:
                    f.write(text)
        return dest, out

    def read(self, *parts):
        with open(os.path.join(*parts)) as f:
            return f.read()

    def test_a_build_replaces_its_files_and_keeps_the_others(self):
        dest, out = self.build()
        us.install({"install_dir": out}, dest)
        self.assertEqual(("new", "new exe", "kept"), (self.read(out, "Plugin.dll"), self.read(out, "renderer", "Renderer.exe"), self.read(out, "fetched.dll")))
        left = sorted(os.path.relpath(os.path.join(root, n), out) for root, _, names in os.walk(out) for n in names)
        self.assertEqual(["Plugin.dll", "fetched.dll", "renderer/Renderer.exe"], left)

    def test_a_process_holding_the_old_file_keeps_the_old_file(self):
        dest, out = self.build()
        target = os.path.join(out, "renderer", "Renderer.exe")
        before = os.stat(target).st_ino
        with open(target) as held:
            us.install({"install_dir": out}, dest)
            self.assertEqual("old exe", held.read())
        self.assertEqual("new exe", self.read(target))
        self.assertNotEqual(before, os.stat(target).st_ino)


if __name__ == "__main__":
    unittest.main(verbosity=1)
