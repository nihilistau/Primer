"""Which requests the live browser may make: public http(s) hosts on the guarding proxy's
ports, plus data:, blob: and about:blank. The host rules are primer.grounding.web._check_host's (non-global addresses,
IPv4 embedded in IPv6, names resolving to either), so there is one copy of them."""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse

from primer.grounding.web import WebError, _check_host

_INERT = ("data", "blob")  # content the page already holds; no network request is made
_HOSTED = ("http", "https", "ws", "wss")
_DEFAULT_PORT = {"http": 80, "ws": 80, "https": 443, "wss": 443}
# The ports the guarding proxy connects to (proxy.py uses this same set). Checking them here
# too gives an off-list port the clear "can't be opened live" refusal, not a tunnel failure.
ALLOWED_PORTS = frozenset({80, 443, *range(8080, 8444)})


def _local_name(host: str) -> bool:
    """Names Chrome or the OS send to this machine or the local network without public DNS:
    localhost and *.localhost (Chrome maps them to loopback itself), *.local (mDNS), and
    single-label names (LLMNR/NetBIOS). Refused before any lookup, since a resolver that
    rewrites unknown names to a public address would otherwise make them look public."""
    name = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
        return False  # an address literal: _check_host judges it
    except ValueError:
        pass
    return (name == "localhost" or name.endswith(".localhost") or name == "local"
            or name.endswith(".local") or "." not in name)


def allowed(url: str, resolve=socket.getaddrinfo) -> bool:
    """True only for a URL the live browser may load. Anything unclear is refused."""
    if not isinstance(url, str) or not url or len(url) > 1_000_000:
        return False
    try:
        u = urllib.parse.urlsplit(url.strip())
        scheme = u.scheme.lower()
        if scheme in _INERT:
            return True
        if scheme == "about":
            return u.path == "blank"
        if scheme not in _HOSTED:
            return False  # file:, chrome:, edge:, javascript:, ftp: ...
        host = u.hostname or ""
        port = u.port  # raises ValueError on a bad port
    except ValueError:
        return False
    if (_DEFAULT_PORT[scheme] if port is None else port) not in ALLOWED_PORTS:
        return False
    if _local_name(host):
        return False
    try:
        _check_host(host, resolve)
    except WebError:  # WebRefused (not public) and WebError (cannot resolve) alike
        return False
    except Exception:
        return False
    return True
