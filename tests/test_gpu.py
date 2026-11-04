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
                            "waiting_pages": 0, "waiting_images": 0, "error": None,
                            "background": False}


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


# background (pre-emptible) turns: drafting on an idle GPU


def test_background_only_when_idle():
    gate, rec = _gate()
    bg = gate.background()
    assert bg is not None and gate.state()["holder"] == "page"
    assert gate.background() is None  # one holder at a time
    bg.release()
    bg.release()  # idempotent
    assert gate.state()["holder"] is None

    with gate.page():  # a held page
        assert gate.background() is None

    with gate.image():  # a held picture (and the text is paused after it)
        assert gate.background() is None
    # text paused for pictures: refuse rather than resume on the draft's account
    assert gate.state()["text_paused"] is True
    assert gate.background() is None and rec.log == ["pause"]
    gate.release_idle()
    assert rec.log == ["pause", "resume"]
    bg = gate.background()
    assert bg is not None
    bg.release()

    # a queued picture
    held = gate.page()
    assert held.try_enter()
    queued = gate.image()
    assert queued.try_enter() is False
    held.__exit__(None, None, None)
    assert gate.state()["waiting_images"] == 1 and gate.background() is None
    queued.cancel()

    # a switch in progress (release_idle resuming the text outside the lock)
    in_resume, finish_resume = threading.Event(), threading.Event()

    def slow_resume():
        in_resume.set()
        assert finish_resume.wait(WAIT)

    gate = GpuGate("auto", lambda: None, slow_resume)
    with gate.image():
        pass
    t = threading.Thread(target=gate.release_idle)
    t.start()
    assert in_resume.wait(WAIT)
    assert gate.background() is None
    finish_resume.set()
    t.join(WAIT)
    assert not t.is_alive()
    bg = gate.background()
    assert bg is not None
    bg.release()


def test_background_never_pauses_or_resumes():
    gate, rec = _gate()
    with gate.background() as bg:
        assert bg.preempted() is False
    assert rec.log == [] and gate.state()["holder"] is None


@pytest.mark.parametrize("kind", ["page", "image"])
def test_background_preempted_by_waiting_page_and_image(kind):
    gate, rec = _gate()
    bg = gate.background()
    assert bg is not None
    assert bg.preempted() is False
    assert bg.wait_preempted(0.02) is False
    entered = threading.Event()

    def other():
        with getattr(gate, kind)():
            entered.set()

    t = threading.Thread(target=other)
    t0 = time.monotonic()
    t.start()
    assert bg.wait_preempted(WAIT) is True  # woken by the waiter, not by the timeout
    assert time.monotonic() - t0 < 0.5
    assert bg.preempted() is True
    assert not entered.is_set()  # it waits for the background turn, like for any page
    bg.release()
    assert entered.wait(WAIT)
    t.join(WAIT)
    assert not t.is_alive()
    assert bg.preempted() is False  # released: nothing to step aside for
    st = gate.state()
    assert st["holder"] is None and st["waiting_pages"] == 0 and st["waiting_images"] == 0
    assert rec.log == (["pause"] if kind == "image" else [])


def test_background_release_wakes_waiters():
    gate, rec = _gate()
    bg = gate.background()
    entered = threading.Event()

    def page():
        with gate.page():
            entered.set()

    t = threading.Thread(target=page)
    t.start()
    assert _until(lambda: gate.state()["waiting_pages"] == 1)
    assert not entered.is_set()
    released = time.monotonic()
    with bg:  # the context manager releases on exit too
        pass
    assert entered.wait(0.2)
    assert time.monotonic() - released < 0.2
    t.join(WAIT)
    assert not t.is_alive() and gate.state()["holder"] is None and rec.log == []


def test_background_released_on_exception():
    gate, rec = _gate()
    with pytest.raises(RuntimeError):
        with gate.background():
            raise RuntimeError("draft failed")
    assert gate.state()["holder"] is None
    assert gate.background() is not None


def test_manual_mode_background():
    gate, rec = _gate("manual")
    bg = gate.background()
    assert bg is not None and bg.preempted() is False
    assert gate.background() is None
    with gate.page():  # manual mode lets a real page start alongside: the draft steps aside
        assert bg.preempted() is True
        assert bg.wait_preempted(0.01) is True
    bg.release()
    with gate.image():
        assert gate.background() is None
    bg = gate.background()
    assert bg is not None
    bg.release()
    assert rec.log == [] and gate.state()["holder"] is None


def test_wait_preempted_returns_on_release():
    gate, rec = _gate()
    bg = gate.background()
    result = []
    t = threading.Thread(target=lambda: result.append(bg.wait_preempted(WAIT)))
    t0 = time.monotonic()
    t.start()
    time.sleep(0.02)  # let it block
    bg.release()
    t.join(WAIT)
    assert not t.is_alive() and result == [False] and time.monotonic() - t0 < 0.5


def test_state_says_when_a_draft_holds_the_gate():
    gate, _ = _gate()
    assert gate.state()["background"] is False
    with gate.background() as bg:
        st = gate.state()
        assert st["background"] is True and st["holder"] == "page"
        bg.release()
        assert gate.state()["background"] is False
    with gate.page():
        assert gate.state()["background"] is False


def test_requested_background_turn_is_real_work():
    gate, _ = _gate()
    bg = gate.background()
    page = gate.page()
    assert page.try_enter() is False  # queued behind the draft
    assert bg.preempted() is True
    bg.set_requested(True)  # a reader adopted the draft
    st = gate.state()
    assert st["background"] is False and st["holder"] == "page"
    assert bg.preempted() is False and bg.wait_preempted(0.05) is False
    bg.set_requested(False)  # ... and left again
    assert gate.state()["background"] is True and bg.wait_preempted(0.05) is True
    bg.set_requested(True)
    bg.release()
    assert gate.state()["background"] is False
    with page:
        assert gate.state()["holder"] == "page"
    bg.set_requested(False)  # released: no-op
    assert gate.state()["background"] is False and gate.state()["holder"] is None
