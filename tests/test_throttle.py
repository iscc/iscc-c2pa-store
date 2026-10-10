"""Tests of the rate limits: rate parsing, client keys, fixed windows per scope and client, the wait until a reset,
and the clients without limits.

Rates come from the settings named by each throttle, so tests set them with the pytest-django `settings` fixture.
Counters live in the Django cache of the test settings (local memory); the clock is fixed where a test needs exact
window boundaries.
"""

import pytest
from django.test import RequestFactory

from iscc_c2pa_store import throttle
from iscc_c2pa_store.throttle import WindowThrottle, client_key, normal, parse_rate

SETTING = "ISCC_C2PA_STORE_RATE_WRITE"


@pytest.fixture
def clock(monkeypatch):
    # type: (pytest.MonkeyPatch) -> list[float]
    """Fix the clock at the start of a minute; set clock[0] to move it."""
    now = [6000.0]
    monkeypatch.setattr(throttle.time, "time", lambda: now[0])
    return now


def request(address="203.0.113.1"):
    # type: (str) -> object
    """A request from a client address."""
    return RequestFactory().get("/v1/services/status", REMOTE_ADDR=address)


def allowed(limit, count, address="203.0.113.1"):
    # type: (WindowThrottle, int, str) -> list[bool]
    """Send a number of requests through a limit and return whether each was allowed."""
    return [limit.allow_request(request(address)) for _ in range(count)]


@pytest.mark.parametrize(
    ("rate", "parsed"),
    [
        ("10/m", (10, 60)),
        ("5/s", (5, 1)),
        ("100/h", (100, 3600)),
        ("1/d", (1, 86400)),
        ("120/minute", (120, 60)),
        ("3/Hour", (3, 3600)),
        ("7/ sec", (7, 1)),
    ],
)
def test_parse_rate(rate, parsed):
    assert parse_rate(rate) == parsed


@pytest.mark.parametrize("rate", ["10/x", "10", "10/"])
def test_parse_rate_unknown_unit(rate):
    with pytest.raises(ValueError, match="Invalid rate"):
        parse_rate(rate)


def test_parse_rate_invalid_count():
    with pytest.raises(ValueError, match="Invalid rate"):
        parse_rate("ten/m")


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:DB8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("::1", "::/64"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("", ""),
        ("unknown", "unknown"),
        ("203.0.113.1, 198.51.100.2", "203.0.113.1, 198.51.100.2"),
    ],
)
def test_client_key(address, key):
    assert client_key(address) == key


def test_throttle_names_scope_and_setting():
    limit = WindowThrottle("read", "ISCC_C2PA_STORE_RATE_READ")
    assert (limit.scope, limit.setting) == ("read", "ISCC_C2PA_STORE_RATE_READ")


def test_throttle_reads_rate_from_setting(settings):
    limit = WindowThrottle("write", SETTING)
    settings.ISCC_C2PA_STORE_RATE_WRITE = "120/m"
    assert limit.rate() == (120, 60)
    settings.ISCC_C2PA_STORE_RATE_WRITE = "5/h"
    assert limit.rate() == (5, 3600)


