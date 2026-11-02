import threading
import time

import pytest

from primer.gpu import GpuGate

WAIT = 2.0  # every wait is bounded so a regression fails instead of hanging


def _until(pred, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


class Recorder:
    """pause_text / resume_text callbacks that log into a shared list."""

    def __init__(self, log=None, fail_pause=False, fail_resume=False):
        self.log = [] if log is None else log
        self.fail_pause, self.fail_resume = fail_pause, fail_resume

    def pause(self):
        self.log.append("pause")
        if self.fail_pause:
            raise RuntimeError("pause blew up")

    def resume(self):
        self.log.append("resume")
        if self.fail_resume:
            raise RuntimeError("resume blew up")


def _gate(mode="auto", **kw):
    rec = Recorder(**kw)
    return GpuGate(mode, rec.pause, rec.resume), rec


def test_manual_mode_never_pauses():
    gate, rec = _gate("manual")
    with gate.image():
        assert gate.state()["holder"] == "image" and gate.state()["text_paused"] is False
    with gate.page():
        assert gate.state()["holder"] == "page"
    gate.release_idle()
    assert rec.log == []
    assert gate.state() == {"mode": "manual", "holder": None, "text_paused": False,
                            "waiting_pages": 0, "waiting_images": 0, "error": None}


def test_image_pauses_and_idle_release_resumes():
    gate, rec = _gate()
    with gate.image():
        assert rec.log == ["pause"]
        assert gate.state()["holder"] == "image" and gate.state()["text_paused"] is True
    st = gate.state()
    assert st["holder"] is None and st["text_paused"] is True and rec.log == ["pause"]
    gate.release_idle()
    assert rec.log == ["pause", "resume"] and gate.state()["text_paused"] is False
    gate.release_idle()  # nothing paused: no second resume
    assert rec.log == ["pause", "resume"]


def test_consecutive_images_keep_text_paused():
    gate, rec = _gate()
    for _ in range(3):
        with gate.image():
            pass
    assert rec.log == ["pause"] and gate.state()["text_paused"] is True
    gate.release_idle()
    assert rec.log == ["pause", "resume"]


def test_page_waits_for_running_image_then_goes_first():
    log: list[str] = []
    rec = Recorder(log)
    gate = GpuGate("auto", rec.pause, rec.resume)
    first_running, let_first_finish = threading.Event(), threading.Event()

    def image1():
        with gate.image():
            log.append("image1")
            first_running.set()
            assert let_first_finish.wait(WAIT)

    def image2():
        with gate.image():
            log.append("image2")

    def page():
        with gate.page():
            log.append("page")

    t1 = threading.Thread(target=image1)
    t1.start()
    assert first_running.wait(WAIT)
    t2 = threading.Thread(target=image2)
    t2.start()
    assert _until(lambda: gate.state()["waiting_images"] == 1)
    t3 = threading.Thread(target=page)
    t3.start()
    assert _until(lambda: gate.state()["waiting_pages"] == 1)
    assert gate.state()["holder"] == "image"
    let_first_finish.set()
    for t in (t1, t2, t3):
        t.join(WAIT)
        assert not t.is_alive()
    assert log == ["pause", "image1", "resume", "page", "pause", "image2"]
    assert gate.state()["holder"] is None and gate.state()["waiting_images"] == 0


def test_failed_image_job_still_restores_text_servers():
    gate, rec = _gate()
    with pytest.raises(RuntimeError, match="sd crashed"):
        with gate.image():
            raise RuntimeError("sd crashed")
    assert gate.state()["holder"] is None
    gate.release_idle()
    assert rec.log == ["pause", "resume"] and gate.state()["text_paused"] is False


def test_pause_error_recorded_not_raised():
    gate, rec = _gate(fail_pause=True)
    with gate.image():
        pass
    st = gate.state()
    assert "pause blew up" in st["error"] and st["holder"] is None and st["text_paused"] is False

    gate, rec = _gate(fail_resume=True)
    with gate.image():
        pass
    with gate.page():  # the resume error never reaches the page
        pass
    assert "resume blew up" in gate.state()["error"] and rec.log == ["pause", "resume"]


def test_page_without_contention_does_not_resume():
    gate, rec = _gate()
    with gate.page():
        assert gate.state()["holder"] == "page"
    assert rec.log == [] and gate.state()["holder"] is None


def test_release_idle_does_nothing_while_gate_is_held():
    gate, rec = _gate()
    with gate.image():
        pass
    with gate.image():
        gate.release_idle()
        assert rec.log == ["pause"] and gate.state()["text_paused"] is True


def test_page_priority_holds_after_failed_try_enter():
    """A page told to wait (try_enter() False) is queued at once, so an image arriving before
    it blocks still goes after it."""
    log: list[str] = []
    rec = Recorder(log)
    gate = GpuGate("auto", rec.pause, rec.resume)
    first = gate.image()
    with first:
        log.append("image1")
        turn = gate.page()
        assert turn.try_enter() is False
        assert gate.state()["waiting_pages"] == 1
    image2_ran = threading.Event()

    def image2():
        with gate.image():
            log.append("image2")
            image2_ran.set()

    t = threading.Thread(target=image2)
    t.start()
    assert _until(lambda: gate.state()["waiting_images"] == 1)
    assert not image2_ran.is_set()
    with turn:
        log.append("page")
        assert gate.state()["waiting_pages"] == 0
    t.join(WAIT)
    assert not t.is_alive()
    assert log == ["pause", "image1", "resume", "page", "pause", "image2"]


def test_cancel_unqueues_a_waiting_turn():
    gate, rec = _gate()
    image2_ran = threading.Event()

    def image2():
        with gate.image():
            image2_ran.set()

    with gate.image():
        turn = gate.page()
        assert turn.try_enter() is False
        t = threading.Thread(target=image2)
        t.start()
        assert _until(lambda: gate.state()["waiting_images"] == 1)
    assert not image2_ran.wait(0.05)  # still behind the queued page
    turn.cancel()
    turn.cancel()  # idempotent
    assert image2_ran.wait(WAIT)
    t.join(WAIT)
    st = gate.state()
    assert st["waiting_pages"] == 0 and st["waiting_images"] == 0 and st["holder"] is None


def test_interrupt_during_switch_releases_gate():
    calls = []

    def pause():
        calls.append("pause")
        if len(calls) == 1:
            raise KeyboardInterrupt

    gate = GpuGate("auto", pause, lambda: None)
    with pytest.raises(KeyboardInterrupt):
        with gate.image():
            pytest.fail("body must not run")
    st = gate.state()
    assert st["holder"] is None and st["waiting_images"] == 0 and st["text_paused"] is False
    with gate.image():  # the gate is usable again
        assert gate.state()["text_paused"] is True
    assert calls == ["pause", "pause"]
