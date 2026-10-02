"""Which syllable is sung on which note (app/score/lyrics.py) - pure, no Qt."""
import time
from fractions import Fraction as F

import pytest

from app.score import lyrics as L

# Chunk sizes (syllables between breaks) and phrase sizes (notes between rests) of
# two songs in the developer's library: a plan-first song, whose melody YuE2 wrote
# for these words, and a cover, whose melody was transcribed and whose lyric tags
# do not match the melody's parts at all.
PLANNED_CHUNKS = (6, 6, 7, 7, 7, 7, 8, 8, 3, 8, 3, 7, 4, 4, 5, 3, 8, 3, 7, 5, 5, 8, 4, 6, 6, 7, 7, 7, 7, 8, 8, 3,
                  8, 3, 7, 4, 4, 5, 3, 8, 3, 7, 5, 5, 8, 4, 1, 6, 6, 1, 6, 7, 2, 7, 6, 5, 5, 6, 3, 8, 3, 7, 4, 4,
                  5, 3, 8, 3, 7, 5, 5, 8, 4)
PLANNED_PHRASES = (13, 14, 7, 7, 8, 8, 3, 9, 11, 4, 10, 11, 10, 11, 12, 13, 15, 7, 7, 20, 9, 11, 4, 10, 11, 10,
                   12, 12, 36, 12, 4, 8, 4, 9, 11, 4, 10, 11, 21, 12)
COVER_CHUNKS = (10, 8, 5, 6, 10, 7, 7, 8, 8, 7, 4, 4, 4, 5, 5, 6, 9, 8, 4, 5, 5, 6, 10, 10, 8, 7, 4, 4, 4, 5, 5,
                6, 9, 8, 4, 5, 5, 6, 10, 10, 10, 7, 6, 6)
COVER_PHRASES = (10, 3, 5, 5, 6, 10, 7, 7, 8, 8, 8, 4, 4, 5, 5, 5, 7, 9, 9, 5, 5, 5, 6, 10, 8, 2, 8, 8, 4, 4, 5,
                 10, 7, 9, 9, 10, 11, 10, 8, 2, 10, 7, 6, 6)


def sizes(groups, chunks, phrases):
    return [(list(chunks[c0:c1]), list(phrases[p0:p1])) for c0, c1, p0, p1 in groups]


def spans_for(phrase_sizes):
    """Eighth notes, with an eighth rest between phrases."""
    spans, time = [], F(0)
    for size in phrase_sizes:
        for _ in range(size):
            spans.append((time, time + F(1, 2)))
            time += F(1, 2)
        time += F(1, 2)
    return spans


def test_syllables_skip_tags_blank_lines_and_punctuation():
    text = "[Verse]\n搭上這尾班車　靠向你\n\n[Chorus]\n喜歡你，就算！"
    found = L.syllables(text)
    assert "".join(s.text for s in found) == "搭上這尾班車靠向你喜歡你就算"
    assert all(s.cjk for s in found)
    assert [(s.line, s.col) for s in found[:2]] == [(1, 0), (1, 1)]
    assert (found[6].text, found[6].line, found[6].col) == ("靠", 1, 7)    # after a full-width space


def test_chunks_break_at_spaces_and_punctuation_between_chinese():
    found = L.syllables("搭上 這尾，班車\n靠向")
    assert [s.chunk for s in found] == [0, 0, 1, 1, 2, 2, 3, 3]


def test_latin_words_are_one_syllable_and_stay_together():
    found = L.syllables("don't stop, believing\n我 love 你")
    assert [s.text for s in found] == ["don't", "stop", "believing", "我", "love", "你"]
    assert [s.chunk for s in found] == [0, 0, 1, 2, 3, 4]
    assert not found[0].cjk and found[3].cjk


def test_columns_count_code_points_beyond_the_basic_plane():
    found = L.syllables("𠝹開佢")
    assert [(s.text, s.col) for s in found] == [("𠝹", 0), ("開", 1), ("佢", 2)]


def test_replace_keeps_every_other_line():
    text = "[Verse]\n搭上這尾班車\n\n[Chorus]\n喜歡你"
    assert L.replace(text, 1, 2, 1, "呢") == "[Verse]\n搭上呢尾班車\n\n[Chorus]\n喜歡你"
    assert L.replace_line(text, 4, "愛你") == "[Verse]\n搭上這尾班車\n\n[Chorus]\n愛你"
    with pytest.raises(ValueError):
        L.replace(text, 9, 0, 1, "x")
    with pytest.raises(ValueError):
        L.replace(text, 1, 5, 3, "x")
    with pytest.raises(ValueError):
        L.replace_line(text, -1, "x")


def test_phrases_split_at_rests():
    spans = [(F(0), F(1)), (F(1), F(2)), (F(5, 2), F(3)), (F(3), F(4)), (F(6), F(7))]
    assert L.phrases(spans) == [0, 0, 1, 1, 2]
    assert L.phrases([]) == []


