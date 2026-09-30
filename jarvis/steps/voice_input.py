"""Record from the mic, transcribe via faster-whisper on CAESAR (:8787), understand the answer."""
from __future__ import annotations

import io
import os
import re

import httpx

from jarvis.clients.jira_client import JiraClient
from jarvis.config import JarvisConfig
from jarvis.steps.ticket_speech import guess_ticket, resolve_spoken_ticket

_SAMPLE_RATE = 16000
TICKET_SECONDS = 5
ANSWER_SECONDS = 3
# Commands (mic button, after the wake word, ja/nein). Measured 30.09.2026 on CAESAR with the same 24 test
# clips: large-v3-turbo as accurate as large-v3, median 6.7 s vs 7.2 s. WHISPER_MODEL switches back if needed.
_WHISPER_MODEL = os.environ.get("WHISPER_MODEL") or "deepdml/faster-whisper-large-v3-turbo-ct2"
_LANGUAGE = "de"
_TIMEOUT = httpx.Timeout(60, connect=5)  # fail fast when CAESAR's service is down
# Primes Whisper with the spelling of project keys; without it "JW fünf" comes back as "Berber J. W. Fung".
# It must contain no complete ticket key: Whisper sometimes echoes the prompt (e.g. on silence), and an
# echoed "JW-9" would start a run nobody asked for.
_WHISPER_PROMPT = "Projekte JW und WMCNL. Ticketnummer, Ja, Nein."
_DEFAULT_MIC_DEVICE = "1"
_DEFAULT_WHISPER_URL = "http://192.168.178.64:8787"

# Approval changes code, so only explicit agreement counts; filler words ("ok", "weiter") do not.
# The "no" side may be broad: it can only reject or make an answer unclear, never approve.
_YES = {"ja", "jawohl", "yes", "yeah", "yep", "genehmigt", "genehmige"}
_NO = {"nein", "no", "nope", "reject", "ablehnen", "abgelehnt", "abbrechen", "stopp", "stop", "nicht"}


def _mic_device() -> int | str:
    """MIC_DEVICE: a device index or part of its name. Default 1 = Onboard MIC (Intel Smart Sound).

    Indices shift when USB / DisplayLink audio is plugged in; a name such as "Onboard MIC" does not.
    """
    value = os.environ.get("MIC_DEVICE", _DEFAULT_MIC_DEVICE).strip()
    return int(value) if value.isdigit() else value


def record_audio(seconds: float) -> bytes:
    """Record from the microphone, return WAV bytes (16 kHz mono int16)."""
    # Audio libraries load only here: servers without PortAudio (the console on SOKRATES-1) can still transcribe.
    import sounddevice as sd
    import soundfile as sf

    recording = sd.rec(
        int(seconds * _SAMPLE_RATE), samplerate=_SAMPLE_RATE, channels=1, dtype="int16", device=_mic_device()
    )
    sd.wait()
    buffer = io.BytesIO()
    sf.write(buffer, recording, _SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def transcribe_audio(
    audio: bytes,
    config: JarvisConfig | None = None,
    *,
    filename: str = "audio.wav",
    content_type: str = "audio/wav",
    model: str | None = None,
) -> str:
    """Send the audio (WAV from the mic, or webm/ogg from a browser) to faster-whisper, return the transcript."""
    base = (config.voice.stt_url if config else os.environ.get("WHISPER_URL") or _DEFAULT_WHISPER_URL).rstrip("/")
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.post(
            f"{base}/v1/audio/transcriptions",  # OpenAI-compatible API (faster-whisper-server)
            files={"file": (filename, audio, content_type)},
            data={"model": model or _WHISPER_MODEL, "language": _LANGUAGE, "prompt": _WHISPER_PROMPT},
        )
        if response.status_code == 404:  # older whisper-asr-webservice API
            response = client.post(
                f"{base}/asr", params={"language": _LANGUAGE}, files={"audio_file": (filename, audio, content_type)}
            )
    response.raise_for_status()
    return response.json()["text"].strip()


def extract_ticket_id(text: str) -> str | None:
    """Best ticket key in a transcript ("Bearbeite JW achtzehn" -> "JW-18"), not yet checked against Jira."""
    return guess_ticket(text).candidate


def parse_yes_no(text: str) -> bool | None:
    """True for ja/yes, False for nein/no/reject, None when unclear (or both were said)."""
    words = set(re.findall(r"[a-zäöüß]+", text.lower()))
    yes, no = bool(words & _YES), bool(words & _NO)
    return None if yes == no else yes


def voice_input(config: JarvisConfig | None = None, seconds: float = TICKET_SECONDS) -> str | None:
    """Record, transcribe, check the ticket against Jira. Unclear: offer the open tickets to type in."""
    print(f"🎤 Listening... ({seconds:g} seconds)")
    transcript = transcribe_audio(record_audio(seconds), config)
    print(f"📝 Heard: {transcript}")
    if config is None:
        return extract_ticket_id(transcript)
    result = resolve_spoken_ticket(transcript, JiraClient(config.jira))
    print(f"   {result.log_line()}")
    if result.ticket_id:
        print(f"🎯 Ticket: {result.ticket_id} ({result.summary})")
        return result.ticket_id
    if result.say:
        print(f"❓ {result.say}")
    return _choose_open_ticket(result.open_tickets)


def _choose_open_ticket(open_tickets: list[dict]) -> str | None:
    if not open_tickets:
        return None
    for n, t in enumerate(open_tickets, 1):
        print(f"   {n:>2}. {t['key']:<8} {t['summary']}")
    try:
        answer = input("Nummer oder Ticket (Enter = abbrechen): ").strip().upper()
    except EOFError:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(open_tickets):
        return open_tickets[int(answer) - 1]["key"]
    return answer if any(t["key"] == answer for t in open_tickets) else None


def listen_yes_no(config: JarvisConfig | None = None, seconds: float = ANSWER_SECONDS) -> bool | None:
    print(f"🎤 Listening for ja / nein... ({seconds:g} seconds)")
    transcript = transcribe_audio(record_audio(seconds), config)
    print(f"📝 Heard: {transcript}")
    return parse_yes_no(transcript)
