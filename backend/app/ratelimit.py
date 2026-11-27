"""
Tiny in-memory failed-attempt limiter for logins and 2FA codes (no extra dependency).

Per process: with several workers each keeps its own counts, so the real limit is at most workers x max_attempts,
which is still a hard stop on password / 2FA guessing. Restarting the app clears the counts.
"""
import time
from collections import deque


class AttemptLimiter:
    def __init__(self, max_attempts: int, window_seconds: float, clock=time.monotonic, max_keys: int = 10_000):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._fails: dict[str, deque] = {}

    def _prune(self, key: str) -> deque:
        q = self._fails.get(key)
        if q is None:
            return deque()
        cutoff = self._clock() - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            self._fails.pop(key, None)
        return q

    def retry_after(self, key: str) -> int:
        """Seconds until another attempt is allowed; 0 if not blocked."""
        q = self._prune(key)
        if len(q) < self.max_attempts:
            return 0
        return max(1, int(q[0] + self.window - self._clock()) + 1)

    def fail(self, key: str) -> None:
        if key not in self._fails and len(self._fails) >= self._max_keys:
            self._fails.pop(next(iter(self._fails)))          # bound memory if someone sprays many keys
        self._fails.setdefault(key, deque()).append(self._clock())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)
