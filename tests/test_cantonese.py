"""Cantonese readings and the tone check (app/cantonese.py)."""
from fractions import Fraction as F

import pytest

from app import cantonese as C


def eighths(count, rest_after=()):
    spans, time = [], F(0)
    for index in range(count):
        spans.append((time, time + F(1, 2)))
        time += F(1, 2) + (F(1, 2) if index in rest_after else 0)
    return spans


def test_levels_follow_0243():
    # 0 2 4 3 = ling4 ji6 sei3 saam1: the four levels, lowest first.
    assert [C.tone_level(r) for r in ("ling4", "ji6", "sei3", "saam1")] == [0, 1, 2, 3]
    assert C.tone_level("ngo5") == C.tone_level("sei3")
    assert C.tone_level("hai2") == C.tone_level("saam1")
    assert C.tone_level(None) is None and C.tone_level("") is None


def test_fit():
    assert C.fit(0, 3, 60, 64) == "ok"            # both rise
    assert C.fit(3, 0, 64, 60) == "ok"            # both fall
    assert C.fit(2, 2, 60, 60) == "ok"            # both stay
    assert C.fit(2, 2, 60, 62) == "ok"            # a whole tone on one level is normal
    assert C.fit(2, 2, 60, 63) == "mild"          # a leap on one level
    assert C.fit(0, 3, 60, 60) == "mild"          # the tone rises, the melody stays
    assert C.fit(0, 3, 64, 60) == "clash"         # the tone rises, the melody falls
    assert C.fit(3, 1, 60, 62) == "clash"


def test_is_cantonese():
    assert C.is_cantonese("Cantonese, warm piano pop, 88 BPM")
    assert C.is_cantonese("warm pop, cantonese female vocal")
    assert not C.is_cantonese("Mandarin, Canto-pop, 90 BPM")
    assert not C.is_cantonese("")


def test_check_rates_steps_inside_a_phrase(monkeypatch):
    readings = {"一": "jat1", "二": "ji6", "三": "saam1", "四": "sei3"}
    monkeypatch.setattr(C, "line_readings", lambda line: tuple(readings.get(c) for c in line))
    # 一(3) 二(1) 三(3) | 四(2), with a rest before 四.
    spans = eighths(4, rest_after={2})
    result = C.check("一二三四", spans, [67, 60, 59, 62])
    assert result.readings == ["jat1", "ji6", "saam1", "sei3"]
    assert result.fit == [None, "ok", "clash", None]          # 四 follows a rest: not checked
    assert (result.clashes, result.mild) == (1, 0)


def test_check_skips_words_without_their_own_note(monkeypatch):
    monkeypatch.setattr(C, "line_readings", lambda line: tuple("saam1" if c == "三" else "ji6" for c in line))
    result = C.check("一二三", eighths(2), [60, 61])
    assert result.alignment.crammed == {1: [2]}
    assert result.fit == [None, "ok", None]


def test_check_override_counts_only_while_the_word_is_there(monkeypatch):
    monkeypatch.setattr(C, "line_readings", lambda line: ("ji6",) * len(line))
    spans, pitches = eighths(2), [60, 64]
    assert C.check("二行", spans, pitches).fit[1] == "mild"
    assert C.check("二行", spans, pitches, {(0, 1): ("行", "hong4")}).fit[1] == "clash"
    assert C.check("二航", spans, pitches, {(0, 1): ("行", "hong4")}).fit[1] == "mild"


def test_check_ignores_latin_words(monkeypatch):
    monkeypatch.setattr(C, "line_readings", lambda line: tuple("ji6" if "一" <= c <= "鿿" else None for c in line))
    result = C.check("一 love 二", eighths(3), [60, 50, 70])
    assert result.readings == ["ji6", None, "ji6"] and result.fit == [None, None, None]


def test_common_characters_are_big5_level_one():
    chars = C.common_characters()
    assert len(chars) == 5401 and len(set(chars)) == 5401
    assert chars[0] == "一" and "我" in chars and "佢" not in chars


# --- with the real readings ---------------------------------------------------
def test_readings_use_the_words_around_them():
    pytest.importorskip("ToJyutping")
    assert C.available() and C.failure() == ""
    assert C.line_readings("我愛你") == ("ngo5", "oi3", "nei5")
    assert C.line_readings("銀行")[1] == "hong4"
    assert C.line_readings("行路")[0] == "haang4"
    assert C.line_readings("佢 hi，")[:2] == ("keoi5", None)
    assert len(C.line_readings("𠝹開 hello")) == len("𠝹開 hello")
    assert C.candidates("行")[0] and len(C.candidates("行")) > 1
    assert C.candidates("ab") == []


def test_homophones_share_the_usual_reading():
    pytest.importorskip("ToJyutping")
    found = C.homophones("hong4", exclude="行")
    assert found and "行" not in found
    assert all(C.candidates(char)[0] == "hong4" for char in found)
    singles = [len(C.candidates(char)) == 1 for char in found]
    assert singles == sorted(singles, reverse=True)            # one-reading characters first
    assert C.homophones("zzz9") == []
