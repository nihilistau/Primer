"""The Setup installer: run a resolved kit Plan one item at a time in the background, verify
everything, write the tier and add the kit's picture/video models (built from the plan's roles,
since their files sit in separate repo folders) plus any others the Models scan finds.

One worker thread, items strictly one at a time (so two items never write the same dest at once).
Pause cancels the current download and keeps its `.part`; resume runs it again and the
downloader continues it with an HTTP Range. State lives in var/setup.json (shared with
approvals.py through one StateFile), written on every state change and, for progress, at most
every PERSIST_EVERY_S. A run that was running or paused when Primer stopped comes back paused:
nothing downloads until the user presses Resume.

Zips (runtimes, fonts) are downloaded to `<root>/.setup/downloads/`, extracted into a fresh
`<root>/.setup/staging/<item id>/`, then merged into `<root>/<dest>` without overwriting any
existing file (identical bytes are fine; a different file fails the item), recorded in
`<dest>/.primer-setup.json` (see kits.py), and the zip and staging folder deleted. Every .exe
and .dll a zip placed waits for the user's approval before it can run (approvals.py).
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from primer.config import REPO_ROOT, TIERS_DIR
from primer.images.families import FAMILIES
from primer.images.models import ImageModel, ModelStore
from primer.images.scan import scan_folder
from primer.maps.pmtiles import Archive, PMTilesError
from primer.setup.approvals import EXEC_EXT, UNKNOWN_PUBLISHER, Approvals, StateFile, norm
from primer.setup.download import DownloadCancelled, DownloadError, DownloadGone, download
from primer.setup.extract import ExtractCancelled, ExtractError, extract_zip
from primer.setup.kits import (SETUP_MARKER, KitError, Plan, PlanItem, fits, have_under, load_kits,
                               resolve, which_llama_server)
from primer.setup.net import TOKEN_HOST, CatalogueError, Net, host_matches
from primer.setup.tierwriter import TierError, port_free
from primer.setup import tierwriter

log = logging.getLogger(__name__)

GB = 1024 ** 3
PERSIST_EVERY_S = 2.0
SPEED_WINDOW_S = 5.0
JOIN_S = 10.0
MAX_SCAN_FILES = 2000
HASH_TEXT_MAX = 4096
# a state the worker is in the middle of; restored as "queued"
BUSY = ("downloading", "verifying", "extracting")
FINISHED = ("done", "present", "skipped")
_PROGRESS = re.compile(r"data ([\d.]+) / ([\d.]+) MB")


# the picture and video models a kit brings, by the plan roles that fill each family's files:
# their files sit in separate repo folders, which the Models scan can't pair up
KIT_MODELS = (
    ("qwen-image-2.1", {"diffusion": "image", "llm": "image_encoder", "vae": "image_vae"}),
    ("wan2.2-ti2v-5b", {"diffusion": "video", "t5xxl": "video_encoder", "vae": "video_vae"}),
)


def _same_files(a: dict, b: dict) -> bool:
    def key(files):
        return {k: os.path.normcase(os.path.abspath(v)) for k, v in files.items() if v}
    return key(a) == key(b)


class SetupError(Exception):
    """Setup can't do what was asked; `reason` is a short sentence for the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SetupConflict(SetupError):
    """Refused because of what Setup is doing right now (running, pausing, the plan changed)."""


class _Cancelled(Exception):
    pass


@dataclasses.dataclass
class _Run:
    state: str = "queued"
    bytes: int = 0
    total: int = 0
    error: str = ""
    gone: bool = False  # the server no longer has the file: Resume won't help, a new plan may
    samples: collections.deque = dataclasses.field(default_factory=collections.deque)

    def to_json(self) -> dict:
        return {"state": self.state, "bytes": self.bytes, "total": self.total, "error": self.error}


def _plan_from_json(d: dict) -> Plan:
    names = {f.name for f in dataclasses.fields(PlanItem)}
    items = []
    for raw in d["items"]:
        item = PlanItem(**{k: v for k, v in raw.items() if k in names})
        item.allowed_ext = tuple(item.allowed_ext or ())
        items.append(item)
    return Plan(kit=d["kit"], label=d.get("label", ""), items=items, skipped=list(d.get("skipped", [])),
                checked=d.get("checked", ""), error=str(d.get("error") or ""))


