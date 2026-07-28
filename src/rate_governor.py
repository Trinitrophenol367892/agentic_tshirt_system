"""
Conservative Rate Governor for NVIDIA NIM API.

NIM enforces a 40 RPM (requests per minute) hard limit.
This governor operates at 25% of that capacity (10 RPM) to ensure
we NEVER come close to the limit, even with retries or clock skew.

Design principles:
- Maximum 10 requests per 60-second sliding window
- Minimum 6 seconds between any two requests
- All API calls across the entire system route through this governor
- Verbose logging of throttle events and wait times
- Thread-safe (uses a lock for concurrent access)
"""
import time
import threading
from collections import deque
from .logger import setup_logger

log_governor = setup_logger("rate_governor")


class RateGovernor:
    """
    Sliding-window rate limiter with conservative defaults.

    NIM hard limit:  40 RPM
    Governor limit:  10 RPM (25% utilization)
    Min spacing:     6.0 seconds between requests
    """

    def __init__(self, max_rpm: int = 10, min_spacing_seconds: float = 6.0):
        self.max_rpm = max_rpm
        self.min_spacing = min_spacing_seconds
        self.window_seconds = 60.0
        self._request_times: deque = deque()
        self._lock = threading.Lock()
        self._total_requests = 0
        self._total_wait_time = 0.0

        log_governor.info("Rate Governor initialized.")
        log_governor.info("  NIM hard limit:     40 RPM")
        log_governor.info("  Governor limit:     %d RPM (%.0f%% utilization)", max_rpm, (max_rpm / 40) * 100)
        log_governor.info("  Min spacing:        %.1fs between requests", self.min_spacing)
        log_governor.info("  Sliding window:     %.0fs", self.window_seconds)

    def acquire(self, context: str = "API call") -> float:
        """
        Acquire permission to make an API request.
        Blocks (sleeps) if necessary to stay within limits.
        Returns the time spent waiting (in seconds).
        """
        with self._lock:
            now = time.time()
            wait_time = 0.0

            # Purge timestamps outside the sliding window
            while self._request_times and (now - self._request_times[0]) > self.window_seconds:
                self._request_times.popleft()

            # Check 1: Are we at the RPM limit for this window?
            if len(self._request_times) >= self.max_rpm:
                oldest = self._request_times[0]
                wait_for_window = self.window_seconds - (now - oldest) + 0.5  # +0.5s buffer
                if wait_for_window > 0:
                    log_governor.warning("  [%s] RPM limit reached (%d/%d in window). Waiting %.1fs...",
                                        context, len(self._request_times), self.max_rpm, wait_for_window)
                    time.sleep(wait_for_window)
                    wait_time += wait_for_window
                    now = time.time()
                    # Purge again after sleeping
                    while self._request_times and (now - self._request_times[0]) > self.window_seconds:
                        self._request_times.popleft()

            # Check 2: Is the minimum spacing satisfied?
            if self._request_times:
                last_request = self._request_times[-1]
                elapsed_since_last = now - last_request
                if elapsed_since_last < self.min_spacing:
                    wait_for_spacing = self.min_spacing - elapsed_since_last + 0.1  # +0.1s buffer
                    log_governor.debug("  [%s] Min spacing not met (%.1fs < %.1fs). Waiting %.1fs...",
                                      context, elapsed_since_last, self.min_spacing, wait_for_spacing)
                    time.sleep(wait_for_spacing)
                    wait_time += wait_for_spacing

            # Record this request
            self._request_times.append(time.time())
            self._total_requests += 1
            self._total_wait_time += wait_time

            if wait_time > 0:
                log_governor.info("  [%s] Throttled: waited %.1fs total.", context, wait_time)
            else:
                log_governor.debug("  [%s] Cleared immediately. (Request #%d, %d in current window)",
                                  context, self._total_requests, len(self._request_times))

            return wait_time

    def get_stats(self) -> dict:
        """Return current governor statistics."""
        with self._lock:
            now = time.time()
            # Count requests in current window
            active = sum(1 for t in self._request_times if (now - t) <= self.window_seconds)
            return {
                "total_requests": self._total_requests,
                "total_wait_time": round(self._total_wait_time, 2),
                "requests_in_current_window": active,
                "max_rpm": self.max_rpm,
                "min_spacing": self.min_spacing,
                "utilization_pct": round((active / self.max_rpm) * 100, 1) if self.max_rpm > 0 else 0,
            }

    def log_stats(self):
        """Log current statistics."""
        stats = self.get_stats()
        log_governor.info("  Governor Stats: %d total requests | %.1fs total wait | %d/%d in window (%.0f%% util)",
                         stats["total_requests"], stats["total_wait_time"],
                         stats["requests_in_current_window"], stats["max_rpm"],
                         stats["utilization_pct"])


# Singleton instance used across the entire system
governor = RateGovernor(max_rpm=10, min_spacing_seconds=6.0)
