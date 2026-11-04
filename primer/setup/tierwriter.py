"""Write a ready tier (tiers/local-<kit>.toml) for an installed kit.

A small TOML writer for the tier structure Primer reads (no third-party TOML library). The tier
is regenerated whole, never edited in place, and only ever under a `local-` name, so a user's
own tiers are untouched. Every path is absolute with forward slashes.
"""

from __future__ import annotations

import math
import os
import re
import socket
import tempfile
import tomllib
from pathlib import Path

from primer.config import TIERS_DIR, ConfigError, load_tier
from primer.setup.kits import Plan, PlanItem

DEFAULT_PORTS = {"text": 8191, "fast": 8192, "images": 8820, "reference": 8830, "voice": 8840}
# used by other tools on this kind of machine: never chosen, even when free right now
AVOID_PORTS = frozenset({1234, 8091, 8092, 8766, 8810, 8811, 8813, 8814})
MAX_TRIES = 200
LOCAL_NAME = re.compile(r"local-[A-Za-z0-9_-]+")
NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}


class TierError(ValueError):
    pass


# ── TOML ──────────────────────────────────────────────────────────────────────────────────────

_BARE = re.compile(r"[A-Za-z0-9_-]+")
_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _str(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ESC:
            out.append(_ESC[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _key(k) -> str:
    if not isinstance(k, str) or not k:
        raise TierError(f"bad TOML key {k!r}")
    return k if _BARE.fullmatch(k) else _str(k)


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise TierError("a tier can't hold inf or nan")
        return repr(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }" if v else "{}"
    raise TierError(f"can't write {type(v).__name__} to TOML")


def dumps(data: dict) -> str:
    """Top-level scalars, then [tables], their [table.sub] tables, and inline tables below that."""
    lines = []
    tables = []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{_key(k)} = {_value(v)}")
    for k, v in tables:
        _table(lines, [k], v)
    return "\n".join(lines) + "\n"


def _table(lines: list, path: list, table: dict) -> None:
    if lines:
        lines.append("")
    lines.append("[" + ".".join(_key(p) for p in path) + "]")
    subs = []
    for k, v in table.items():
        if isinstance(v, dict) and len(path) == 1:
            subs.append((k, v))
        else:
            lines.append(f"{_key(k)} = {_value(v)}")
    for k, v in subs:
        _table(lines, path + [k], v)


# ── ports ─────────────────────────────────────────────────────────────────────────────────────


def port_free(port: int) -> bool:
    """Nothing answers on 127.0.0.1:port and it can be bound (briefly, then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return False
        except OSError:
            pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: don't share a port in use
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def choose_ports(probe=port_free, defaults: dict | None = None, roles=None) -> dict:
    """{role: port}, each from its default upwards, skipping ports in use, reserved or taken."""
    defaults = dict(DEFAULT_PORTS if defaults is None else defaults)
    out: dict[str, int] = {}
    for role, start in defaults.items():
        if roles is not None and role not in roles:
            continue
        port = start
        for _ in range(MAX_TRIES):
            if port not in AVOID_PORTS and port not in out.values() and 1024 <= port <= 65535 and probe(port):
                break
            port += 1
        else:
            raise TierError(f"no free port near {start} for {role}")
        out[role] = port
    return out


# ── the tier ──────────────────────────────────────────────────────────────────────────────────


def _llama(roles: dict, at) -> str:
    rt = roles.get("runtime_llama")
    if rt is not None and rt.use_path:
        return rt.use_path.replace("\\", "/")
    if rt is not None:
        return at(rt, rt.exe)
    raise TierError("the kit has no llama.cpp runtime (skipped or not available) and no "
                    "llama-server was found on the PATH, so its models can't be started")


def _cpu_args(roles: dict, plan: Plan) -> list[str]:
    """Extra arguments for a CPU-only server: a GPU build of llama.cpp (CUDA, Vulkan, or one from
    the PATH whose kind is unknown) still claims the GPU unless told --device none."""
    rt = roles.get("runtime_llama")
    if rt is not None and not rt.use_path and rt.choice == "cpu":
        return []
    return ["--device", "none"]


def _engine(llama: str, model: str, port: int, *, gpu: bool, ctx: int, cpu_args=()) -> dict:
    launch = [llama, "-m", model, "--port", str(port), "-ngl", "99" if gpu else "0", "-c", str(ctx)]
    return {
        "kind": "openai",
        "base_url": f"http://127.0.0.1:{port}",
        "model": Path(model).stem,
        "managed": True,
        "gpu": gpu,
        "launch": launch + ([] if gpu else list(cpu_args)),
        "ready_timeout_s": 180,
        "extra": NO_THINKING,
    }


_LOCAL_URL = re.compile(r"http://127\.0\.0\.1:(\d+)")


def ports_in(path: Path) -> set[int]:
    """The ports an existing tier file uses (reusable when that same tier is rewritten)."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return set()
    out: set[int] = set()
    for key in ("text", "fast", "images", "reference", "voice"):
        sec = data.get(key)
        if not isinstance(sec, dict):
            continue
        port = sec.get("port")
        if isinstance(port, int) and not isinstance(port, bool):
            out.add(port)
        m = _LOCAL_URL.match(sec.get("base_url", "")) if isinstance(sec.get("base_url"), str) else None
        if m:
            out.add(int(m.group(1)))
    return out


def build(plan: Plan, root, name: str, probe=port_free, reuse=frozenset()) -> dict:
    """The tier as a dict ready for dumps(). Ports in `reuse` (the tier being replaced uses
    them, so its own servers may hold them) count as free."""
    root = Path(root).resolve()

    def at(item: PlanItem, sub: str | None = None) -> str:
        p = root / item.dest
        return (p / sub if sub else p).as_posix()

    roles: dict[str, PlanItem] = {}
    zims: list[PlanItem] = []
    for it in plan.items:
        if it.error:
            continue
        if it.role == "reference":
            zims.append(it)
        else:
            roles.setdefault(it.role, it)
    text = roles.get("text")
    if text is None:
        raise TierError("the kit has no writing model, so no tier can be written")
    has_ref = bool(zims) and "runtime_kiwix" in roles
    has_voice = "voice" in roles and "voice_mmproj" in roles
    wanted = {"text"}
    wanted |= {"fast"} if "fast" in roles else set()
    wanted |= {"images"} if "runtime_sd" in roles else set()
    wanted |= {"reference"} if has_ref else set()
    wanted |= {"voice"} if has_voice else set()
    llama = _llama(roles, at)
    cpu_args = _cpu_args(roles, plan)
    ports = choose_ports(lambda p: p in reuse or probe(p), roles=wanted)

    data: dict = {"name": name}
    data["source_mode"] = "reference" if has_ref else "storyteller"
    data["gpu_mode"] = "auto"
    if "fast" not in roles:
        data["fast"] = "text"  # one server does both
    data["text"] = _engine(llama, at(text), ports["text"], gpu=text.gpu, ctx=8192,
                           cpu_args=cpu_args)  # used only when the text runs on the CPU
    if "fast" in roles:
        data["fast"] = _engine(llama, at(roles["fast"]), ports["fast"], gpu=False, ctx=4096,
                               cpu_args=cpu_args)
    if "runtime_sd" in roles:
        sd = roles["runtime_sd"]
        data["images"] = {"sd_server": at(sd, sd.exe), "port": ports["images"]}
    if has_ref:
        kiwix = roles["runtime_kiwix"]
        data["reference"] = {"kiwix_serve": at(kiwix, kiwix.exe), "zims": [at(z) for z in zims],
                             "port": ports["reference"]}
    if "map" in roles and "fonts" in roles:
        fonts = roles["fonts"]
        data["map"] = {"pmtiles": at(roles["map"]), "assets": at(fonts, fonts.assets_subdir)}
    if has_voice:
        p = ports["voice"]
        data["voice"] = {
            "kind": "lfm3", "base_url": f"http://127.0.0.1:{p}", "voice": "female", "managed": True,
            "launch": [llama, "-m", at(roles["voice"]), "--mmproj", at(roles["voice_mmproj"]),
                       "--port", str(p), "-ngl", "0", "-c", "8192", *cpu_args],
            "ready_timeout_s": 180,
        }
    return data


def write(plan: Plan, root, tiers_dir: Path = TIERS_DIR, name: str | None = None, *, probe=port_free) -> Path:
    """Write tiers_dir/<name>.toml (default local-<kit>) atomically, after checking that
    primer.config.load_tier accepts it."""
    name = name or f"local-{plan.kit}"
    if not LOCAL_NAME.fullmatch(name):
        raise TierError(f"Setup only writes tiers named local-<kit>, not {name!r}")
    tiers_dir = Path(tiers_dir)
    path = tiers_dir / f"{name}.toml"
    data = build(plan, root, name, probe, reuse=ports_in(path))
    text = (f"# Written by Primer Setup for the {plan.label} kit. Setup regenerates this file whole,\n"
            "# so make your own tier (a copy under another name) to change it.\n\n" + dumps(data))
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:  # e.g. a path holding an undecodable (surrogate) character
        raise TierError("a path in this tier can't be written as UTF-8") from None
    try:
        tomllib.loads(text)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / f"{name}.toml").write_bytes(raw)
            load_tier(name, Path(tmp))
    except (tomllib.TOMLDecodeError, ConfigError) as e:
        raise TierError(f"the written tier would not load: {e}") from e
    tiers_dir.mkdir(parents=True, exist_ok=True)
    part = tiers_dir / f".{name}.toml.tmp"
    try:
        part.write_bytes(raw)
        os.replace(part, path)
    finally:
        if part.exists():
            part.unlink()
    return path