def _free_space(root: Path) -> int:
    p = Path(root)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def _same_bytes(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return _sha256(a) == _sha256(b)
    except OSError:
        return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def default_map_runner(out: Path, maxzoom: int, progress, cancel: threading.Event) -> None:
    """Cut the map with tools/map_extract.py (our own script) in a subprocess. Its progress lines
    ("data X / Y MB") feed `progress`; cancel terminates it, and its .part resumes next time."""
    from primer import children

    cmd = [sys.executable, "-I", str(Path(REPO_ROOT) / "tools" / "map_extract.py"),
           "--out", str(out), "--maxzoom", str(int(maxzoom))]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", creationflags=flags)
    except OSError as e:
        raise SetupError(f"the map tool could not start: {e}") from None
    children.adopt(proc, "setup-map")
    tail: collections.deque = collections.deque(maxlen=5)

    def read() -> None:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            tail.append(line)
            m = _PROGRESS.search(line)
            if m:
                progress(int(float(m.group(1)) * 1e6), int(float(m.group(2)) * 1e6))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        while proc.poll() is None:
            if cancel.wait(0.2):
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise DownloadCancelled("the map was stopped; it resumes from its partial file")
        reader.join(5)
    finally:
        children.release(proc.pid)
    if proc.returncode != 0:
        last = tail[-1] if tail else f"exit code {proc.returncode}"
        raise SetupError(f"the map could not be cut ({last})")


class Installer:
    """See the module docstring. Public: plan, start, status, pause, resume, cancel,
    acknowledge, shutdown, and `approvals` (the Approvals sharing var/setup.json)."""

    def __init__(self, var_dir, catalogues, *, tiers_dir=TIERS_DIR, transport=None, resolve=None,
                 downloader=download, map_runner=None, clock=time.monotonic,
                 manifest: dict | None = None, free_space: Callable[[Path], int] = _free_space,
                 llama_server: Callable[[], str | None] = which_llama_server,
                 probe: Callable[[int], bool] = port_free,
                 scan: Callable[[Path], list] | None = None, store: ModelStore | None = None) -> None:
        self.var_dir = Path(var_dir)
        self.catalogues = catalogues
        self.tiers_dir = Path(tiers_dir)
        self._transport, self._resolve = transport, resolve
        self._downloader = downloader
        self._map_runner = map_runner or default_map_runner
        self._clock = clock
        self._manifest = manifest
        self._free_space = free_space
        self._llama_server = llama_server
        self._probe = probe
        self._scan = scan or (lambda folder: scan_folder(folder, MAX_SCAN_FILES))
        self._store = store
        self._file = StateFile(self.var_dir)
        self.approvals = Approvals(self.var_dir, self._file)

        self._lock = threading.RLock()
        self._plan: Plan | None = None
        self._root: Path | None = None
        self._kit: str | None = None
        self._choices: dict = {}
        self._runs: dict[str, _Run] = {}
        self._licences: list[str] = []
        self._state = "idle"
        self._error = ""
        self._tier: str | None = None
        self._models_added: list[str] = []
        self._worker: threading.Thread | None = None
        self._current: str | None = None
        self._cancel = threading.Event()
        self._cancel_item = False
        self._pause_req = False
        self._stop = False
        self._last_persist = float("-inf")
        self._restore()

    # ── persistence ───────────────────────────────────────────────────────────────────────────

    def _restore(self) -> None:
        try:
            data = self._file.read()
        except OSError as e:  # left in place (not moved aside): the next write may succeed
            log.warning("cannot read var/setup.json (%s); starting with no saved run", e)
            return
        lic = data.get("licences")
        self._licences = [x for x in lic if isinstance(x, str)] if isinstance(lic, list) else []
        try:
            if not isinstance(data.get("plan"), dict):
                return
            plan = _plan_from_json(data["plan"])
            runs = {}
            saved = data.get("items") if isinstance(data.get("items"), dict) else {}
            for item in plan.items:
                raw = saved.get(item.id) if isinstance(saved.get(item.id), dict) else {}
                run = _Run(state=str(raw.get("state") or ("present" if item.present else "queued")),
                           bytes=int(raw.get("bytes") or 0), total=int(raw.get("total") or item.size or 0),
                           error=str(raw.get("error") or ""))
                if run.state in BUSY:
                    run.state = "queued"
                runs[item.id] = run
            root = Path(data["root"])
        except (KeyError, TypeError, ValueError) as e:
            log.warning("var/setup.json: the saved run can't be restored (%s); starting fresh", e)
            return
        self._plan, self._runs, self._root = plan, runs, root
        self._kit = data.get("kit") or plan.kit
        self._choices = data.get("choices") if isinstance(data.get("choices"), dict) else {}
        state = data.get("state")
        self._state = "paused" if state in ("running", "paused", "done") else (
            state if state in ("idle", "failed", "ready") else "idle")
        self._error = str(data.get("error") or "")
        self._tier = data.get("tier") if isinstance(data.get("tier"), str) else None
        added = data.get("models_added")
        self._models_added = [x for x in added if isinstance(x, str)] if isinstance(added, list) else []

    def _snapshot(self) -> dict:
        with self._lock:
            return {
                "root": str(self._root) if self._root else None,
                "kit": self._kit, "choices": dict(self._choices),
                "plan": self._plan.to_json() if self._plan else None,
                "items": {k: r.to_json() for k, r in self._runs.items()},
                "licences": list(self._licences), "state": self._state, "error": self._error,
                "tier": self._tier, "models_added": list(self._models_added),
            }

    def _persist(self, force: bool = True) -> None:
        now = self._clock()
        if not force and now - self._last_persist < PERSIST_EVERY_S:
            return
        self._last_persist = now
        try:  # the snapshot is taken under the file's lock, so the last write is the newest state
            self._file.update(lambda d: d.update(self._snapshot()))
        except OSError as e:
            log.warning("could not save var/setup.json: %s", e)

    # ── the plan ──────────────────────────────────────────────────────────────────────────────

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def plan(self, kit_id: str, choices: dict | None, root) -> Plan:
        """Resolve the kit against the catalogues and what is already under `root`; it becomes
        the pending plan (replacing any earlier one that isn't running)."""
        if not isinstance(root, (str, os.PathLike)) or not str(root) or "\0" in str(root):
            raise SetupError("the destination must be an absolute folder")
        root = Path(root)
        if not root.is_absolute():
            raise SetupError("the destination must be an absolute folder")
        with self._lock:
            if self._busy():
                raise SetupConflict("Setup is downloading; pause it before choosing another kit")
        try:
            plan = resolve(kit_id, self.catalogues, have_under(root, self._recorded_exe()), choices or {},
                           llama_server=self._llama_server(), manifest=self._manifest)
        except KitError as e:
            raise SetupError(str(e)) from None
        plan.error = self._tier_problem(plan, root)
        with self._lock:
            if self._busy():
                raise SetupConflict("Setup is downloading; pause it before choosing another kit")
            self._plan, self._root, self._kit = plan, root, kit_id
            self._choices = dict(choices or {})
            self._runs = {i.id: _Run(state="present" if i.present else "queued", total=i.size)
                          for i in plan.items}
            self._state, self._error, self._tier, self._models_added = "idle", "", None, []
        self._persist()
        return plan

    def _recorded_exe(self) -> Callable[[Path], bool]:
        """Is this program in var/setup.json's installed files? (A runtime already in the folder
        but not recorded here is installed again, so its files can be approved as a release.)"""
        try:
            installed = self._file.read().get("installed")
        except OSError:
            installed = None
        keys = set(installed) if isinstance(installed, dict) else set()
        return lambda path: norm(path) in keys

    @staticmethod
    def _tier_problem(plan: Plan, root) -> str:
        """Why no tier could be written from this plan ('' when one can): checked before anything
        downloads, with every port taken as free (the real ports are chosen at the end)."""
        if any(i.error for i in plan.items):
            return ""  # the items' own errors say what is wrong
        try:
            tierwriter.build(plan, root, f"local-{plan.kit}", probe=lambda port: True)
        except TierError as e:
            return str(e)
        return ""

    def acknowledge(self, licence_id: str) -> None:
        if not isinstance(licence_id, str) or not licence_id:
            raise SetupError("name the licence to accept")
        with self._lock:
            if licence_id not in self._licences:
                self._licences.append(licence_id)
        self._persist()

    # ── running ───────────────────────────────────────────────────────────────────────────────

    def start(self, kit: str | None = None, root=None, choices: dict | None = None) -> None:
        """Run the pending plan. `kit`, `root` and `choices`, when given (the API passes what the
        window showed), must be the plan's: otherwise SetupConflict (check the plan again)."""
        expect = (kit, root, choices)
        with self._lock:
            plan, plan_root = self._plan, self._root
            if self._busy():
                raise SetupConflict("Setup is already running")
            if plan is None or plan_root is None:
                raise SetupError("choose a kit first")
            if expect != (None, None, None) and not self._matches(*expect):
                raise SetupConflict("the plan changed; check it again")
            root = plan_root
            if any(i.error for i in plan.items):
                bad = "; ".join(f"{i.label}: {i.error}" for i in plan.items if i.error)
                raise SetupError(f"some items could not be found ({bad}); try again or skip them")
            problem = plan.error or self._tier_problem(plan, root)
            if problem:
                raise SetupError(problem)
        try:
            free = self._free_space(root)
        except OSError as e:
            raise SetupError(f"cannot check the free space on {root}: {e}") from None
        ok, short = fits(plan, free)
        if not ok:
            raise SetupError(f"not enough space on {root}: about {short / GB:.1f} GB more is needed")
        with self._lock:
            self._check_unchanged(plan)
            missing = [l["licence"] for l in plan.licences() if l["key"] not in self._licences]
            if missing:
                word = "licence" if len(missing) == 1 else "licences"
                raise SetupError(f"accept the {word} first: {', '.join(missing)}")
        try:  # before anything downloads: executables placed under root are then guarded
            self.approvals.add_root(root)
        except OSError as e:
            raise SetupError(f"cannot record the destination in var/setup.json: {e}") from None
        with self._lock:
            self._check_unchanged(plan)
            for run in self._runs.values():
                if run.state == "failed":
                    run.state, run.error = "queued", ""
            self._launch()
        self._persist()

    def _matches(self, kit, root, choices) -> bool:
        """With the lock held: kit, root and choices are the pending plan's."""
        if kit != self._kit or not isinstance(choices, dict) or dict(choices) != self._choices:
            return False
        try:
            return norm(Path(root)) == norm(self._root)
        except (TypeError, ValueError, OSError):
            return False

    def _check_unchanged(self, plan: Plan) -> None:
        """With the lock held: the plan start() checked is still the pending one, not running."""
        if self._busy():
            raise SetupConflict("Setup is already running")
        if self._plan is not plan:
            raise SetupConflict("the plan changed; check it again")

    def _launch(self) -> None:
        """With the lock held: start the worker."""
        self._state, self._error = "running", ""
        self._pause_req = self._stop = False
        self._worker = threading.Thread(target=self._work, name="primer-setup", daemon=True)
        self._worker.start()

    def pause(self) -> None:
        with self._lock:
            if not self._busy() or self._state != "running":
                return
            self._pause_req = True
            self._state = "paused"
            self._cancel.set()
        self._persist()

    def resume(self) -> None:
        """Continue a paused run, or retry the items of a failed one (a run never started
        starts, with start()'s checks)."""
        with self._lock:
            worker = self._worker
            if worker is not None and worker.is_alive() and not (self._pause_req or self._stop):
                return  # already running
        if worker is not None and worker.is_alive():
            worker.join(JOIN_S)  # a pause in progress: the current download is stopping
            if worker.is_alive():
                raise SetupConflict("Setup is still pausing; try again in a moment")
        with self._lock:
            if self._plan is None:
                raise SetupError("choose a kit first")
            state = self._state
        if state == "idle":
            self.start()
            return
        with self._lock:
            if self._state == "ready" or self._busy():
                return
            for run in self._runs.values():
                if run.state == "failed":
                    run.state, run.error = "queued", ""
            self._launch()
        self._persist()

    def cancel(self, item_id: str) -> None:
        """Skip an item: the current one stops (its .part is kept), a queued one won't start."""
        with self._lock:
            run = self._runs.get(item_id)
            if run is None:
                raise SetupError(f"no item {item_id!r} in this plan")
            if item_id == self._current:
                self._cancel_item = True
                self._cancel.set()
                return
            if run.state in ("queued", "failed"):
                run.state, run.error = "skipped", "cancelled"
            elif run.state != "skipped":
                raise SetupError(f"{item_id} is already {run.state}")
        self._persist()

    def shutdown(self) -> None:
        """Stop the worker (the current download's .part is kept); the run resumes paused."""
        with self._lock:
            self._stop = True
            self._cancel.set()
            worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(JOIN_S)
        self._persist()

    # ── the worker ────────────────────────────────────────────────────────────────────────────

    def _next(self) -> PlanItem | None:
        for item in self._plan.items:
            if self._runs[item.id].state == "queued":
                return item
        return None

    def _work(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._stop or self._pause_req:
                        break
                    item = self._next()
                    if item is None:
                        break
                    run = self._runs[item.id]
                    run.state, run.error = "downloading", ""
                    run.samples.clear()
                    self._current, self._cancel_item = item.id, False
                    self._cancel = cancel = threading.Event()
                self._persist()
                gone = False
                try:
                    self._install(item, run, cancel)
                    outcome, error = "done", ""
                except (DownloadCancelled, _Cancelled):
                    with self._lock:
                        if self._cancel_item:
                            outcome, error = "skipped", "cancelled (the partial file is kept)"
                        else:
                            outcome, error = "queued", ""
                except DownloadGone:
                    host = urllib.parse.urlsplit(item.url or "").hostname or "its server"
                    outcome, gone = "failed", True
                    error = f"{item.label} is no longer offered by {host}; check the plan again"
                except (SetupError, DownloadError, CatalogueError, ExtractError) as e:
                    outcome, error = "failed", e.reason
                except (OSError, PMTilesError, TierError) as e:
                    outcome, error = "failed", str(e)
                except Exception as e:  # a bug must not leave the run stuck "running"
                    log.exception("setup item %s failed", item.id)
                    outcome, error = "failed", f"unexpected error: {e}"
                with self._lock:
                    run.state, run.error, run.gone = outcome, error, gone
                    run.samples.clear()
                    self._current = None
                    self._cancel_item = False
                self._persist()
            self._end()
        except Exception as e:
            log.exception("the setup worker failed")
            with self._lock:
                self._state, self._error = "failed", f"unexpected error: {e}"
            self._persist()

    def _end(self) -> None:
        with self._lock:
            if self._stop or self._pause_req:
                self._state = "paused"
                finish = False
            else:
                failed = [i.label for i in self._plan.items if self._runs[i.id].state == "failed"]
                gone = [r.error for r in self._runs.values() if r.state == "failed" and r.gone]
                if gone:  # Resume would ask for the same missing file again
                    self._state, self._error, finish = "failed", "; ".join(gone), False
                elif failed:
                    self._state = "failed"
                    self._error = (f"{len(failed)} item{'s' if len(failed) > 1 else ''} failed: "
                                   f"{', '.join(failed)}; press Resume to try again")
                    finish = False
                else:
                    self._state, finish = "done", True
        self._persist()
        if finish:
            self._finish()

    def _finish(self) -> None:
        """Every item is in place: write the tier, add the scanned models, report ready."""
        with self._lock:
            plan, root = self._plan, self._root
            ready = [i for i in plan.items if self._runs[i.id].state in ("done", "present")]
        sub = Plan(kit=plan.kit, label=plan.label, items=ready, skipped=plan.skipped, checked=plan.checked)
        try:
            path = tierwriter.write(sub, root, self.tiers_dir, probe=self._probe)
        except (TierError, OSError) as e:
            with self._lock:
                self._state, self._error = "failed", f"the tier could not be written: {e}"
            self._persist()
            return
        added = self._add_models(root / "models", ready, root)
        with self._lock:
            self._tier = path.stem
            self._models_added = added
            self._state, self._error = "ready", ""
        self._persist()

    @staticmethod
    def _kit_proposals(items: list[PlanItem], root: Path) -> list[dict]:
        """The kit's own picture and video models, from the plan's roles (KIT_MODELS): one per
        family whose every file is in place."""
        by_role = {i.role: i for i in items}
        out = []
        for family, slots in KIT_MODELS:
            if not all(role in by_role for role in slots.values()):
                continue
            files = {slot: str(Path(root) / by_role[role].dest) for slot, role in slots.items()}
            out.append({"name": by_role[slots["diffusion"]].label, "family": family, "files": files,
                        "missing": [], "settings": {}, "loras": []})
        return out

    def _add_models(self, folder: Path, items=(), root: Path | None = None) -> list[str]:
        """The kit's picture and video models (built from the plan's roles, see KIT_MODELS),
        then the Models scan's complete proposals for anything else under `folder`, each added
        the way the Models window's Add does (the family's defaults under the proposal's
        settings) unless a model with the same family and files is already there. The store
        makes the first model of each kind its default, so an existing default is kept."""
        proposals = self._kit_proposals(list(items), root) if root is not None else []
        try:
            store = self._store or ModelStore(self.var_dir)
            existing = store.list()
        except Exception as e:  # the tier is written either way
            log.warning("the model store can't be read: %s", e)
            return []
        if folder.is_dir():
            try:
                proposals += self._scan(folder)
            except Exception as e:
                log.warning("the Models scan of %s failed: %s", folder, e)
        added = []
        for p in proposals:
            fam = FAMILIES.get(p.get("family"))
            if fam is None or p.get("missing"):
                continue
            if any(m.family == p["family"] and _same_files(m.files, p["files"]) for m in existing):
                continue
            try:
                saved = store.save(ImageModel(id="", name=p["name"], family=p["family"], files=p["files"],
                                              settings={**fam.defaults, **(p.get("settings") or {})},
                                              loras=p.get("loras") or []))
            except Exception as e:
                log.warning("could not add the scanned model %s: %s", p.get("name"), e)
                continue
            existing.append(saved)
            added.append(saved.name)
        return added

    # ── one item ──────────────────────────────────────────────────────────────────────────────

    def _set_state(self, run: _Run, state: str) -> None:
        with self._lock:
            run.state = state
        self._persist()

    def _progress(self, run: _Run):
        def cb(done: int, total) -> None:
            now = self._clock()
            with self._lock:
                run.bytes = int(done)
                if total:
                    run.total = int(total)
                run.samples.append((now, run.bytes))
                while len(run.samples) > 2 and now - run.samples[0][0] > SPEED_WINDOW_S:
                    run.samples.popleft()
                if run.state == "downloading" and run.total and run.bytes >= run.total:
                    run.state = "verifying"
            self._persist(force=False)
        return cb

    def _install(self, item: PlanItem, run: _Run, cancel: threading.Event) -> None:
        if item.use_path:
            return
        if item.kind == "map":
            self._install_map(item, run, cancel)
        elif item.kind in ("runtime", "fonts"):
            self._install_zip(item, run, cancel)
        else:
            self._fetch(item, self._root / item.dest, run, cancel)

    def _token(self) -> str | None:
        try:
            return (self.var_dir / "keys" / "huggingface.key").read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _hash_text(self, url: str, n: int) -> str:
        """The first n-hex-digit token of a small published hash file (`<url>.sha256` / `.md5`)."""
        net = Net(stream_transport=self._transport, resolve=self._resolve)
        try:
            resp = net.open_stream(url)
        except CatalogueError as e:
            raise SetupError(f"the published hash could not be fetched: {e.reason}") from None
        try:
            if resp.status in (404, 410):  # the file it belongs to is gone with it
                raise DownloadGone("the published hash file was not found on the server")
            if resp.status != 200:
                raise SetupError(f"the published hash could not be fetched (the server answered {resp.status})")
            try:
                body = resp.stream.read(HASH_TEXT_MAX)
            except OSError as e:
                raise SetupError(f"the published hash could not be fetched: {e}") from None
        finally:
            try:
                resp.stream.close()
            except Exception:
                pass
        text = body.decode("ascii", "replace") if isinstance(body, bytes) else str(body)
        m = re.search(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{%d}(?![0-9A-Fa-f])" % n, text)
        if not m:
            raise SetupError("the published hash file holds no hash")
        return m.group(0).lower()

    def _hashes(self, item: PlanItem) -> tuple[str | None, str | None]:
        """(sha256, md5) to check the item against, fetching a published hash file if need be."""
        sha256, md5 = item.sha256, item.md5
        if not sha256 and not md5:
            if item.sha256_url:
                sha256 = self._hash_text(item.sha256_url, 64)
            elif item.md5_url:
                md5 = self._hash_text(item.md5_url, 32)
            else:
                raise SetupError("there is no published hash to check this file against")
        return sha256, md5

    def _fetch(self, item: PlanItem, dest: Path, run: _Run, cancel: threading.Event, *,
               sha256: str | None = None, md5: str | None = None) -> Path:
        if not sha256 and not md5:
            sha256, md5 = self._hashes(item)
        if not item.url:
            raise SetupError("the item has no download address")
        host = urllib.parse.urlsplit(item.url).hostname or ""
        token = self._token() if host_matches(host, TOKEN_HOST) else None
        if cancel.is_set():
            raise _Cancelled()
        size = (item.size or None) if item.size_exact else None  # a rough size: the hash decides
        return self._downloader(item.url, dest, size=size, sha256=sha256, md5=md5,
                                progress=self._progress(run), cancel=cancel, transport=self._transport,
                                resolve=self._resolve, token=token,
                                allowed_ext=tuple(item.allowed_ext) or None)

    def _install_map(self, item: PlanItem, run: _Run, cancel: threading.Event) -> None:
        out = self._root / item.dest
        out.parent.mkdir(parents=True, exist_ok=True)
        self._map_runner(out, item.maxzoom, self._progress(run), cancel)
        if cancel.is_set() and not out.is_file():
            raise _Cancelled()
        self._set_state(run, "verifying")
        try:
            with Archive(out) as a:
                a.header  # opening reads the header and the root directory
        except (PMTilesError, OSError) as e:
            raise SetupError(f"the map could not be read back: {e}") from None

    def _install_zip(self, item: PlanItem, run: _Run, cancel: threading.Event) -> None:
        root = self._root
        name = item.archive or ""
        if not name or Path(name).name != name or not name.lower().endswith(".zip"):
            raise SetupError(f"unexpected archive name {name!r}")
        zpath = root / ".setup" / "downloads" / name
        sha256, md5 = self._hashes(item)
        if not self._verified(zpath, item.size, sha256, md5):  # else: verified before a restart
            self._fetch(item, zpath, run, cancel, sha256=sha256, md5=md5)
        self._set_state(run, "extracting")
        staging = root / ".setup" / "staging" / item.id
        _rmtree(staging)  # ours: a stale one from an interrupted run
        try:
            extract_zip(zpath, staging, skip_symlinks=item.symlinks == "skip", cancel=cancel)
        except ExtractCancelled:
            raise _Cancelled() from None
        src = self._layout(item, staging)
        dest = root / item.dest
        zsha = _sha256(zpath)
        # the marker goes in first, as "installing" (not yet present for have_under), so a
        # crash mid-merge still leaves dest's executables guarded without setup.json
        self._record(dest, item.id, zsha, provisional=True)
        placed = self._merge(src, dest, cancel)
        if item.kind == "runtime" or any(p.suffix.lower() in EXEC_EXT for p in placed):
            publisher = item.origin.get("repo") or urllib.parse.urlsplit(item.url or "").hostname or ""
            release = item.origin.get("tag") or item.origin.get("release") or ""
            self.approvals.record_installed(
                [{"path": str(p), "publisher": publisher, "release": release, "item": item.id}
                 for p in placed if p.suffix.lower() in EXEC_EXT])
        self._record(dest, item.id, zsha)
        for leftover in (zpath,):
            try:
                leftover.unlink()
            except OSError:
                pass
        _rmtree(staging)

    @staticmethod
    def _layout(item: PlanItem, staging: Path) -> Path:
        """The folder to merge: the staging folder, or for a runtime whose zip wraps everything
        in one top folder (kiwix-tools), that folder. Every expected file must be there."""
        expect = [x for x in dict.fromkeys((item.exe, item.marker)) if x]
        src = staging
        if item.kind == "runtime" and expect and not all((staging / e).is_file() for e in expect):
            entries = list(staging.iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                src = entries[0]
        for e in expect:
            if not (src / e).is_file():
                raise SetupError(f"the release's layout changed: {e} not found")
        return src

    @staticmethod
    def _verified(path: Path, size: int, sha256: str | None, md5: str | None) -> bool:
        """A zip already downloaded and matching its published hash (e.g. before a restart)."""
        try:
            if not path.is_file() or (size and path.stat().st_size != size):
                return False
            if sha256:
                return _sha256(path) == sha256.lower()
            if md5:
                h = hashlib.md5()
                with open(path, "rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(block)
                return h.hexdigest() == md5.lower()
        except OSError:
            return False
        return False

    @staticmethod
    def _merge(src: Path, dest: Path, cancel: threading.Event | None = None) -> list[Path]:
        """Move src's files into dest, never overwriting: an identical file already there is
        fine, a different one fails the item before anything moves. Returns every dest file."""
        files = [p for p in sorted(src.rglob("*")) if p.is_file()]
        moves = []
        for f in files:
            target = dest / f.relative_to(src)
            if target.exists() or target.is_symlink():
                if not target.is_file() or not _same_bytes(f, target):
                    rel = target.relative_to(dest).as_posix()
                    raise SetupError(f"{rel} is already in {dest} and is a different file; "
                                     "move it away and try again")
                continue
            moves.append((f, target))
        for f, target in moves:
            if cancel is not None and cancel.is_set():
                raise _Cancelled()  # what moved stays; a retry finds those files identical
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(f, target)
        return [dest / f.relative_to(src) for f in files]

    @staticmethod
    def _record(dest: Path, item_id: str, sha: str, *, provisional: bool = False) -> None:
        """Merge {item_id: sha} into dest/.primer-setup.json, atomically: under "installing"
        while the merge runs (provisional), then under "items" (the item is in place)."""
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / SETUP_MARKER
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            data = {"items": {}}
        installing = data.get("installing") if isinstance(data.get("installing"), dict) else {}
        if provisional:
            installing[item_id] = sha
        else:
            installing.pop(item_id, None)
            data["items"][item_id] = sha
        if installing:
            data["installing"] = installing
        else:
            data.pop("installing", None)
        tmp = path.with_name(f".{SETUP_MARKER}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ── approvals ─────────────────────────────────────────────────────────────────────────────

    def approve_release(self, item_id: str) -> dict:
        """Approve every pending executable and DLL that item placed (the UI's "approve all
        from this release"), re-hashing each. {"approved": [paths], "refused": [paths]}.
        Files Setup didn't install (planted, or unlisted) are never included: those are
        approved one at a time with approve(path, sha256)."""
        if not isinstance(item_id, str) or not item_id:
            raise SetupError("name the item whose files to approve")
        with self._lock:
            known = self._plan is not None and any(i.id == item_id for i in self._plan.items)
        try:
            rows = [p for p in self.approvals.pending()
                    if p["item"] == item_id and p["publisher"] != UNKNOWN_PUBLISHER]
        except OSError as e:
            raise SetupError(f"cannot read var/setup.json: {e}") from None
        if not rows and not known:
            raise SetupError(f"no item {item_id!r} in this plan")
        if not rows:
            raise SetupError(f"nothing from {item_id!r} is waiting for approval")
        out = {"approved": [], "refused": []}
        for row in rows:
            try:
                ok = self.approvals.approve(row["path"], notify=False)
            except OSError:
                ok = False
            out["approved" if ok else "refused"].append(row["path"])
        if out["approved"]:
            self.approvals.notify()
        return out

    # ── status ────────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _speed(run: _Run) -> tuple[float, float | None]:
        if run.state not in ("downloading", "verifying") or len(run.samples) < 2:
            return 0.0, None
        (t0, b0), (t1, b1) = run.samples[0], run.samples[-1]
        if t1 <= t0 or b1 <= b0:
            return 0.0, None
        speed = (b1 - b0) / (t1 - t0)
        eta = max(0.0, (run.total - run.bytes) / speed) if run.total else None
        return round(speed, 1), (round(eta, 1) if eta is not None else None)

    def busy(self) -> bool:
        """Setup is doing something that writes: the worker is alive (running, a pause still
        winding down, or the tier being written after the last item), or the state says so."""
        with self._lock:
            return self._busy() or self._state in ("running", "done")

    def state(self) -> str:
        """Just the run's state (idle/running/paused/done/failed/ready): cheap, for /v1/status."""
        with self._lock:
            return self._state

    def root(self) -> str | None:
        """The pending or last run's destination root."""
        with self._lock:
            return str(self._root) if self._root else None

    def manifest(self) -> dict:
        """The kits manifest this installer resolves against."""
        return self._manifest if self._manifest is not None else load_kits()

    def free_space(self, root) -> int:
        """Free bytes where `root` is (or would be created). OSError when it can't be checked."""
        return self._free_space(Path(root))

    def status(self) -> dict:
        with self._lock:
            plan = self._plan
            items = []
            for item in plan.items if plan else []:
                run = self._runs[item.id]
                speed, eta = self._speed(run)
                items.append({"id": item.id, "label": item.label, "kind": item.kind, "state": run.state,
                              "bytes": run.bytes, "total": run.total or item.size, "speed_bps": speed,
                              "eta_s": eta, "error": run.error})
            licences = [{**l, "accepted": l["key"] in self._licences}
                        for l in (plan.licences() if plan else [])]
            out = {"state": self._state, "kit": self._kit, "label": plan.label if plan else "",
                   "root": str(self._root) if self._root else None, "choices": dict(self._choices),
                   "plan": plan.to_json() if plan else None, "items": items, "licences": licences,
                   "tier": self._tier, "error": self._error, "models_added": list(self._models_added)}
        try:
            out["approvals"] = self.approvals.pending()
        except Exception as e:
            log.warning("could not list pending approvals: %s", e)
            out["approvals"] = []
        return {k: out[k] for k in ("state", "kit", "label", "root", "choices", "plan", "items", "licences",
                                    "approvals", "tier", "error", "models_added")}
