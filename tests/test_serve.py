import pytest

import serve
from primer.config import TIERS_DIR, ConfigError, write_settings
from primer.hardware import Hardware, suggest_tier

HW = Hardware(None, 0.0, 16.0)


@pytest.mark.parametrize("saved", ["../evil", "C:/x/evil", "//host/share/evil", "gone", 7])
def test_bad_saved_tier_falls_back_to_suggested_with_warning(tmp_path, capsys, saved):
    write_settings(tmp_path, {"tier": saved})
    tier, note = serve.choose_tier(None, tmp_path, TIERS_DIR, HW)
    assert tier.name == suggest_tier(HW) and "suggested" in note
    assert "warning" in capsys.readouterr().err


def test_corrupt_settings_file_falls_back_to_suggested(tmp_path, capsys):
    (tmp_path / "settings.json").write_text("{not json")
    tier, _ = serve.choose_tier(None, tmp_path, TIERS_DIR, HW)
    assert tier.name == suggest_tier(HW) and "warning" in capsys.readouterr().err


def test_saved_and_cli_tiers_are_used(tmp_path):
    write_settings(tmp_path, {"tier": "hosted"})
    assert serve.choose_tier(None, tmp_path, TIERS_DIR, HW) == (serve.load_tier("hosted"), "")
    assert serve.choose_tier("hybrid", tmp_path, TIERS_DIR, HW)[0].name == serve.load_tier("hybrid").name


def test_bad_cli_tier_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        serve.choose_tier("../evil", tmp_path, TIERS_DIR, HW)


def test_shutdown_stops_servers_even_if_page_shutdown_is_interrupted(tmp_path, monkeypatch):
    """A second Ctrl+C while prefetch work winds down must not skip stopping the model servers."""
    calls = []

    class Servers:
        def __init__(self, tier):
            pass

        def start_all(self, wait=False):
            calls.append("start_all")

        def stop_all(self):
            calls.append("stop_all")

    class Pages:
        grounder = object()
        source_mode = "storyteller"

        def __init__(self, *a, **kw):
            pass

        def shutdown(self, drain=True):
            calls.append(f"pages.shutdown(drain={drain})")
            raise KeyboardInterrupt  # the second Ctrl+C

    class Server:
        def serve_forever(self):
            raise KeyboardInterrupt  # the first Ctrl+C

        def server_close(self):
            calls.append("server_close")

    class Images:
        def __init__(self, *a, **kw):
            pass

        def shutdown(self):
            calls.append("images.shutdown")

        def release_gpu(self):
            pass

    monkeypatch.setattr(serve, "ROOT", tmp_path)
    monkeypatch.setattr(serve, "ImageService", Images)
    monkeypatch.setattr(serve, "ServerManager", Servers)
    monkeypatch.setattr(serve, "PageService", Pages)
    monkeypatch.setattr(serve, "make_server", lambda *a, **kw: Server())
    monkeypatch.setattr(serve, "detect_hardware", lambda: HW)
    monkeypatch.setattr(serve.signal, "signal", lambda *a: None)
    with pytest.raises(KeyboardInterrupt):
        serve.main(["--tier", "light"])
    assert "stop_all" in calls and "pages.shutdown(drain=False)" in calls
    assert calls.index("stop_all") < calls.index("pages.shutdown(drain=False)")
    assert calls.index("images.shutdown") < calls.index("stop_all")


def test_serve_marks_unfinished_media_failed_on_start(tmp_path):
    from primer.graph import Graph

    var = tmp_path / "var"
    g = Graph(var / "primer.sqlite")
    node = g.node_for("trebuchet", "concept", title="Trebuchet")
    queued = g.add_media(node.id, "", "image", "m", "p", {})
    g.update_media(queued.id, status="generating")
    done = g.add_media(node.id, "", "image", "m", "p", {})
    g.update_media(done.id, status="done", file="a" * 64 + ".png")
    tier = serve.load_tier("light")
    app = serve.build_app(tier, var, tmp_path / "ui", TIERS_DIR, hardware=HW)
    try:
        job = app.graph.get_media(queued.id)
        assert job.status == "failed" and job.error == "Primer restarted"
        assert app.graph.get_media(done.id).status == "done"
        assert app.store.path == var / "images.json"
        assert app.pages.gate is app.gate and app.images.gate is app.gate
        assert app.gate.mode == tier.gpu_mode and app.images.media_dir == var / "media"
        assert app.images.fast is app.pages.fast
    finally:
        app.images.shutdown()
        app.pages.shutdown(drain=False)


def test_build_app_gate_pauses_only_gpu_text_servers(tmp_path):
    tier = serve.load_tier("hybrid")  # its text server is marked gpu = true
    app = serve.build_app(tier, tmp_path / "var", tmp_path / "ui", TIERS_DIR, hardware=HW)
    try:
        gpu = app.servers.gpu_servers()
        assert gpu and all(s.spec.gpu for s in gpu)
        assert app.text_pause is not None and app.text_pause._servers is app.servers
        assert app.text_pause.release_gpu == app.images.release_gpu
    finally:
        app.images.shutdown()
        app.pages.shutdown(drain=False)
