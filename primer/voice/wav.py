"""Joining and measuring PCM WAV files (stdlib `wave`)."""
from __future__ import annotations

import io
import struct
import wave


def _read(data: bytes) -> tuple[tuple[int, int, int], bytes]:
    """((channels, width, rate), the sample bytes) of a plain PCM WAV. The frame count comes from
    the bytes actually there, not the header (a streaming server may not know the size)."""
    i = data.find(b"fmt ", 12, 64)
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE" and i >= 0 and len(data) >= i + 10:
        tag = struct.unpack("<H", data[i + 8:i + 10])[0]
        if tag != 1:  # 3 is float, 0xFFFE extensible
            raise ValueError(f"unsupported WAV format (type {tag}); plain PCM is needed")
    try:
        w = wave.open(io.BytesIO(data), "rb")
    except (wave.Error, EOFError):
        raise ValueError("not a WAV file") from None
    with w:
        if w.getcomptype() != "NONE":
            raise ValueError("unsupported WAV format (compressed); plain PCM is needed")
        fmt = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        frames = w.readframes(w.getnframes())
    frame = fmt[0] * fmt[1]
    return fmt, frames[:len(frames) // frame * frame]


def duration(wav: bytes) -> float:
    """Seconds of audio in a WAV."""
    (channels, width, rate), frames = _read(wav)
    return len(frames) // (width * channels) / rate


def join(parts: list[bytes], gap_s: float = 0.25) -> tuple[bytes, list[float]]:
    """One WAV from `parts` with `gap_s` of silence between them, and each part's duration in
    seconds. ValueError for no parts, a part that isn't a WAV, or parts of different formats."""
    if not parts:
        raise ValueError("no audio to join")
    fmt: tuple[int, int, int] | None = None
    frames: list[bytes] = []
    durations: list[float] = []
    for i, part in enumerate(parts):
        try:
            this, data = _read(part)
        except ValueError as exc:
            raise ValueError(f"part {i + 1}: {exc}") from None
        if fmt is None:
            fmt = this
        elif this != fmt:
            raise ValueError(f"part {i + 1} has a different audio format "
                             f"(channels, width, rate) {this} than the first {fmt}")
        frames.append(data)
        durations.append(len(data) // (this[1] * this[0]) / this[2])
    assert fmt is not None
    channels, width, rate = fmt
    quiet = b"\x80" if width == 1 else b"\x00"  # 8-bit WAV is unsigned: silence is 0x80
    silence = quiet * (width * channels * round(max(gap_s, 0.0) * rate))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(silence.join(frames))
    return out.getvalue(), durations
