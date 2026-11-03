import io
import wave

import pytest

from primer.voice import wav
from tests.fake_tts_server import make_wav


def test_wav_join_and_durations():
    a, b = make_wav(24000), make_wav(12000)  # 1.0 s and 0.5 s
    out, durs = wav.join([a, b])
    assert durs == [1.0, 0.5]
    assert abs(wav.duration(out) - 1.75) < 1e-6  # plus a 0.25 s gap between
    assert wav.duration(a) == 1.0
    one, d1 = wav.join([a])
    assert d1 == [1.0] and wav.duration(one) == 1.0
    with wave.open(io.BytesIO(out)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
    assert abs(wav.duration(wav.join([a, b], gap_s=0)[0]) - 1.5) < 1e-6


def test_wav_join_rejects_mismatch():
    a = make_wav(100)
    with pytest.raises(ValueError, match="format"):
        wav.join([a, make_wav(100, rate=16000)])
    with pytest.raises(ValueError, match="format"):
        wav.join([a, make_wav(100, channels=2)])
    with pytest.raises(ValueError, match="WAV"):
        wav.join([a, b"not a wav at all"])
    with pytest.raises(ValueError):
        wav.join([])
    with pytest.raises(ValueError, match="WAV"):
        wav.duration(b"")


def test_wav_streamed_header_8bit_and_unsupported_formats():
    a = make_wav(24000)
    ff = bytes([255]) * 4
    streamed = a[:4] + ff + a[8:40] + ff + a[44:]  # sizes unknown
    assert wav.duration(streamed) == 1.0
    out, durs = wav.join([streamed, make_wav(12000)])
    assert durs == [1.0, 0.5] and abs(wav.duration(out) - 1.75) < 1e-6
    short = a[:40] + (24000 * 2).to_bytes(4, "little") + a[44:44 + 2 * 100]  # header claims more than there is
    assert wav.duration(short) == 100 / 24000
    # 8-bit silence is 0x80
    out, _ = wav.join([make_wav(10, width=1), make_wav(10, width=1)])
    with wave.open(io.BytesIO(out)) as w:
        frames = w.readframes(w.getnframes())
    assert frames[10:10 + 6000] == bytes([128]) * 6000
    # extensible and float WAVs are refused clearly
    for tag in (0xFFFE, 3):
        odd = a[:20] + tag.to_bytes(2, "little") + a[22:]
        with pytest.raises(ValueError, match="unsupported WAV format"):
            wav.duration(odd)
        with pytest.raises(ValueError, match="unsupported WAV format"):
            wav.join([a, odd])
