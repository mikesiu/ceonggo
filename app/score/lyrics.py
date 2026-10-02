"""Which word is sung on which note - an estimate for the score editor's Words lane.

YuE2 reads the lyrics and the score as two separate texts and pairs syllables
with notes by itself; nothing in the ABC says which word goes where. This makes
the same pairing the way a listener would: one syllable per note, in order, kept
in step at the natural breaks - a line end, a space or a comma in the lyrics, a
rest in the melody.

The pairing is global rather than section by section: a transcribed melody's
parts rarely match the lyric's [Verse] / [Chorus] tags (a cover in the library
has 29 words under [Intro] and two notes in the melody's intro), while the totals
do agree, because YuE2 itself simply reads the words in order.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from typing import Sequence

TAG = re.compile(r"^\s*\[([^\]]+)\]\s*$")           # same rule as music.SECTION_RE
TOKEN = re.compile(                                   # Han (with extensions), kana, Hangul; Latin words
    r"(?P<cjk>[〇㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
    r"𠀀-𱍏])"
    r"|(?P<word>[A-Za-z0-9À-ɏ]+(?:['’][A-Za-zÀ-ɏ]+)*)")

MERGE = 1.5      # cost of each extra chunk or phrase folded into one group
EMPTY = 2.0      # extra cost of words with no notes, or notes with no words
WIDEST = 12      # most chunks (or phrases) one group may span; a bridge can need 8


@dataclass(frozen=True)
class Syllable:
    text: str
    line: int        # index into lyrics.split("\n")
    col: int         # code-point column in that line
    chunk: int       # words between two breaks share a chunk
    cjk: bool


@dataclass
class Alignment:
    syllables: list[Syllable]
    sings: list[int | None]          # per vocal note (onset order): the syllable sung, None = no word
    starts: list[bool]               # per note: a syllable begins here (otherwise it is held or unsung)
    phrase: list[int]                # per note: its phrase, phrases being split by rests
    crammed: dict[int, list[int]]    # note -> syllables left over after it; -1 when there are no notes

    @property
    def crammed_count(self) -> int:
        return sum(len(extra) for extra in self.crammed.values())

    @property
    def extra_notes(self) -> int:
        return sum(1 for start in self.starts if not start)

    def notes_of(self, syllable: int) -> list[int]:
        return [i for i, sung in enumerate(self.sings) if sung == syllable]


def syllables(lyrics: str) -> list[Syllable]:
    """Every sung syllable: a CJK character, or a Latin word. Tags and punctuation are skipped.

    A chunk breaks at each line start and at any gap between two syllables, except
    a plain space or hyphen between two Latin words ("don't stop" is one chunk).
    """
    result: list[Syllable] = []
    chunk = -1
    for number, line in enumerate(lyrics.split("\n")):
        if TAG.match(line):
            continue
        previous: tuple[int, bool] | None = None          # (end of the last syllable, was it CJK)
        for match in TOKEN.finditer(line):
            cjk = match.lastgroup == "cjk"
            if previous is None:
                chunk += 1
            else:
                gap = line[previous[0]:match.start()]
                latin_pair = not cjk and not previous[1] and set(gap) <= {" ", "-"}
                if gap and not latin_pair:
                    chunk += 1
            result.append(Syllable(match.group(), number, match.start(), chunk, cjk))
            previous = (match.end(), cjk)
    return result


def phrases(spans: Sequence[tuple[Fraction, Fraction]]) -> list[int]:
    """The phrase of each (onset, end) span, in onset order; a gap before a note starts a new one."""
    result: list[int] = []
    previous_end = None
    for onset, end in spans:
        if previous_end is None:
            result.append(0)
        else:
            result.append(result[-1] + (1 if onset > previous_end else 0))
        previous_end = end
    return result


def _sizes(labels: Sequence[int]) -> tuple[int, ...]:
    sizes: list[int] = []
    for index, label in enumerate(labels):
        if index == 0 or label != labels[index - 1]:
            sizes.append(0)
        sizes[-1] += 1
    return tuple(sizes)


def _prefix(sizes: Sequence[int]) -> list[int]:
    total, result = 0, [0]
    for size in sizes:
        total += size
        result.append(total)
    return result


@lru_cache(maxsize=32)
def group(chunks: tuple[int, ...], phrases: tuple[int, ...]) -> tuple[tuple[int, int, int, int], ...]:
    """Pair runs of lyric chunks with runs of melodic phrases so the counts agree best.

    Returns ``(first chunk, end chunk, first phrase, end phrase)`` per group, ends
    exclusive; one side of a group may be empty. A banded dynamic programme: the
    band always contains the path that steps whichever side is behind, so it can
    never come up empty, and the inner loop stops once a wider group can only cost
    more. Memoised on the sizes, so moving a note's pitch never re-runs it.
    """
    m, n = len(chunks), len(phrases)
    s, k = _prefix(chunks), _prefix(phrases)
    band = abs(s[m] - k[n]) + max(chunks + phrases, default=0) + 8
    inf = float("inf")
    cost = [[inf] * (n + 1) for _ in range(m + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (n + 1) for _ in range(m + 1)]
    cost[0][0] = 0.0
    for i in range(m + 1):
        for j in range(n + 1):
            if (i == 0 and j == 0) or abs(s[i] - k[j]) > band:
                continue
            best, step = inf, None
            if i and cost[i - 1][j] + chunks[i - 1] + EMPTY < best:
                best, step = cost[i - 1][j] + chunks[i - 1] + EMPTY, (1, 0)
            if j and cost[i][j - 1] + phrases[j - 1] + EMPTY < best:
                best, step = cost[i][j - 1] + phrases[j - 1] + EMPTY, (0, 1)
            for a in range(1, min(WIDEST, i) + 1):
                words = s[i] - s[i - a]
                for b in range(1, min(WIDEST, j) + 1):
                    notes = k[j] - k[j - b]
                    merges = MERGE * (a + b - 2)
                    total = cost[i - a][j - b] + abs(words - notes) + merges
                    if total < best:
                        best, step = total, (a, b)
                    if notes > words and notes - words + merges >= best:
                        break
            cost[i][j], back[i][j] = best, step
    groups = []
    i, j = m, n
    while i or j:
        a, b = back[i][j]
        groups.append((i - a, i, j - b, j))
        i, j = i - a, j - b
    return tuple(reversed(groups))


def align(sylls: list[Syllable], spans: Sequence[tuple[Fraction, Fraction]]) -> Alignment:
    """Place ``sylls`` on the notes given as (onset, end) spans in onset order.

    Within a group the syllables take the notes in order. Notes left over hold the
    previous syllable while the phrase goes on (a melisma) and are unsung after a
    rest; syllables left over are "crammed" onto the group's last note.
    """
    phrase = phrases(spans)
    count = len(phrase)
    chunk_offsets = _prefix(_sizes([syl.chunk for syl in sylls]))
    note_offsets = _prefix(_sizes(phrase))
    sings: list[int | None] = [None] * count
    starts = [False] * count
    crammed: dict[int, list[int]] = {}
    for c0, c1, p0, p1 in group(_sizes([syl.chunk for syl in sylls]), _sizes(phrase)):
        words = range(chunk_offsets[c0], chunk_offsets[c1])
        notes = range(note_offsets[p0], note_offsets[p1])
        for q, note in enumerate(notes):
            if q < len(words):
                sings[note], starts[note] = words[q], True
            elif q and phrase[note] == phrase[note - 1]:
                sings[note] = sings[note - 1]
        if len(words) > len(notes):
            if notes:
                key = notes[-1]
            else:
                after = note_offsets[p0]
                key = after - 1 if after > 0 else after if after < count else -1
            crammed.setdefault(key, []).extend(words[len(notes):])
    return Alignment(list(sylls), sings, starts, phrase, crammed)


def replace(lyrics: str, line: int, col: int, length: int, text: str) -> str:
    """``lyrics`` with ``length`` characters at (line, col) replaced by ``text``."""
    lines = lyrics.split("\n")
    if not 0 <= line < len(lines) or col < 0 or length < 0 or col + length > len(lines[line]):
        raise ValueError("That place is not in the lyrics")
    lines[line] = lines[line][:col] + text + lines[line][col + length:]
    return "\n".join(lines)


def replace_line(lyrics: str, line: int, text: str) -> str:
    lines = lyrics.split("\n")
    if not 0 <= line < len(lines):
        raise ValueError("That line is not in the lyrics")
    lines[line] = text
    return "\n".join(lines)
