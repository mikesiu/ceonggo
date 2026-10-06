"""A score as an OpenUtau project (.ustx), to be sung by a DiffSinger voicebank.

OpenUtau keeps its projects as YAML. Only the parts it cannot fill in itself are
written: tempo, metres, one track, one voice part with the vocal notes. Expressions
and a singer are left to OpenUtau (it adds its defaults on load, and the singer is
chosen there), and ``ustx_version: 0.6`` lets it upgrade the file as it does any
older project.

The words come from the score editor's Words lane (``app.cantonese.check``): the
same pairing of syllables with notes, and the readings the user picked. A
Cantonese song gets toneless Jyutping ("gam", not "gam1"): OpenUtau's DiffSinger
Jyutping phonemizer romanises characters without tones and looks the syllable up
in the voicebank's dictionary, and square brackets would mean raw phonemes. The
melody carries the tone. Writing the syllable rather than the character keeps the
reading Ceonggo chose - OpenUtau's own guess for 行 or 長 may differ.

The YAML is written by hand (the window has no YAML library) in a subset any YAML
reader takes: block mappings, and strings in JSON's double-quoted form.
"""
from __future__ import annotations

import json
import re
from fractions import Fraction

from app.cantonese import WordCheck
from app.score import theory
from app.score.model import Score

RESOLUTION = 480                     # OpenUtau ticks per quarter note
VERSION = "0.6"
JYUTPING_PHONEMIZER = "OpenUtau.Core.DiffSinger.DiffSingerJyutpingPhonemizer"
DEFAULT_PHONEMIZER = "OpenUtau.Core.DefaultPhonemizer"
#: OpenUtau's "hold the previous syllable" lyric, for a melisma.
EXTEND = "+"
#: For a note no word reaches. "aa" is a Jyutping syllable; "a" is OpenUtau's default.
UNSUNG = {True: "aa", False: "a"}

_TONE = re.compile(r"[1-6]$")


def ticks(time: Fraction) -> int:
    """Quarter notes -> OpenUtau ticks (exact for anything down to 1/480 of a beat)."""
    return round(Fraction(time) * RESOLUTION)


def toneless(reading: str) -> str:
    return _TONE.sub("", reading)


def lyrics_for_notes(note_count: int, check: WordCheck | None, cantonese: bool) -> list[str]:
    """One OpenUtau lyric per vocal note, in onset order.

    A note where a syllable starts gets that syllable; a note that holds it gets
    "+"; a note no word reaches gets a plain vowel. Syllables the Words lane had
    to squeeze onto one note have nowhere to go and are left out (see ``dropped``).
    """
    if check is None or len(check.alignment.sings) != note_count:
        return [UNSUNG[cantonese]] * note_count
    result = []
    for sung, starts in zip(check.alignment.sings, check.alignment.starts):
        if sung is None:
            result.append(UNSUNG[cantonese])
        elif not starts:
            result.append(EXTEND)
        else:
            reading = check.readings[sung] if cantonese else None
            result.append(toneless(reading) if reading else check.alignment.syllables[sung].text)
    return result


def dropped(check: WordCheck | None) -> int:
    """Syllables without a note of their own, which the project cannot sing."""
    return check.alignment.crammed_count if check is not None else 0


def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def to_ustx(score: Score, *, name: str, check: WordCheck | None = None, cantonese: bool = False,
            tempo_scale: float = 1.0, comment: str = "") -> str:
    """The score's vocal line as a .ustx project.

    ``tempo_scale`` (the song's clock slope, see ``app.score.timing``) slows or
    speeds the tempo so the singing lines up with a song YuE2 sang off the score's
    clock; the bar grid itself is unchanged.
    """
    notes = sorted(score.voices["Vocal"], key=lambda n: n.onset)
    lyrics = lyrics_for_notes(len(notes), check, cantonese)
    bpm = score.bpm / (tempo_scale or 1.0)

    lines = [
        f"name: {_q(name)}",
        f"comment: {_q(comment)}",
        "output_dir: Vocal",
        "cache_dir: UCache",
        f"ustx_version: {VERSION}",
        f"resolution: {RESOLUTION}",
        f"bpm: {_number(bpm)}",
        f"beat_per_bar: {score.meter[0]}",
        f"beat_unit: {score.meter[1]}",
        f"key: {theory.pitch_class(theory.tonic(score.key))}",
        "time_signatures:",
    ]
    previous = None
    for bar, meter in enumerate(score.meters):
        if meter != previous:
            lines += [f"- bar_position: {bar}", f"  beat_per_bar: {meter[0]}", f"  beat_unit: {meter[1]}"]
            previous = meter
    lines += [
        "tempos:",
        "- position: 0",
        f"  bpm: {_number(bpm)}",
        "tracks:",
        "- singer: ''",
        f"  phonemizer: {JYUTPING_PHONEMIZER if cantonese else DEFAULT_PHONEMIZER}",
        "  renderer_settings: {}",
        "  track_name: Vocal",
        "  track_color: Blue",
        "  mute: false",
        "  solo: false",
        "  volume: 0",
        "  pan: 0",
        "voice_parts:",
        f"- name: {_q(name)}",
        "  comment: ''",
        "  track_no: 0",
        "  position: 0",
        f"  duration: {ticks(score.length)}",
        "  notes:" if notes else "  notes: []",
    ]
    for note, lyric in zip(notes, lyrics):
        start = ticks(note.onset)
        lines += [
            f"  - position: {start}",
            f"    duration: {max(1, ticks(note.end) - start)}",
            f"    tone: {note.pitch}",
            f"    lyric: {_q(lyric)}",
            "    pitch:",
            "      data:",
            "      - {x: -40, y: 0, shape: io}",
            "      - {x: 40, y: 0, shape: io}",
            "      snap_first: true",
            "    vibrato: {length: 0, period: 175, depth: 25, in: 10, out: 10, shift: 0, drift: 0, vol_link: 0}",
            "    phoneme_expressions: []",
            "    phoneme_overrides: []",
        ]
    lines += ["  curves: []", "wave_parts: []"]
    return "\n".join(lines) + "\n"
