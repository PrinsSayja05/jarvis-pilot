"""Recognise the wake phrase ("Hallo JARVIS", "Hello JARVIS", "Hey/Hi/OK JARVIS") in a transcript.

The browser only sends a short clip when it detects speech; this decides whether that clip
was the wake phrase. A greeting is required right before the name, so a sentence that merely
mentions JARVIS ("JARVIS hat den PR geöffnet") does not wake it up. Whatever follows the name
in the same breath ("Hallo JARVIS, Ticket JW achtzehn") is returned as the command.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

GREETINGS = {"hallo", "halo", "hello", "helo", "hey", "hei", "hi", "ok", "okay", "okey"}
# How Whisper tends to write the name, in German or English mode.
_NAME_VARIANTS = {"jarvis", "javis", "jarviss", "jervis", "jarwis", "jarves", "charvis", "tscharvis", "dschavis", "scharvis", "jaavis",
                  "dervis", "dervus", "derwis"}  # the larger Whisper models often hear "Hallo, Dervis"
_MAX_GREETING_GAP = 1  # "Hallo JARVIS", "Hallo, äh JARVIS"


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_name(token: str) -> bool:
    return token in _NAME_VARIANTS or (5 <= len(token) <= 8 and _distance(token, "jarvis") <= 1)


@dataclass
class WakeResult:
    woke: bool
    greeting: str = ""
    command: str = ""   # text after the name in the same clip, e.g. "ticket jw 18"
    maybe: bool = False  # greeting + something that starts like the name ("Hallo, JavaScript ..."): ask a better model


# The small wake model sometimes merges the name with the next word ("Hallo, JavaScript OpenTicket").
_NAME_PREFIXES = ("jar", "jav", "jer", "jaa", "char", "tschar", "dschar", "schar", "derv", "derw")


def find_wake_phrase(text: str) -> WakeResult:
    tokens = re.findall(r"[a-zäöüß]+|\d+", text.lower())
    maybe = False
    for i, tok in enumerate(tokens):
        greeted = any(tokens[g] in GREETINGS for g in range(max(0, i - 1 - _MAX_GREETING_GAP), i))
        if not greeted:
            continue
        if _is_name(tok):
            greeting = next(tokens[g] for g in range(max(0, i - 1 - _MAX_GREETING_GAP), i) if tokens[g] in GREETINGS)
            return WakeResult(True, greeting, " ".join(tokens[i + 1:]))
        maybe = maybe or tok.startswith(_NAME_PREFIXES)
    return WakeResult(False, maybe=maybe)
