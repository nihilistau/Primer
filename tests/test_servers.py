import os
import socket
import sys
from pathlib import Path

import pytest

from primer.config import EngineSpec, Tier
from primer.servers import ManagedServer, ServerManager

FAKE = str(Path(__file__).resolve().parent / "fake_openai_server.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_spec(tmp_path, **kw) -> EngineSpec:
    port = free_port()
    reply = tmp_path / f"reply{port}.txt"
    reply.write_text("hello", encoding="utf-8")
    return EngineSpec("openai", f"http://127.0.0.1:{port}", managed=True,
                      launch=(sys.executable, FAKE, "--port", str(port), "--reply-file", str(reply)),
                      ready_timeout_s=15, **kw)


def test_already_healthy_is_external():
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True, launch=("nope",)), health=lambda: True)
    s.start()
    assert s.status()["state"] == "external" and s.status()["pid"] is None
    s.stop()
    assert s.status()["state"] == "external"


def test_unmanaged_unhealthy_is_stopped_with_reason():
    s = ManagedServer("text", EngineSpec("openai", "http://x"), health=lambda: False)
    s.start()
    st = s.status()
    assert st["state"] == "stopped" and "not managed" in st["reason"]


def test_missing_binary_reports_failed():
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True, launch=("definitely-not-a-binary-xyz",)), health=lambda: False)
    s.start()
    st = s.status()
    assert st["state"] == "failed" and "definitely-not-a-binary-xyz" in st["reason"]


def test_launch_then_ready_then_stop(tmp_path):
    s = ManagedServer("text", fake_spec(tmp_path))
    try:
        s.start()
        assert s.status()["state"] == "ready" and s.status()["pid"]
    finally:
        s.stop()
    assert s.status()["state"] == "stopped" and s.status()["pid"] is None
    assert s.proc is None or s.proc.poll() is not None


def test_never_healthy_times_out_and_kills():
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True, launch=(sys.executable, "-c", "import time; time.sleep(60)"), ready_timeout_s=1), health=lambda: False)
    s.start()
    st = s.status()
    assert st["state"] == "failed" and "not healthy after 1" in st["reason"] and s.proc.poll() is not None


def test_nonblocking_start_reports_starting_then_ready(tmp_path):
    s = ManagedServer("text", fake_spec(tmp_path))
    try:
        s.start(wait=False)
        assert s.status()["state"] in ("starting", "ready")
        s._thread.join(20)
        assert s.status()["state"] == "ready"
    finally:
        s.stop()


def test_manager_single_server_when_fast_equals_text():
    spec = EngineSpec("openai", "http://x")
    m = ServerManager(Tier("t", spec, spec))
    sts = m.statuses()
    assert [s["role"] for s in sts] == ["text", "fast"]
    assert len(m._unique()) == 1


def test_manager_two_servers_start_stop(tmp_path):
    a, b = fake_spec(tmp_path), fake_spec(tmp_path)
    m = ServerManager(Tier("t", a, b))
    try:
        m.start_all(wait=True)
        assert [s["state"] for s in m.statuses()] == ["ready", "ready"]
    finally:
        m.stop_all()
    assert [s["state"] for s in m.statuses()] == ["stopped", "stopped"]


def test_manager_unknown_role():
    spec = EngineSpec("openai", "http://x")
    with pytest.raises(ValueError):
        ServerManager(Tier("t", spec, spec)).start("slow")


def test_second_start_keeps_ready_server_managed(tmp_path):
    s = ManagedServer("text", fake_spec(tmp_path))
    try:
        s.start()
        pid = s.status()["pid"]
        s.start()
        st = s.status()
        assert st["state"] == "ready" and st["pid"] == pid
        proc = s.proc
    finally:
        s.stop()
    assert proc.poll() is not None and s.status()["state"] == "stopped"


def test_concurrent_starts_launch_one_process(tmp_path, monkeypatch):
    import subprocess
    launched = []
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: launched.append(1) or real(*a, **k))
    s = ManagedServer("text", fake_spec(tmp_path))
    try:
        s.start(wait=False)
        s.start(wait=False)
        s.start(wait=False)
        s._thread.join(20)
        assert len(launched) == 1 and s.status()["state"] == "ready"
    finally:
        s.stop()


def test_stop_during_starting_leaves_nothing_running():
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True, launch=(sys.executable, "-c", "import time; time.sleep(60)"), ready_timeout_s=30), health=lambda: False)
    s.start(wait=False)
    import time
    while s.proc is None:
        time.sleep(0.05)
    proc = s.proc
    s.stop()
    s._thread.join(5)
    assert proc.poll() is not None and s.status()["state"] == "stopped"


