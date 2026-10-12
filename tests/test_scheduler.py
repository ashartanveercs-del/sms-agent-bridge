import asyncio

import pytest

from smsbridge.scheduler import FairScheduler, QueueFull, SchedulerClosed


class Recorder:
    """Handler double: records start order, peak concurrency and same-sender overlap."""

    def __init__(self, delay: float = 0.0, fail_on: tuple = (), block: bool = False) -> None:
        self.delay = delay
        self.fail_on = set(fail_on)
        self.block = block
        self.started: list[tuple[str, int]] = []
        self.finished: list[tuple[str, int]] = []
        self.active = 0
        self.peak = 0
        self.running_keys: set[str] = set()
        self.same_sender_overlap = False

    async def __call__(self, item: tuple[str, int]) -> None:
        key, _ = item
        if key in self.running_keys:
            self.same_sender_overlap = True
        self.running_keys.add(key)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.append(item)
        try:
            if self.block:
                await asyncio.Event().wait()
            await asyncio.sleep(self.delay)
            if item in self.fail_on:
                raise RuntimeError("handler blew up")
        finally:
            self.active -= 1
            self.running_keys.discard(key)
            self.finished.append(item)


def submit_all(scheduler: FairScheduler, items) -> None:
    for key, n in items:
        scheduler.submit(key, (key, n))


async def test_one_senders_messages_run_in_order_and_never_overlap():
    handler = Recorder(delay=0.005)
    scheduler = FairScheduler(handler, workers=4)
    scheduler.start()
    submit_all(scheduler, [(k, n) for n in range(5) for k in ("A", "B")])

    await asyncio.wait_for(scheduler.drain(), 2)
    assert [n for k, n in handler.started if k == "A"] == [0, 1, 2, 3, 4]
    assert [n for k, n in handler.started if k == "B"] == [0, 1, 2, 3, 4]
    assert not handler.same_sender_overlap
    await scheduler.close()


async def test_different_senders_run_in_parallel_up_to_the_pool_size():
    handler = Recorder(delay=0.03)
    scheduler = FairScheduler(handler, workers=3)
    scheduler.start()
    submit_all(scheduler, [(f"S{i}", 0) for i in range(6)])

    await asyncio.wait_for(scheduler.drain(), 2)
    assert handler.peak == 3
    await scheduler.close()


async def test_a_chatty_sender_cannot_starve_others():
    handler = Recorder()
    scheduler = FairScheduler(handler, workers=1)
    scheduler.start()
    submit_all(scheduler, [("A", n) for n in range(5)] + [("B", 0), ("C", 0)])

    await asyncio.wait_for(scheduler.drain(), 2)
    assert handler.started == [("A", 0), ("B", 0), ("C", 0), ("A", 1), ("A", 2), ("A", 3),
                               ("A", 4)]
    await scheduler.close()


async def test_a_busy_sender_rejoins_the_rotation_behind_waiting_senders():
    handler = Recorder(delay=0.02)
    scheduler = FairScheduler(handler, workers=1)
    scheduler.start()
    scheduler.submit("A", ("A", 0))
    await asyncio.sleep(0.005)          # A0 is now in flight
    scheduler.submit("A", ("A", 1))
    scheduler.submit("B", ("B", 0))

    await asyncio.wait_for(scheduler.drain(), 2)
    assert handler.started == [("A", 0), ("B", 0), ("A", 1)]
    await scheduler.close()


async def test_a_failing_handler_does_not_kill_the_worker_or_the_senders_queue():
    handler = Recorder(fail_on=(("A", 0),))
    scheduler = FairScheduler(handler, workers=1)
    scheduler.start()
    submit_all(scheduler, [("A", 0), ("A", 1), ("B", 0)])

    await asyncio.wait_for(scheduler.drain(), 2)
    assert sorted(handler.started) == [("A", 0), ("A", 1), ("B", 0)]
    stats = scheduler.stats()
    assert (stats["processed"], stats["failed"]) == (2, 1)
    await scheduler.close()


async def test_per_sender_backlog_is_bounded():
    scheduler = FairScheduler(Recorder(), workers=1, max_pending_per_sender=2)
    assert scheduler.submit("A", ("A", 0)) == 1
    assert scheduler.submit("A", ("A", 1)) == 2
    with pytest.raises(QueueFull) as err:
        scheduler.submit("A", ("A", 2))
    assert err.value.scope == "sender"
    assert scheduler.submit("B", ("B", 0)) == 1
    assert scheduler.stats()["rejected"] == 1


async def test_total_backlog_is_bounded():
    scheduler = FairScheduler(Recorder(), workers=1, max_pending_total=3)
    submit_all(scheduler, [("A", 0), ("B", 0), ("C", 0)])
    with pytest.raises(QueueFull) as err:
        scheduler.submit("D", ("D", 0))
    assert err.value.scope == "total"


async def test_the_in_flight_message_does_not_count_against_the_backlog():
    handler = Recorder(delay=0.02)
    scheduler = FairScheduler(handler, workers=1, max_pending_per_sender=1)
    scheduler.start()
    scheduler.submit("A", ("A", 0))
    await asyncio.sleep(0.005)          # A0 leaves the queue and is in flight
    scheduler.submit("A", ("A", 1))     # one waiting: allowed
    with pytest.raises(QueueFull):
        scheduler.submit("A", ("A", 2))
    await asyncio.wait_for(scheduler.drain(), 2)
    await scheduler.close()


async def test_stats_report_queue_state():
    handler = Recorder(block=True)
    scheduler = FairScheduler(handler, workers=1)
    scheduler.start()
    submit_all(scheduler, [("A", 0), ("A", 1), ("B", 0)])
    await asyncio.sleep(0.01)
    stats = scheduler.stats()
    assert stats["in_flight"] == 1
    assert stats["pending"] == 2
    assert stats["senders"] == 2
    assert stats["workers"] == 1
    await scheduler.close(grace_seconds=0.01)


async def test_close_stops_intake_and_cancels_work_stuck_past_the_timeout():
    handler = Recorder(block=True)
    scheduler = FairScheduler(handler, workers=2)
    scheduler.start()
    submit_all(scheduler, [("A", 0), ("B", 0)])
    await asyncio.sleep(0.01)

    await asyncio.wait_for(scheduler.close(grace_seconds=0.05), 1)

    assert sorted(handler.finished) == [("A", 0), ("B", 0)]  # cancelled, cleanup ran
    with pytest.raises(SchedulerClosed):
        scheduler.submit("A", ("A", 1))


async def test_close_lets_queued_work_finish_within_the_timeout():
    handler = Recorder(delay=0.01)
    scheduler = FairScheduler(handler, workers=1)
    scheduler.start()
    submit_all(scheduler, [("A", 0), ("A", 1), ("B", 0)])

    await scheduler.close(grace_seconds=2)

    assert len(handler.finished) == 3


async def test_drain_waits_while_work_is_in_flight():
    scheduler = FairScheduler(Recorder(block=True), workers=1)
    scheduler.start()
    scheduler.submit("A", ("A", 0))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(scheduler.drain(), 0.02)
    await scheduler.close(grace_seconds=0.01)


def test_pool_size_and_limits_are_validated():
    with pytest.raises(ValueError):
        FairScheduler(Recorder(), workers=0)
    with pytest.raises(ValueError):
        FairScheduler(Recorder(), max_pending_per_sender=0)
