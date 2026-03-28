"""Token-bucket rate limiter for external API calls."""

import time
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    """Simple token-bucket rate limiter.

    Args:
        max_tokens: Maximum burst capacity.
        refill_rate: Tokens added per second.
    """

    max_tokens: float
    refill_rate: float
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)

    def __post_init__(self):
        self._tokens = self.max_tokens
        self._last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.max_tokens, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now

    async def acquire(self):
        """Wait until a token is available, then consume one."""
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) / self.refill_rate
            await _async_sleep(wait)


async def _async_sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


# Pre-configured limiters per data source
LIMITERS = {
    "yfinance": RateLimiter(max_tokens=5, refill_rate=2.0),      # ~2 req/sec
    "newsapi": RateLimiter(max_tokens=5, refill_rate=0.0012),     # ~100/day
    "edgar": RateLimiter(max_tokens=10, refill_rate=10.0),        # 10 req/sec
    "fred": RateLimiter(max_tokens=10, refill_rate=2.0),          # 120/min
}
