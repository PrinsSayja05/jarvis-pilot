"""Record from the mic, transcribe via faster-whisper on CAESAR (:8787), understand the answer."""
from __future__ import annotations

import io
import os
import re

import httpx

from jarvis.config import JarvisConfig

_SAMPLE_RATE = 16000
TICKET_SECONDS = 5
ANSWER_SECONDS = 3
_WHISPER_MODEL = "Systran/faster-whisper-large-v3"
_LANGUAGE = "de"
_TIMEOUT = httpx.Timeout(60, connect=5)  # fail fast when CAESAR's service is down
# Primes Whisper with the spelling of project keys; without it "JW fünf" comes back as "Berber J. W. Fung".
# It must contain no complete ticket key: Whisper sometimes echoes the prompt (e.g. on silence), and an
# echoed "JW-9" would start a run nobody asked for.
_WHISPER_PROMPT = "Projekte JW und WMCNL. Ticketnummer, Ja, Nein."
_DEFAULT_MIC_DEVICE = "1"
_DEFAULT_WHISPER_URL ="http://192.168.178.64:8787"

# Known project keys first: Whisper often writes "JW 5", "J W 5" or "JW5" instead of "JW-5".
# A generic "WORD 5" pattern would turn ordinary speech ("bitte 5") into a ticket key.
_KNOWN_PROJECTS = ("WMCNL", "JW")
_KNOWN_PATTERN = re.compile(
    r"\b(" + "|".join(r"\.?\s?".join(key) for key in _KNOWN_PROJECTS) + r")\.?[\s\-_.:]*(\d+(?:\s+\d+)*)\b"
)
_GENERIC_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,7})\b")  # any other project: needs the hyphen

_UNITS = {
    "null": 0, "zero": 0, "eins": 1, "ein": 1, "eine": 1, "one": 1, "zwei": 2, "zwo": 2, "two": 2,
    "drei": 3, "three": 3, "vier": 4, "four": 4, "fünf": 5, "fuenf": 5, "five": 5, "sechs": 6, "six": 6,
    "sieben": 7, "seven": 7, "acht": 8, "eight": 8, "neun": 9, "nine": 9, "zehn": 10, "ten": 10,
    "elf": 11, "eleven": 11, "zwölf": 12, "twelve": 12, "dreizehn": 13, "thirteen": 13, "vierzehn": 14,
    "fourteen": 14, "fünfzehn": 15, "fifteen": 15, "sechzehn": 16, "sixteen": 16, "siebzehn": 17,
    "seventeen": 17, "achtzehn": 18, "eighteen": 18, "neunzehn": 19, "nineteen": 19,
}
_TENS = {
    "zwanzig": 20, "twenty": 20, "dreißig": 30, "dreissig": 30, "thirty": 30, "vierzig": 40, "forty": 40,
    "fünfzig": 50, "fifty": 50, "sechzig": 60, "sixty": 60, "siebzig": 70, "seventy": 70,
    "achtzig": 80, "eighty": 80, "neunzig": 90, "ninety": 90,
}
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
) -> str:
    """Send the audio (WAV from the mic, or webm/ogg from a browser) to faster-whisper, return the transcript."""
    base = (config.voice.stt_url if config else os.environ.get("WHISPER_URL") or _DEFAULT_WHISPER_URL).rstrip("/")
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.post(
            f"{base}/v1/audio/transcriptions",  # OpenAI-compatible API (faster-whisper-server)
            files={"file": (filename, audio, content_type)},
            data={"model": _WHISPER_MODEL, "language": _LANGUAGE, "prompt": _WHISPER_PROMPT},
        )
        if response.status_code == 404:  # older whisper-asr-webservice API
            response = client.post(
                f"{base}/asr", params={"language": _LANGUAGE}, files={"audio_file": (filename, audio, content_type)}
            )
    response.raise_for_status()
    return response.json()["text"].strip()


def _word_value(word: str) -> int | None:
    word = word.lower()
    if word in _UNITS:
        return _UNITS[word]
    if word in _TENS:
        return _TENS[word]
    for sep in ("und", "-"):  # "einundzwanzig", "twenty-one"
        head, found, tail = word.partition(sep)
        if found and sep == "und" and head in _UNITS and tail in _TENS:
            return _TENS[tail] + _UNITS[head]
        if found and sep == "-" and head in _TENS and tail in _UNITS:
            return _TENS[head] + _UNITS[tail]
    return None


def words_to_digits(text: str) -> str:
    """'JW fünf' -> 'JW 5', 'zwei fünf sechs sechs' -> '2 5 6 6'."""
    def replace(match: re.Match) -> str:
        value = _word_value(match.group(0))
        return str(value) if value is not None else match.group(0)

    return re.sub(r"[A-Za-zÄÖÜäöüß]+(?:-[A-Za-z]+)?", replace, text)


def extract_ticket_id(text: str) -> str | None:
    """'Fix JW-5', 'Bearbeite JW fünf', 'j w 5' or 'WMCNL 2566' -> the ticket key, or None."""
    upper = words_to_digits(text).upper()
    match = _KNOWN_PATTERN.search(upper)
    if match:
        return f"{re.sub(r'[\s.]', '', match.group(1))}-{re.sub(r'\s', '', match.group(2))}"
    match = _GENERIC_PATTERN.search(upper)
    return f"{match.group(1)}-{match.group(2)}" if match else None


def parse_yes_no(text: str) -> bool | None:
    """True for ja/yes, False for nein/no/reject, None when unclear (or both were said)."""
    words = set(re.findall(r"[a-zäöüß]+", text.lower()))
    yes, no = bool(words & _YES), bool(words & _NO)
    return None if yes == no else yes


def voice_input(config: JarvisConfig | None = None, seconds: float = TICKET_SECONDS) -> str | None:
    """Record, transcribe, and return the ticket key heard (None if there was none)."""
    print(f"🎤 Listening... ({seconds:g} seconds)")
    transcript = transcribe_audio(record_audio(seconds), config)
    print(f"📝 Heard: {transcript}")
    ticket_id = extract_ticket_id(transcript)
    if ticket_id:
        print(f"🎯 Ticket: {ticket_id}")
    return ticket_id


def listen_yes_no(config: JarvisConfig | None = None, seconds: float = ANSWER_SECONDS) -> bool | None:
    print(f"🎤 Listening for ja / nein... ({seconds:g} seconds)")
    transcript = transcribe_audio(record_audio(seconds), config)
    print(f"📝 Heard: {transcript}")
    return parse_yes_no(transcript)
