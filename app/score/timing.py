"""Where a finished song really sings its score: a straight-line clock, score seconds -> audio seconds.

YuE2 follows the score's notes but not quite its clock. Transcribing two songs in
the library and lining the sung notes up with their scores, the singing ran 2.4 s
and 3.4 s ahead of the score at the start, closing to 0.2 s and 2.2 s by the end -
a straight line both times, with 85-100 % of the notes matching in every 20 s
stretch. So one slope and one shift per song are enough to find a line in its audio.

Pure stdlib: the notes come in as (seconds, MIDI pitch) pairs.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Sequence

WINDOW = 20.0        # seconds of score fitted at a time
REACH = 8.0          # furthest the singing may be from the score, either way
STEP = 0.025         # shift resolution, seconds
TOLERANCE = 0.12     # a sung note this close to its shifted score note counts as the same note


@dataclass(frozen=True)
class Clock:
    slope: float = 1.0
    shift: float = 0.0          # audio = slope * score + shift

    def audio(self, score_seconds: float) -> float:
        return self.slope * score_seconds + self.shift

    def score(self, audio_seconds: float) -> float:
        return (audio_seconds - self.shift) / self.slope

    def to_dict(self) -> dict:
        return {"slope": self.slope, "shift": self.shift}

    @classmethod
    def from_dict(cls, data: dict) -> "Clock":
        return cls(float(data["slope"]), float(data["shift"]))


def _hits(written: Sequence[tuple[float, int]], times: list[float], pitches: list[int], shift: float) -> int:
    hits = 0
    for t, p in written:
        target = t + shift
        i = bisect_left(times, target - TOLERANCE)
        while i < len(times) and times[i] <= target + TOLERANCE:
            if pitches[i] == p:
                hits += 1
                break
            i += 1
    return hits


def fit_clock(written: Sequence[tuple[float, int]], heard: Sequence[tuple[float, int]]) -> Clock | None:
    """The clock that best carries ``written`` (score) notes onto ``heard`` (transcribed) ones.

    Each 20 s of score gets the shift that matches most notes; stretches where
    under half match are dropped (a transcription misses some notes, and a song
    can stray), and a weighted straight line goes through the rest. None when no
    stretch matches - the audio is then not this score sung.
    """
    heard = sorted(heard)
    times, pitches = [t for t, _ in heard], [p for _, p in heard]
    written = sorted(written)
    if not written or not heard:
        return None
    points: list[tuple[float, float, int]] = []          # (score time, shift, weight)
    start = 0.0
    while start <= written[-1][0]:
        part = [(t, p) for t, p in written if start <= t < start + WINDOW]
        start += WINDOW
        if len(part) < 4:
            continue
        scores = [(_hits(part, times, pitches, k * STEP), k * STEP)
                  for k in range(-int(REACH / STEP), int(REACH / STEP) + 1)]
        best_hits = max(hits for hits, _ in scores)
        # Every shift within the tolerance of the true one matches as well, so take
        # the middle of the best run, not its edge.
        tied = [shift for hits, shift in scores if hits == best_hits]
        best_shift = tied[len(tied) // 2]
        if best_hits and best_hits * 2 >= len(part):
            points.append((sum(t for t, _ in part) / len(part), best_shift, best_hits))
    if not points:
        return None
    if len(points) == 1:
        return Clock(1.0, points[0][1])
    total = sum(w for *_, w in points)
    mean_t = sum(t * w for t, _, w in points) / total
    mean_s = sum(s * w for _, s, w in points) / total
    spread = sum(w * (t - mean_t) ** 2 for t, _, w in points)
    drift = sum(w * (t - mean_t) * (s - mean_s) for t, s, w in points) / spread if spread else 0.0
    return Clock(1.0 + drift, mean_s - drift * mean_t)
