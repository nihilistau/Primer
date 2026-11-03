import pytest

from primer.config import VoiceConfig
from primer.voice.client import VoiceError, make_voice
from tests import fake_tts_server


@pytest.fixture
def tts():
    server, url = fake_tts_server.serve()
    yield server, url
    server.kill()


def test_lfm3_client_speaks(tts):
    server, url = tts
    v = make_voice(VoiceConfig("lfm3", url, voice="male"))
    assert v.health()
    wav = v.speak("Hello there")
    assert wav[:4] == b"RIFF"
    _, path, body, _ = server.requests[-1]
    assert (path, body) == ("/tts", {"input": "Hello there", "voice": "male"})


def test_openai_client_speaks_with_key(tts, tmp_path):
    server, url = tts
    key = tmp_path / "key.txt"
    key.write_text("sekret\n")
    v = make_voice(VoiceConfig("openai", url + "/v1", voice="alloy", model="tts-1", api_key_file=str(key)))
    assert v.health()
    assert v.speak("Hi")[:4] == b"RIFF"
    posts = [r for r in server.requests if r[0] == "POST"]
    _, path, body, headers = posts[-1]
    assert path == "/v1/audio/speech"
    assert body == {"model": "tts-1", "input": "Hi", "voice": "alloy", "response_format": "wav"}
    assert headers["Authorization"] == "Bearer sekret"
    assert any(r[0] == "GET" and r[3].get("Authorization") == "Bearer sekret" for r in server.requests)


def test_client_non_wav_is_error():
    for reply in (b"<html>nope</html>", b""):
        server, url = fake_tts_server.serve(reply=reply)
        try:
            with pytest.raises(VoiceError, match="WAV|empty"):
                make_voice(VoiceConfig("lfm3", url)).speak("x")
        finally:
            server.kill()


def test_client_unreachable_and_http_errors():
    server, url = fake_tts_server.serve(fail_after=0)
    try:
        with pytest.raises(VoiceError, match="503"):
            make_voice(VoiceConfig("lfm3", url)).speak("x")
    finally:
        server.kill()
    dead = make_voice(VoiceConfig("lfm3", "http://127.0.0.1:1"))
    assert dead.health() is False
    with pytest.raises(VoiceError, match="cannot reach"):
        dead.speak("x")


def test_unreadable_key_file_is_error(tts, tmp_path):
    _, url = tts
    v = make_voice(VoiceConfig("openai", url, api_key_file=str(tmp_path / "missing")))
    with pytest.raises(VoiceError, match="api key"):
        v.speak("x")


def test_local_voice_bypasses_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    server, url = fake_tts_server.serve()
    try:
        assert make_voice(VoiceConfig("lfm3", url)).speak("x")[:4] == b"RIFF"
    finally:
        server.kill()


def test_speak_uses_the_configured_timeout(tts, monkeypatch):
    server, url = tts
    v = make_voice(VoiceConfig("lfm3", url, timeout_s=42.0))
    seen = []
    real = v._opener.open
    monkeypatch.setattr(v._opener, "open", lambda req, timeout=None: seen.append(timeout) or real(req, timeout=timeout))
    v.speak("Hello")
    assert seen == [42.0]
