"""Move models: list the configured model files with their drive, and copy chosen ones to another
folder, verifying each copy, then point Setup's own tiers and var/images.json at the copies.

The originals are always kept (the user deletes them when happy). Only `tiers/local-*.toml`
(Setup's own, regenerated whole anyway) is rewritten; any other tier that names a moved file is
reported, never edited. Paths are compared normalised (realpath + normcase).
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import shutil
import sys
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Callable

from primer.config import REPO_ROOT, ConfigError, Tier, load_tier
from primer.images.models import ModelStore, StoreCorrupt, kind_of
from primer.setup.approvals import norm, sha256_file
from primer.setup.tierwriter import LOCAL_NAME, TierError, dumps

log = logging.getLogger(__name__)

# GetDriveTypeW
DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM = 2, 3, 4, 5
CHUNK = 4 * 1024 * 1024
SPACE_MARGIN = 256 * 1024 ** 2
GB = 1024 ** 3


class MoveError(Exception):
    """The move can't start; `reason` is one sentence for the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MoveConflict(MoveError):
    """Refused because a move is already running."""


class _Cancelled(Exception):
    pass


SETUP_OWNED = "installed by Setup — to use another drive, choose a different folder in Setup"


# ── drives ────────────────────────────────────────────────────────────────────────────────────


def drive_of(path) -> str:
    """The drive a path is on: "D:", "\\\\server\\share", or "/" off Windows."""
    return os.path.splitdrive(os.path.abspath(os.fspath(path)))[0] or "/"


def drive_type(path) -> int | None:
    """GetDriveTypeW for the path's drive on Windows (DRIVE_* above); None elsewhere."""
    if sys.platform != "win32":
        return None
    drive = drive_of(path)
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive.rstrip("\\") + "\\")))
    except (OSError, AttributeError, ValueError):
        return None


def is_unc(path) -> bool:
    r"""A UNC share or a Windows device path (\\server\share, \\?\, \\.\, //host)."""
    return os.fspath(path)[:2] in ("\\\\", "//", "\\/", "/\\")


_PREFIXES = ("\\\\?\\UNC\\", "\\\\?\\", "\\\\.\\", "//?/UNC/", "//?/", "//./")


def plain(path) -> str:
    r"""The path without a \\?\ or \\.\ prefix (\\?\UNC\host\x becomes \\host\x)."""
    s = os.fspath(path)
    for pre in _PREFIXES:
        if s[:len(pre)].upper() == pre.upper():
            rest = s[len(pre):]
            return "\\\\" + rest if "UNC" in pre else rest
    return s


def removable(kind: int | None) -> bool:
    return kind in (DRIVE_REMOVABLE, DRIVE_CDROM)


# STORAGE_BUS_TYPE values for drives that hang off an external bus: 1394, USB, SD, MMC
_EXTERNAL_BUSES = {4, 7, 12, 13}


def bus_type(path) -> int | None:
    """The storage bus of the path's drive (STORAGE_BUS_TYPE) on Windows, or None. An external
    USB disk reports DRIVE_FIXED, so this is how it is told apart from an internal one."""
    if sys.platform != "win32":
        return None
    drive = drive_of(path)
    if len(drive) != 2 or drive[1] != ":":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        # no access rights needed to ask a volume for its device properties
        h = k32.CreateFileW(f"\\\\.\\{drive}", 0, 3, None, 3, 0, None)
        if h in (None, wintypes.HANDLE(-1).value):
            return None
        try:
            query = (ctypes.c_uint32 * 3)(0, 0, 0)  # StorageDeviceProperty, PropertyStandardQuery
            out = (ctypes.c_ubyte * 1024)()
            got = wintypes.DWORD(0)
            ok = k32.DeviceIoControl(h, 0x2D1400, query, ctypes.sizeof(query), out, ctypes.sizeof(out),
                                     ctypes.byref(got), None)  # IOCTL_STORAGE_QUERY_PROPERTY
            if not ok or got.value < 32:
                return None
            return int.from_bytes(bytes(out[28:32]), "little")  # STORAGE_DEVICE_DESCRIPTOR.BusType
        finally:
            k32.CloseHandle(h)
    except (OSError, AttributeError, ValueError):
        return None


