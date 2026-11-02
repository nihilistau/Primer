"""The map lens: places on a page, pinned on an offline PMTiles vector map."""
from __future__ import annotations

from dataclasses import dataclass

from primer.version import VERSION

# what Primer calls itself to the map build server (and to Wikipedia): Cloudflare in front of
# build.protomaps.com refuses Python's default User-Agent
USER_AGENT = f"Primer/{VERSION} (https://github.com/nihilistau/Primer)"


@dataclass(frozen=True)
class Place:
    """A place a page names. lat/lon stay None until a Wikipedia article gave coordinates."""
    name: str
    what: str = ""
    lat: float | None = None
    lon: float | None = None
    title: str = ""
    source: str = ""  # "reference", "web", or "" (not located)
