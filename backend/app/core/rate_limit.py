"""In-process sliding-window rate limiter.

Keys are derived from the authenticated user id (never from client-supplied headers). State is
per process: with several replicas, move this to Redis (see README, "Known limitations").
"""

import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> tuple[bool, int]:
        """Record an attempt. Returns ``(allowed, retry_after_seconds)``. A limit of 0 disables."""
        if self.limit <= 0:
            return True, 0
        now = time.monotonic()
        with self._lock:
            window = self._hits[key]
            while window and now - window[0] >= self.window_seconds:
                window.popleft()
            if len(window) >= self.limit:
                retry_after = int(self.window_seconds - (now - window[0])) + 1
                return False, retry_after
            window.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