def external(path) -> bool:
    """On a USB, FireWire or card-reader disk (slow, and it may be unplugged)."""
    return bus_type(path) in _EXTERNAL_BUSES


def slow(path, kind: int | None) -> bool:
    return removable(kind) or kind == DRIVE_REMOTE or is_unc(path) or external(path)


def fixed_drives() -> list[str]:
    """The fixed, internal drives, e.g. ["C:", "D:"] (not external USB disks); [] off Windows."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (OSError, AttributeError):
        return []
    out = []
    for i in range(26):
        if mask & (1 << i):
            d = f"{chr(65 + i)}:"
            if drive_type(d + "\\") == DRIVE_FIXED and not external(d + "\\"):
                out.append(d)
    return out


def inside(path, folder) -> bool:
    """path is folder or lies under it (normalised)."""
    p, f = norm(plain(path)), norm(plain(folder))
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


# ── the listing ───────────────────────────────────────────────────────────────────────────────


def _launch_files(prefix: str, launch) -> list[tuple[str, str]]:
    out = []
    launch = list(launch or ())
    for i, arg in enumerate(launch[:-1]):
        if arg in ("-m", "--model"):
            out.append((prefix, launch[i + 1]))
        elif arg == "--mmproj":
            out.append((f"{prefix}_mmproj", launch[i + 1]))
    return out


def tier_files(tier: Tier) -> list[tuple[str, str]]:
    """(role, path) for every model file the tier's launch commands and sections name."""
    out = _launch_files("text", tier.text.launch)
    out += _launch_files("fast", tier.fast.launch)
    if tier.voice is not None:
        out += _launch_files("voice", tier.voice.launch)
    if tier.reference is not None:
        out += [("reference", z) for z in tier.reference.zims]
    if tier.map is not None:
        out.append(("map", tier.map.pmtiles))
    return out


def list_models(tier: Tier, store: ModelStore | None, *,
                drive_type: Callable[[str], int | None] = drive_type,
                owned: Callable[[str], bool] | None = None, reloads: bool = False) -> list[dict]:
    """[{path, size, exists, drive, removable, slow, roles, source, setup, movable, why,
    reloads_tier}] for the current tier's model files and var/images.json's model files. One
    row per file (normalised); roles merged. `owned(path)`: Setup installed it (under a Setup
    root or a marked folder), so it isn't movable. `reloads`: the current tier is Setup's own,
    so moving one of its files restarts its servers."""
    rows = _rows(tier, store, drive_type)
    for row in rows:
        row["setup"] = bool(owned(row["path"])) if owned is not None else False
        row["movable"] = row["exists"] and not row["setup"]
        row["why"] = SETUP_OWNED if row["setup"] else ("" if row["exists"] else "the file is missing")
        row["reloads_tier"] = reloads and row["source"] == "tier"
    return rows


def _rows(tier: Tier, store: ModelStore | None, drive_type) -> list[dict]:
    found: list[tuple[str, str, str]] = [(role, p, "tier") for role, p in tier_files(tier)]
    if store is not None:
        for m in store.list():
            for slot, p in m.files.items():
                found.append((f"{kind_of(m)}: {m.name} ({slot})", p, "images"))
    rows: dict[str, dict] = {}
    types: dict[str, int | None] = {}
    for role, p, source in found:
        if not isinstance(p, str) or not p or not os.path.isabs(p):
            continue
        key = norm(p)
        row = rows.get(key)
        if row is None:
            path = os.path.normpath(p)
            drive = drive_of(path)
            if drive not in types:
                types[drive] = drive_type(path)
            kind = types[drive]
            try:
                size, exists = os.stat(path).st_size, True
            except OSError:
                size, exists = None, False
            row = rows[key] = {"path": path, "size": size, "exists": exists, "drive": drive,
                               "removable": removable(kind), "slow": slow(path, kind),
                               "roles": [], "source": source}
        if role not in row["roles"]:
            row["roles"].append(role)
        if source == "tier":
            row["source"] = "tier"
    return list(rows.values())


