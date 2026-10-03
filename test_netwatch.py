import unittest

import netwatch

OK   = "http=200 dns=0.01s connect=0.02s total=0.27s"
SLOW = "http=200 dns=0.01s connect=0.02s total=8.90s"
DOWN = "http=000 dns=0.00s connect=0.00s total=9.00s"


class WhenAFailedFetchIsThisMachinesProblem(unittest.TestCase):
    def results(self, plugin_list, other):
        return list(zip(netwatch.URLS, (plugin_list, other)))

    def test_a_fetch_that_works_from_macos_is_a_slow_server_or_a_busy_game(self):
        self.assertFalse(netwatch.ours(self.results(SLOW, OK), streak=1))

    def test_one_server_down_while_another_answers_is_that_servers_outage(self):
        self.assertFalse(netwatch.ours(self.results(DOWN, OK), streak=1))
        self.assertFalse(netwatch.ours(self.results("", OK), streak=2))

    def test_nothing_answering_from_macos_is_the_network(self):
        self.assertTrue(netwatch.ours(self.results(DOWN, DOWN), streak=1))
        self.assertTrue(netwatch.ours(self.results("", ""), streak=1))

    def test_a_game_that_keeps_failing_is_ours_even_when_macos_gets_through(self):
        self.assertTrue(netwatch.ours(self.results(OK, OK), streak=netwatch.KEEPS_FAILING))


if __name__ == "__main__":
    unittest.main()
