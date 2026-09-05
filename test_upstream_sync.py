#!/usr/bin/env python3
"""Tests for the upstream sync decisions. Run: python3 -m unittest test_upstream_sync"""
import hashlib, importlib.util, os, unittest

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


if __name__ == "__main__":
    unittest.main(verbosity=1)
