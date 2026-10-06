"""Cantonese readings and the tone check for the score editor's Words lane.

Sung Cantonese can sound like the wrong word even when every vowel is right: the
melody moves against the word tones (倒字). Cantopop lyricists avoid that with
the 0243 rule - the six tones sit on four pitch levels, and from one syllable to
the next the melody should move the same way as the level does. This checks
exactly that, on the editor's estimate of which syllable sits on which note
(``app.score.lyrics``).

ToJyutping (BSD-2) supplies the readings and chooses between a character's
readings from the words around it (銀行 hong4, 行路 haang4). It is imported here
only, on first use, so nothing else depends on its API, and a build without its
data shows a note instead of failing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from typing import Sequence

from app.score.lyrics import Alignment, align, syllables

#: Jyutping tone -> its 0243 pitch level, 0 lowest ("0243" read in Cantonese is
#: ling4 ji6 sei3 saam1). Tones 3 and 5, and tones 1 and 2, share a level.
LEVELS = {"4": 0, "6": 1, "3": 2, "5": 2, "1": 3, "2": 3}

_module = None
_failure = ""
_homophones: dict[str, list[str]] | None = None


def _converter():
    global _module, _failure
    if _module is None and not _failure:
        try:
            import ToJyutping                    # loads its pronunciation data on import
            _module = ToJyutping
        except Exception as error:  # noqa: BLE001 - shown in the editor; it must still open
            _failure = str(error) or type(error).__name__
    return _module


def available() -> bool:
    return _converter() is not None


def failure() -> str:
    """Why readings are unavailable ("" when they are)."""
    _converter()
    return _failure


def is_cantonese(style: str) -> bool:
    return bool(re.search(r"\bcantonese\b", style, re.IGNORECASE))


def tone_level(reading: str | None) -> int | None:
    return LEVELS.get(reading[-1]) if reading else None


def fit(level_a: int, level_b: int, pitch_a: int, pitch_b: int) -> str:
    """How a step from syllable a to b sits: "ok" (same way), "mild" (one side level), "clash" (opposite).

    A step of up to a whole tone on one tone level counts as "ok": real Cantopop
    does it all the time (55 of 60 such steps in a cover in the library).
    """
    tone = (level_b > level_a) - (level_b < level_a)
    melody = (pitch_b > pitch_a) - (pitch_b < pitch_a)
    if tone == melody or (tone == 0 and abs(pitch_b - pitch_a) <= 2):
        return "ok"
    if tone == 0 or melody == 0:
        return "mild"
    return "clash"


@lru_cache(maxsize=4096)
def line_readings(line: str) -> tuple[str | None, ...]:
    """One reading per code point of ``line`` (None where nothing is sung), chosen in context."""
    module = _converter()
    if module is None:
        return (None,) * len(line)
    pairs = module.get_jyutping_list(line)
    if len(pairs) == len(line) and all(char == source for (char, _), source in zip(pairs, line)):
        return tuple(reading for _, reading in pairs)
    return tuple((candidates(char) or [None])[0] for char in line)   # never misplace a reading


def candidates(char: str) -> list[str]:
    """Every reading of ``char``, the most likely first."""
    module = _converter()
    if module is None or len(char) != 1:
        return []
    return list(module.get_jyutping_candidates(char)[0][1])


def common_characters() -> list[str]:
    """The 5401 frequently used characters (Big5 level 1, 0xA440-0xC67E), in Big5 order."""
    chars = []
    for lead in range(0xA4, 0xC7):
        for trail in [*range(0x40, 0x7F), *range(0xA1, 0xFF)]:
            if (lead, trail) > (0xC6, 0x7E):
                break
            try:
                chars.append(bytes((lead, trail)).decode("big5"))
            except UnicodeDecodeError:
                continue
    return chars


def homophones(reading: str, exclude: str = "", limit: int = 24) -> list[str]:
    """Common characters whose usual reading is ``reading``, those with only one reading first.

    A character that merely *can* be read this way is left out: the singer would
    most likely read it the usual way, which is the problem being fixed.
    """
    global _homophones
    if _homophones is None:
        if _converter() is None:
            return []
        index: dict[str, list[tuple[bool, int, str]]] = {}
        for order, char in enumerate(common_characters()):
            readings = candidates(char)
            if readings:
                index.setdefault(readings[0], []).append((len(readings) > 1, order, char))
        _homophones = {key: [char for *_, char in sorted(found)] for key, found in index.items()}
    return [char for char in _homophones.get(reading, []) if char != exclude][:limit]


@dataclass
class WordCheck:
    alignment: Alignment
    readings: list[str | None]      # per syllable
    fit: list[str | None]           # per syllable: how it follows the one before; None = not checked
    clashes: int
    mild: int


def check(lyrics: str, spans: Sequence[tuple[Fraction, Fraction]], pitches: Sequence[int],
          overrides: dict[tuple[int, int], tuple[str, str]] | None = None) -> WordCheck:
    """Align ``lyrics`` to the vocal notes and check each step between neighbouring syllables.

    A step is checked only inside a phrase (a rest frees the melody) and only when
    both syllables have their own note and a known tone. ``overrides`` maps
    (line, col) to (character, reading); it counts only while that character is
    still there, and changes the check, never what is sung.
    """
    sylls = syllables(lyrics)
    alignment = align(sylls, spans)
    lines = lyrics.split("\n")
    overrides = overrides or {}
    readings: list[str | None] = []
    for syl in sylls:
        chosen = overrides.get((syl.line, syl.col))
        if not syl.cjk:
            readings.append(None)
        elif chosen and chosen[0] == syl.text:
            readings.append(chosen[1])
        else:
            readings.append(line_readings(lines[syl.line])[syl.col])
    fits: list[str | None] = [None] * len(sylls)
    previous = None                          # the note where the previous syllable started
    for note, (sung, start) in enumerate(zip(alignment.sings, alignment.starts)):
        if not start:
            continue
        if previous is not None and alignment.phrase[previous] == alignment.phrase[note] \
                and alignment.sings[previous] == sung - 1:
            a, b = tone_level(readings[sung - 1]), tone_level(readings[sung])
            if a is not None and b is not None:
                fits[sung] = fit(a, b, pitches[previous], pitches[note])
        previous = note
    return WordCheck(alignment, readings, fits, fits.count("clash"), fits.count("mild"))