# ── moving ────────────────────────────────────────────────────────────────────────────────────


def _free(path) -> int:
    return shutil.disk_usage(path).free


def check(paths, dest, allowed, free_space: Callable = _free, *,
          owned: Callable[[str], bool] | None = None,
          drive_type: Callable[[str], int | None] = drive_type) -> tuple[list[Path], Path, int]:
    """Validate a move; (sources, dest, bytes to copy) or MoveError. A file of the same name and
    size already at dest isn't counted (it is reused if identical, refused if not)."""
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise MoveError("choose the files to move")
    if not isinstance(dest, (str, os.PathLike)) or not os.fspath(dest) or "\0" in os.fspath(dest):
        raise MoveError("the destination must be an absolute folder")
    raw = os.fspath(dest)
    if is_unc(raw) or is_unc(plain(raw)):
        raise MoveError("the destination must be a folder on a local drive, not a network or device path")
    dest = Path(raw)
    if not dest.is_absolute():
        raise MoveError("the destination must be an absolute folder")
    dest = Path(os.path.abspath(dest))
    kind = drive_type(str(dest))
    if kind == DRIVE_REMOTE:
        raise MoveError("the destination is a network drive; choose a local drive")
    if removable(kind):
        raise MoveError("the destination is a removable drive (slow, and it may be unplugged); choose a fixed drive")
    if not dest.is_dir():
        raise MoveError(f"the destination {dest} does not exist")
    if inside(dest, REPO_ROOT):
        raise MoveError("the destination is inside the Primer folder; choose another")
    ok = {norm(plain(a)) for a in allowed}
    srcs: list[Path] = []
    seen = set()
    for p in paths:
        key = norm(plain(p)) if "\0" not in p else ""
        if not key or not os.path.isabs(p) or key not in ok:
            raise MoveError(f"{p} is not a configured model file")
        if key in seen:
            continue
        seen.add(key)
        src = Path(os.path.normpath(plain(p)))
        if owned is not None and owned(str(src)):
            raise MoveError(f"{src.name} is {SETUP_OWNED}")
        if not src.is_file():
            raise MoveError(f"{src} is missing")
        if norm(dest / src.name) == norm(src):
            raise MoveError(f"{src.name} is already in {dest}")
        srcs.append(src)
    total = 0
    for src in srcs:
        size = src.stat().st_size
        try:
            there = (dest / src.name).stat().st_size
        except OSError:
            there = None
        if there != size:
            total += size
    try:
        free = free_space(dest)
    except OSError as e:
        raise MoveError(f"cannot check the free space on {dest}: {e}") from None
    if free < total + SPACE_MARGIN:
        short = total + SPACE_MARGIN - free
        raise MoveError(f"not enough space on {dest}: about {short / GB:.1f} GB more is needed")
    return srcs, dest, total


def _copy(src: Path, part: Path, cancel, step: Callable[[int], None]) -> str:
    """Stream src into part; the SHA-256 of what was read."""
    h = hashlib.sha256()
    with open(src, "rb") as f, open(part, "wb") as out:
        while True:
            if cancel is not None and cancel.is_set():
                raise _Cancelled()
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
            out.write(block)
            step(len(block))
        out.flush()
        os.fsync(out.fileno())
    return h.hexdigest()


