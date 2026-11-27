"""Failed-attempt limiter used by login and 2FA."""
import unittest

from tests._loader import load

rl = load("ratelimit")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class LimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.lim = rl.AttemptLimiter(3, 60, clock=self.clock)

    def test_blocks_after_max_failures(self):
        for _ in range(2):
            self.lim.fail("a")
        self.assertEqual(self.lim.retry_after("a"), 0)
        self.lim.fail("a")
        self.assertGreater(self.lim.retry_after("a"), 0)

    def test_retry_after_counts_down_and_clears(self):
        for _ in range(3):
            self.lim.fail("a")
        first = self.lim.retry_after("a")
        self.assertTrue(55 <= first <= 61)
        self.clock.t += 30
        self.assertLess(self.lim.retry_after("a"), first)
        self.clock.t += 31
        self.assertEqual(self.lim.retry_after("a"), 0)

    def test_keys_are_independent(self):
        for _ in range(3):
            self.lim.fail("a")
        self.assertEqual(self.lim.retry_after("b"), 0)

    def test_success_resets(self):
        for _ in range(3):
            self.lim.fail("a")
        self.lim.reset("a")
        self.assertEqual(self.lim.retry_after("a"), 0)

    def test_old_failures_expire_one_by_one(self):
        self.lim.fail("a")
        self.clock.t += 40
        self.lim.fail("a")
        self.lim.fail("a")
        self.assertGreater(self.lim.retry_after("a"), 0)
        self.clock.t += 21                                  # the first failure is now outside the window
        self.assertEqual(self.lim.retry_after("a"), 0)

    def test_memory_is_bounded(self):
        small = rl.AttemptLimiter(3, 60, clock=self.clock, max_keys=5)
        for i in range(50):
            small.fail(f"k{i}")
        self.assertLessEqual(len(small._fails), 5)


if __name__ == "__main__":
    unittest.main()
