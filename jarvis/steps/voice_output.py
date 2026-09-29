"""Send text to openedai-speech on CAESAR (:8788), play the returned audio.

speak() plays and returns when done. speak_async() queues the line and returns at once;
one background worker plays queued lines in order, so JARVIS keeps working while it
talks and lines never overlap. Best effort: a speech failure is printed, never raised.
"""
from __future__ import annotations

import io
import os
import queue
import threading

import httpx

from jarvis.config import JarvisConfig

_MODEL = "tts-1"
_TIMEOUT = httpx.Timeout(60, connect=5)  # fail fast when CAESAR's service is down
_VOICE = "alloy"
_DEFAULT_SPEECH_URL = "http://192.168.178.64:8788"

_lines: "queue.Queue[tuple[str, str]]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _speech_url(config: JarvisConfig | None) -> str:
    if config is not None:
        return config.voice.tts_url
    return os.environ.get("SPEECH_URL") or os.environ.get("TTS_URL") or _DEFAULT_SPEECH_URL


def _play(text: str, url: str) -> None:
    try:
        import sounddevice as sd  # loaded on first use: importing this module must work without PortAudio
        import soundfile as sf

        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.post(
                f"{url.rstrip('/')}/v1/audio/speech",
                json={"model": _MODEL, "input": text, "voice": _VOICE, "response_format": "wav"},
            )
        response.raise_for_status()
        data, sample_rate = sf.read(io.BytesIO(response.content), dtype="float32")
        sd.play(data, sample_rate)
        sd.wait()
    except Exception as exc:
        print(f"Voice output failed: {exc}")


def _run_worker() -> None:
    while True:
        text, url = _lines.get()
        try:
            _play(text, url)
        finally:
            _lines.task_done()


def speak_async(text: str, config: JarvisConfig | None = None) -> None:
    """Queue a line and return immediately."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = threading.Thread(target=_run_worker, name="jarvis-voice", daemon=True)
            _worker.start()
    _lines.put((text, _speech_url(config)))


def wait_until_spoken() -> None:
    """Block until every queued line has been played (e.g. before listening, or at exit)."""
    _lines.join()


def speak(text: str, config: JarvisConfig | None = None) -> None:
    """Play a line and return when it has been spoken (after anything already queued)."""
    wait_until_spoken()
    _play(text, _speech_url(config))