def _same(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and sha256_file(a) == sha256_file(b)
    except OSError:
        return False


def _replace(obj, mapping: dict[str, Path], hits: list[str]):
    if isinstance(obj, str):
        if os.path.isabs(obj) and "\0" not in obj and norm(obj) in mapping:
            hits.append(obj)
            return mapping[norm(obj)].as_posix()
        return obj
    if isinstance(obj, list):
        return [_replace(x, mapping, hits) for x in obj]
    if isinstance(obj, dict):
        return {k: _replace(v, mapping, hits) for k, v in obj.items()}
    return obj


def _write_tier(path: Path, original: str, data: dict) -> None:
    head = []
    for line in original.splitlines():
        if not line.startswith("#"):
            break
        head.append(line)
    text = ("\n".join(head) + "\n\n" if head else "") + dumps(data)
    name = path.stem
    try:
        tomllib.loads(text)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / path.name).write_text(text, encoding="utf-8")
            load_tier(name, Path(tmp))
    except (tomllib.TOMLDecodeError, ConfigError) as e:
        raise TierError(f"the updated tier would not load: {e}") from e
    part = path.with_name(f".{path.name}.tmp")
    try:
        part.write_text(text, encoding="utf-8")
        os.replace(part, path)
    finally:
        if part.exists():
            part.unlink()


def _update_tiers(tiers_dir: Path, mapping: dict[str, Path], out: dict) -> None:
    for f in sorted(Path(tiers_dir).glob("*.toml")):
        local = bool(LOCAL_NAME.fullmatch(f.stem))
        try:
            original = f.read_text(encoding="utf-8")
            data = tomllib.loads(original)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            if local:
                out["failed"].append({"path": str(f), "error": f"{f.name} could not be read: {e}"})
            continue
        hits: list[str] = []
        new = _replace(data, mapping, hits)
        if not hits:
            continue
        if local:
            try:
                _write_tier(f, original, new)
                out["tiers_updated"].append(f.stem)
            except (OSError, TierError) as e:
                out["failed"].append({"path": str(f), "error": f"{f.name} could not be updated: {e}"})
        else:
            for h in hits:
                row = {"tier": f.stem, "path": os.path.normpath(h)}
                if row not in out["other_tiers"]:
                    out["other_tiers"].append(row)


def _update_images(var_dir: Path, mapping: dict[str, Path], out: dict) -> None:
    store = ModelStore(var_dir)
    try:
        models = store.list()
    except StoreCorrupt as e:
        out["failed"].append({"path": str(store.path), "error": str(e)})
        return
    def change(m):
        files = {k: (str(mapping[norm(v)]) if isinstance(v, str) and os.path.isabs(v) and norm(v) in mapping else v)
                 for k, v in m.files.items()}
        return dataclasses.replace(m, files=files) if files != m.files else None

    for m in models:
        if change(m) is None:
            continue
        try:  # re-read and saved under the store's lock, with the store's own validation
            saved = store.update(m.id, change)
            if saved is not None:
                out["images_updated"].append(saved.name)
        except KeyError:
            pass  # deleted meanwhile
        except (ValueError, OSError, StoreCorrupt) as e:
            out["failed"].append({"path": str(store.path), "error": f"{m.name}: {e}"})


