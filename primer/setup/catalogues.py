"""One object over the three catalogue clients, so callers (and test fakes) have a single seam."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from primer.config import REPO_ROOT
from primer.setup import github, hf, kiwixcat
from primer.setup.net import Net

DEFAULT_TOKEN_FILE = Path(REPO_ROOT) / "var" / "keys" / "huggingface.key"
# GitHub allows 60 unauthenticated requests an hour: a releases listing is kept this long, so
# swapping or skipping items (each a new plan) and the update check don't use them up
GITHUB_TTL_S = 600.0


class Catalogues:
    def __init__(self, net: Net | None = None, token_file=None, clock=time.monotonic) -> None:
        self.net = net or Net()
        self.token_file = Path(token_file) if token_file else DEFAULT_TOKEN_FILE
        self._clock = clock
        self._releases: dict[str, tuple[float, list]] = {}  # repo -> (expires, listing)
        self._lock = threading.Lock()

    def token(self) -> str | None:
        try:
            return self.token_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def hf_files(self, repo: str) -> list[dict]:
        return hf.files(repo, self.token(), net=self.net)

    def hf_card(self, repo: str) -> dict:
        return hf.card(repo, self.token(), net=self.net)

    def zims(self, lang: str = "eng", name_prefix: str = "wikipedia_en_") -> list[dict]:
        return kiwixcat.zims(lang, name_prefix, net=self.net)

    def github_releases(self, repo: str) -> list[dict]:
        """github.releases(repo), cached for GITHUB_TTL_S (a failure is not cached)."""
        with self._lock:
            hit = self._releases.get(repo)
            if hit is not None and hit[0] > self._clock():
                return hit[1]
        listing = github.releases(repo, net=self.net)
        with self._lock:
            self._releases[repo] = (self._clock() + GITHUB_TTL_S, listing)
        return listing

    def latest_asset(self, repo: str, pattern: str, prefer_tag_prefix: str | None = None) -> dict:
        return github.latest_asset(repo, pattern, prefer_tag_prefix=prefer_tag_prefix,
                                   listing=self.github_releases(repo))
