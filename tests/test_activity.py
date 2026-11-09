"""Primer's half of Kairos's app-activity record (primer/activity.py), and the signals hook."""
import json

from primer.activity import ActivitySink, event_name
from primer.reader.signals import Signals


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _rows(root):
    out = []
    for f in sorted((root / "primer").glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return out


def test_event_names():
    assert event_name("open") == "open_page"
    assert event_name("depth", "deeper") == "deeper_page"
    assert event_name("regenerate") == "regenerate_page"
    assert event_name("answer", "not yet") == "quiz_not_yet"


def test_sink_writes_the_kairos_contract(tmp_path):
    sink = ActivitySink(tmp_path, clock=Clock(1767344460.0))
    assert sink.note("open", "  Kookaburra \n laughing ", "n1", "start")
    [row] = _rows(tmp_path)
    assert row == {"at": 1767344460000, "event": "open_page", "title": "Kookaburra laughing", "ref": "n1"}
    assert "private" not in row
    assert not sink.note("open", "   ", "n2")  # no title, no row


def test_sink_private_marks_every_row(tmp_path):
    ActivitySink(tmp_path, private=True, clock=Clock(1767344460.0)).note("open", "Owls", "n1")
    assert _rows(tmp_path)[0]["private"] is True


def test_sink_limits(tmp_path):
    ActivitySink(tmp_path, clock=Clock(1767344460.0)).note("answer", "x" * 500, "n" * 500, "y" * 80)
    row = _rows(tmp_path)[0]
    assert len(row["title"]) == 200 and len(row["ref"]) == 200 and len(row["event"]) <= 40


def test_from_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMER_ACTIVITY_DIR", raising=False)
    assert ActivitySink.from_env() is None
    monkeypatch.setenv("PRIMER_ACTIVITY_DIR", str(tmp_path))
    monkeypatch.setenv("PRIMER_ACTIVITY_PRIVATE", "1")
    s = ActivitySink.from_env()
    assert s is not None and s.private and s.dir == tmp_path / "primer"


def test_signals_tell_kairos_with_the_page_title(g, tmp_path):
    sig = Signals(g, clock=Clock(1767344460.0), activity=ActivitySink(tmp_path, clock=Clock(1767344460.0)))
    node = g.node_for("Kookaburra", "concept")
    sig.record("open", node.id, "start")
    sig.record("depth", node.id, "deeper")
    rows = _rows(tmp_path)
    assert [(r["event"], r["title"], r["ref"]) for r in rows] == [
        ("open_page", node.title, node.id), ("deeper_page", node.title, node.id)]
    assert sig.counted()["opens"] == 1  # the signal itself still counted


def test_signals_without_a_sink_write_nothing(g, tmp_path):
    sig = Signals(g, clock=Clock(1.0))
    sig.record("open", g.node_for("Owls", "concept").id, "start")
    assert not (tmp_path / "primer").exists()


def test_a_broken_sink_never_costs_a_signal(g):
    class Broken:
        def note(self, *a, **k):
            raise RuntimeError("disk gone")
    sig = Signals(g, clock=Clock(1.0), activity=Broken())
    sig.record("open", g.node_for("Owls", "concept").id, "start")
    assert sig.counted()["opens"] == 1


def _app(tmp_path, g):
    from primer.config import EngineSpec, Tier
    from primer.hardware import Hardware
    from primer.pages import PageService
    from primer.server import App
    from primer.servers import ManagedServer, ServerManager
    from tests.fakes import FakeEngine

    spec = EngineSpec(kind="openai", base_url="http://127.0.0.1:1", model="fake")
    tier = Tier(name="test", text=spec, fast=spec)
    servers = ServerManager(tier)
    servers._servers = {"text": ManagedServer("text", spec, health=lambda: True),
                        "fast": ManagedServer("fast", spec, health=lambda: True)}
    pages = PageService(g, FakeEngine(["# X\nx"]), FakeEngine(["[]"]))
    (tmp_path / "ui").mkdir(exist_ok=True)
    (tmp_path / "tiers").mkdir(exist_ok=True)
    return App(tier=tier, servers=servers, pages=pages, graph=g, hardware=Hardware(None, 0.0, 16.0),
               var_dir=tmp_path / "var", ui_dir=tmp_path / "ui", tiers_dir=tmp_path / "tiers")


def test_app_wires_the_sink_from_env(tmp_path, monkeypatch, g):
    monkeypatch.delenv("PRIMER_ACTIVITY_DIR", raising=False)
    app = _app(tmp_path, g)
    try:
        assert app.signals.activity is None          # unset: nothing is shared
    finally:
        app.profile.shutdown(); app.pages.shutdown()
    monkeypatch.setenv("PRIMER_ACTIVITY_DIR", str(tmp_path / "act"))
    app = _app(tmp_path, g)
    try:
        assert isinstance(app.signals.activity, ActivitySink)
        assert app.pages.signals is app.signals      # the pages record through the same signals
    finally:
        app.profile.shutdown(); app.pages.shutdown()