def test_group_pairs_a_verse_with_its_phrases():
    chunks, phrases = (6, 6, 7, 7, 7, 7, 8, 8), (11, 14, 7, 7, 8, 8, 2)
    assert sizes(L.group(chunks, phrases), chunks, phrases) == [
        ([6, 6], [11]), ([7, 7], [14]), ([7], [7]), ([7], [7]), ([8], [8]), ([8], [8, 2])]


def test_group_on_a_planned_song():
    groups = sizes(L.group(PLANNED_CHUNKS, PLANNED_PHRASES), PLANNED_CHUNKS, PLANNED_PHRASES)
    assert groups[:6] == [([6, 6], [13]), ([7, 7], [14]), ([7], [7]), ([7], [7]), ([8], [8]), ([8], [8])]
    assert ([1, 6, 6, 1, 6, 7, 2, 7], [36]) in groups          # the bridge sung as one long phrase
    assert sum(len(c) for c, _ in groups) == len(PLANNED_CHUNKS)
    assert sum(len(p) for _, p in groups) == len(PLANNED_PHRASES)


def test_group_on_a_cover_stays_in_step_across_unrelated_parts():
    groups = sizes(L.group(COVER_CHUNKS, COVER_PHRASES), COVER_CHUNKS, COVER_PHRASES)
    assert groups[:3] == [([10], [10]), ([8], [3, 5]), ([5], [5])]
    assert groups.count(([10], [8, 2])) == 2
    assert groups[-4:] == [([10], [10]), ([7], [7]), ([6], [6]), ([6], [6])]


def test_align_holds_within_a_phrase_and_leaves_notes_after_a_rest_unsung():
    # Three words; the phrase has four notes, then one more note after a rest.
    found = L.syllables("一二三")
    spans = [(F(0), F(1)), (F(1), F(2)), (F(2), F(3)), (F(3), F(4)), (F(5), F(6))]
    result = L.align(found, spans)
    assert result.sings == [0, 1, 2, 2, None]
    assert result.starts == [True, True, True, False, False]
    assert result.extra_notes == 2 and result.crammed_count == 0
    assert result.notes_of(2) == [2, 3]


def test_align_crams_leftover_words_onto_the_last_note():
    result = L.align(L.syllables("一二三四五"), [(F(0), F(1)), (F(1), F(2)), (F(2), F(3))])
    assert result.sings == [0, 1, 2]
    assert result.crammed == {2: [3, 4]}
    assert result.crammed_count == 2


def test_align_edge_cases():
    empty = L.align([], [(F(0), F(1))])
    assert empty.sings == [None] and not empty.crammed
    nothing = L.align(L.syllables("一二"), [])
    assert nothing.sings == [] and nothing.crammed == {-1: [0, 1]}
    long = L.align(L.syllables("一二三四五六七八九十" * 10), [(F(0), F(1))])
    assert long.sings == [0] and long.crammed_count == 99


def test_align_a_large_song_quickly():
    text = "\n".join("一二三四五 六七八九十" for _ in range(150))
    spans = spans_for([5, 5] * 150)
    started = time.perf_counter()
    result = L.align(L.syllables(text), spans)
    assert time.perf_counter() - started < 5
    assert all(result.starts) and result.crammed_count == 0


def test_line_span_covers_a_line_and_its_held_notes():
    text = "[Verse]\n一二\n三四"
    spans = [(F(0), F(1)), (F(1), F(2)), (F(2), F(3)), (F(4), F(5)), (F(5), F(6))]
    result = L.align(L.syllables(text), spans)
    assert result.sings == [0, 1, 1, 2, 3]
    assert L.line_span(result, 1) == (0, 2)
    assert L.line_span(result, 2) == (3, 4)
    assert L.line_span(result, 0) is None


def test_audition_lyrics_tags_the_line_with_its_part():
    text = "[Verse]\n搭上這尾班車\n[Chorus]\n就算多辛酸 也不想投訴  "
    assert L.audition_lyrics(text, 3, "chorus") == "[Chorus]\n就算多辛酸 也不想投訴"
    assert L.audition_lyrics(text, 1, "pre-chorus") == "[Pre-Chorus]\n搭上這尾班車"
    assert L.audition_lyrics(text, 1, "") == "[Verse]\n搭上這尾班車"


def test_a_phrase_is_the_words_between_spaces():
    text = "[Verse]\n搭上這尾班車 靠向你那肩膊\nhold me, now"
    spans = [(F(i), F(i + 1)) for i in range(12)] + [(F(13), F(14)), (F(14), F(15)), (F(16), F(17))]
    result = L.align(L.syllables(text), spans)
    chunk = next(s.chunk for s in result.syllables if s.text == "這")
    assert L.chunk_text(result, chunk) == "搭上這尾班車"
    assert L.line_span(result, chunk=chunk) == (0, 5)
    assert L.line_span(result, 1) == (0, 11)
    assert L.chunk_text(result, next(s.chunk for s in result.syllables if s.text == "hold")) == "hold me"
