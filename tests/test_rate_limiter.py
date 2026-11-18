"""Offline checks for the per-process authenticated rate limiter."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from infra.rate_limiter import RateLimiter, TokenBucket


def test_limiter_holds_state_lock_while_consuming(monkeypatch):
    limiter = RateLimiter({"enabled": True})
    consume = TokenBucket.consume

    def checked_consume(bucket):
        assert limiter._lock.locked()
        return consume(bucket)

    monkeypatch.setattr(TokenBucket, "consume", checked_consume)
    assert limiter.is_allowed("service")


def test_concurrent_requests_share_one_allowance():
    workers = 16
    limiter = RateLimiter({"enabled": True, "default": {"capacity": 3, "fill_rate": 0}})
    ready = Barrier(workers, timeout=10)

    def request(_):
        ready.wait()
        return limiter.is_allowed("service")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        assert sum(pool.map(request, range(workers))) == 3
    assert limiter.metrics()["service"]["available"] == 0
    assert limiter.is_allowed("other-service")
    limiter.reconfigure({"enabled": False})
    assert limiter.is_allowed("service")
    assert limiter.metrics() == {}