def test_health_exception_reports_failed():
    def boom():
        raise RuntimeError("kaput")
    s = ManagedServer("text", EngineSpec("openai", "http://x"), health=boom)
    s.start()
    assert s.status()["state"] == "failed" and "kaput" in s.status()["reason"]


# --- launcher-style commands (lms server start) ---
LAUNCHER = str(Path(__file__).resolve().parent / "fake_launcher.py")


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_pid_file(pid_file) -> None:
    import os
    import signal
    try:
        os.kill(int(Path(pid_file).read_text()), signal.SIGTERM)
    except (OSError, ValueError):
        pass


@pytest.fixture
def launcher_spec(tmp_path):
    port = free_port()
    reply = tmp_path / "reply.txt"
    reply.write_text("hello", encoding="utf-8")
    pid_file = tmp_path / "pid"

    def make(with_stop: bool) -> EngineSpec:
        base = (sys.executable, LAUNCHER, "--pid-file", str(pid_file))
        return EngineSpec("openai", f"http://127.0.0.1:{port}", managed=True,
                          launch=base + ("--port", str(port), "--reply-file", str(reply)),
                          stop=base + ("--stop",) if with_stop else (), ready_timeout_s=15)
    make.port = port
    yield make
    _kill_pid_file(pid_file)


def test_launcher_exit_zero_becomes_ready(launcher_spec):
    s = ManagedServer("text", launcher_spec(True))
    s.start()
    st = s.status()
    assert st["state"] == "ready" and st["pid"] is None and s.proc.poll() == 0
    assert _port_open(launcher_spec.port)
    s.stop()


def test_launcher_stop_command_stops_server(launcher_spec):
    import time
    s = ManagedServer("text", launcher_spec(True))
    s.start()
    s.stop()
    assert s.status()["state"] == "stopped" and s.status()["reason"] == ""
    for _ in range(50):
        if not _port_open(launcher_spec.port):
            break
        time.sleep(0.1)
    assert not _port_open(launcher_spec.port)


def test_launcher_without_stop_command_leaves_it_running(launcher_spec):
    s = ManagedServer("text", launcher_spec(False))
    s.start()
    s.stop()
    st = s.status()
    assert st["state"] == "ready" and "no stop command" in st["reason"]
    assert _port_open(launcher_spec.port)
    s.start()  # a second start must not relaunch
    assert s.status()["state"] == "ready"


def test_nonzero_exit_fails_immediately():
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True,
                                         launch=(sys.executable, "-c", "raise SystemExit(3)"),
                                         ready_timeout_s=30), health=lambda: False)
    s.start()
    st = s.status()
    assert st["state"] == "failed" and "code 3" in st["reason"]


def test_exit_zero_never_healthy_times_out():
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True,
                                         launch=(sys.executable, "-c", "pass"),
                                         ready_timeout_s=1), health=lambda: False)
    s.start()
    assert s.status()["state"] == "failed" and "not healthy after 1" in s.status()["reason"]


def test_stop_command_never_runs_for_external(tmp_path):
    marker = tmp_path / "ran"
    spec = EngineSpec("openai", "http://x", managed=True, launch=("nope",),
                      stop=(sys.executable, "-c", f"open({str(marker)!r}, 'w')"))
    s = ManagedServer("text", spec, health=lambda: True)
    s.start()
    s.stop()
    assert s.status()["state"] == "external" and not marker.exists()


# --- auth failures surface in the reason ---
def test_unauthorised_health_puts_hint_in_reason(fake_server):
    fake_server.auth_status = 401
    s = ManagedServer("text", EngineSpec("openai", fake_server.url))
    s.start()
    st = s.status()
    assert st["state"] == "stopped" and "HTTP 401" in st["reason"] and "api_key_file" in st["reason"]


def test_unauthorised_launch_timeout_reason(fake_server):
    fake_server.auth_status = 401
    spec = EngineSpec("openai", fake_server.url, managed=True,
                      launch=(sys.executable, "-c", "import time; time.sleep(60)"), ready_timeout_s=1)
    s = ManagedServer("text", spec)
    s.start()
    st = s.status()
    assert st["state"] == "failed" and "not healthy after 1" in st["reason"] and "HTTP 401" in st["reason"]


# --- fix round 1: terminal states and launcher detection ---
def _flag_stop(tmp_path, code=0):
    marker = tmp_path / "stop_ran"
    return marker, (sys.executable, "-c", f"open({str(marker)!r}, 'w'); raise SystemExit({code})")


