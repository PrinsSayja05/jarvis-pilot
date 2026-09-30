"""Find the ticket a person meant in a Whisper transcript, and check it against Jira.

Whisper rarely writes "JW-18". It writes "JW18.", "J. W. achtzehn", "JW, Nummer 18",
"JW eighteen" or loses the number completely ("... comes with JW, yes."). So:

1. find the project key (JW, WMCNL; spelled out letters and "JW18" glued forms included),
2. look at most MAX_GAP_WORDS words further for a number: digits or a number word 1-99
   in German or English (Whisper may answer in either language, whatever the UI shows),
3. check the candidate against Jira: exists? open?

German "ein"/"eine" are not read as 1: in "JW, ein Ticket für ..." they are articles.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("jarvis.ticket_speech")

KNOWN_PROJECTS = ("WMCNL", "JW")
DEFAULT_PROJECT = "JW"          # whose open tickets are offered when no number was understood
MAX_GAP_WORDS = 4
_GENERIC_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,7})\b")  # other projects: only with the hyphen

_DE_UNITS = {"eins": 1, "zwei": 2, "zwo": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5,
             "sechs": 6, "sieben": 7, "acht": 8, "neun": 9}
_DE_COMPOUND_UNITS = {"ein": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5,
                      "sechs": 6, "sieben": 7, "acht": 8, "neun": 9}
_DE_TEENS = {"zehn": 10, "elf": 11, "zwölf": 12, "zwoelf": 12, "dreizehn": 13, "vierzehn": 14,
             "fünfzehn": 15, "fuenfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19}
_DE_TENS = {"zwanzig": 20, "dreißig": 30, "dreissig": 30, "vierzig": 40, "fünfzig": 50, "fuenfzig": 50,
            "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90}
_EN_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_EN_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
             "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _build_number_words() -> dict[str, int]:
    words: dict[str, int] = {**_DE_UNITS, **_DE_TEENS, **_DE_TENS, **_EN_UNITS, **_EN_TEENS, **_EN_TENS}
    for unit_word, unit in _DE_COMPOUND_UNITS.items():          # einundzwanzig ... neunundneunzig
        for tens_word, tens in _DE_TENS.items():
            words[f"{unit_word}und{tens_word}"] = tens + unit
    for tens_word, tens in _EN_TENS.items():                    # twenty-one / twentyone ... ninety-nine
        for unit_word, unit in _EN_UNITS.items():
            words[f"{tens_word}-{unit_word}"] = words[f"{tens_word}{unit_word}"] = tens + unit
    return words


NUMBER_WORDS = _build_number_words()
_TENS_VALUES = set(_DE_TENS.values())
_EN_UNIT_VALUES = {v: k for k, v in _EN_UNITS.items()}


@dataclass
class TicketGuess:
    project: str | None = None      # project key heard, e.g. "JW"
    candidate: str | None = None    # e.g. "JW-18"
    assumed: bool = False           # project not heard, DEFAULT_PROJECT assumed from "Ticket <number>"


@dataclass
class SpokenTicket:
    transcript: str
    status: str                     # found | not_open | not_found | no_number | nothing
    project: str | None = None
    candidate: str | None = None
    ticket_id: str | None = None    # set only when the ticket exists and is open
    summary: str = ""
    open_tickets: list[dict] = field(default_factory=list)
    say: str = ""                   # German sentence for the speaker
    assumed_project: bool = False

    def log_line(self) -> str:
        checked = {"found": "existiert, offen", "not_open": "existiert, aber nicht offen",
                   "not_found": "existiert nicht", "no_number": "keine Nummer", "nothing": "kein Projekt gehört"}
        project = f"{self.project} (angenommen)" if self.assumed_project else (self.project or "-")
        return (f"heard={self.transcript!r} project={project} "
                f"candidate={self.candidate or '-'} check={checked.get(self.status, self.status)}")


def _tokens(text: str) -> list[str]:
    """Lowercase words and numbers; punctuation separates, a hyphen inside a word (twenty-one) stays."""
    return re.findall(r"[a-zäöüß]+(?:-[a-zäöüß]+)*|\d+", text.lower())


def _match_project(tokens: list[str], i: int) -> tuple[str, int, str | None] | None:
    """(project, tokens used, glued digits) if a project key starts at tokens[i]."""
    for key in KNOWN_PROJECTS:
        low = key.lower()
        if tokens[i] == low:
            return key, 1, None
        if tokens[i].startswith(low) and tokens[i][len(low):].isdigit():   # "jw18" never happens (digits split), kept for safety
            return key, 1, tokens[i][len(low):]
        letters = list(low)                                                  # "j w" / "j. w."
        if tokens[i:i + len(letters)] == letters:
            return key, len(letters), None
    return None


def _number_at(tokens: list[str], j: int) -> tuple[int, int] | None:
    """(value, tokens used) for a number starting at tokens[j], or None."""
    def value(tok: str) -> int | None:
        return int(tok) if tok.isdigit() else NUMBER_WORDS.get(tok)

    first = value(tokens[j]) if j < len(tokens) else None
    if first is None:
        return None
    # "twenty one" (English, two words)
    if first in _TENS_VALUES and not tokens[j].isdigit() and j + 1 < len(tokens) and tokens[j + 1] in _EN_UNITS:
        return first + _EN_UNITS[tokens[j + 1]], 2
    # spoken digit by digit: "2 5 6 6", "zwei fünf sechs sechs" -> 2566. Only single digits are joined,
    # so "JW 18 2026" (ticket, then a year) stays JW-18.
    digits, used = [], 0
    for tok in tokens[j:j + 7]:
        v = value(tok)
        if v is None or v > 9:
            break
        digits.append(str(v)); used += 1
    if used > 1:
        return int("".join(digits)), used
    return first, 1


def guess_ticket(text: str) -> TicketGuess:
    """Best guess from a transcript: which project, and which ticket key if a number follows."""
    # Other projects are only accepted in the exact KEY-123 form.
    generic = _GENERIC_KEY.search(text.upper())
    tokens = _tokens(text)
    guess = TicketGuess()
    for i in range(len(tokens)):
        found = _match_project(tokens, i)
        if not found:
            continue
        project, used, glued = found
        guess.project = guess.project or project
        if glued:
            return TicketGuess(project, f"{project}-{int(glued)}")
        start = i + used
        for j in range(start, min(start + MAX_GAP_WORDS + 1, len(tokens))):
            number = _number_at(tokens, j)
            if number:
                return TicketGuess(project, f"{project}-{number[0]}")
    if generic and generic.group(1) not in KNOWN_PROJECTS:
        return TicketGuess(generic.group(1), f"{generic.group(1)}-{generic.group(2)}")
    if guess.project is None:
        # Whisper sometimes swallows the key ("Bearbeite Ticket JW 18" -> "Der Bayticketjet W18"): the word
        # "Ticket" (also inside a garbled word) with a number right after it means a ticket of DEFAULT_PROJECT.
        # The candidate is still checked against Jira before anything is selected.
        for i, tok in enumerate(tokens):
            if "ticket" not in tok:
                continue
            for j in range(i + 1, min(i + MAX_GAP_WORDS + 2, len(tokens))):
                number = _number_at(tokens, j)
                if number:
                    return TicketGuess(DEFAULT_PROJECT, f"{DEFAULT_PROJECT}-{number[0]}", assumed=True)
    return guess


def resolve_spoken_ticket(transcript: str, jira) -> SpokenTicket:
    """Guess + check against Jira (`jira` is a JiraClient). Every attempt is logged."""
    guess = guess_ticket(transcript)
    result = SpokenTicket(transcript=transcript, status="nothing", project=guess.project, candidate=guess.candidate,
                          assumed_project=guess.assumed)
    project = guess.project or DEFAULT_PROJECT

    def offer_open() -> None:
        try:
            result.open_tickets = [  # Epics group work, JARVIS cannot implement one: not offered
                {"key": t.key, "summary": t.summary, "status": t.status}
                for t in jira.search_open(project) if t.issue_type.casefold() != "epic"
            ]
        except Exception as exc:  # a Jira hiccup must not break voice input
            logger.warning("voice ticket: could not load open %s tickets: %s", project, exc)

    if guess.candidate:
        ticket = None
        try:
            ticket = jira.get_ticket(guess.candidate)
        except Exception as exc:
            logger.info("voice ticket: %s not found in Jira (%s)", guess.candidate, type(exc).__name__)
        if ticket is None:
            result.status = "not_found"
            result.say = f"Das Ticket {guess.candidate} gibt es nicht. Welches Ticket meinen Sie?"
            offer_open()
        else:
            result.summary = ticket.summary
            offer_open()
            open_keys = {t["key"] for t in result.open_tickets}
            if not result.open_tickets or ticket.key in open_keys:
                result.status, result.ticket_id = "found", ticket.key
                result.say = f"Ticket {ticket.key}: {ticket.summary}."
            else:
                result.status = "not_open"
                result.say = f"Das Ticket {ticket.key} ist nicht mehr offen. Welches Ticket meinen Sie?"
    elif guess.project:
        result.status = "no_number"
        result.say = f"Ich habe {guess.project} gehört, aber keine Nummer. Welches Ticket meinen Sie?"
        offer_open()
    logger.info("voice ticket: %s", result.log_line())
    return result