def test_invalid_rate_setting_fails_on_request(settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "ten/m"
    with pytest.raises(ValueError, match="Invalid rate 'ten/m'"):
        WindowThrottle("write", SETTING).allow_request(request())


def test_allows_up_to_limit_in_window(clock, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "3/m"
    assert allowed(WindowThrottle("write", SETTING), 5) == [True, True, True, False, False]


def test_limit_follows_setting_between_requests(clock, settings):
    limit = WindowThrottle("write", SETTING)
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/m"
    assert allowed(limit, 2) == [True, False]
    settings.ISCC_C2PA_STORE_RATE_WRITE = "3/m"
    assert allowed(limit, 2) == [True, False]  # the third request of the window is within 3/m, the fourth is not


def test_counts_per_client_address(clock, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/m"
    limit = WindowThrottle("write", SETTING)
    assert allowed(limit, 2, "203.0.113.1") == [True, False]
    assert allowed(limit, 2, "203.0.113.2") == [True, False]


def test_ipv6_clients_are_counted_per_64(clock, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "2/m"
    limit = WindowThrottle("write", SETTING)
    assert allowed(limit, 1, "2001:db8:1:2::1") == [True]
    assert allowed(limit, 2, "2001:db8:1:2::abcd") == [True, False]  # another address of the same /64
    assert allowed(limit, 2, "2001:db8:1:3::1") == [True, True]  # the next /64 has its own count


def test_ipv4_mapped_clients_share_the_ipv4_count(clock, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/m"
    limit = WindowThrottle("write", SETTING)
    assert allowed(limit, 1, "203.0.113.1") == [True]
    assert allowed(limit, 1, "::ffff:203.0.113.1") == [False]


def test_counts_per_scope(clock, settings):
    settings.ISCC_C2PA_STORE_RATE_READ = "1/m"
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/m"
    reads = WindowThrottle("read", "ISCC_C2PA_STORE_RATE_READ")
    writes = WindowThrottle("write", SETTING)
    assert allowed(reads, 2) == [True, False]
    assert allowed(writes, 2) == [True, False]


def test_instances_of_one_scope_share_the_count(clock, settings):
    # Counts live in the cache, not on the instance, so threads and routes with the same scope share them.
    settings.ISCC_C2PA_STORE_RATE_WRITE = "2/m"
    assert allowed(WindowThrottle("write", SETTING), 2) == [True, True]
    assert allowed(WindowThrottle("write", SETTING), 1) == [False]


def test_next_window_starts_a_fresh_count(clock, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/m"
    limit = WindowThrottle("write", SETTING)
    assert allowed(limit, 2) == [True, False]
    clock[0] = 6059.5
    assert allowed(limit, 1) == [False]
    clock[0] = 6060.0
    assert allowed(limit, 2) == [True, False]


def test_unthrottled_addresses_are_never_limited(clock, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/m"
    settings.ISCC_C2PA_STORE_UNTHROTTLED = ["203.0.113.9", "2001:db8:1:2::9"]
    limit = WindowThrottle("write", SETTING)
    assert allowed(limit, 3, "203.0.113.9") == [True, True, True]
    assert allowed(limit, 3, "2001:db8:1:2::9") == [True, True, True]
    assert allowed(limit, 2, "203.0.113.1") == [True, False]


def test_unthrottled_requests_are_not_counted(clock, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/m"
    settings.ISCC_C2PA_STORE_UNTHROTTLED = ["2001:db8:1:2::9"]
    limit = WindowThrottle("write", SETTING)
    assert allowed(limit, 3, "2001:db8:1:2::9") == [True, True, True]
    # Another address of the same /64 is limited, and has its full limit left.
    assert allowed(limit, 2, "2001:db8:1:2::1") == [True, False]


def test_wait_is_time_left_in_window(clock, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/m"
    limit = WindowThrottle("write", SETTING)
    assert limit.wait() == 60
    clock[0] = 6045.0
    assert limit.wait() == 15
    clock[0] = 6059.5
    assert limit.wait() == 0.5


def test_wait_follows_the_window_of_the_setting(clock, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/h"
    assert WindowThrottle("write", SETTING).wait() == 3600 - 6000 % 3600


def test_wait_with_real_clock_is_within_window(settings):
    settings.ISCC_C2PA_STORE_RATE_READ = "1/h"
    assert 0 < WindowThrottle("read", "ISCC_C2PA_STORE_RATE_READ").wait() <= 3600


def test_counter_expired_between_add_and_incr_allows(settings):
    # A cache that keeps nothing behaves like a counter that expired right after `add`: `incr` finds no key.
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/m"
    assert allowed(WindowThrottle("write", SETTING), 3) == [True, True, True]


@pytest.mark.parametrize(
    ("address", "canonical"),
    [("2001:0DB8::0009", "2001:db8::9"), (" 192.0.2.1 ", "192.0.2.1"), ("not-an-address", "not-an-address")],
)
def test_normal(address, canonical):
    assert normal(address) == canonical
