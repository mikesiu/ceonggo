"""Finding where a finished song really sings its score (app/score/timing.py)."""
import random

from app.score.timing import Clock, fit_clock


def melody(count=400, seed=1):
    rng = random.Random(seed)
    t, notes = 15.0, []
    for _ in range(count):
        notes.append((round(t, 3), rng.choice([62, 65, 67, 70, 72, 74, 77])))
        t += rng.choice([0.2, 0.4, 0.4, 0.8]) + (1.5 if rng.random() < 0.1 else 0)
    return notes


def test_recovers_a_song_that_runs_ahead_and_drifts():
    # Like the library's plan-first song: 2.4 s early at the start, 0.15 s at the end.
    written = melody()
    truth = Clock(1.0127, -2.56)
    rng = random.Random(2)
    heard = [(truth.audio(t) + rng.uniform(-0.05, 0.05), p) for t, p in written if rng.random() > 0.15]
    heard += [(rng.uniform(0, 200), rng.choice([60, 64, 69])) for _ in range(40)]     # transcription noise
    clock = fit_clock(written, heard)
    assert clock is not None
    for t in (16.0, 100.0, 190.0):
        assert abs(clock.audio(t) - truth.audio(t)) < 0.1
    assert abs(clock.score(clock.audio(42.0)) - 42.0) < 1e-9


def test_a_constant_shift_and_round_trip():
    written = melody(120)
    clock = fit_clock(written, [(t - 3.4, p) for t, p in written])
    assert abs(clock.shift + 3.4) < 0.03 and abs(clock.slope - 1) < 0.002
    assert Clock.from_dict(clock.to_dict()) == clock


def test_no_clock_when_the_audio_is_something_else():
    written = melody(200, seed=3)
    assert fit_clock(written, melody(200, seed=4)) is None
    assert fit_clock([], written) is None and fit_clock(written, []) is None
