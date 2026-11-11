#!/usr/bin/env python3
"""Run: python3 -m unittest test_register_dev_plugin"""
import importlib.util, os, unittest

spec = importlib.util.spec_from_file_location("rdp", os.path.join(os.path.dirname(os.path.abspath(__file__)), "register_dev_plugin.py"))
rdp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rdp)


class GameDetection(unittest.TestCase):
    def test_both_launch_shapes_are_the_game_and_the_crash_handler_is_not(self):
        self.assertTrue(rdp.game_in_listing('Z:\\\\Users\\\\x\\\\Library\\\\Application Support\\\\XIV on Mac\\\\ffxiv\\\\game\\\\ffxiv_dx11.exe //**sqex0003token'))
        self.assertTrue(rdp.game_in_listing('/Users/x/Library/Application Support/XIV on Mac/ffxiv/game/ffxiv_dx11.exe //**sqex0003token'))
        self.assertFalse(rdp.game_in_listing('/bin/zsh -c echo Z:\\\\x\\\\game\\\\ffxiv_dx11.exe //token'))
        self.assertFalse(rdp.game_in_listing('Z:\\\\Users\\\\x\\\\Library\\\\Application Support\\\\XIV on Mac\\\\dalamud\\\\Hooks\\\\15.0.4.0\\\\DalamudCrashHandler.exe --process-handle=404 --log-directory=Z:\\\\x\\\\logs ffxiv_dx11.exe'))


if __name__ == "__main__":
    unittest.main()
