"""The Setup installer: plans run one item at a time with fakes (no network, no real servers)."""

import io
import json
import threading
import time
import tomllib
import zipfile
from pathlib import Path

import pytest

from primer.images.models import ModelStore
from primer.maps.pmtiles import Archive, write_archive
from primer.setup.approvals import Approvals, norm
from primer.setup.download import DownloadCancelled, DownloadError
from primer.setup.installer import Installer, SetupError
from tests.test_setup_kits import FakeCatalogues

COMMIT = "028c18f713baecad011301ff7a69acc39bcc2ae7"
ZIM_SHA = "ab" * 32
KIWIX_MD5 = "cd" * 16
PUBLIC = [(2, 1, 6, "", ("104.18.2.2", 443))]
TB = 10 ** 12


def public_resolve(host, port):
    return PUBLIC


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


def symlink_member(name):
    import stat
    info = zipfile.ZipInfo(name)
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


LLAMA_ZIP = zip_bytes([("llama-server.exe", b"MZ llama"), ("ggml.dll", b"MZ ggml"), ("LICENSE", b"MIT")])
KIWIX_ZIP = zip_bytes([("kiwix-tools_win-x86_64-3.8.1/kiwix-serve.exe", b"MZ kiwix"),
                       ("kiwix-tools_win-x86_64-3.8.1/README", b"read me")])
FONTS_ZIP = zip_bytes([(f"basemaps-assets-{COMMIT}/fonts/Noto Sans Regular/0-255.pbf", b"glyphs"),
                       (f"basemaps-assets-{COMMIT}/sprites/v4/light.json", b"{}"),
                       (symlink_member(f"basemaps-assets-{COMMIT}/fonts/Alias"), b"Noto Sans Regular")])


def blob_for(url: str) -> bytes:
    if "llama" in url and url.endswith(".zip"):
        return LLAMA_ZIP
    if "kiwix-tools" in url:
        return KIWIX_ZIP
    if "codeload" in url:
        return FONTS_ZIP
    return b"data:" + url.encode()


