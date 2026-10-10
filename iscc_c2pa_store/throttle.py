"""Rate limits per client that are safe across threads: fixed time windows counted atomically in the cache.

django-ninja's own throttles keep per-request state on shared instances and share one cache key between all
anonymous limits, so reads would count against uploads. Here each limit has its own scope and setting, the count is
one atomic cache increment, and the wait is derived from the clock alone. A client is its address, or its /64 network
for IPv6, where one subscriber usually holds a whole /64. Addresses listed in `ISCC_C2PA_STORE_UNTHROTTLED` (the
operator's own resolvers) are not limited.
"""

import ipaddress
import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest  # noqa: F401 (type comments)
from ninja.throttling import BaseThrottle

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_rate(rate):
    # type: (str) -> tuple[int, int]
    """Parse a rate like `10/m` into (requests, window seconds); the unit is the first letter of s, m, h or d.

    :raises ValueError: naming the rate, if it does not have that form.
    """
    count, _, unit = rate.partition("/")
    try:
        return int(count), UNITS[unit.strip()[:1].lower()]
    except (KeyError, ValueError):
        raise ValueError(f"Invalid rate {rate!r}: use <requests>/<s|m|h|d>, e.g. 10/m") from None


def normal(address):
    # type: (str) -> str
    """The canonical text of an IP address (lower case, compressed IPv6), or the text itself if it is none."""
    try:
        return str(ipaddress.ip_address(address.strip()))
    except ValueError:
        return address


def client_key(address):
    # type: (str) -> str
    """The rate limit key of a client address: the address itself, the /64 network for IPv6."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv4Address):
        return str(ip)
    if ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ipaddress.IPv6Network((ip, 64), strict=False))


class WindowThrottle(BaseThrottle):
    """Allow a number of requests per client in each fixed time window, counted per scope.

    The rate is read from the named setting on every request, so it follows the configuration.
    """

    def __init__(self, scope, setting):
        # type: (str, str) -> None
        self.scope = scope
        self.setting = setting

    def rate(self):
        # type: () -> tuple[int, int]
        """The configured (requests, window seconds)."""
        return parse_rate(getattr(settings, self.setting))

    def allow_request(self, request):
        # type: (HttpRequest) -> bool
        """Count this request in the current window of its client and tell whether it is within the limit."""
        address = self.get_ident(request) or ""
        if normal(address) in {normal(exempt) for exempt in settings.ISCC_C2PA_STORE_UNTHROTTLED}:
            return True
        limit, window = self.rate()
        key = f"throttle:{self.scope}:{client_key(address)}:{int(time.time() // window)}"
        cache.add(key, 0, window)
        try:
            return cache.incr(key) <= limit
        except ValueError:  # the window expired between add and incr
            cache.set(key, 1, window)
            return True

    def wait(self):
        # type: () -> float
        """Seconds until the current window ends."""
        window = self.rate()[1]
        return window - time.time() % window
