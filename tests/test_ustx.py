"""The OpenUtau export (app/score/ustx.py) and the remix worker's pure parts."""
import re
from fractions import Fraction as F

from app import cantonese as C
from app.score import ustx
from app.score.model import Note, Score

import remix_worker


def song(lyrics="", meters=((4, 4),) * 2, notes=()):
    score = Score(bpm=90, key="Am", meters=list(meters), lyrics=lyrics)
    score.voices["Vocal"] = [Note(F(onset), pitch, F(length)) for onset, pitch, length in notes]
    return score


def check(score):
    vocal = sorted(score.voices["Vocal"], key=lambda n: n.onset)
    return C.check(score.lyrics, [(n.onset, n.end) for n in vocal], [n.pitch for n in vocal])


def lyrics_of(text):
    return re.findall(r'lyric: "(.*?)"', text)


def test_ticks_are_480_a_quarter():
    assert ustx.ticks(F(1)) == 480
    assert ustx.ticks(F(1, 3)) == 160
    assert ustx.ticks(F(9, 2)) == 2160


def test_cantonese_words_become_toneless_jyutping():
    # 銀行 reads hong4 here (not haang4): the reading in context, as the Words lane shows it.
    score = song("我去銀行", notes=[(0, 60, 1), (1, 62, 1), (2, 64, 1), (3, 62, 1)])
    text = ustx.to_ustx(score, name="t", check=check(score), cantonese=True)
    assert lyrics_of(text) == ["ngo", "heoi", "ngan", "hong"]
    assert ustx.JYUTPING_PHONEMIZER in text


def test_a_chosen_reading_is_kept():
    score = song("行", notes=[(0, 60, 1)])
    vocal = score.voices["Vocal"]
    words = C.check(score.lyrics, [(n.onset, n.end) for n in vocal], [n.pitch for n in vocal],
                    {(0, 0): ("行", "hong4")})
    assert lyrics_of(ustx.to_ustx(score, name="t", check=words, cantonese=True)) == ["hong"]


def test_held_and_unsung_notes():
    # Two words over three notes in one phrase: the third holds the second word ("+").
    # A note after a rest that no word reaches gets a plain vowel.
    score = song("你好", notes=[(0, 60, 1), (1, 62, 1), (2, 64, 1), (4, 65, 1)])
    assert lyrics_of(ustx.to_ustx(score, name="t", check=check(score), cantonese=True)) == ["nei", "hou", "+", "aa"]
    assert ustx.lyrics_for_notes(2, None, False) == ["a", "a"]


def test_other_languages_keep_their_words():
    score = song("hello world", notes=[(0, 60, 1), (1, 62, 1)])
    text = ustx.to_ustx(score, name="t", check=check(score), cantonese=False)
    assert lyrics_of(text) == ["hello", "world"]
    assert ustx.DEFAULT_PHONEMIZER in text


def test_notes_tempo_metres_and_key():
    score = song(meters=[(4, 4), (2, 4), (4, 4)], notes=[(F(1, 2), 69, F(3, 2)), (4, 71, 2)])
    text = ustx.to_ustx(score, name="喺度", tempo_scale=1.0125)
    assert 'name: "喺度"' in text and "ustx_version: 0.6" in text and "resolution: 480" in text
    assert "key: 9" in text                                       # A minor -> A
    assert text.count("bpm: 88.888889") == 2                      # 90 / 1.0125, header and tempo list
    assert re.findall(r"- bar_position: (\d+)", text) == ["0", "1", "2"]
    assert re.findall(r"  - position: (\d+)\n    duration: (\d+)\n    tone: (\d+)", text) == [
        ("240", "720", "69"), ("1920", "960", "71")]
    assert "  duration: 4800" in text                             # 4 + 2 + 4 beats


def test_squeezed_words_are_counted():
    score = song("一二三", notes=[(0, 60, 1)])
    assert ustx.dropped(check(score)) == 2
    assert ustx.dropped(None) == 0


def test_mix_filter_places_the_vocal_on_the_songs_clock():
    assert "adelay=delays=1500:all=1" in remix_worker.mix_filter(1.5, 0)
    late = remix_worker.mix_filter(-2.742, 0)
    assert "atrim=start=2.742" in late and "adelay" not in late
    assert "volume=-3.0dB" in remix_worker.mix_filter(0, -3)
    assert remix_worker.mix_filter(0, 0).endswith("loudnorm=I=-14:TP=-1.0:LRA=11:print_format=json[out]")


def test_second_loudness_pass_uses_the_measurement():
    report = ('[Parsed_loudnorm_5 @ 0000] \n{\n\t"input_i" : "-9.81",\n\t"input_tp" : "0.42",\n'
              '\t"input_lra" : "5.10",\n\t"input_thresh" : "-19.95",\n\t"output_i" : "-14.02",\n'
              '\t"normalization_type" : "dynamic",\n\t"target_offset" : "0.02"\n}\n')
    measured = remix_worker.measured_loudness(report)
    assert measured["input_i"] == "-9.81"
    second = remix_worker.mix_filter(0, 0, measured)
    assert "measured_I=-9.81" in second and "offset=0.02" in second and "linear=true" in second