@pytest.mark.parametrize("stop_code", [None, 2])
def test_stop_during_launcher_startup_ends_stopped(tmp_path, stop_code):
    import time
    stop = () if stop_code is None else _flag_stop(tmp_path, stop_code)[1]
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True, launch=(sys.executable, "-c", "pass"),
                                         stop=stop, ready_timeout_s=30), health=lambda: False)
    s.start(wait=False)
    while s.proc is None or s.proc.poll() is None:
        time.sleep(0.05)
    s.stop()
    s._thread.join(5)
    assert s.status()["state"] == "stopped"
    s.start(wait=False)  # must be startable again, not stuck
    s.stop()
    assert s.status()["state"] == "stopped"


def test_real_server_exiting_zero_after_ready_can_be_restarted(tmp_path):
    import time
    state = {"up": False}
    marker = tmp_path / "n"
    code = f"open({str(marker)!r}, 'a').write('x'); import time; time.sleep(3)"
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True, launch=(sys.executable, "-c", code),
                                         ready_timeout_s=10), health=lambda: state["up"])
    s.start(wait=False)
    while s.proc is None:
        time.sleep(0.05)
    state["up"] = True
    s._thread.join(10)
    assert s.status()["state"] == "ready"
    s.proc.wait(10)  # exits 0 on its own: a plain server that died, not a detached launcher
    state["up"] = False
    s.start(wait=False)  # process gone and unhealthy: must relaunch, not no-op
    deadline = time.monotonic() + 10
    while marker.read_text() != "xx" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.read_text() == "xx"
    s.stop()
    assert s.status()["state"] == "stopped"


def test_dead_detached_server_stop_skips_stop_command(launcher_spec, tmp_path):
    marker, stop_cmd = _flag_stop(tmp_path)
    spec = launcher_spec(False)
    import dataclasses
    s = ManagedServer("text", dataclasses.replace(spec, stop=stop_cmd))
    s.start()
    assert s.status()["state"] == "ready"
    _kill_pid_file(tmp_path / "pid")
    import time
    for _ in range(50):
        if not _port_open(launcher_spec.port):
            break
        time.sleep(0.1)
    s.stop()
    assert s.status()["state"] == "stopped" and not marker.exists()


def test_exit_zero_never_healthy_reason_mentions_launcher():
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True, launch=(sys.executable, "-c", "pass"),
                                         ready_timeout_s=1), health=lambda: False)
    s.start()
    assert "launcher exited" in s.status()["reason"]


def test_stop_command_runs_even_if_launcher_still_running(tmp_path):
    import time
    marker, stop_cmd = _flag_stop(tmp_path)
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True,
                                         launch=(sys.executable, "-c", "import time; time.sleep(60)"),
                                         stop=stop_cmd, ready_timeout_s=30), health=lambda: False)
    s.start(wait=False)
    while s.proc is None:
        time.sleep(0.05)
    s.stop()
    s._thread.join(5)
    assert marker.exists() and s.status()["state"] == "stopped"


def test_stop_on_failed_launcher_keeps_original_reason():
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True, launch=(sys.executable, "-c", "pass"),
                                         ready_timeout_s=1), health=lambda: False)
    s.start()
    s.stop()
    st = s.status()
    assert st["state"] == "stopped" and "not healthy after 1" in st["reason"] and "launcher exited" in st["reason"]


def test_start_during_slow_stop_keeps_the_new_run(tmp_path):
    import threading
    import time
    entered = tmp_path / "entered"
    slow = (sys.executable, "-c", f"import time; open({str(entered)!r}, 'w'); time.sleep(1.5)")
    up = {"v": False}
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True,
                                         launch=(sys.executable, "-c", "import time; time.sleep(60)"),
                                         stop=slow, ready_timeout_s=10), health=lambda: up["v"])

    def wait_for(cond):
        end = time.monotonic() + 10
        while not cond() and time.monotonic() < end:
            time.sleep(0.05)
        assert cond()

    try:
        s.start(wait=False)
        wait_for(lambda: s.proc is not None)
        up["v"] = True
        s._thread.join(10)
        assert s.status()["state"] == "ready"
        t = threading.Thread(target=s.stop)
        t.start()
        wait_for(entered.exists)  # stop() is inside its stop command
        up["v"] = False
        first = s.proc
        s.start(wait=False)
        wait_for(lambda: s.proc is not first)
        up["v"] = True
        t.join(10)
        s._thread.join(10)
        assert s.status()["state"] == "ready" and s.proc is not None and s.proc.poll() is None
    finally:
        if s.proc is not None:
            s.proc.kill()


