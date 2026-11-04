"""The tier writer: a small TOML writer, port choice, and tiers that load_tier accepts."""

import socket
import tomllib

import pytest

from primer.config import load_tier
from primer.servers import ServerManager
from primer.setup import tierwriter
from primer.setup.kits import resolve
from primer.setup.tierwriter import AVOID_PORTS, TierError, choose_ports, dumps, port_free, write
from tests.test_setup_kits import FakeCatalogues


def nothing(item):
    return False


def all_free(port):
    return True


# ── TOML ──────────────────────────────────────────────────────────────────────────────────────


def test_toml_writer_round_trips():
    data = {
        "name": 'odd "name" \\ with\ttabs\nand lines \x01',
        "fast": "text",
        "n": 3, "x": 1.5, "flag": False,
        "text": {
            "launch": ["C:/Program Files/llama/llama-server.exe", "-m", 'a\\b "c".gguf'],
            "ready_timeout_s": 180,
            "extra": {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 40},
            "odd key": "v",
        },
    }
    text = dumps(data)
    assert tomllib.loads(text) == data
    assert "[text.extra]" in text and "chat_template_kwargs = { enable_thinking = false }" in text
    with pytest.raises(TierError):
        dumps({"x": float("nan")})
    with pytest.raises(TierError):
        dumps({"x": object()})


# ── ports ─────────────────────────────────────────────────────────────────────────────────────


def test_ports_avoid_busy():
    busy = {8191, 8192, 8193, 8820, 8840}
    ports = choose_ports(lambda p: p not in busy)
    assert ports == {"text": 8194, "fast": 8195, "images": 8821, "reference": 8830, "voice": 8841}
    # never the ports other tools use, even when they look free
    assert {1234, 8091, 8092, 8766, 8810, 8811, 8813, 8814} <= AVOID_PORTS
    start = dict(tierwriter.DEFAULT_PORTS, images=8810)
    ports = choose_ports(all_free, defaults=start)
    assert ports["images"] == 8812  # 8810 and 8811 are reserved
    assert len(set(ports.values())) == len(ports)

    # the real probe: a port someone listens on is not free; once released it is
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert not port_free(port)
    finally:
        s.close()
    assert port_free(port)


# ── writing tiers ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kit", ["light", "hybrid", "workstation"])
def test_tier_written_loads(tmp_path, kit):
    root = tmp_path / "Primer Data"
    tiers = tmp_path / "tiers"
    tiers.mkdir()
    plan = resolve(kit, FakeCatalogues(), nothing)
    path = write(plan, root, tiers, probe=all_free)
    assert path == tiers / f"local-{kit}.toml" and path.is_file()
    assert not list(tiers.glob("*.tmp"))
    tier = load_tier(f"local-{kit}", tiers)
    rootp = root.resolve().as_posix()

    by_role = {i.role: i for i in plan.items}
    llama = f"{rootp}/{by_role['runtime_llama'].dest}llama-server.exe"
    text_model = f"{rootp}/{by_role['text'].dest}"
    assert tier.text.launch[0] == llama
    assert tier.text.launch[1:3] == ("-m", text_model)
    assert tier.text.extra == {"chat_template_kwargs": {"enable_thinking": False}}
    assert tier.text.ready_timeout_s == 180 and tier.text.managed
    assert tier.text.base_url == "http://127.0.0.1:8191"
    assert tier.gpu_mode == "auto"
    assert tier.source_mode == "reference"  # every kit has a reference
    assert tier.voice.ready_timeout_s == 180
    if kit == "light":
        assert tier.fast is tier.text  # one server for both
        assert len(ServerManager(tier)._unique()) == 3  # text(=fast), reference, voice
        assert "-ngl" in tier.text.launch and tier.text.launch[tier.text.launch.index("-ngl") + 1] == "0"
        assert not tier.text.gpu
        assert tier.images is None
    else:
        assert tier.text.gpu
        assert tier.text.launch[tier.text.launch.index("-ngl") + 1] == "99"
        assert tier.text.launch[tier.text.launch.index("-c") + 1] == "8192"
        assert not tier.fast.gpu and tier.fast.base_url == "http://127.0.0.1:8192"
        assert tier.fast.launch[tier.fast.launch.index("-ngl") + 1] == "0"
        assert tier.fast.launch[tier.fast.launch.index("-c") + 1] == "4096"
        assert tier.fast.extra == {"chat_template_kwargs": {"enable_thinking": False}}
        assert tier.images.sd_server == f"{rootp}/{by_role['runtime_sd'].dest}sd-server.exe"
        assert tier.images.port == 8820
    zims = [f"{rootp}/{i.dest}" for i in plan.items if i.role == "reference"]
    assert tier.reference.zims == tuple(zims) and tier.reference.port == 8830
    assert tier.reference.kiwix_serve.startswith(f"{rootp}/tools/kiwix-tools/")
    assert tier.map.pmtiles == f"{rootp}/{by_role['map'].dest}"
    assert tier.map.assets.startswith(f"{rootp}/maps/basemaps-assets/basemaps-assets-028c18f")
    v = tier.voice
    assert v.kind == "lfm3" and v.base_url == "http://127.0.0.1:8840" and v.managed
    assert v.launch[v.launch.index("--mmproj") + 1] == f"{rootp}/{by_role['voice_mmproj'].dest}"
    assert v.launch[v.launch.index("-ngl") + 1] == "0"
    for arg in tier.text.launch + v.launch:
        assert "\\" not in arg
    # CPU servers launched from a GPU build of llama.cpp are kept off the GPU; Light has no GPU use
    cpu_flag = kit != "light"
    assert ("--device" in v.launch) == cpu_flag
    assert ("--device" in tier.fast.launch) == cpu_flag
    assert "--device" not in tier.text.launch
    if cpu_flag:
        assert v.launch[v.launch.index("--device") + 1] == "none"
        assert tier.fast.launch[tier.fast.launch.index("--device") + 1] == "none"


