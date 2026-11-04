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


# --- the voice role ---

def _voice_tier(**kw):
    from primer.config import VoiceConfig
    spec = EngineSpec("openai", "http://x")
    return Tier("t", spec, spec, voice=VoiceConfig(**kw))


def test_voice_role_launch_and_not_gpu(monkeypatch):
    from primer import servers as servers_mod

    seen = []

    class FakeProc:
        pid = 4343
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
    tier = _voice_tier(kind="lfm3", base_url="http://127.0.0.1:8840", managed=True,
                       launch=("llama-server", "--port", "8840"))
    m = ServerManager(tier)
    assert m.ROLES == ("text", "fast", "voice")
    v = m._servers["voice"]
    assert v.spec.managed and not v.spec.gpu and v.spec.launch == ("llama-server", "--port", "8840")
    assert m.gpu_servers() == []
    v._health = lambda: bool(seen)
    v.start(wait=True)
    assert seen == [["llama-server", "--port", "8840"]]
    assert v.status()["state"] == "ready"
    v.stop()


def test_external_voice_is_not_managed_and_absent_without_config():
    m = ServerManager(_voice_tier(kind="lfm3", base_url="http://127.0.0.1:9999"))
    v = m._servers["voice"]
    assert not v.spec.managed
    assert next(s for s in m.statuses() if s["role"] == "voice")["state"] == "stopped"
    spec = EngineSpec("openai", "http://x")
    assert "voice" not in ServerManager(Tier("t", spec, spec)).ROLES


def test_alive_follows_process_and_detached():
    from primer.config import EngineSpec
    from primer.servers import ManagedServer

    s = ManagedServer("voice", EngineSpec(kind="openai", base_url="http://127.0.0.1:1", model="m"),
                      health=lambda: False)
    assert s.alive() is False

    class P:
        code = None

        def poll(self):
            return self.code
    s.proc = P()
    assert s.alive() is True
    s.proc.code = 0
    assert s.alive() is False
    s._detached = True
    assert s.alive() is True


# --- Phase 9: executables Setup downloaded run only once approved by SHA-256 ---
class _FakeProc:
    pid = 424242

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 1

    kill = terminate

    def wait(self, timeout=None):
        self.code = 1 if self.code is None else self.code
        return self.code


@pytest.fixture
def fake_popen(monkeypatch):
    """Popen that records its calls and returns a process that 'runs' until terminated."""
    import subprocess

    from primer import servers as srv

    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append(cmd) or _FakeProc())
    monkeypatch.setattr(srv.children, "adopt", lambda *a, **k: None)
    monkeypatch.setattr(srv.children, "release", lambda *a, **k: None)
    return calls


def _setup_root(tmp_path):
    from primer.setup.approvals import Approvals

    var, root = tmp_path / "var", tmp_path / "kit"
    exe = root / "tools" / "llama.cpp" / "b1" / "llama-server.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ original")
    ap = Approvals(var)
    ap.add_root(root)
    ap.record_installed([{"path": str(exe), "publisher": "ggml-org/llama.cpp", "release": "b1"}])
    return var, root, exe, ap


def _server(exe, var, launched):
    from primer.setup.approvals import launch_check

    spec = EngineSpec("openai", "http://127.0.0.1:9", managed=True, launch=(str(exe), "--port", "9"),
                      ready_timeout_s=2)
    return ManagedServer("text", spec, health=lambda: bool(launched), may_launch=launch_check(var))


def test_unapproved_executable_not_launched(tmp_path, fake_popen):
    var, root, exe, ap = _setup_root(tmp_path)
    s = _server(exe, var, fake_popen)
    s.start()
    st = s.status()
    assert fake_popen == []  # nothing was spawned
    assert st["state"] == "unavailable"
    assert st["reason"] == "approve llama-server.exe in Setup before it can run"
    assert [p["path"] for p in ap.pending()] == [os.path.normcase(os.path.realpath(exe))]

    assert ap.approve(exe) is True
    s.start()
    assert len(fake_popen) == 1 and s.status()["state"] == "ready"
    s.stop()

    exe.write_bytes(b"MZ something else")  # a different file at the same path
    fake_popen.clear()
    s2 = _server(exe, var, fake_popen)
    s2.start()
    assert fake_popen == [] and s2.status()["state"] == "unavailable"
    assert not ap.approved(exe)
    assert ap.approve(exe) is False  # not what Setup verified and placed there
    assert ap.approve(exe.with_name("missing.exe")) is False