def move(paths, dest, *, allowed, tiers_dir, var_dir, progress: Callable | None = None,
         cancel: threading.Event | None = None, free_space: Callable = _free,
         owned: Callable[[str], bool] | None = None,
         drive_type: Callable[[str], int | None] = drive_type) -> dict:
    """Copy each file to dest (as <name>.part, SHA-256 of the source and of the copy compared,
    then renamed), then rewrite the paths. Raises MoveError before copying anything when the
    request is bad. {moved: [{from, to}], failed: [{path, error}], other_tiers: [{tier, path}],
    tiers_updated, images_updated, cancelled}. `progress(current, done_bytes, total_bytes)`."""
    srcs, dest, _ = check(paths, dest, allowed, free_space, owned=owned, drive_type=drive_type)
    total = sum(s.stat().st_size for s in srcs)
    out = {"moved": [], "failed": [], "other_tiers": [], "tiers_updated": [], "images_updated": [],
           "cancelled": False}
    done = 0
    report = progress or (lambda cur, d, t: None)
    for src in srcs:
        if cancel is not None and cancel.is_set():
            out["cancelled"] = True
            break
        final = dest / src.name
        size = src.stat().st_size
        report(str(src), done, total)
        if final.exists():
            if _same(src, final):
                out["moved"].append({"from": str(src), "to": str(final)})
            else:
                out["failed"].append({"path": str(src), "error": f"a different file named {src.name} is already "
                                                                 f"in {dest}; move it away first"})
            done += size
            report(str(src), done, total)
            continue
        part = final.with_name(final.name + ".part")
        base = done

        def step(n: int) -> None:
            nonlocal done
            done += n
            report(str(src), done, total)

        try:
            want = _copy(src, part, cancel, step)
            if sha256_file(part) != want:
                part.unlink()
                out["failed"].append({"path": str(src), "error": "the copy did not match the original; "
                                                                 "the drive may be failing"})
            elif final.exists():
                part.unlink()
                out["failed"].append({"path": str(src), "error": f"{src.name} appeared in {dest} meanwhile"})
            else:
                os.replace(part, final)
                out["moved"].append({"from": str(src), "to": str(final)})
        except _Cancelled:
            part.unlink(missing_ok=True)
            out["cancelled"] = True
            break
        except OSError as e:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            out["failed"].append({"path": str(src), "error": f"could not copy: {e}"})
        done = base + size
        report(str(src), done, total)
    if out["moved"]:
        mapping = {norm(m["from"]): Path(m["to"]) for m in out["moved"]}
        _update_tiers(Path(tiers_dir), mapping, out)
        _update_images(Path(var_dir), mapping, out)
    return out


class MoveJob:
    """One move at a time on a background thread; status() for GET /v1/setup/models."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._st = {"state": "idle", "current": "", "done_bytes": 0, "total_bytes": 0, "error": "", "result": None}

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            return dict(self._st)

    def start(self, paths, dest, *, allowed, tiers_dir, var_dir, free_space: Callable = _free,
              owned: Callable[[str], bool] | None = None,
              drive_type: Callable[[str], int | None] = drive_type,
              on_done: Callable[[dict], None] | None = None) -> None:
        """Validate (MoveError now) and run in the background."""
        with self._lock:
            if self.busy():
                raise MoveConflict("a move is already running")
            srcs, dest_path, _ = check(paths, dest, allowed, free_space, owned=owned, drive_type=drive_type)
            total = sum(s.stat().st_size for s in srcs)
            self._cancel = cancel = threading.Event()
            self._st = {"state": "running", "current": "", "done_bytes": 0, "total_bytes": total,
                        "error": "", "result": None}

            def progress(cur, done, tot) -> None:
                with self._lock:
                    self._st.update(current=cur, done_bytes=done, total_bytes=tot)

            def run() -> None:
                try:
                    result = move([str(s) for s in srcs], dest_path, allowed=allowed, tiers_dir=tiers_dir,
                                  var_dir=var_dir, progress=progress, cancel=cancel, free_space=free_space,
                                  owned=owned, drive_type=drive_type)
                    error = ""
                    if on_done is not None:
                        try:
                            on_done(result)
                        except Exception as e:
                            log.warning("after the move: %s", e)
                            error = f"the files were copied, but the tier could not be reloaded: {e}"
                    state = "cancelled" if result.get("cancelled") else "done"
                except MoveError as e:
                    result, error, state = None, e.reason, "failed"
                except Exception as e:  # a bug must not leave the move stuck "running"
                    log.exception("the model move failed")
                    result, error, state = None, f"unexpected error: {e}", "failed"
                with self._lock:
                    self._st.update(state=state, error=error, result=result, current="")

            self._thread = threading.Thread(target=run, name="primer-setup-move", daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)