def test_cpu_build_needs_no_device_flag(tmp_path):
    plan = resolve("hybrid", FakeCatalogues(), nothing, {"llama_cpp": "cpu"})
    write(plan, tmp_path / "root", tmp_path, probe=all_free)
    tier = load_tier("local-hybrid", tmp_path)
    for launch in (tier.text.launch, tier.fast.launch, tier.voice.launch):
        assert "--device" not in launch
    # llama-server from the PATH: its build is unknown, so the CPU roles get the flag
    plan = resolve("hybrid", FakeCatalogues(), nothing, llama_server="/opt/llama-server")
    write(plan, tmp_path / "root", tmp_path, probe=all_free)
    tier = load_tier("local-hybrid", tmp_path)
    assert "--device" in tier.fast.launch and "--device" in tier.voice.launch
    assert "--device" not in tier.text.launch


def test_no_llama_runtime_is_an_error(tmp_path):
    plan = resolve("light", FakeCatalogues(), nothing, {"llama_cpp": "skip"})
    with pytest.raises(TierError, match="llama"):
        write(plan, tmp_path / "root", tmp_path, probe=all_free)
    assert not list(tmp_path.glob("*.toml"))


def test_rewrite_reuses_its_own_ports(tmp_path):
    plan = resolve("hybrid", FakeCatalogues(), nothing)
    write(plan, tmp_path / "root", tmp_path, probe=all_free)
    first = load_tier("local-hybrid", tmp_path)
    # now that tier's servers are running on those ports: rewriting keeps them
    taken = {8191, 8192, 8820, 8830, 8840}
    write(plan, tmp_path / "root", tmp_path, probe=lambda p: p not in taken)
    again = load_tier("local-hybrid", tmp_path)
    assert again.text.base_url == first.text.base_url and again.voice.base_url == first.voice.base_url
    assert again.images.port == first.images.port and again.reference.port == first.reference.port
    # an unreadable old tier is ignored: busy ports are then skipped
    (tmp_path / "local-hybrid.toml").write_bytes(b"\xff\xfe not toml")
    write(plan, tmp_path / "root", tmp_path, probe=lambda p: p not in taken)
    assert load_tier("local-hybrid", tmp_path).text.base_url == "http://127.0.0.1:8193"


def test_unencodable_path_is_a_tier_error(tmp_path):
    plan = resolve("light", FakeCatalogues(), nothing)
    with pytest.raises(TierError, match="UTF-8"):
        write(plan, tmp_path / "bad\udcffname", tmp_path, probe=all_free)
    assert not list(tmp_path.glob("*.toml")) and not list(tmp_path.glob(".*.tmp"))


def test_tier_uses_path_llama_and_skips_sections(tmp_path):
    tiers = tmp_path / "tiers"
    tiers.mkdir()
    plan = resolve("hybrid", FakeCatalogues(), nothing,
                   {"sd_cpp": "skip", "zim_top": "skip", "map": "skip"},
                   llama_server="C:\\Tools\\llama\\llama-server.exe")
    write(plan, tmp_path / "root", tiers, probe=all_free)
    tier = load_tier("local-hybrid", tiers)
    assert tier.text.launch[0] == "C:/Tools/llama/llama-server.exe"
    assert tier.images is None and tier.reference is None and tier.map is None
    assert tier.source_mode == "storyteller"
    raw = tomllib.loads((tiers / "local-hybrid.toml").read_text(encoding="utf-8"))
    assert raw["source_mode"] == "storyteller"


def test_tier_name_and_plan_checked(tmp_path):
    plan = resolve("light", FakeCatalogues(), nothing)
    with pytest.raises(TierError, match="local-"):
        write(plan, tmp_path, tmp_path, name="hybrid", probe=all_free)  # never a user's own tier
    with pytest.raises(TierError, match="writing"):
        write(resolve("light", FakeCatalogues(), nothing, {"writing_model": "skip"}),
              tmp_path, tmp_path, probe=all_free)
    # rewriting replaces the file
    p1 = write(plan, tmp_path / "a", tmp_path, probe=all_free)
    p2 = write(plan, tmp_path / "b", tmp_path, probe=all_free)
    assert p1 == p2 and "/b/" in p2.read_text(encoding="utf-8")


def test_cpu_servers_on_a_gpu_build_stay_off_the_gpu(tmp_path):
    # Light offers only the CPU build; a Vulkan build (Hybrid's choice) runs fast and voice on the CPU
    plan = resolve("hybrid", FakeCatalogues(), nothing, {"llama_cpp": "vulkan"})
    write(plan, tmp_path / "root", tmp_path, probe=all_free)
    tier = load_tier("local-hybrid", tmp_path)
    for launch in (tier.fast.launch, tier.voice.launch):
        assert launch[launch.index("--device") + 1] == "none"