def test_executable_outside_setup_root_is_unaffected(tmp_path, fake_popen):
    var, root, exe, ap = _setup_root(tmp_path)
    own = tmp_path / "mine" / "llama-server.exe"
    own.parent.mkdir()
    own.write_bytes(b"MZ hand configured")
    s = _server(own, var, fake_popen)
    s.start()
    assert len(fake_popen) == 1 and s.status()["state"] == "ready"
    s.stop()


def test_unapproved_dll_beside_the_executable_blocks_it(tmp_path, fake_popen):
    var, root, exe, ap = _setup_root(tmp_path)
    dll = exe.with_name("ggml-cuda.dll")
    dll.write_bytes(b"MZ dll")
    ap.record_installed([{"path": str(dll), "publisher": "ggml-org/llama.cpp", "release": "b1"}])
    assert ap.approve(exe)
    s = _server(exe, var, fake_popen)
    s.start()
    assert fake_popen == [] and "approve ggml-cuda.dll in Setup" in s.status()["reason"]
    assert ap.approve(dll)
    s.start()
    assert len(fake_popen) == 1
    s.stop()


def test_launch_check_default_applies_to_every_server(tmp_path, fake_popen, monkeypatch):
    from primer import servers as srv
    from primer.setup.approvals import launch_check

    var, root, exe, ap = _setup_root(tmp_path)
    monkeypatch.setattr(srv, "_default_may_launch", None)
    srv.set_launch_check(launch_check(var))
    spec = EngineSpec("openai", "http://127.0.0.1:9", managed=True, launch=(str(exe),), ready_timeout_s=2)
    s = ManagedServer("text", spec, health=lambda: False)
    s.start()
    assert fake_popen == [] and s.status()["state"] == "unavailable"


# --- fix round 1: the guard fails closed, planted DLLs, commands, retry ---
def _marked_exe(tmp_path, approve=True):
    """An exe in a folder holding Setup's marker, recorded and (optionally) approved."""
    var, root, exe, ap = _setup_root(tmp_path)
    (exe.parent / ".primer-setup.json").write_text('{"items": {"llama_cpp": "x"}}', encoding="utf-8")
    if approve:
        assert ap.approve(exe)
    return var, root, exe, ap


def test_guard_holds_when_setup_json_is_deleted(tmp_path, fake_popen):
    var, root, exe, ap = _marked_exe(tmp_path)
    (var / "setup.json").unlink()
    exe.write_bytes(b"MZ swapped")
    s = _server(exe, var, fake_popen)
    s.start()
    assert fake_popen == [] and s.status()["state"] == "unavailable"


def test_guard_holds_when_setup_json_is_corrupt(tmp_path, fake_popen):
    var, root, exe, ap = _marked_exe(tmp_path)
    (var / "setup.json").write_text("{broken", encoding="utf-8")
    exe.write_bytes(b"MZ swapped")
    s = _server(exe, var, fake_popen)
    s.start()
    assert fake_popen == [] and s.status()["state"] == "unavailable"
    assert (var / "setup.json.bad").exists()


def test_unreadable_setup_json_refuses_and_is_not_moved(tmp_path, fake_popen, monkeypatch):
    from pathlib import Path as P

    var, root, exe, ap = _marked_exe(tmp_path)
    real = P.read_bytes

    def locked(self):
        if self.name == "setup.json":
            raise PermissionError(13, "locked")
        return real(self)

    monkeypatch.setattr(P, "read_bytes", locked)
    s = _server(exe, var, fake_popen)
    s.start()
    assert fake_popen == [] and s.status()["state"] == "unavailable"
    assert "could not check" in s.status()["reason"]
    monkeypatch.undo()
    assert (var / "setup.json").exists() and not (var / "setup.json.bad").exists()


