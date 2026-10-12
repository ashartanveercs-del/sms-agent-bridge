from fakes import FakeClock

from smsbridge.dedupe import DedupeCache


def test_first_claim_wins_and_repeats_are_rejected():
    cache = DedupeCache()
    assert cache.claim("SM1")
    assert not cache.claim("SM1")
    assert cache.claim("SM2")
    assert "SM1" in cache


def test_release_lets_a_retry_through():
    # claim -> hand-off fails -> release, so Twilio's retry or fallback URL can deliver it
    cache = DedupeCache()
    assert cache.claim("SM1")
    cache.release("SM1")
    assert cache.claim("SM1")


def test_entries_expire_after_the_ttl():
    clock = FakeClock()
    cache = DedupeCache(ttl_seconds=60, clock=clock)
    cache.claim("SM1")
    clock.advance(59)
    assert not cache.claim("SM1")
    clock.advance(2)
    assert cache.claim("SM1")


def test_capacity_evicts_the_oldest_entry_one_at_a_time():
    cache = DedupeCache(max_entries=3)
    for sid in ("SM1", "SM2", "SM3", "SM4"):
        assert cache.claim(sid)
    assert len(cache) == 3
    assert "SM1" not in cache
    # everything newer is still remembered; emptying the whole cache at capacity would
    # forget recent SIDs and answer their retries a second time
    assert not cache.claim("SM2")
    assert not cache.claim("SM4")


def test_empty_keys_are_never_treated_as_duplicates():
    cache = DedupeCache()
    assert cache.claim("")
    assert cache.claim("")
    assert len(cache) == 0
