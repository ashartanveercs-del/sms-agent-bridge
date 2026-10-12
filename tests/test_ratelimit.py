from fakes import FakeClock

from smsbridge.ratelimit import Cooldown, SlidingWindowLimiter


def test_allows_up_to_the_limit_inside_the_window():
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60, clock=FakeClock())
    assert [limiter.check("A").allowed for _ in range(4)] == [True, True, True, False]
    assert limiter.check("B").allowed


def test_only_the_first_rejection_in_a_window_asks_for_a_notice():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, clock=FakeClock())
    limiter.check("A")
    first, second = limiter.check("A"), limiter.check("A")
    assert (first.allowed, first.notify) == (False, True)
    assert (second.allowed, second.notify) == (False, False)


def test_the_window_slides():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60, clock=clock)
    limiter.check("A")
    clock.advance(30)
    limiter.check("A")
    assert not limiter.check("A").allowed
    clock.advance(31)  # the first message has left the window
    assert limiter.check("A").allowed


def test_rejected_messages_do_not_extend_the_block():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check("A")
    for _ in range(5):
        clock.advance(10)
        assert not limiter.check("A").allowed
    clock.advance(11)  # 61 s after the only accepted message
    assert limiter.check("A").allowed


def test_a_zero_limit_disables_limiting():
    limiter = SlidingWindowLimiter(limit=0, window_seconds=60)
    assert all(limiter.check("A").allowed for _ in range(100))


def test_memory_is_bounded_by_evicting_the_least_recent_sender():
    limiter = SlidingWindowLimiter(limit=5, window_seconds=60, max_senders=100)
    for n in range(1000):
        limiter.check(f"S{n}")
    assert limiter.tracked_senders() == 100


def test_cooldown_fires_once_per_period_per_key():
    clock = FakeClock()
    gate = Cooldown(period_seconds=60, clock=clock)
    assert gate.ready("A")
    assert not gate.ready("A")
    assert gate.ready("B")
    clock.advance(61)
    assert gate.ready("A")