def test_stopping_exited_plain_process_has_no_launcher_note():
    import time
    up = {"v": False}
    code = "import time; time.sleep(2)"
    s = ManagedServer("text", EngineSpec("openai", "http://x", managed=True,
                                         launch=(sys.executable, "-c", code), ready_timeout_s=10),
                      health=lambda: up["v"])
    s.start(wait=False)
    while s.proc is None:
        time.sleep(0.05)
    up["v"] = True
    s._thread.join(10)
    s.proc.wait(10)  # a real server that exited 0 on its own
    up["v"] = False
    s.stop()
    st = s.status()
    assert st["state"] == "stopped" and "launcher" not in st["reason"]


# --- pause command (final fix wave) ---

def test_pause_runs_the_pause_command_and_leaves_the_server_up(tmp_path):
    marker = tmp_path / "unloaded.txt"
    spec = fake_spec(tmp_path, pause=(sys.executable, "-c", f"open(r'{marker}', 'w').write('x')"))
    s = ManagedServer("text", spec)
    s.start()
    try:
        assert s.status()["state"] == "ready"
        s.pause()
        assert marker.read_text() == "x"
        st = s.status()
        assert st["state"] == "ready" and st["pid"] is not None  # still serving
    finally:
        s.stop()


def test_failing_pause_command_raises_with_reason(tmp_path):
    spec = fake_spec(tmp_path, pause=(sys.executable, "-c", "raise SystemExit(3)"))
    s = ManagedServer("text", spec, health=lambda: True)
    with pytest.raises(RuntimeError, match="pause command exited with code 3"):
        s.pause()
    missing = ManagedServer("text", fake_spec(tmp_path, pause=("no-such-pause-cmd",)), health=lambda: True)
    with pytest.raises(RuntimeError, match="pause command not found: no-such-pause-cmd"):
        missing.pause()


def test_pause_without_a_pause_command_stops(tmp_path):
    s = ManagedServer("text", fake_spec(tmp_path))
    s.start()
    s.pause()
    assert s.status()["state"] == "stopped"


# --- the reference (kiwix-serve) role ---

def _ref_tier(**kw):
    from primer.config import ReferenceConfig
    spec = EngineSpec("openai", "http://x")
    return Tier("t", spec, spec, reference=ReferenceConfig(**kw))


def test_reference_role_launch_args(monkeypatch):
    from primer import servers as servers_mod

    seen = []

    class FakeProc:
        pid = 4242
        returncode = None

        def __init__(self, argv, **kw):
            seen.append(list(argv))

        def poll(self):
            return None

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(servers_mod.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(servers_mod.children, "adopt", lambda *a, **k: None)
    monkeypatch.setattr(servers_mod.children, "release", lambda *a, **k: None)
    tier = _ref_tier(kiwix_serve="C:/k/kiwix-serve.exe", zims=("D:/z/a.zim", "b.zim"), port=8831)
    m = ServerManager(tier)
    ref = m._servers["reference"]
    assert ref.spec.managed and not ref.spec.gpu
    assert m.gpu_servers() == []
    ref._health = lambda: bool(seen)  # unhealthy until the launch has happened
    ref.start(wait=True)
    assert seen == [["C:/k/kiwix-serve.exe", "--address=127.0.0.1", "--port=8831",
                     os.path.normpath("D:/z/a.zim"), "b.zim"]]  # kiwix-serve on Windows needs D:\z
    assert ref.status()["state"] == "ready"
    ref.stop()


def test_external_reference_is_not_managed():
    m = ServerManager(_ref_tier(base_url="http://127.0.0.1:9999"))
    ref = m._servers["reference"]
    assert not ref.spec.managed and ref.spec.base_url == "http://127.0.0.1:9999"
    st = next(s for s in m.statuses() if s["role"] == "reference")
    assert st["state"] == "stopped"


def test_no_reference_role_without_config():
    spec = EngineSpec("openai", "http://x")
    m = ServerManager(Tier("t", spec, spec))
    assert [s["role"] for s in m.statuses()] == ["text", "fast"]
    with pytest.raises(ValueError):
        m.start("reference")


def test_reference_role_listed_and_controllable():
    m = ServerManager(_ref_tier(base_url="http://127.0.0.1:9999"))
    assert [s["role"] for s in m.statuses()] == ["text", "fast", "reference"]
    m.stop("reference")
