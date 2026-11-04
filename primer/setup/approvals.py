"""Executables Setup downloaded run only after the user approves them, recorded by SHA-256.

var/setup.json is shared with the Installer: every write goes through one `StateFile` per path
(one lock, read-modify-write, atomic replace), and each side only changes its own keys. The keys
kept here:

- `approvals`: {normalised path: sha256} the user approved;
- `installed`: {normalised path: {sha256, size, mtime_ns, publisher, release, file, item}}, every
  .exe and .dll the installer placed (so pending approvals can show where each came from);
- `roots`: every destination root Setup has installed into.

A launch is guarded when its executable lies in or below a folder holding Setup's
`.primer-setup.json` marker (written in every extracted item's folder, so the guard holds even
when var/setup.json is lost), is a recorded installed file, or lies under a recorded root's
`tools/` or `.setup/` folder (where Setup puts programs; the user's own files elsewhere in a
broad chosen folder are not Setup's business). A
guarded executable runs only when its current bytes hash to the approved SHA-256 and every .exe
and .dll beside it (recorded or not: a planted DLL would load into it) is approved too. Anything
else (hand-configured, on the PATH, a winget install) is unaffected. The check fails closed: a
setup.json that can't be read refuses the launch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from primer.config import REPO_ROOT
from primer.setup.kits import SETUP_MARKER

log = logging.getLogger(__name__)

STATE_NAME = "setup.json"
CHUNK = 1024 * 1024
EXEC_EXT = (".exe", ".dll")
UNKNOWN_PUBLISHER = "unknown — not installed by Setup"
MAX_REFUSED = 200  # launch targets remembered for pending()
_RETRIES, _RETRY_S = 3, 0.05
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def norm(path) -> str:
    """The key a path is recorded under: absolute, links resolved, case-folded on Windows."""
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _retry(fn):
    for _ in range(_RETRIES):
        try:
            return fn()
        except PermissionError:  # a transient sharing violation on Windows (antivirus, indexer)
            time.sleep(_RETRY_S)
    return fn()


def _is_exec(name: str) -> bool:
    return name.lower().endswith(EXEC_EXT)


class StateFile:
    """var/setup.json: read and update under one per-file lock, written atomically.

    A file that isn't JSON (or isn't an object) is moved aside to setup.json.bad once and reading
    starts fresh. Any other read error (permissions, a locked file) raises OSError: the file is
    left alone and callers decide (the launch check refuses)."""

    def __init__(self, var_dir) -> None:
        self.path = Path(var_dir) / STATE_NAME
        key = os.path.normcase(os.path.abspath(self.path))
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(key, threading.RLock())

    def read(self) -> dict:
        with self.lock:
            try:
                raw = _retry(self.path.read_bytes)
            except FileNotFoundError:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                return self._bad(f"not JSON: {e}")
            if not isinstance(data, dict):
                return self._bad("not a JSON object")
            return data

    def _bad(self, why: str) -> dict:
        bad = self.path.with_name(STATE_NAME + ".bad")
        log.warning("%s is corrupt (%s); keeping it as %s and starting fresh", self.path, why, bad.name)
        try:
            os.replace(self.path, bad)
        except OSError as e:
            log.warning("could not move %s aside: %s", self.path, e)
        return {}

    def update(self, fn: Callable[[dict], None]) -> dict:
        """fn(data) changes the state in place; the result is written atomically. fn runs with
        this file's lock held (lock order: StateFile, then any lock fn takes)."""
        with self.lock:
            data = self.read()
            fn(data)
            self._write(data)
            return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{STATE_NAME}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            _retry(lambda: os.replace(tmp, self.path))
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass


class _View:
    """One read of setup.json: the approvals, installed files and roots."""

    def __init__(self, d: dict) -> None:
        self.appr = d.get("approvals") if isinstance(d.get("approvals"), dict) else {}
        self.inst = {k: v for k, v in d.get("installed", {}).items() if isinstance(v, dict)} \
            if isinstance(d.get("installed"), dict) else {}
        roots = d.get("roots") if isinstance(d.get("roots"), list) else []
        self.roots = [r for r in roots if isinstance(r, str)]