class FakeDownloader:
    """download()'s signature; writes the blob to dest. `block` names a URL fragment whose
    download writes half to .part and then waits for its cancel Event (or `release`)."""

    def __init__(self, block=None, fail=None):
        self.calls = []
        self.block, self.fail = block, dict(fail or {})
        self.release = threading.Event()
        self.blocked = threading.Event()

    def __call__(self, url, dest, *, size=None, sha256=None, md5=None, progress=None, cancel=None,
                 transport=None, resolve=None, token=None, allowed_ext=None, allow_unhashed=False):
        dest = Path(dest)
        self.calls.append({"url": url, "dest": dest, "size": size, "sha256": sha256, "md5": md5, "token": token,
                           "allowed_ext": allowed_ext, "part_had": (dest.with_name(dest.name + ".part")).exists()})
        for frag, reason in list(self.fail.items()):
            if frag in url:
                del self.fail[frag]
                raise DownloadError(reason)
        data = blob_for(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        if self.block and self.block in url:
            self.block = None
            part.write_bytes(data[: len(data) // 2])
            if progress:
                progress(len(data) // 2, len(data))
            self.blocked.set()
            while not self.release.is_set():
                if cancel is not None and cancel.wait(0.01):
                    raise DownloadCancelled("the download was cancelled; it can be resumed later")
        part.write_bytes(data)
        if progress:
            progress(len(data), len(data))
        part.replace(dest)
        return dest


def hash_transport(url, headers):
    if url.endswith(".sha256"):
        body = f"{ZIM_SHA}  wikipedia_en_top_mini_2026-09.zim\n".encode()
    elif url.endswith(".md5"):
        body = f"{KIWIX_MD5}  kiwix-tools_win-x86_64-3.8.1.zip\n".encode()
    else:
        return 404, {}, io.BytesIO(b"")
    return 200, {"content-length": str(len(body))}, io.BytesIO(body)


def fake_map_runner(out, maxzoom, progress, cancel):
    progress(1, 2)
    write_archive(out, [(0, b"tile")], {"name": "test"}, min_zoom=0, max_zoom=maxzoom,
                  bounds=(-180.0, -85.0, 180.0, 85.0), center=(0.0, 0.0, 0))
    progress(2, 2)


def fake_scan(folder):
    return [
        {"name": "pic", "family": "sdxl", "files": {"model": str(Path(folder) / "a.safetensors")},
         "missing": [], "notes": [], "settings": {}, "loras": []},
        {"name": "half", "family": "qwen-image-2.1", "files": {"diffusion": str(Path(folder) / "b.gguf")},
         "missing": ["vae"], "notes": [], "settings": {}, "loras": []},
    ]


def make(tmp_path, downloader=None, **kw):
    opts = dict(tiers_dir=tmp_path / "tiers", transport=hash_transport, resolve=public_resolve,
                downloader=downloader or FakeDownloader(), map_runner=fake_map_runner,
                free_space=lambda root: TB, llama_server=lambda: None, probe=lambda port: True,
                scan=fake_scan)
    opts.update(kw)
    return Installer(tmp_path / "var", FakeCatalogues(), **opts)


def accept_all(inst):
    for lic in inst.status()["licences"]:
        inst.acknowledge(lic["key"])


def states(inst):
    return {i["id"]: i["state"] for i in inst.status()["items"]}


# ── the whole run ─────────────────────────────────────────────────────────────────────────────


def test_installer_runs_plan_and_writes_tier(tmp_path):
    dl = FakeDownloader()
    inst = make(tmp_path, dl)
    root = tmp_path / "kit"
    plan = inst.plan("light", {}, root)
    assert plan.ok and inst.status()["state"] == "idle"
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    st = inst.status()
    assert st["state"] == "ready", st
    assert st["tier"] == "local-light"
    assert set(states(inst).values()) == {"done"}
    # hash files fetched first and used
    by_url = {c["url"]: c for c in dl.calls}
    zim = next(c for u, c in by_url.items() if u.endswith(".zim"))
    assert zim["sha256"] == ZIM_SHA and zim["allowed_ext"] == (".zim",)
    assert zim["size"] is None  # Kiwix sizes are rounded up to a KiB: the hash decides
    kiwix = next(c for u, c in by_url.items() if "kiwix-tools" in u)
    assert kiwix["md5"] == KIWIX_MD5 and kiwix["sha256"] is None
    # zips: downloaded under .setup, extracted, merged, recorded; zip and staging gone
    assert kiwix["dest"] == root / ".setup" / "downloads" / "kiwix-tools_win-x86_64-3.8.1.zip"
    kdir = root / "tools" / "kiwix-tools" / "3.8.1"
    assert (kdir / "kiwix-serve.exe").read_bytes() == b"MZ kiwix"  # single top folder unwrapped
    marker = json.loads((kdir / ".primer-setup.json").read_text(encoding="utf-8"))
    assert list(marker["items"]) == ["kiwix_tools"]
    assert not any((root / ".setup" / "downloads").iterdir())
    assert not (root / ".setup" / "staging").exists() or not any((root / ".setup" / "staging").iterdir())
    fonts = root / "maps" / "basemaps-assets" / f"basemaps-assets-{COMMIT}"
    assert (fonts / "fonts" / "Noto Sans Regular" / "0-255.pbf").read_bytes() == b"glyphs"
    assert not (fonts / "fonts" / "Alias").exists()  # the symlink was skipped
    # the map was cut and checked
    with Archive(root / "maps" / "world-z8.pmtiles") as a:
        assert a.header["max_zoom"] == 8
    # the tier
    tier = tomllib.loads((tmp_path / "tiers" / "local-light.toml").read_text(encoding="utf-8"))
    assert tier["text"]["launch"][0].endswith("tools/llama.cpp/b12102/llama-server.exe")
    # executables wait for approval
    pending = {p["file"] for p in st["approvals"]}
    assert pending == {"llama-server.exe", "ggml.dll", "kiwix-serve.exe"}
    row = next(p for p in st["approvals"] if p["file"] == "llama-server.exe")
    assert row["publisher"] == "ggml-org/llama.cpp" and row["release"] == "b12102"
    assert row["path"] == norm(root / "tools" / "llama.cpp" / "b12102" / "llama-server.exe")
    assert Approvals(tmp_path / "var").guarded(root / "tools" / "anything.exe")
    # the Models scan's complete proposals were added like the Models window's Add
    models = ModelStore(tmp_path / "var").list()
    assert [m.name for m in models] == ["pic"]
    assert models[0].settings["steps"] == 20  # the family defaults, as the Models window adds them
    assert st["models_added"] == ["pic"]
    inst.shutdown()
    # a second run adds nothing twice
    inst2 = make(tmp_path)
    plan2 = inst2.plan("light", {}, root)
    assert {i.id for i in plan2.items if i.present} >= {"map", "map_fonts", "llama_cpp", "kiwix_tools"}


def test_installer_resumes_from_state(tmp_path):
    dl = FakeDownloader(block="LFM3-Audio")
    inst = make(tmp_path, dl)
    root = tmp_path / "kit"
    inst.plan("light", {}, root)
    accept_all(inst)
    inst.start()
    assert dl.blocked.wait(5)
    assert inst.status()["state"] == "running"
    cur = next(i for i in inst.status()["items"] if i["state"] == "downloading")
    assert cur["bytes"] > 0 and cur["total"] > 0
    inst.shutdown()  # Primer closing mid-download: the .part stays
    parts = list(root.rglob("*.part"))
    assert len(parts) == 1

    dl2 = FakeDownloader()
    again = make(tmp_path, dl2)
    st = again.status()
    assert st["state"] == "paused"  # not restarted on its own
    assert st["items"][0]["state"] == "done"
    assert states(again)[cur["id"]] == "queued"
    assert dl2.calls == []
    again.resume()
    wait_for(lambda: again.status()["state"] in ("ready", "failed"))
    assert again.status()["state"] == "ready", again.status()
    first = dl2.calls[0]
    assert "LFM3-Audio" in first["url"] and first["part_had"]  # resumed from the .part
    assert all("LFM3-1.7B" not in c["url"] for c in dl2.calls)  # finished items not fetched again
    again.shutdown()


def test_pause_resume_and_cancel(tmp_path):
    dl = FakeDownloader(block="LFM3-Audio")
    inst = make(tmp_path, dl)
    inst.plan("light", {}, tmp_path / "kit")
    accept_all(inst)
    inst.start()
    assert dl.blocked.wait(5)
    inst.pause()
    wait_for(lambda: inst.status()["state"] == "paused")
    assert list((tmp_path / "kit").rglob("*.part"))
    inst.resume()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    inst.shutdown()

    dl = FakeDownloader(block=".zim")
    inst = make(tmp_path / "b", dl)
    inst.plan("light", {}, tmp_path / "b" / "kit")
    accept_all(inst)
    inst.start()
    assert dl.blocked.wait(5)
    zim_id = next(i["id"] for i in inst.status()["items"] if i["state"] == "downloading")
    inst.cancel(zim_id)
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    st = inst.status()
    item = next(i for i in st["items"] if i["id"] == zim_id)
    assert item["state"] == "skipped" and "cancelled" in item["error"]
    assert list((tmp_path / "b" / "kit").rglob("*.zim.part"))  # kept
    assert st["state"] == "ready"  # the tier is written without the skipped item
    tier = tomllib.loads((tmp_path / "b" / "tiers" / "local-light.toml").read_text(encoding="utf-8"))
    assert "reference" not in tier
    inst.shutdown()


# ── refusals ──────────────────────────────────────────────────────────────────────────────────


def test_licence_required(tmp_path):
    inst = make(tmp_path)
    inst.plan("light", {}, tmp_path / "kit")
    rows = inst.status()["licences"]
    lic = [l["key"] for l in rows]
    names = [l["licence"] for l in rows]
    assert "other" in names and "CC-BY-SA-4.0" in names
    # "other" for two repos is two sets of terms: accepting one doesn't accept the other
    others = [l for l in rows if l["licence"] == "other"]
    assert len(others) == 2 and len({l["link"] for l in others}) == 2
    inst.acknowledge(others[0]["key"])
    st = {l["key"]: l["accepted"] for l in inst.status()["licences"]}
    assert st[others[0]["key"]] is True and st[others[1]["key"]] is False
    with pytest.raises(SetupError, match="licence"):
        inst.start()
    for key in lic[:-1]:
        inst.acknowledge(key)
    with pytest.raises(SetupError, match=rows[-1]["licence"]):
        inst.start()
    inst.acknowledge(lic[-1])
    assert all(l["accepted"] for l in inst.status()["licences"])
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    inst.shutdown()
    # acknowledgements persist
    assert all(l["accepted"] for l in make(tmp_path).status()["licences"])


def test_start_refusals(tmp_path):
    inst = make(tmp_path, free_space=lambda root: 1024 ** 3)
    with pytest.raises(SetupError, match="kit"):
        inst.start()
    inst.plan("light", {}, tmp_path / "kit")
    accept_all(inst)
    with pytest.raises(SetupError, match=r"about [\d.]+ GB more"):
        inst.start()
    broken = Installer(tmp_path / "v2", FakeCatalogues(broken={"LiquidAI/LFM3-1.7B-GGUF"}),
                       tiers_dir=tmp_path / "t2", free_space=lambda r: TB, llama_server=lambda: None)
    broken.plan("light", {}, tmp_path / "kit2")
    with pytest.raises(SetupError, match="503"):
        broken.start()
    with pytest.raises(SetupError, match="absolute"):
        inst.plan("light", {}, "relative/folder")


def test_failed_item_reported_and_retried(tmp_path):
    dl = FakeDownloader(fail={".zim": "the SHA256 hash does not match the published hash; the download was discarded"})
    inst = make(tmp_path, dl)
    inst.plan("light", {}, tmp_path / "kit")
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    st = inst.status()
    assert st["state"] == "failed" and st["tier"] is None
    bad = next(i for i in st["items"] if i["state"] == "failed")
    assert "hash does not match" in bad["error"]
    assert sum(1 for i in st["items"] if i["state"] == "done") == len(st["items"]) - 1
    inst.resume()  # retries what failed
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    assert inst.status()["state"] == "ready"
    inst.shutdown()


def test_runtime_layout_changed_fails_the_item(tmp_path, monkeypatch):
    import tests.test_setup_installer as me
    monkeypatch.setattr(me, "LLAMA_ZIP", zip_bytes([("bin/other.exe", b"MZ")]))
    inst = make(tmp_path)
    inst.plan("light", {}, tmp_path / "kit")
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    item = next(i for i in inst.status()["items"] if i["id"] == "llama_cpp")
    assert item["state"] == "failed"
    assert item["error"] == "the release's layout changed: llama-server.exe not found"
    inst.shutdown()


def test_merge_never_overwrites_a_different_file(tmp_path):
    root = tmp_path / "kit"
    clash = root / "tools" / "llama.cpp" / "b12102" / "ggml.dll"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"someone else's dll")
    inst = make(tmp_path)
    inst.plan("light", {}, root)
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    item = next(i for i in inst.status()["items"] if i["id"] == "llama_cpp")
    assert item["state"] == "failed" and "ggml.dll" in item["error"] and "already" in item["error"]
    assert clash.read_bytes() == b"someone else's dll"
    inst.shutdown()


def test_unhashed_item_refused(tmp_path):
    inst = make(tmp_path)
    inst.plan("light", {}, tmp_path / "kit")
    zim = next(i for i in inst._plan.items if i.kind == "zim")
    zim.sha256_url = None
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    item = next(i for i in inst.status()["items"] if i["id"] == zim.id)
    assert item["state"] == "failed" and "hash" in item["error"]
    inst.shutdown()


def test_hf_token_only_for_huggingface(tmp_path):
    keys = tmp_path / "var" / "keys"
    keys.mkdir(parents=True)
    (keys / "huggingface.key").write_text("hf_secret\n", encoding="utf-8")
    dl = FakeDownloader()
    inst = make(tmp_path, dl)
    inst.plan("light", {}, tmp_path / "kit")
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    for c in dl.calls:
        want = "hf_secret" if c["url"].startswith("https://huggingface.co/") else None
        assert c["token"] == want, c["url"]
    inst.shutdown()


def test_corrupt_state_file_starts_fresh(tmp_path):
    var = tmp_path / "var"
    var.mkdir()
    (var / "setup.json").write_text("{not json", encoding="utf-8")
    inst = make(tmp_path)
    assert inst.status()["state"] == "idle"
    assert (var / "setup.json.bad").read_text(encoding="utf-8") == "{not json"


def test_status_shape(tmp_path):
    inst = make(tmp_path, clock=lambda: 0.0)
    assert inst.status() == {"state": "idle", "kit": None, "label": "", "root": None, "choices": {},
                             "plan": None, "items": [], "licences": [], "approvals": [],
                             "tier": None, "error": "", "models_added": []}
    inst.plan("light", {"map": "z9"}, tmp_path / "kit")
    st = inst.status()
    assert st["kit"] == "light" and st["choices"] == {"map": "z9"} and st["plan"]["kit"] == "light"
    assert set(st["items"][0]) == {"id", "label", "kind", "state", "bytes", "total", "speed_bps",
                                   "eta_s", "error"}
    json.dumps(st)


# ── with the real downloader ──────────────────────────────────────────────────────────────────


def test_real_downloader_end_to_end(tmp_path):
    """The real download() and extract_zip() behind the installer, over a fake HTTPS transport."""
    import hashlib

    from primer.setup import kits

    model = b"GGUF" + bytes(4000)
    llama = zip_bytes([("llama-server.exe", b"MZ real"), ("llama.dll", b"MZ lib")])
    sha = lambda b: hashlib.sha256(b).hexdigest()
    manifest = kits.validate({
        "_checked": "2026-11-04", "map_estimates": {"8": 1},
        "kits": [{"id": "mini", "label": "Mini", "for": {"min_vram_gb": 0, "max_vram_gb": None}, "items": [
            {"id": "writing_model", "role": "text", "label": "Writer", "default": "q8",
             "source": {"type": "hf", "repo": "org/model-GGUF"},
             "choices": [{"id": "q8", "label": "Q8", "file": "model-Q8_0.gguf"}]},
            {"id": "llama_cpp", "role": "runtime_llama", "label": "llama.cpp", "default": "cpu",
             "source": {"type": "github", "repo": "ggml-org/llama.cpp", "tool": "llama.cpp",
                        "exe": "llama-server.exe", "licence": "MIT"},
             "choices": [{"id": "cpu", "label": "CPU", "pattern": r"bin-win-cpu-x64\.zip$"}]},
        ]}],
    })
    url_zip = "https://github.com/ggml-org/llama.cpp/releases/download/b1/llama-b1-bin-win-cpu-x64.zip"

    class Cat:
        def hf_files(self, repo):
            return [{"path": "model-Q8_0.gguf", "size": len(model), "sha256": sha(model)}]

        def hf_card(self, repo):
            return {"license": "apache-2.0", "gated": False}

        def zims(self, *a):
            return []

        def latest_asset(self, repo, pattern, prefer_tag_prefix=None):
            return {"tag": "b1", "name": "llama-b1-bin-win-cpu-x64.zip", "size": len(llama),
                    "url": url_zip, "sha256": sha(llama)}

    blobs = {"https://huggingface.co/org/model-GGUF/resolve/main/model-Q8_0.gguf": model, url_zip: llama}

    def transport(url, headers):
        data = blobs.get(url)
        if data is None:
            return 404, {}, io.BytesIO(b"")
        return 200, {"content-length": str(len(data))}, io.BytesIO(data)

    inst = Installer(tmp_path / "var", Cat(), tiers_dir=tmp_path / "tiers", transport=transport,
                     resolve=public_resolve, manifest=manifest, free_space=lambda r: TB,
                     llama_server=lambda: None, probe=lambda p: True, scan=lambda f: [])
    root = tmp_path / "kit"
    inst.plan("mini", {}, root)
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    st = inst.status()
    assert st["state"] == "ready", st
    assert (root / "models" / "org" / "model-GGUF" / "model-Q8_0.gguf").read_bytes() == model
    assert (root / "tools" / "llama.cpp" / "b1" / "llama-server.exe").read_bytes() == b"MZ real"
    assert {p["file"] for p in st["approvals"]} == {"llama-server.exe", "llama.dll"}
    assert st["items"][0]["bytes"] == st["items"][0]["total"] == len(model)
    inst.shutdown()


# ── fix round 1 ───────────────────────────────────────────────────────────────────────────────


def test_persist_writes_the_state_current_at_write_time(tmp_path):
    inst = make(tmp_path)
    real = inst._file.update

    def racing(fn):
        with inst._lock:  # another thread changes the state after _persist was called
            inst._state = "paused"
        return real(fn)

    inst._file.update = racing
    inst._persist()
    assert json.loads((tmp_path / "var" / "setup.json").read_text(encoding="utf-8"))["state"] == "paused"


def test_start_refuses_when_the_plan_changes_meanwhile(tmp_path):
    holder = {}

    def free(root):
        holder["inst"].plan("light", {"map": "z9"}, tmp_path / "kit")  # replaced mid-check
        return TB

    inst = holder["inst"] = make(tmp_path, free_space=free)
    inst.plan("light", {}, tmp_path / "kit")
    accept_all(inst)
    with pytest.raises(SetupError, match="the plan changed"):
        inst.start()
    assert inst.status()["state"] == "idle"


def test_add_root_failure_starts_nothing(tmp_path):
    dl = FakeDownloader()
    inst = make(tmp_path, dl)
    inst.plan("light", {}, tmp_path / "kit")
    accept_all(inst)

    def boom(root):
        raise PermissionError(13, "locked")

    inst.approvals.add_root = boom
    with pytest.raises(SetupError, match="cannot record the destination"):
        inst.start()
    time.sleep(0.05)
    assert dl.calls == [] and inst.status()["state"] == "idle" and not inst._busy()


def test_md5_zip_already_downloaded_is_reused(tmp_path, monkeypatch):
    import hashlib

    # a one-item kit with no writing model: no tier could be written, which isn't this test's matter
    monkeypatch.setattr(Installer, "_tier_problem", staticmethod(lambda plan, root: ""))

    from primer.setup import kits

    url = "https://download.kiwix.org/release/kiwix-tools/kiwix-tools_win-x86_64-3.8.1.zip"
    manifest = kits.validate({
        "_checked": "2026-11-04", "map_estimates": {"8": 1},
        "kits": [{"id": "mini", "label": "Mini", "for": {"min_vram_gb": 0, "max_vram_gb": None}, "items": [
            {"id": "kiwix_tools", "role": "runtime_kiwix", "label": "kiwix-tools", "default": "win64",
             "source": {"type": "url", "tool": "kiwix-tools", "release": "3.8.1", "exe": "kiwix-serve.exe",
                        "licence": "GPL-3.0"},
             "choices": [{"id": "win64", "label": "Windows", "url": url, "md5_url": url + ".md5",
                          "size": len(KIWIX_ZIP)}]}]}]})
    md5 = hashlib.md5(KIWIX_ZIP).hexdigest()

    def transport(u, headers):
        return 200, {}, io.BytesIO(f"{md5}  x.zip\n".encode())

    root = tmp_path / "kit"
    z = root / ".setup" / "downloads" / "kiwix-tools_win-x86_64-3.8.1.zip"
    z.parent.mkdir(parents=True)
    z.write_bytes(KIWIX_ZIP)  # verified and kept before a restart
    dl = FakeDownloader()
    inst = make(tmp_path, dl, manifest=manifest, transport=transport)
    inst.plan("mini", {}, root)
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    assert dl.calls == []
    assert states(inst) == {"kiwix_tools": "done"}
    assert (root / "tools" / "kiwix-tools" / "3.8.1" / "kiwix-serve.exe").exists()
    inst.shutdown()


def test_approve_release_approves_one_item_and_notifies_once(tmp_path):
    inst = make(tmp_path)
    inst.plan("light", {}, tmp_path / "kit")
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    calls = []
    inst.approvals.on_approved = lambda: calls.append(1)
    out = inst.approve_release("llama_cpp")
    assert sorted(Path(p).name for p in out["approved"]) == ["ggml.dll", "llama-server.exe"]
    assert out["refused"] == [] and calls == [1]
    assert {p["file"] for p in inst.status()["approvals"]} == {"kiwix-serve.exe"}
    with pytest.raises(SetupError, match="nothing"):
        inst.approve_release("llama_cpp")
    inst.shutdown()


def test_cancel_during_merge_keeps_the_item_resumable(tmp_path):
    inst = make(tmp_path)
    src, dest = tmp_path / "src", tmp_path / "dest"
    (src / "a").mkdir(parents=True)
    (src / "a" / "x.dll").write_bytes(b"x")
    ev = threading.Event()
    ev.set()
    from primer.setup.installer import _Cancelled
    with pytest.raises(_Cancelled):
        inst._merge(src, dest, ev)
    assert not (dest / "a" / "x.dll").exists()


# ── fix round 2 ───────────────────────────────────────────────────────────────────────────────


def test_approve_release_never_approves_planted_files(tmp_path):
    from primer.setup.approvals import UNKNOWN_PUBLISHER, launch_check

    inst = make(tmp_path)
    root = tmp_path / "kit"
    inst.plan("light", {}, root)
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    bindir = root / "tools" / "llama.cpp" / "b12102"
    planted = bindir / "version.dll"
    planted.write_bytes(b"MZ planted")
    for bad in ("", None):
        with pytest.raises(SetupError, match="name the item"):
            inst.approve_release(bad)
    with pytest.raises(SetupError, match="no item"):
        inst.approve_release("nonsense")
    for item in [i.id for i in inst._plan.items]:
        try:
            inst.approve_release(item)
        except SetupError:
            pass
    row = next(p for p in inst.status()["approvals"] if p["file"] == "version.dll")
    assert row["publisher"] == UNKNOWN_PUBLISHER
    assert not inst.approvals.approved(planted)
    reason = launch_check(tmp_path / "var", inst.approvals)(str(bindir / "llama-server.exe"))
    assert reason == "approve version.dll in Setup before llama-server.exe can run"
    assert inst.approvals.approve(planted, row["sha256"])  # one at a time, as shown
    assert launch_check(tmp_path / "var", inst.approvals)(str(bindir / "llama-server.exe")) is None
    inst.shutdown()


def test_marker_is_written_before_the_merge(tmp_path, monkeypatch):
    from primer.setup import installer as im
    from primer.setup.kits import recorded

    seen = {}
    real = im.Installer._merge

    def crash(src, dest, cancel=None):
        marker = dest / ".primer-setup.json"
        seen.setdefault(dest.name, json.loads(marker.read_text(encoding="utf-8")))
        raise OSError("power cut")

    monkeypatch.setattr(im.Installer, "_merge", staticmethod(crash))
    inst = make(tmp_path)
    root = tmp_path / "kit"
    inst.plan("light", {}, root)
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    assert seen["b12102"] == {"items": {}, "installing": {"llama_cpp": seen["b12102"]["installing"]["llama_cpp"]}}
    bindir = root / "tools" / "llama.cpp" / "b12102"
    assert not recorded(bindir, "llama_cpp")  # not present: a retry installs it
    assert Approvals(tmp_path / "no-state").guarded(bindir / "llama-server.exe")  # marker alone
    inst.shutdown()

    monkeypatch.setattr(im.Installer, "_merge", staticmethod(real))
    again = make(tmp_path)
    again.resume()
    wait_for(lambda: again.status()["state"] in ("ready", "failed"))
    assert again.status()["state"] == "ready"
    data = json.loads((bindir / ".primer-setup.json").read_text(encoding="utf-8"))
    assert list(data["items"]) == ["llama_cpp"] and "installing" not in data
    again.shutdown()


# ── final review fixes ────────────────────────────────────────────────────────────────────────


def _finish_with_fake_files(inst, root):
    """Every item of the pending plan in place (fake files at the model dests), then the finish."""
    for item in inst._plan.items:
        if item.kind == "model":
            f = root / item.dest
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
    for run in inst._runs.values():
        run.state = "done"
    inst._finish()
    return inst.status()


@pytest.mark.parametrize("kit, families", [
    ("hybrid", {"qwen-image-2.1"}),
    ("workstation", {"qwen-image-2.1", "wan2.2-ti2v-5b"}),
])
def test_kit_picture_and_video_models_are_added_from_the_plan(tmp_path, kit, families):
    from primer.images.families import FAMILIES

    inst = make(tmp_path, scan=lambda folder: [])  # the scan can't pair files in separate repo folders
    root = tmp_path / "kit"
    plan = inst.plan(kit, {}, root)
    assert plan.ok, plan.error
    st = _finish_with_fake_files(inst, root)
    assert st["state"] == "ready", st
    store = ModelStore(tmp_path / "var")
    models = {m.family: m for m in store.list()}
    assert set(models) == families
    dest = {i.role: root / i.dest for i in plan.items}
    q = models["qwen-image-2.1"]
    assert q.files == {"diffusion": str(dest["image"]), "llm": str(dest["image_encoder"]),
                       "vae": str(dest["image_vae"])}
    assert q.settings == {**FAMILIES["qwen-image-2.1"].defaults} and q.name == "Qwen-Image 2.1 (pictures)"
    assert store.default_id("image") == q.id
    if "wan2.2-ti2v-5b" in families:
        w = models["wan2.2-ti2v-5b"]
        assert w.files == {"diffusion": str(dest["video"]), "t5xxl": str(dest["video_encoder"]),
                           "vae": str(dest["video_vae"])}
        assert store.default_id("video") == w.id
    assert sorted(st["models_added"]) == sorted(m.name for m in models.values())
    # finishing again adds nothing twice
    inst._finish()
    assert len(store.list()) == len(families) and inst.status()["models_added"] == []


def test_kit_models_keep_an_existing_default(tmp_path):
    from primer.images.models import ImageModel

    store = ModelStore(tmp_path / "var")
    mine = tmp_path / "mine.safetensors"
    mine.write_bytes(b"x")
    old = store.save(ImageModel(id="", name="Mine", family="sdxl", files={"model": str(mine)}, settings={}))
    inst = make(tmp_path, scan=lambda folder: [])
    root = tmp_path / "kit"
    inst.plan("workstation", {"image_vae": "skip"}, root)  # no VAE: no Qwen-Image model
    _finish_with_fake_files(inst, root)
    fams = [m.family for m in store.list()]
    assert fams == ["sdxl", "wan2.2-ti2v-5b"]
    assert store.default_id("image") == old.id  # the user's default stays
    assert store.default_id("video") is not None


@pytest.mark.parametrize("choices, llama, why", [
    ({"writing_model": "skip"}, None, "writing model"),
    ({"llama_cpp": "skip"}, None, "llama.cpp"),
])
def test_a_plan_that_cannot_write_a_tier_is_refused_before_downloading(tmp_path, choices, llama, why):
    dl = FakeDownloader()
    inst = make(tmp_path, dl, llama_server=lambda: llama)
    plan = inst.plan("light", choices, tmp_path / "kit")
    assert not plan.ok and why in plan.error and not any(i.error for i in plan.items)
    assert plan.to_json()["error"] == plan.error and plan.to_json()["ok"] is False
    accept_all(inst)
    with pytest.raises(SetupError, match=why):
        inst.start()
    assert dl.calls == [] and inst.status()["state"] == "idle"
    # a llama-server on the PATH stands in for the llama.cpp download
    ok = make(tmp_path / "b", llama_server=lambda: "/usr/local/bin/llama-server")
    assert ok.plan("light", {}, tmp_path / "kit").ok


def test_start_refuses_another_kit_root_or_choices(tmp_path):
    from primer.setup.installer import SetupConflict

    inst = make(tmp_path)
    root = tmp_path / "kit"
    inst.plan("light", {"map": "z9"}, root)
    accept_all(inst)
    for kit, where, choices in (("hybrid", root, {"map": "z9"}), ("light", tmp_path / "other", {"map": "z9"}),
                                ("light", root, {})):
        with pytest.raises(SetupConflict, match="check it again"):
            inst.start(kit, where, choices)
    inst.start("light", tmp_path / "x" / ".." / "kit", {"map": "z9"})
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    inst.shutdown()


def test_runtimes_in_an_existing_folder_are_installed_again_with_a_fresh_var(tmp_path):
    root = tmp_path / "kit"
    first = make(tmp_path / "a")
    first.plan("light", {}, root)
    accept_all(first)
    first.start()
    wait_for(lambda: first.status()["state"] in ("ready", "failed"))
    first.shutdown()
    # the same data folder, another Primer (a fresh var/): the runtimes aren't in its records
    dl = FakeDownloader()
    again = make(tmp_path / "b", dl)
    plan = again.plan("light", {}, root)
    present = {i.id for i in plan.items if i.present}
    assert {"map", "map_fonts"} <= present and not {"llama_cpp", "kiwix_tools"} & present
    accept_all(again)
    again.start()
    wait_for(lambda: again.status()["state"] in ("ready", "failed"))
    st = again.status()
    assert st["state"] == "ready", st
    assert {u["url"].rsplit("/", 1)[-1] for u in dl.calls} >= {"llama-b12102-bin-win-cpu-x64.zip"}
    rows = {p["file"]: p for p in st["approvals"]}
    assert rows["llama-server.exe"]["publisher"] == "ggml-org/llama.cpp"  # one release to approve
    assert len(again.approve_release("llama_cpp")["approved"]) == 2
    # now recorded: present next time
    assert {"llama_cpp", "kiwix_tools"} <= {i.id for i in make(tmp_path / "b").plan("light", {}, root).items
                                             if i.present}
    again.shutdown()


def test_a_file_the_server_no_longer_has_says_check_the_plan(tmp_path):
    from primer.setup.download import DownloadGone

    class Gone(FakeDownloader):
        def __call__(self, url, dest, **kw):
            if url.endswith(".zim"):
                raise DownloadGone("the file was not found on the server")
            return super().__call__(url, dest, **kw)

    inst = make(tmp_path, Gone())
    inst.plan("light", {}, tmp_path / "kit")
    accept_all(inst)
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    st = inst.status()
    assert st["state"] == "failed"
    label = next(i["label"] for i in st["items"] if i["id"] == "zim_top")
    assert st["error"] == f"{label} is no longer offered by lb.download.kiwix.org; check the plan again"
    assert "Resume" not in st["error"]
    inst.shutdown()


def test_the_root_rule_guards_only_setups_own_folders(tmp_path):
    ap = Approvals(tmp_path / "var")
    root = tmp_path / "data"
    ap.add_root(root)
    assert ap.guarded(root / "tools" / "x" / "a.exe")
    assert ap.guarded(root / ".setup" / "staging" / "a.exe")
    assert not ap.guarded(root / "my-own" / "tool.exe")  # the user's own program in a broad folder
    assert not ap.guarded(root / "toolsmith" / "a.exe")
    mine = root / "my-own" / "tool.exe"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"MZ mine")
    assert ap.refusal(str(mine)) is None
