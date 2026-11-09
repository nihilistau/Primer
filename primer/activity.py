"""Telling Kairos what you read: Primer's half of Kairos's app-activity record.

Kairos (the companion stack) reads `activity/<app>/<YYYY-MM-DD>.jsonl` beside her memory
registry; the contract is `harness/model/activity.py` there. Primer appends one JSON line per
reading signal (a page opened, Deeper or Simpler, Regenerate, a quiz answer) to `primer/` in that
directory, so she can say "you were reading about kookaburras" without scraping Primer. The row's
`ref` is the page's node id, which the Primer MCP tool (`primer.mcp_server`, action `page`) reads.

OFF UNLESS POINTED AT A DIRECTORY: `PRIMER_ACTIVITY_DIR` (Kairos's `var/memory/activity`). Unset,
nothing is written and nothing changes. `PRIMER_ACTIVITY_PRIVATE=1` writes every row with
`"private": true`, which Kairos never reads: Primer's off-the-record switch.

Never breaks reading: a failed write is logged and dropped, like a failed signal.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

APP = "primer"
EVENT_MAX = 40
TITLE_MAX = 200
# reading-signal kind -> Kairos event name (the detail refines depth and answer)
_EVENTS = {"open": "open_page", "regenerate": "regenerate_page"}


def event_name(kind: str, detail: str = "") -> str:
    if kind in _EVENTS:
        return _EVENTS[kind]
    if kind == "depth":
        return f"{detail or 'other'}_page"          # deeper_page / simpler_page
    if kind == "answer":
        return "quiz_" + (detail or "answered").replace(" ", "_")   # quiz_right / quiz_partly / quiz_not_yet
    return kind


class ActivitySink:
    """Appends rows for Kairos. One instance per App; thread-safe."""

    def __init__(self, root: str | Path, *, private: bool = False, clock=time.time):
        self.dir = Path(root) / APP
        self.private = private
        self.clock = clock
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "ActivitySink | None":
        root = os.environ.get("PRIMER_ACTIVITY_DIR", "").strip()
        if not root:
            return None
        return cls(root, private=os.environ.get("PRIMER_ACTIVITY_PRIVATE") == "1")

    def note(self, kind: str, title: str, node_id: str | None = None, detail: str = "") -> bool:
        title = " ".join((title or "").split())[:TITLE_MAX]
        if not title:
            return False
        now = self.clock()
        row = {"at": int(now * 1000), "event": event_name(kind, detail)[:EVENT_MAX], "title": title}
        if node_id:
            row["ref"] = str(node_id)[:TITLE_MAX]
        if self.private:
            row["private"] = True
        day = _dt.date.fromtimestamp(now).isoformat()   # the local day, as Kairos's transcripts
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                with open(self.dir / f"{day}.jsonl", "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return True
        except OSError:
            log.exception("writing a Kairos activity row failed")
            return False
