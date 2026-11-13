"""In-process limiter for failed logins (single server process, assumption A2)."""

import time
from collections import deque
from collections.abc import Callable


class LoginRateLimiter:
    """Counts failures per key in a sliding window. Success resets the key."""

    def __init__(
        self,
        max_failures: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._max = max_failures
        self._window = window_s
        self._clock = clock
        self._max_keys = max_keys
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures.get(key, deque())
        while q and now - q[0] >= self._window:
            q.popleft()
        if not q:
            self._failures.pop(key, None)
        return q

    def retry_after(self, key: str) -> int:
        """Seconds until the key may try again; 0 when allowed."""
        now = self._clock()
        q = self._prune(key, now)
        if len(q) < self._max:
            return 0
        return max(1, int(self._window - (now - q[0])) + 1)

    def record_failure(self, key: str) -> None:
        now = self._clock()
        if key not in self._failures and len(self._failures) >= self._max_keys:
            # Bound memory under a spray of random usernames: drop the stalest entries
            for stale in sorted(self._failures, key=lambda k: self._failures[k][-1])[:1000]:
                del self._failures[stale]
        self._failures.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