class Approvals:
    def __init__(self, var_dir, state: StateFile | None = None) -> None:
        self.state = state or StateFile(var_dir)
        self._cache: dict[tuple, str] = {}
        self._cache_lock = threading.Lock()
        self._refused: dict[str, str] = {}  # guarded files a launch was refused for: key -> name
        # called (no arguments) after an approval is recorded; serve.py retries the servers
        # that were refused for want of one
        self.on_approved: Callable[[], None] | None = None

    def _view(self) -> _View:
        return _View(self.state.read())

    # ── hashing, cached by (path, size, mtime, file id) ───────────────────────────────────────

    def current_sha256(self, path) -> str | None:
        """The file's SHA-256 now, or None when it is missing or unreadable."""
        key = norm(path)
        try:
            st = os.stat(key)
        except OSError:
            return None
        ck = (key, st.st_size, st.st_mtime_ns, st.st_ino)
        with self._cache_lock:
            hit = self._cache.get(ck)
        if hit:
            return hit
        try:
            digest = sha256_file(key)
        except OSError:
            return None
        with self._cache_lock:
            self._cache[ck] = digest
        return digest

    # ── what Setup placed ─────────────────────────────────────────────────────────────────────

    def add_root(self, root) -> None:
        key = norm(root)

        def fn(d):
            roots = d.get("roots") if isinstance(d.get("roots"), list) else []
            if key not in roots:
                roots.append(key)
            d["roots"] = roots

        self.state.update(fn)

    def record_installed(self, entries: list[dict]) -> None:
        """entries: {path, publisher, release, item?}; each file is hashed now (it was just
        placed from a verified archive). A re-installed path loses any earlier approval unless
        its bytes are unchanged."""
        rows = {}
        for e in entries:
            key = norm(e["path"])
            sha = self.current_sha256(key)
            try:
                st = os.stat(key)
            except OSError:
                continue
            if sha is None:
                continue
            rows[key] = {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                         "publisher": e.get("publisher", ""), "release": e.get("release", ""),
                         "file": os.path.basename(key), "item": e.get("item", "")}

        def fn(d):
            inst = d.get("installed") if isinstance(d.get("installed"), dict) else {}
            inst.update(rows)
            d["installed"] = inst

        self.state.update(fn)

    # ── approving ─────────────────────────────────────────────────────────────────────────────

    def pending(self) -> list[dict]:
        """Executables and DLLs waiting for approval: every installed one whose approval isn't
        its install-time SHA-256 (or whose size/mtime changed since), plus unrecorded .exe/.dll
        files sitting beside them (a planted file). Installed files are not re-hashed here, so a
        status poll stays cheap; approve() and the launch check hash. Rows:
        [{path, sha256, publisher, release, file, item, changed}]."""
        v = self._view()
        out = []
        folders = set()
        for key, row in sorted(v.inst.items()):
            folders.add(os.path.dirname(key))
            try:
                st = os.stat(key)
            except OSError:
                continue
            changed = ("size" in row and st.st_size != row["size"]) or \
                      ("mtime_ns" in row and st.st_mtime_ns != row["mtime_ns"])
            if not changed and row.get("sha256") and v.appr.get(key) == row["sha256"]:
                continue
            out.append({"path": key, "sha256": row.get("sha256", ""), "publisher": row.get("publisher", ""),
                        "release": row.get("release", ""), "file": row.get("file") or os.path.basename(key),
                        "item": row.get("item", ""), "changed": bool(changed)})
        with self._cache_lock:
            refused = list(self._refused.items())
        extra = [(norm(os.path.join(f, n)), n) for f in sorted(folders) for n in self._execs_in(f)]
        listed = set()
        for key, name in extra + refused:  # planted beside installed files, then refused launches
            if key in v.inst or key in listed:
                continue
            listed.add(key)
            sha = self.current_sha256(key)
            if sha is None or v.appr.get(key) == sha:
                continue
            out.append({"path": key, "sha256": sha, "publisher": UNKNOWN_PUBLISHER, "release": "",
                        "file": name, "item": "", "changed": False})
        return out

    @staticmethod
    def _execs_in(folder: str) -> list[str]:
        try:
            with os.scandir(folder) as it:
                return sorted(e.name for e in it if _is_exec(e.name) and e.is_file())
        except OSError:
            return []

    def approve(self, path, sha256: str | None = None, *, notify: bool = True) -> bool:
        """Record the file as approved by its SHA-256 now. False when it is missing, when it no
        longer matches what Setup installed there, or when `sha256` (what the user was shown)
        doesn't match it."""
        key = norm(path)
        now = self.current_sha256(key)
        if now is None:
            return False
        if sha256 is not None and sha256.lower() != now:
            return False
        row = self._view().inst.get(key)
        if row and row.get("sha256") and row["sha256"] != now:
            return False  # changed since Setup placed it from a verified archive

        def fn(d):
            appr = d.get("approvals") if isinstance(d.get("approvals"), dict) else {}
            appr[key] = now
            d["approvals"] = appr

        self.state.update(fn)
        if notify:
            self.notify()
        return True

    def notify(self) -> None:
        hook = self.on_approved
        if hook is not None:
            try:
                hook()
            except Exception:
                log.exception("the on_approved hook failed")

    def _approved(self, key: str, v: _View) -> bool:
        want = v.appr.get(key)
        return bool(want) and self.current_sha256(key) == want

    def approved(self, path) -> bool:
        """The file exists and its current SHA-256 is the approved one."""
        return self._approved(norm(path), self._view())

    # ── the launch check ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _marked(key: str) -> bool:
        """Some folder at or above the file holds Setup's marker."""
        d = os.path.dirname(key)
        while True:
            if os.path.isfile(os.path.join(d, SETUP_MARKER)):
                return True
            parent = os.path.dirname(d)
            if parent == d:
                return False
            d = parent

    @staticmethod
    def _under_roots(key: str, v: _View) -> bool:
        return key in v.inst or any(key == r or key.startswith(r.rstrip(os.sep) + os.sep) for r in v.roots)

    @staticmethod
    def _in_setup_folders(key: str, v: _View) -> bool:
        """For the launch guard: a recorded installed file, or under <root>/tools or
        <root>/.setup of a recorded root (with the markers, that covers all Setup places)."""
        if key in v.inst:
            return True
        for r in v.roots:
            for sub in ("tools", ".setup"):
                d = os.path.normcase(os.path.join(r, sub))
                if key == d or key.startswith(d.rstrip(os.sep) + os.sep):
                    return True
        return False

    def owned_check(self, extra_roots=()) -> Callable[[str], bool]:
        """guarded() for many paths with one read of setup.json (plus `extra_roots`, e.g. the
        pending plan's root). If setup.json can't be read, every path counts as Setup's."""
        try:
            v = self._view()
        except OSError:
            return lambda path: True
        v.roots = v.roots + [norm(r) for r in extra_roots if r]

        def owned(path) -> bool:
            key = norm(path)
            return self._under_roots(key, v) or self._marked(key)

        return owned

    def guarded(self, path) -> bool:
        """Setup placed this file, or it lies in a Setup-marked folder or in a Setup root's
        tools/ or .setup/ folder."""
        key = norm(path)
        return self._marked(key) or self._in_setup_folders(key, self._view())

    @staticmethod
    def _locate(executable: str) -> str | None:
        """The file Popen (cwd=REPO_ROOT) would run: relative paths against REPO_ROOT, bare
        names from REPO_ROOT or else the PATH."""
        if os.path.isabs(executable):
            return executable
        if os.path.dirname(executable):
            return os.path.join(REPO_ROOT, executable)
        local = os.path.join(REPO_ROOT, executable)
        if os.path.isfile(local):
            return local
        return shutil.which(executable)

    def refusal(self, executable: str) -> str | None:
        """Why this executable may not run (None: it may). Reads setup.json once; an unreadable
        setup.json raises (the launch check turns that into a refusal)."""
        if not executable:
            return None
        path = self._locate(executable)
        if path is None:
            return None  # not found: the launch itself reports that
        key = norm(path)
        v = self._view()
        if not (self._marked(key) or self._in_setup_folders(key, v)):
            return None
        name = os.path.basename(path)
        if not self._approved(key, v):
            self._remember(key, os.path.basename(path))
            return f"approve {os.path.basename(executable)} in Setup before it can run"
        folder = os.path.dirname(key)
        for other in self._execs_in(folder):
            okey = norm(os.path.join(folder, other))
            if okey != key and not self._approved(okey, v):
                self._remember(okey, other)
                return f"approve {other} in Setup before {name} can run"
        return None

    def _remember(self, key: str, name: str) -> None:
        """A guarded file a launch was refused for: pending() lists it even when Setup didn't
        record it, so it can be shown and approved on its own."""
        with self._cache_lock:
            self._refused.pop(key, None)
            self._refused[key] = name
            while len(self._refused) > MAX_REFUSED:
                self._refused.pop(next(iter(self._refused)))


def launch_check(var_dir, approvals: Approvals | None = None) -> Callable[[str], str | None]:
    """may_launch(executable) for primer.servers: a refusal reason, or None. Fails closed."""
    ap = approvals or Approvals(var_dir)

    def may_launch(executable: str) -> str | None:
        try:
            return ap.refusal(executable)
        except Exception as e:  # never launch on a check that broke
            log.exception("launch check failed")
            return f"Setup could not check {os.path.basename(str(executable))}: {e}"

    return may_launch
