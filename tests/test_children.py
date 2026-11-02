"""Crash orphans: the pid ledger and (on Windows) the kill-on-close job object.
Only dummy processes these tests start themselves are ever killed."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from primer import children
from primer.config import EngineSpec
from primer.servers import ManagedServer
from tests.test_servers import fake_spec

ROOT = Path(__file__).resolve().parent.parent
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def _gone(pid: int, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if children.identity(pid) is None:
            return True
        time.sleep(0.05)
    return children.identity(pid) is None


def _kill_own(pid: int, ident) -> None:
    """Clean up a dummy this test started (only if it still is that same process)."""
    if ident is not None and children.identity(pid) == ident:
        os.kill(pid, signal.SIGTERM)


def _crashed_owner(path: Path) -> None:
    """Make every entry look launched by a Primer that is no longer running."""
    data = json.loads(path.read_text())
    for e in data["children"]:
        e["owner"] = {"pid": e["owner"]["pid"], "exe": "c:/gone/python.exe", "create_time": "0"}
    path.write_text(json.dumps(data))


@pytest.fixture
def sleeper():
    procs = []

    def start():
        p = subprocess.Popen(SLEEPER)
        procs.append(p)
        return p

    yield start
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()


@pytest.fixture
def ledger_off():
    yield
    children.use_ledger(None)


def test_reap_stops_a_recorded_leftover(tmp_path, sleeper):
    p = sleeper()
    assert children.Ledger(tmp_path).record("text", p.pid, 8081)
    entry = json.loads((tmp_path / "children.json").read_text())["children"][0]
    assert entry["role"] == "text" and entry["pid"] == p.pid and entry["port"] == 8081
    assert entry["exe"] and entry["create_time"] and entry["owner"]["pid"] == os.getpid()
    assert children.reap_leftovers(tmp_path) == []  # its Primer (this process) still runs
    assert p.poll() is None
    _crashed_owner(tmp_path / "children.json")
    msgs = children.reap_leftovers(tmp_path)
    assert msgs == [f"stopped a leftover text server from the last run (pid {p.pid})"]
    assert p.wait(10) is not None
    assert not (tmp_path / "children.json").exists()


@pytest.mark.parametrize("field, value", [("create_time", "1"), ("exe", "c:\\not\\the\\same.exe")])
def test_reap_never_kills_a_process_that_does_not_match(tmp_path, sleeper, field, value):
    p = sleeper()
    ledger = children.Ledger(tmp_path)
    ledger.record("image", p.pid)
    data = json.loads(ledger.path.read_text())
    data["children"][0][field] = value  # the pid was reused by some other process
    ledger.path.write_text(json.dumps(data))
    _crashed_owner(ledger.path)
    assert children.reap_leftovers(tmp_path) == []
    time.sleep(0.3)
    assert p.poll() is None  # still running
    assert not ledger.path.exists()  # the stale entry is dropped


def test_reap_leaves_the_servers_of_a_primer_that_is_still_running(tmp_path, sleeper):
    server, other_primer = sleeper(), sleeper()
    ledger = children.Ledger(tmp_path)
    ledger.record("text", server.pid)
    data = json.loads(ledger.path.read_text())
    exe, created = children.identity(other_primer.pid)
    data["children"][0]["owner"] = {"pid": other_primer.pid, "exe": exe, "create_time": created}
    ledger.path.write_text(json.dumps(data))
    assert children.reap_leftovers(tmp_path) == []
    time.sleep(0.3)
    assert server.poll() is None
    assert [e["pid"] for e in json.loads(ledger.path.read_text())["children"]] == [server.pid]
    other_primer.kill()  # that Primer crashed: now its server is a leftover
    other_primer.wait()
    assert len(children.reap_leftovers(tmp_path)) == 1
    assert server.wait(10) is not None


def test_forget_removes_the_entry_and_dead_entries_are_pruned(tmp_path, sleeper):
    a, b = sleeper(), sleeper()
    ledger = children.Ledger(tmp_path)
    ledger.record("text", a.pid)
    ledger.record("fast", b.pid)
    ledger.forget(a.pid)
    assert [e["pid"] for e in json.loads(ledger.path.read_text())["children"]] == [b.pid]
    b.kill()
    b.wait()
    ledger.forget(12345678)  # any write prunes entries whose process is gone
    assert not ledger.path.exists()
    assert ledger.record("text", b.pid) is False  # not running: nothing recorded


def test_unreadable_ledger_is_ignored(tmp_path):
    (tmp_path / "children.json").write_text("{nope")
    assert children.reap_leftovers(tmp_path) == []


def test_managed_server_is_recorded_and_a_clean_stop_forgets_it(tmp_path, ledger_off):
    children.use_ledger(tmp_path / "var")
    s = ManagedServer("fast", fake_spec(tmp_path))
    s.start()
    try:
        path = tmp_path / "var" / "children.json"
        entries = json.loads(path.read_text())["children"]
        assert [(e["role"], e["pid"]) for e in entries] == [("fast", s.proc.pid)]
        assert entries[0]["port"] == int(s.spec.base_url.rsplit(":", 1)[1])
    finally:
        s.stop()
    assert not path.exists()


def test_build_app_reaps_leftovers_before_anything_starts(tmp_path, sleeper, ledger_off):
    import serve
    from primer.config import TIERS_DIR, load_tier
    from primer.hardware import Hardware

    var = tmp_path / "var"
    p = sleeper()
    children.Ledger(var).record("text", p.pid)
    _crashed_owner(var / "children.json")
    app = serve.build_app(load_tier("light"), var, tmp_path / "ui", TIERS_DIR,
                          hardware=Hardware(gpu_name=None, vram_gb=0, ram_gb=16))
    try:
        assert p.wait(10) is not None
        assert children._ledger is not None and children._ledger.path == var / "children.json"
    finally:
        app.images.shutdown()
        app.pages.shutdown(drain=False)


_PRIMER_LIKE = r"""
import subprocess, sys, os
sys.path.insert(0, sys.argv[1])
from primer import children
direct = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
children.adopt(direct, "text")
# a launcher: once it is in the job, it starts a detached server and exits (like lms server start)
launcher = subprocess.Popen([sys.executable, "-c", '''
import subprocess, sys
sys.stdin.readline()
kw = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)
print(p.pid, flush=True)
'''], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
children.adopt(launcher, "text")
launcher.stdin.write("go\n"); launcher.stdin.flush()
daemon = int(launcher.stdout.readline())
launcher.wait()
print(direct.pid, daemon, flush=True)
sys.stdin.readline()
os._exit(0)  # die without stopping anything, like a crash
"""


@pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows only")
def test_job_object_kills_direct_children_but_not_what_a_launcher_started():
    primer = subprocess.Popen([sys.executable, "-c", _PRIMER_LIKE, str(ROOT)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    idents = {}
    try:
        direct, daemon = map(int, primer.stdout.readline().split())
        idents = {direct: children.identity(direct), daemon: children.identity(daemon)}
        assert all(idents.values())
        primer.stdin.write("\n")
        primer.stdin.flush()
        primer.wait(10)
        assert _gone(direct), "the direct child outlived its parent"
        time.sleep(0.5)
        assert children.identity(daemon) == idents[daemon], "the launcher's server was killed"
    finally:
        if primer.poll() is None:
            primer.kill()
            primer.wait()
        for pid, ident in idents.items():
            _kill_own(pid, ident)


def test_job_assignment_is_a_no_op_off_windows(monkeypatch, sleeper):
    monkeypatch.setattr(children.sys, "platform", "linux")
    assert children._Job().assign(sleeper()) is False


def test_adopt_without_a_ledger_still_works(sleeper, ledger_off):
    children.use_ledger(None)
    children.adopt(sleeper(), "text")  # no ledger: only the job object; never raises
    children.release(1)