def test_planted_dll_beside_an_approved_exe_blocks_it(tmp_path, fake_popen):
    from primer.setup.approvals import UNKNOWN_PUBLISHER

    var, root, exe, ap = _marked_exe(tmp_path)
    planted = exe.with_name("version.dll")
    planted.write_bytes(b"MZ planted")
    s = _server(exe, var, fake_popen)
    s.start()
    assert fake_popen == [] and "approve version.dll in Setup" in s.status()["reason"]
    row = next(p for p in ap.pending() if p["file"] == "version.dll")
    assert row["publisher"] == UNKNOWN_PUBLISHER
    assert ap.approve(planted)
    s.start()
    assert len(fake_popen) == 1
    s.stop()


def test_same_size_and_mtime_replacement_is_not_approved(tmp_path):
    var, root, exe, ap = _marked_exe(tmp_path)
    st = os.stat(exe)
    assert ap.approved(exe)
    other = tmp_path / "other.exe"
    other.write_bytes(b"MZ origxnal")  # same length
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(other, exe)
    assert os.stat(exe).st_size == st.st_size and os.stat(exe).st_mtime_ns == st.st_mtime_ns
    assert not ap.approved(exe)


def test_relative_launch_paths_resolve_against_repo_root():
    from primer.config import REPO_ROOT
    from primer.setup.approvals import Approvals

    assert Approvals._locate(os.path.join("tools", "x.exe")) == os.path.join(REPO_ROOT, "tools", "x.exe")


def test_pending_does_not_rehash_installed_files(tmp_path, monkeypatch):
    from primer.setup import approvals as apm

    var, root, exe, ap = _setup_root(tmp_path)
    calls = []
    monkeypatch.setattr(apm, "sha256_file", lambda p: calls.append(p) or "0" * 64)
    fresh = apm.Approvals(var)
    assert [p["file"] for p in fresh.pending()] == ["llama-server.exe"]
    assert calls == []


def test_unapproved_pause_and_stop_commands_are_not_run(tmp_path, fake_popen, monkeypatch):
    import subprocess

    var, root, exe, ap = _marked_exe(tmp_path, approve=False)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    from primer.setup.approvals import launch_check

    spec = EngineSpec("openai", "http://127.0.0.1:9", managed=True, launch=("python",),
                      pause=(str(exe), "unload"), stop=(str(exe), "stop"))
    s = ManagedServer("text", spec, health=lambda: False, may_launch=launch_check(var))
    with pytest.raises(RuntimeError, match="pause command not run"):
        s.pause()
    assert "stop command not run" in s._run_stop_command()
    assert ran == []


def test_retry_unavailable_starts_servers_once_approved(tmp_path, fake_popen):
    var, root, exe, ap = _marked_exe(tmp_path, approve=False)
    s = _server(exe, var, fake_popen)
    s.start()
    assert s.status()["state"] == "unavailable"
    mgr = ServerManager.__new__(ServerManager)
    mgr._servers = {"text": s, "fast": s}
    assert ap.approve(exe)
    roles = mgr.retry_unavailable()
    assert roles == ["text"]  # one server behind both roles: started once
    import time
    end = time.monotonic() + 5
    while s.status()["state"] != "ready" and time.monotonic() < end:
        time.sleep(0.01)
    assert s.status()["state"] == "ready" and len(fake_popen) == 1
    s.stop()


def test_refused_unlisted_exe_is_pending_and_approvable_alone(tmp_path, fake_popen):
    from primer.setup.approvals import UNKNOWN_PUBLISHER, Approvals, launch_check

    var = tmp_path / "var"
    folder = tmp_path / "elsewhere" / "tool"
    folder.mkdir(parents=True)
    (folder / ".primer-setup.json").write_text('{"items": {}}', encoding="utf-8")
    exe = folder / "Lonely.exe"
    exe.write_bytes(b"MZ lonely")
    ap = Approvals(var)
    assert ap.pending() == []
    spec = EngineSpec("openai", "http://127.0.0.1:9", managed=True, launch=(str(exe),), ready_timeout_s=2)
    s = ManagedServer("text", spec, health=lambda: bool(fake_popen), may_launch=launch_check(var, ap))
    s.start()
    assert fake_popen == [] and s.status()["state"] == "unavailable"
    row = next(p for p in ap.pending() if p["file"] == "Lonely.exe")
    assert row["publisher"] == UNKNOWN_PUBLISHER and row["item"] == ""
    assert ap.approve(row["path"], row["sha256"])
    assert ap.pending() == []
    s.start()
    assert len(fake_popen) == 1
    s.stop()
