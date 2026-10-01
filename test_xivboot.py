import datetime
import unittest

import xivboot


def at(h, m, s):
    return datetime.datetime(2026, 10, 1, h, m, s)


GAME = r"Z:\Users\someone\Library\Application Support\XIV on Mac\ffxiv\game\ffxiv_dx11.exe //**args"
HANDLER = r"Z:\Users\someone\Library\Application Support\XIV on Mac\dalamud\Hooks\15.0.3.6\DalamudCrashHandler.exe --process-handle=1 Z:\x\ffxiv_dx11.exe"


class WhichProcessABootBelongsTo(unittest.TestCase):
    def test_game_processes_are_read_with_their_launch_times(self):
        out = (f"  9930 Thu Oct  1 10:53:14 2026     {GAME}\n"
               f"  9950 Thu Oct  1 10:53:16 2026     {HANDLER}\n"
               " 10993 Thu Oct 11 10:56:06 2026     /usr/bin/python3 xivboot.py --watch\n"
               "garbage\n")
        self.assertEqual({9930: datetime.datetime(2026, 10, 1, 10, 53, 14)}, xivboot.parse_games(out))

    def test_the_boot_is_matched_to_the_game_launched_just_before_its_first_line(self):
        games = {9930: at(10, 53, 14)}
        self.assertEqual("9930", xivboot.boot_pid(at(10, 53, 18), games))

    def test_a_relaunch_is_never_taken_for_the_boot_that_wedged_before_it(self):
        games = {10993: at(10, 56, 6)}
        self.assertIsNone(xivboot.boot_pid(at(10, 53, 18), games))
        self.assertTrue(xivboot.boot_gone(at(10, 53, 18), games))

    def test_with_two_windows_the_one_that_is_booting_is_chosen(self):
        games = {26630: at(6, 27, 36), 41798: at(10, 53, 14)}
        self.assertEqual("41798", xivboot.boot_pid(at(10, 53, 18), games))

    def test_a_window_launched_long_before_the_boot_is_not_its_process(self):
        games = {26630: at(6, 27, 36)}
        self.assertIsNone(xivboot.boot_pid(at(10, 53, 18), games))
        self.assertFalse(xivboot.boot_gone(at(10, 53, 18), games))

    def test_no_game_at_all_means_the_boot_is_gone(self):
        self.assertIsNone(xivboot.boot_pid(at(10, 53, 18), {}))
        self.assertTrue(xivboot.boot_gone(at(10, 53, 18), {}))


if __name__ == "__main__":
    unittest.main()
