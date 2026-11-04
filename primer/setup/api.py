"""The logic behind Setup's HTTP routes (/v1/setup/*); primer/server.py does the routing.

Every method returns a JSON-ready value or raises ApiError(status, reason): 400 for a bad
request or a refusal (the installer's, kit's, catalogue's or mover's own sentence), 409 when the
state forbids it right now ("already running", a move in progress), 503 when Primer was started
without Setup. Approvals are called without holding app._lock (an approval starts the servers
waiting for it, under that lock, on its own thread).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from primer.config import REPO_ROOT
from primer.setup import mover
from primer.setup.approvals import Approvals, norm
from primer.setup.installer import SetupConflict, SetupError
from primer.setup.kits import KitError, fits, kit_size_hint, space_needed, suggest_kit
from primer.setup.net import CatalogueError
from primer.setup.tierwriter import LOCAL_NAME

log = logging.getLogger(__name__)

UPDATES_TTL_S = 600.0
UPDATES_ERROR_TTL_S = 60.0  # a check that failed somewhere is tried again sooner
NO_SETUP = "Setup isn't available in this Primer"
SETUP_BUSY = "Setup is downloading or finishing; pause it and let it stop, or let it finish, before moving models"
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


class ApiError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _setup_error(e: Exception) -> ApiError:
    reason = getattr(e, "reason", None) or str(e)
    return ApiError(409 if isinstance(e, (SetupConflict, mover.MoveConflict)) else 400, reason)


def _str_field(body: dict, name: str, what: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value or "\0" in value:
        raise ApiError(400, f"{name} is required ({what})")
    return value


class SetupApi:
    def __init__(self, app, *, drives: Callable[[], list[str]] = mover.fixed_drives,
                 drive_type: Callable[[str], int | None] = mover.drive_type,
                 disk_free: Callable[[str], int] = lambda p: shutil.disk_usage(p).free,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self._drives = drives
        self._drive_type = drive_type
        self._disk_free = disk_free
        self._clock = clock
        self._lock = threading.Lock()  # start/resume vs a move: never both at once
        self._updates_lock = threading.Lock()  # guards _updates
        self._updates_run = threading.Lock()  # one uncached check at a time; the others wait for it
        self._updates: tuple | None = None  # (expires, key, rows, computed at)
        self.job = mover.MoveJob()

    def _inst(self):
        inst = self.app.installer
        if inst is None:
            raise ApiError(503, NO_SETUP)
        return inst

    # ── where to put things ───────────────────────────────────────────────────────────────────

    def root_default(self) -> Path:
        """primer-data on the fixed drive with the most free space; never inside the repo."""
        best, most = None, -1
        for d in self._drives():
            try:
                free = self._disk_free(d.rstrip("\\") + "\\")
            except OSError:
                continue
            if free > most:
                best, most = d, free
        root = Path(best.rstrip("\\") + "\\") / "primer-data" if best else Path(REPO_ROOT).parent / "primer-data"
        if mover.inside(root, REPO_ROOT):
            root = Path(REPO_ROOT).parent / "primer-data"
        return root

    def check_root(self, root) -> Path:
        """The destination as a Path, or ApiError(400) saying what is wrong with it."""
        if not isinstance(root, str) or not root or "\0" in root:
            raise ApiError(400, "the destination must be an absolute folder")
        if mover.is_unc(root) or mover.is_unc(mover.plain(root)):
            raise ApiError(400, "that is a network or device path; choose a folder on a local drive")
        if not Path(root).is_absolute():
            raise ApiError(400, "the destination must be an absolute folder")
        p = Path(os.path.abspath(root))  # normalised: C:\Windows\.. is the drive itself
        if p.parent == p:
            raise ApiError(400, "choose a folder on the drive, not the drive itself (for example primer-data)")
        if mover.inside(p, REPO_ROOT):
            raise ApiError(400, "the destination is inside the Primer folder; choose another")
        if mover.inside(REPO_ROOT, p):
            raise ApiError(400, "the destination holds the Primer folder; choose a folder of its own")
        kind = self._drive_type(str(p))
        if kind == mover.DRIVE_REMOTE:
            raise ApiError(400, "that is a network drive, too slow for models; choose a local drive")
        if mover.removable(kind):
            raise ApiError(400, "that is a removable drive (slow, and it may be unplugged); choose a fixed drive")
        if not p.parent.is_dir():
            raise ApiError(400, f"the folder {p.parent} does not exist")
        if p.exists() and not p.is_dir():
            raise ApiError(400, f"{p} is a file, not a folder")
        return p

    # ── GET /v1/setup ─────────────────────────────────────────────────────────────────────────

    def overview(self) -> dict:
        inst = self._inst()
        try:
            data = inst.manifest()
        except KitError as e:
            raise ApiError(500, str(e)) from None
        hw = self.app.hardware
        kit_id, reason = suggest_kit(hw, data)
        kits = [{"id": k["id"], "label": k["label"], "summary": k.get("summary", ""),
                 "size_hint": kit_size_hint(k, data)} for k in data["kits"]]
        default = self.root_default()
        state = inst.status()
        try:
            free = inst.free_space(state["root"] or default)
        except OSError:
            free = None
        token = (Path(self.app.var_dir) / "keys" / "huggingface.key").is_file()
        return {"hardware": {"gpu": hw.gpu_name, "vram_gb": hw.vram_gb, "ram_gb": hw.ram_gb},
                "suggested": {"kit": kit_id, "reason": reason}, "kits": kits, "root_default": str(default),
                "state": state, "free_bytes": free, "token": token}

    # ── the run ───────────────────────────────────────────────────────────────────────────────

    def plan(self, body: dict) -> dict:
        inst = self._inst()
        kit = _str_field(body, "kit", "a kit id")
        choices = body.get("choices")
        if choices is not None and not (isinstance(choices, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in choices.items())):
            raise ApiError(400, "choices must map item ids to choice ids")
        root = self.check_root(body.get("root"))
        try:
            plan = inst.plan(kit, choices, root)
        except (SetupError, KitError, CatalogueError) as e:
            raise _setup_error(e) from None
        try:
            free = inst.free_space(root)
        except OSError as e:
            raise ApiError(400, f"cannot check the free space on {root}: {e}") from None
        ok, short = fits(plan, free)
        return {**plan.to_json(), "fits": {"ok": ok, "shortfall_bytes": short, "free_bytes": free,
                                           "needed_bytes": space_needed(plan)}}

    def _run(self, action: str, args: tuple = ()) -> dict:
        inst = self._inst()
        with self._lock:
            if self.job.busy():
                raise ApiError(409, "a model move is running; wait for it to finish")
            root = inst.status()["root"]
            if root:
                self.check_root(root)
            try:
                getattr(inst, action)(*args)
            except (SetupError, KitError, CatalogueError) as e:
                raise _setup_error(e) from None
        if root:
            try:  # the worker creates what it needs as well; this makes the choice visible now
                Path(root).mkdir(exist_ok=True)
            except OSError:
                pass
        return inst.status()

    def start(self, body: dict | None = None) -> dict:
        """Start the checked plan. The body names the plan the window showed ({kit, root,
        choices}); if the pending plan is another one: 409, check it again."""
        self._inst()
        body = body if isinstance(body, dict) else {}
        kit = _str_field(body, "kit", "the kit the plan was checked for")
        root = self.check_root(body.get("root"))
        choices = body.get("choices") if body.get("choices") is not None else {}
        if not (isinstance(choices, dict) and all(isinstance(k, str) and isinstance(v, str)
                                                  for k, v in choices.items())):
            raise ApiError(400, "choices must map item ids to choice ids")
        return self._run("start", (kit, root, choices))

    def resume(self) -> dict:
        return self._run("resume")

    def pause(self) -> dict:
        inst = self._inst()
        inst.pause()
        return inst.status()

    def cancel(self, body: dict) -> dict:
        inst = self._inst()
        item = _str_field(body, "item", "the item id to skip")
        try:
            inst.cancel(item)
        except SetupError as e:
            raise _setup_error(e) from None
        return inst.status()

    def licence(self, body: dict) -> dict:
        inst = self._inst()
        # the licence's `key` as the plan's licences list gives it (name and link)
        name = _str_field(body, "licence", "the licence to accept: its key, as status() lists it")
        try:
            inst.acknowledge(name)
        except SetupError as e:
            raise _setup_error(e) from None
        return inst.status()

    # ── approvals ─────────────────────────────────────────────────────────────────────────────

    def approve(self, body: dict) -> dict:
        inst = self._inst()
        path = _str_field(body, "path", "the file to approve")
        sha = body.get("sha256")
        if not isinstance(sha, str) or not _HEX64.fullmatch(sha):
            raise ApiError(400, "sha256 is required: the SHA-256 Setup showed for this file")
        try:
            pending = inst.approvals.pending()
        except OSError as e:
            raise ApiError(400, f"cannot read var/setup.json: {e}") from None
        key = norm(path)
        if not any(r["path"] == key for r in pending):
            raise ApiError(400, "nothing at that path is waiting for approval")
        try:
            ok = inst.approvals.approve(key, sha)
        except OSError:
            ok = False
        if not ok:
            raise ApiError(400, "the file changed since it was shown (or is missing); check it again")
        return {"approved": [key], "refused": [], "state": inst.status()}

    def approve_release(self, body: dict) -> dict:
        inst = self._inst()
        item = body.get("item")
        try:
            out = inst.approve_release(item)
        except SetupError as e:
            raise _setup_error(e) from None
        return {**out, "state": inst.status()}

    # ── updates ───────────────────────────────────────────────────────────────────────────────

    def updates(self, refresh: bool = False) -> list[dict]:
        """Newer ZIMs and runtime builds for what is installed (never installed automatically).
        A catalogue that fails gives its items an `error`. Cached for UPDATES_TTL_S."""
        inst = self._inst()
        st = inst.status()
        states = {i["id"]: i["state"] for i in st["items"]}
        items = [it for it in ((st["plan"] or {}).get("items") or [])
                 if states.get(it["id"]) in ("done", "present")]
        key = (st["kit"], tuple((it["id"], json.dumps(it.get("origin") or {}, sort_keys=True)) for it in items))
        asked = self._clock()

        def cached(fresh_since: float | None) -> list[dict] | None:
            with self._updates_lock:
                c = self._updates
            if c is None or c[1] != key or c[0] <= self._clock():
                return None
            if fresh_since is not None and c[3] < fresh_since:
                return None
            return [dict(r) for r in c[2]]

        hit = None if refresh else cached(None)
        if hit is not None:
            return hit
        with self._updates_run:  # concurrent requests wait for the one in flight and share it
            hit = cached(asked) if refresh else cached(None)
            if hit is not None:
                return hit
            started = self._clock()
            rows = self._check_updates(inst, st["kit"], items)
            ttl = UPDATES_ERROR_TTL_S if any("error" in r for r in rows) else UPDATES_TTL_S
            with self._updates_lock:
                self._updates = (self._clock() + ttl, key, rows, started)
        return [dict(r) for r in rows]

    @staticmethod
    def _reason(e: Exception) -> str:
        return getattr(e, "reason", None) or str(e) or type(e).__name__

    def _check_updates(self, inst, kit_id, items: list[dict]) -> list[dict]:
        cat = inst.catalogues
        try:
            kit = next((k for k in inst.manifest()["kits"] if k["id"] == kit_id), {})
        except KitError:
            kit = {}
        manifest_items = {i["id"]: i for i in kit.get("items", [])}
        rows: list[dict] = []
        zims, zim_error = None, ""
        for it in items:
            origin = it.get("origin") or {}
            base = {"item": it["id"], "label": it.get("label", ""), "kind": it.get("kind", "")}
            if it.get("kind") == "zim" and origin.get("book"):
                installed = origin.get("date", "")
                if not isinstance(installed, str) or not installed:
                    rows.append({**base, "installed": "unknown", "latest": None, "size": None,
                                 "error": "installed date unknown"})
                    continue
                if zims is None and not zim_error:
                    try:
                        zims = cat.zims()
                    except Exception as e:  # never a 500: the row says what failed
                        zim_error = self._reason(e)
                if zim_error:
                    rows.append({**base, "installed": installed, "latest": None, "size": None, "error": zim_error})
                    continue
                newer = [z for z in zims if z.get("name") == origin["book"]
                         and z.get("flavour") == origin.get("flavour") and str(z.get("date", "")) > installed]
                if newer:
                    z = max(newer, key=lambda z: str(z.get("date", "")))
                    rows.append({**base, "installed": installed, "latest": z["date"], "size": z.get("size")})
            elif it.get("kind") == "runtime" and origin.get("repo") and origin.get("tag") and not it.get("use_path"):
                src = manifest_items.get(it["id"], {})
                ch = next((c for c in src.get("choices", []) if c.get("id") == it.get("choice")), {})
                pattern = ch.get("pattern")
                if not pattern:
                    continue
                try:
                    a = cat.latest_asset(origin["repo"], pattern)
                except Exception as e:
                    rows.append({**base, "installed": origin["tag"], "latest": None, "size": None,
                                 "error": self._reason(e)})
                    continue
                if a.get("tag") and a["tag"] != origin["tag"]:
                    rows.append({**base, "installed": origin["tag"], "latest": a["tag"], "size": a.get("size")})
        return rows

    # ── moving models ─────────────────────────────────────────────────────────────────────────

    def _owned(self) -> Callable[[str], bool]:
        """Is a file Setup's (under a recorded root, the pending plan's root, or a marked folder)?"""
        inst = self.app.installer
        approvals = getattr(inst, "approvals", None) or Approvals(self.app.var_dir)
        extra = []
        root = getattr(inst, "root", None)
        if callable(root):
            extra = [r for r in [root()] if r]
        return approvals.owned_check(extra)

    def _reloads(self) -> bool:
        """The current tier is Setup's own: moving its files restarts its servers."""
        name = self.app.tier.name
        return bool(LOCAL_NAME.fullmatch(name)) and (Path(self.app.tiers_dir) / f"{name}.toml").is_file()

    def _listing(self) -> list[dict]:
        return mover.list_models(self.app.tier, self.app.store, drive_type=self._drive_type,
                                 owned=self._owned(), reloads=self._reloads())

    def models(self) -> dict:
        return {"models": self._listing(), "move": self.job.status()}

    def move(self, body: dict) -> dict:
        with self._lock:
            inst = self.app.installer
            if inst is not None and inst.busy():
                raise ApiError(409, SETUP_BUSY)
            rows = self._listing()
            allowed = [r["path"] for r in rows if r["movable"]]
            owned = self._owned()
            try:
                self.job.start(body.get("paths"), body.get("dest"), allowed=allowed,
                               tiers_dir=self.app.tiers_dir, var_dir=self.app.var_dir,
                               free_space=self._disk_free, owned=owned, drive_type=self._drive_type,
                               on_done=self._after_move)
            except mover.MoveError as e:
                setup_files = {norm(r["path"]) for r in rows if r["setup"]}
                paths = body.get("paths") if isinstance(body.get("paths"), list) else []
                hit = [p for p in paths if isinstance(p, str) and "\0" not in p and norm(p) in setup_files]
                if hit and not isinstance(e, mover.MoveConflict):  # say why, not "not configured"
                    raise ApiError(400, f"{Path(hit[0]).name} is {mover.SETUP_OWNED}") from None
                raise _setup_error(e) from None
            picked = {norm(p) for p in body["paths"]}
            reloads = any(r["reloads_tier"] for r in rows if norm(r["path"]) in picked)
        return {"move": self.job.status(), "reloads_tier": reloads}

    def cancel_move(self) -> dict:
        """Stop the running move; the file being copied is dropped (its .part removed)."""
        self.job.cancel()
        self.job.join(10)
        return {"move": self.job.status()}

    def _after_move(self, result: dict) -> None:
        """Setup's own tier is the current one and its paths changed: reload it, as switching does."""
        name = self.app.tier.name
        if name in result["tiers_updated"] and LOCAL_NAME.fullmatch(name) \
                and (Path(self.app.tiers_dir) / f"{name}.toml").is_file():
            self.app.reload_tier(name)
            result["reloaded"] = name

    def shutdown(self) -> None:
        self.job.cancel()
        self.job.join(10)


def setup_status(installer, servers: list[dict]) -> dict:
    """/v1/status's setup summary, cheap (no network, no hashing): `needed` when the current
    tier's text server can't be used and Setup hasn't finished a kit; `running` while a run is
    going or paused."""
    try:
        state = installer.state() if installer is not None else "idle"
    except Exception:
        state = "idle"
    text = next((s for s in servers if s.get("role") == "text"), None)
    usable = text is not None and (text["state"] in ("ready", "external", "starting")
                                   or (text["state"] == "stopped" and not text.get("reason")))
    return {"needed": not usable and state != "ready", "running": state in ("running", "paused")}
