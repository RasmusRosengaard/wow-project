"""Tests for blizz.py's pure parsing logic — no network involved."""
import blizz


class _FakeClock:
    """Deterministic stand-in for time.monotonic()/time.sleep() -- lets
    _TokenBucket tests assert exact wait durations without a real test
    actually sleeping (would make the suite slow and timing-flaky)."""
    def __init__(self, start: float = 0.0):
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_token_bucket_allows_burst_up_to_capacity(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(blizz.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(blizz.time, "sleep", clock.sleep)
    bucket = blizz._TokenBucket(capacity=3, refill_per_second=1)
    # A fresh bucket should let `capacity` calls through with no waiting at all.
    for _ in range(3):
        bucket.acquire()
    assert clock.slept == []


def test_token_bucket_blocks_and_refills_at_the_configured_rate(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(blizz.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(blizz.time, "sleep", clock.sleep)
    bucket = blizz._TokenBucket(capacity=2, refill_per_second=1)
    bucket.acquire()
    bucket.acquire()  # bucket now empty (0 tokens)
    bucket.acquire()  # must wait exactly 1s at refill_per_second=1 for a token
    assert clock.slept == [1.0]


def test_token_bucket_never_exceeds_capacity_even_after_a_long_idle_period(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(blizz.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(blizz.time, "sleep", clock.sleep)
    bucket = blizz._TokenBucket(capacity=2, refill_per_second=1)
    bucket.acquire()
    bucket.acquire()
    clock.now += 1000  # a long idle gap -- tokens must cap at `capacity`, not overflow
    # If this refilled unboundedly, a huge idle gap would let an equally huge
    # burst through immediately afterward -- capacity is the whole point of
    # "burst allowance," not an unlimited banked credit.
    bucket.acquire()
    bucket.acquire()
    assert clock.slept == []  # both immediately available -- capacity was 2, not unbounded
    bucket.acquire()  # a third call now must wait again
    assert clock.slept == [1.0]


def test_api_get_acquires_both_rate_limiters_before_making_a_request(monkeypatch):
    """The real incident this was added for (2026-08-01): every caller of
    api_get() -- the collector, the region scanner, NameCache,
    AppearanceCache, or a one-off script -- must share the same throttling,
    since none of them otherwise have any notion of "how much of the
    shared Blizzard budget is already spent by something else right now.\""""
    calls = []
    monkeypatch.setattr(blizz._burst_limiter, "acquire", lambda: calls.append("burst"))
    monkeypatch.setattr(blizz._hourly_limiter, "acquire", lambda: calls.append("hourly"))
    monkeypatch.setattr(blizz, "get_token", lambda: "fake-token")

    class _FakeGetResponse:
        pass

    def fake_get(*a, **k):
        # both limiters must already have been consulted by the time the
        # real HTTP call actually fires
        assert calls == ["burst", "hourly"]
        return _FakeGetResponse()

    monkeypatch.setattr(blizz.requests, "get", fake_get)
    blizz.api_get("/data/wow/some/path", "dynamic")
    assert calls == ["burst", "hourly"]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def test_list_connected_realms_extracts_ids_from_hrefs(monkeypatch):
    payload = {"connected_realms": [
        {"href": "https://eu.api.blizzard.com/data/wow/connected-realm/1403?namespace=dynamic-eu"},
        {"href": "https://eu.api.blizzard.com/data/wow/connected-realm/1096?namespace=dynamic-eu"},
    ]}
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse(payload))
    assert blizz.list_connected_realms() == [1403, 1096]


def test_list_connected_realms_handles_empty(monkeypatch):
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse({}))
    assert blizz.list_connected_realms() == []


def test_connected_realm_realms_handles_multi_realm_cluster(monkeypatch):
    payload = {"realms": [{"name": "Silvermoon", "slug": "silvermoon", "category": "English"},
                          {"name": "Die Aldor", "slug": "die-aldor", "category": "English"}]}
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse(payload))
    assert blizz.connected_realm_realms(1096) == [
        {"name": "Silvermoon", "slug": "silvermoon", "category": "English"},
        {"name": "Die Aldor", "slug": "die-aldor", "category": "English"},
    ]


def test_connected_realm_realms_extracts_name_slug_and_category(monkeypatch):
    payload = {"realms": [{"name": "Draenor", "slug": "draenor", "category": "English"}]}
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse(payload))
    assert blizz.connected_realm_realms(1403) == [{"name": "Draenor", "slug": "draenor", "category": "English"}]


def test_connected_realm_population_extracts_type(monkeypatch):
    payload = {"population": {"type": "FULL", "name": "Full"}}
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse(payload))
    assert blizz.connected_realm_population(1403) == "FULL"


def test_connected_realm_population_handles_missing_field(monkeypatch):
    monkeypatch.setattr(blizz, "api_get", lambda *a, **k: FakeResponse({}))
    assert blizz.connected_realm_population(1403) is None
