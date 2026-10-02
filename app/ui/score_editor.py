"""See and edit a score as a piano roll with a chord chart, then use it.

Layout, top to bottom inside the scrolling canvas:

    ruler      bar numbers - click to move the playhead, right-click for bars
    sections   VERSE / CHORUS bands - double-click to rename or start one
    chords     chord symbols - click to type one, right-click to remove
    words      (Cantonese songs with lyrics) the syllable expected on each vocal
               note, its Jyutping, and where the melody fights the word tones
    roll       note blocks against a keyboard, one colour per voice

The keyboard and the header lanes stay pinned while the roll scrolls:
the canvas paints them at the edge of whatever part is visible.

Edits never touch the score while the mouse is down. A drag moves a ghost;
releasing commits one edit (so undo is one step and neighbours are only trimmed
where the note finally lands).

Nothing leaves this window unless ``abc_write.to_abc`` has written it and
re-read it back identical, so an edit can never send YuE2 a score that means
something else.
"""
from __future__ import annotations

import hashlib
import html
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSlider, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from app import cantonese, paths
from app.i18n import N, plural, t
from app.score import abc_dialect, preview, theory
from app.score.abc_write import WriteError, to_abc
from app.score.lyrics import audition_lyrics, chunk_text, line_span, replace, replace_line
from app.score.model import History, Note, Score, UnsupportedScore
from app.score.timing import Clock
from app.ui import theme
from app.ui.widgets import heading

F = Fraction
KEY_W = 58
RULER_H, SECTION_H, CHORD_H = 22, 24, 28
HEAD_H = RULER_H + SECTION_H + CHORD_H
LYRIC_H = 50                      # the Words lane, below the chords, when it is shown
ROW_H = 13
EDGE_PX = 6

SECTION_PRESETS = [N("intro"), N("verse"), N("pre-chorus"), N("chorus"), N("bridge"),
                   N("interlude"), N("solo"), N("outro")]
SNAPS = [(F(1), N("1/4")), (F(1, 2), N("1/8")), (F(1, 4), N("1/16"))]
VOICE_LABELS = {"Vocal": N("Vocal"), "Ins": N("Instrument")}
FIT_TEXT = {"clash": N("The melody moves against this word's tone, so it may be heard as another word."),
            "mild": N("The melody and this word's tone do not quite move together.")}


def describe(text_or_score) -> str:
    """'D major · 86 BPM · 65 bars · 3:01' for a score or its ABC text."""
    try:
        score = text_or_score if isinstance(text_or_score, Score) else Score.from_abc(text_or_score)
    except (abc_dialect.AbcError, UnsupportedScore, ValueError):
        return ""
    seconds = int(score.seconds)
    keys = key_name(score.key)
    if score.key_changes:
        keys += " → " + " → ".join(key_name(k) for _, k in score.key_changes[:2])
        if len(score.key_changes) > 2:
            keys += " …"
    return "  ·  ".join([keys, f"{score.bpm} BPM", t("{n} bars", n=score.bars),
                         f"{seconds // 60}:{seconds % 60:02d}"])


def key_name(key: str) -> str:
    name = theory.tonic(key).replace("#", "♯").replace("b", "♭")
    return t("{name} minor", name=name) if theory.is_minor(key) else t("{name} major", name=name)


# ---------------------------------------------------------------------------
class RollCanvas(QWidget):
    changed = Signal()            # after every committed edit
    seek = Signal(object)         # Fraction: playhead moved by the user
    readings_changed = Signal()   # a reading picked for the tone check (the score is unchanged)

    def __init__(self, history: History, lyrics_lane: bool = False, parent=None):
        super().__init__(parent)
        self.history = history
        self.head_h = HEAD_H + (LYRIC_H if lyrics_lane else 0)
        #: (line, col) -> (character, reading) the user picked for the tone check.
        self.readings: dict[tuple[int, int], tuple[str, str]] = {}
        self._words_memo: tuple[tuple, cantonese.WordCheck] | None = None
        #: Set by the dialog: line -> [(label, action)] to hear a lyric line.
        self.line_actions = None
        self.voice = "Vocal"
        self.snap = F(1, 2)
        self.ppq = 36.0                     # pixels per quarter note
        self.playhead = F(0)
        self.selected: list[Note] = []
        self._drag = None                   # dict describing the gesture in progress
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._fit_range()

    # --- geometry --------------------------------------------------------
    @property
    def score(self) -> Score:
        return self.history.score

    def _fit_range(self) -> None:
        pitches = [n.pitch for v in self.score.voices.values() for n in v] or [60, 72]
        self.top = min(108, max(max(pitches) + 4, 79))
        self.bottom = max(24, min(min(pitches) - 4, 48))
        self._resize()

    def _resize(self) -> None:
        width = KEY_W + int(float(self.score.length) * self.ppq) + 240
        height = self.head_h + (self.top - self.bottom + 1) * ROW_H + 4
        self.setMinimumSize(width, height)
        self.resize(width, height)

    def set_zoom(self, ppq: float) -> None:
        self.ppq = ppq
        self._resize()
        self.update()

    def x_of(self, time) -> float:
        return KEY_W + float(time) * self.ppq

    def time_of(self, x: float) -> Fraction:
        return F(max(0.0, (x - KEY_W) / self.ppq)).limit_denominator(64)

    def y_of(self, pitch: int) -> int:
        return self.head_h + (self.top - pitch) * ROW_H

    def pitch_of(self, y: float) -> int:
        return max(self.bottom, min(self.top, self.top - int((y - self.head_h) // ROW_H)))

    def snapped(self, time: Fraction) -> Fraction:
        return F(int(time / self.snap)) * self.snap

    def _visible(self) -> QRect:
        return self.visibleRegion().boundingRect()

    def note_rect(self, note: Note) -> QRectF:
        return QRectF(self.x_of(note.onset), self.y_of(note.pitch) + 1,
                      max(3.0, float(note.duration) * self.ppq - 1), ROW_H - 2)

    def ensure_visible(self, time: Fraction) -> None:
        scroll = self.parent().parent() if self.parent() else None
        if isinstance(scroll, QScrollArea):
            scroll.ensureVisible(int(self.x_of(time)), int(self._visible().center().y()), 200, 0)

    # --- words -----------------------------------------------------------
    @property
    def lyrics_lane(self) -> bool:
        return self.head_h > HEAD_H

    def _vocal(self) -> list[Note]:
        return sorted(self.score.voices["Vocal"], key=lambda n: n.onset)

    def words(self) -> cantonese.WordCheck:
        """The syllable expected on each vocal note and its tone fit; recomputed only after a change."""
        notes = self._vocal()
        key = (self.score.lyrics, tuple((n.onset, n.end, n.pitch) for n in notes),
               tuple(sorted(self.readings.items())))
        if self._words_memo is None or self._words_memo[0] != key:
            check = cantonese.check(self.score.lyrics, [(n.onset, n.end) for n in notes],
                                    [n.pitch for n in notes], self.readings)
            self._words_memo = (key, check)
        return self._words_memo[1]

    # --- painting --------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        view = self._visible()
        score = self.score
        p.fillRect(view, QColor(theme.INPUT_BG))

        # Rows: black keys darker, C lines stronger.
        for pitch in range(self.bottom, self.top + 1):
            y = self.y_of(pitch)
            if y + ROW_H < view.top() or y > view.bottom():
                continue
            black = pitch % 12 in (1, 3, 6, 8, 10)
            p.fillRect(QRect(view.left(), y, view.width(), ROW_H), QColor("#0a0a0d" if black else "#101014"))
            if pitch % 12 == 0:
                p.fillRect(QRect(view.left(), y + ROW_H - 1, view.width(), 1), QColor(theme.LINE_2))

        # Grid: snap lines, beats, bars (bars can differ in length).
        end_x = self.x_of(score.length)
        starts = score.bar_starts
        for bar in range(score.bars):
            if self.x_of(starts[bar + 1]) < view.left() or self.x_of(starts[bar]) > view.right():
                continue
            offset = F(0)
            while starts[bar] + offset < starts[bar + 1]:
                x = int(self.x_of(starts[bar] + offset))
                colour = QColor(theme.LINE_2) if offset == 0 else QColor(theme.LINE) if offset % 1 == 0 \
                    else QColor("#17171c")
                p.fillRect(QRect(x, self.head_h, 1, self.height() - self.head_h), colour)
                offset += self.snap
        p.fillRect(QRectF(end_x, self.head_h, max(0, self.width() - end_x), self.height()), QColor("#050506"))

        # Notes: the voice being edited on top, in the accent.
        other = "Ins" if self.voice == "Vocal" else "Vocal"
        font = QFont(theme.MONO_FONT)
        font.setPixelSize(9)
        p.setFont(font)
        for note in score.voices[other]:
            rect = self.note_rect(note)
            if rect.right() < view.left() or rect.left() > view.right():
                continue
            p.setPen(QPen(QColor(theme.DIM), 1))
            p.setBrush(QColor(95, 95, 106, 60))
            p.drawRoundedRect(rect, 2, 2)
        fill = QColor(theme.ACCENT if self.voice == "Vocal" else theme.ACCENT_2)
        ghost = self._ghost()
        for note in score.voices[self.voice]:
            if ghost and note is ghost[0]:
                continue
            self._draw_note(p, note, fill, view, note in self.selected)
        if ghost:
            _, g = ghost
            colour = QColor(fill)
            colour.setAlpha(170)
            self._draw_note(p, g, colour, view, True)
        if self._drag and self._drag["kind"] == "band":
            p.setPen(QPen(QColor(theme.TEXT), 1, Qt.DashLine))
            p.setBrush(QColor(255, 255, 255, 20))
            p.drawRect(QRect(self._drag["start"], self._drag["now"]).normalized())

        # Playhead.
        px = int(self.x_of(self.playhead))
        p.fillRect(QRect(px, self.head_h, 2, self.height()), QColor(theme.ACCENT))

        self._paint_header(p, view)
        self._paint_keyboard(p, view)

    def _draw_note(self, p: QPainter, note: Note, colour: QColor, view: QRect, selected: bool) -> None:
        rect = self.note_rect(note)
        if rect.right() < view.left() or rect.left() > view.right():
            return
        p.setPen(QPen(QColor(theme.TEXT), 1.5) if selected else Qt.NoPen)
        p.setBrush(colour)
        p.drawRoundedRect(rect, 2.5, 2.5)
        label = theory.note_name(note.pitch, self.score.key_at(self.score.bar_of(note.onset)))
        if rect.width() > 26:
            p.setPen(QColor(theme.ACCENT_INK))
            p.drawText(rect.adjusted(3, 0, -2, 0), Qt.AlignVCenter | Qt.AlignLeft, label)

    def _paint_header(self, p: QPainter, view: QRect) -> None:
        score = self.score
        top = view.top()
        head = QRect(view.left(), top, view.width(), self.head_h)
        p.fillRect(head, QColor(theme.SURFACE))
        p.fillRect(QRect(view.left(), top + self.head_h - 1, view.width(), 1), QColor(theme.LINE_2))
        small = QFont(theme.MONO_FONT)
        small.setPixelSize(10)
        bold = QFont(theme.UI_FONT)
        bold.setPixelSize(11)
        bold.setBold(True)
        chord_font = QFont(theme.UI_FONT)
        chord_font.setPixelSize(13)
        chord_font.setBold(True)

        # Ruler.
        p.setFont(small)
        starts = score.bar_starts
        for bar in range(score.bars + 1):
            x = int(self.x_of(starts[bar]))
            if x < view.left() - 60 or x > view.right() + 2:
                continue
            p.fillRect(QRect(x, top, 1, self.head_h), QColor(theme.LINE))
            if bar < score.bars:
                label = str(bar + 1)
                if bar == 0 or score.meters[bar] != score.meters[bar - 1]:
                    label += f"  {score.meters[bar][0]}/{score.meters[bar][1]}"
                if bar > 0 and score.key_at(bar) != score.key_at(bar - 1):
                    label += "  " + t("Key {key}", key=key_name(score.key_at(bar)))
                p.setPen(QColor(theme.MUTED))
                p.drawText(QRect(x + 4, top, 220, RULER_H), Qt.AlignVCenter, label)

        # Sections.
        sections = sorted(score.sections)
        y = top + RULER_H
        for index, (bar, name) in enumerate(sections):
            end_bar = sections[index + 1][0] if index + 1 < len(sections) else score.bars
            x0, x1 = self.x_of(starts[bar]), self.x_of(starts[end_bar])
            if x1 < view.left() or x0 > view.right():
                continue
            rect = QRectF(x0 + 1, y + 2, x1 - x0 - 2, SECTION_H - 4)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.SURFACE_3 if index % 2 else theme.SURFACE_2))
            p.drawRoundedRect(rect, 4, 4)
            # Keep the name readable when the part starts off-screen, but never
            # let it spill into the next part.
            label_x = max(rect.left() + 6, view.left() + KEY_W + 6)
            label = QRectF(label_x, rect.top(), rect.right() - label_x - 4, rect.height())
            if label.width() > 12:
                p.save()
                p.setClipRect(label)
                p.setFont(bold)
                p.setPen(QColor(theme.TEXT))
                p.drawText(label, Qt.AlignVCenter, t(name).upper() if name in SECTION_PRESETS else name.upper())
                p.restore()

        # Chords.
        y = top + RULER_H + SECTION_H
        p.setFont(chord_font)
        for onset, symbol in score.chords:
            x = self.x_of(onset)
            if x < view.left() - 80 or x > view.right():
                continue
            p.fillRect(QRectF(x, y + 4, 2, CHORD_H - 8), QColor(theme.ACCENT))
            p.setPen(QColor(theme.ACCENT_2))
            p.drawText(QRectF(x + 5, y, 90, CHORD_H), Qt.AlignVCenter, symbol.replace("b", "♭").replace("#", "♯"))

        if self.lyrics_lane:
            self._paint_words(p, view, top + HEAD_H)

        # Corner label over the keyboard.
        corner = QRect(view.left(), top, KEY_W, self.head_h)
        p.fillRect(corner, QColor(theme.SURFACE))
        p.setFont(small)
        p.setPen(QColor(theme.DIM))
        p.drawText(QRect(view.left() + 6, top, KEY_W, RULER_H), Qt.AlignVCenter, t("Bar"))
        p.drawText(QRect(view.left() + 6, top + RULER_H, KEY_W, SECTION_H), Qt.AlignVCenter, t("Part"))
        p.drawText(QRect(view.left() + 6, top + RULER_H + SECTION_H, KEY_W, CHORD_H), Qt.AlignVCenter, t("Chord"))
        if self.lyrics_lane:
            p.drawText(QRect(view.left() + 6, top + HEAD_H, KEY_W, LYRIC_H), Qt.AlignVCenter, t("Words"))

    def _paint_words(self, p: QPainter, view: QRect, y: int) -> None:
        """One cell per vocal note: the syllable and its Jyutping, tinted where the tone fit is poor.

        A held note (the syllable goes on) shows a line, a note with no word a dot,
        and "+N" marks words squeezed onto a note with no note of their own.
        """
        notes = self._vocal()
        check = self.words()
        align = check.alignment
        starts = [i for i, start in enumerate(align.starts) if start]
        # A word's text may run on over its held notes and the gap before the next
        # word; only the next word's start limits it.
        room_end = {i: (self.x_of(notes[nxt].onset) if nxt is not None else self.x_of(notes[i].end) + 80)
                    for i, nxt in zip(starts, starts[1:] + [None])}
        char_font, small_char = QFont(theme.UI_FONT), QFont(theme.UI_FONT)
        char_font.setPixelSize(18)
        small_char.setPixelSize(14)
        reading_font = QFont(theme.UI_FONT)
        reading_font.setPixelSize(12)
        reading_metrics = QFontMetrics(reading_font)
        for index, note in enumerate(notes):
            x0, x1 = self.x_of(note.onset), self.x_of(note.end)
            if x1 < view.left() or x0 > view.right() + 120:
                continue
            cell = QRectF(x0 + 1, y + 2, max(2.0, x1 - x0 - 2), LYRIC_H - 4)
            sung = align.sings[index]
            if sung is None:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(theme.DIM))
                p.drawEllipse(QRectF(cell.center().x() - 2, y + 14, 4, 4))
            elif not align.starts[index]:
                p.fillRect(QRectF(cell.left(), y + 15, cell.width(), 2), QColor(theme.DIM))
            else:
                fit = check.fit[sung]
                colour = QColor(theme.BAD if fit == "clash" else theme.WARN if fit == "mild" else theme.SURFACE_2)
                p.setPen(Qt.NoPen)
                if fit in ("clash", "mild"):
                    # A full-strength bar under the cell: a faint tint alone reads as brown here.
                    p.fillRect(QRectF(cell.left(), cell.bottom() - 3, cell.width(), 3), colour)
                    colour.setAlpha(110 if fit == "clash" else 50)
                p.setBrush(colour)
                p.drawRoundedRect(cell, 3, 3)
                room = room_end[index] - x0 - 3
                # Too narrow even for the smaller character: draw nothing rather than
                # a clipped fragment that looks like another character. Zoom in, or
                # click the cell, to see it.
                font = char_font if room >= 20 else small_char if room >= 15 else None
                if font is not None:
                    p.setFont(font)
                    p.setPen(QColor(theme.TEXT))
                    p.drawText(QRectF(x0 + 2, y + 2, room, 26), Qt.AlignVCenter, align.syllables[sung].text)
                reading = check.readings[sung]
                if reading and font is not None:
                    label = reading if reading_metrics.horizontalAdvance(reading) <= room else reading[-1]
                    p.setFont(reading_font)
                    p.setPen(QColor(theme.MUTED))
                    p.drawText(QRectF(x0 + 2, y + 27, room, 16), Qt.AlignVCenter, label)
            if align.crammed.get(index):
                p.setFont(reading_font)
                p.setPen(QColor(theme.BAD))
                p.drawText(QRectF(cell.right() - 30, cell.top(), 30, 16), Qt.AlignRight | Qt.AlignVCenter,
                           f"+{len(align.crammed[index])}")

    def _paint_keyboard(self, p: QPainter, view: QRect) -> None:
        left = view.left()
        font = QFont(theme.MONO_FONT)
        font.setPixelSize(9)
        p.setFont(font)
        for pitch in range(self.bottom, self.top + 1):
            y = self.y_of(pitch)
            if y < view.top() + self.head_h - ROW_H or y > view.bottom():
                continue
            black = pitch % 12 in (1, 3, 6, 8, 10)
            rect = QRect(left, y, KEY_W, ROW_H)
            p.fillRect(rect, QColor("#1b1b21" if black else "#d9d9de"))
            p.fillRect(QRect(left, y + ROW_H - 1, KEY_W, 1), QColor("#2a2a31" if black else "#9a9aa3"))
            if pitch % 12 == 0 or pitch in (self.top, self.bottom):
                p.setPen(QColor("#111" if not black else theme.MUTED))
                p.drawText(rect.adjusted(4, 0, 0, 0), Qt.AlignVCenter, theory.note_name(pitch, "C"))
        if view.top() + self.head_h > self.y_of(self.top):
            p.fillRect(QRect(left, view.top(), KEY_W, self.head_h), QColor(theme.SURFACE))
        p.fillRect(QRect(left + KEY_W - 1, view.top(), 1, view.height()), QColor(theme.LINE_2))

    # --- hit testing -----------------------------------------------------
    def _lane(self, pos: QPoint) -> str:
        view = self._visible()
        if pos.x() < view.left() + KEY_W:
            return "keys"
        y = pos.y() - view.top()
        if y < RULER_H:
            return "ruler"
        if y < RULER_H + SECTION_H:
            return "sections"
        if y < HEAD_H:
            return "chords"
        if y < self.head_h:
            return "lyrics"
        return "roll"

    def _note_under(self, pos: QPoint) -> tuple[Note | None, bool]:
        for note in reversed(self.score.voices[self.voice]):
            rect = self.note_rect(note)
            if rect.contains(pos):
                return note, pos.x() >= rect.right() - EDGE_PX
        return None, False

    def _chord_near(self, pos: QPoint) -> tuple[Fraction, str] | None:
        for onset, symbol in self.score.chords:
            x = self.x_of(onset)
            if x - 4 <= pos.x() <= x + 60:
                return onset, symbol
        return None

    # --- gestures --------------------------------------------------------
    def _ghost(self):
        drag = self._drag
        if not drag or drag["kind"] not in ("move", "resize", "new"):
            return None
        return drag.get("note"), drag["ghost"]

    def mousePressEvent(self, event) -> None:
        self.setFocus()
        pos = event.position().toPoint()
        lane = self._lane(pos)
        time = self.time_of(pos.x())
        if lane == "ruler":
            if event.button() == Qt.RightButton:
                self._bar_menu(event.globalPosition().toPoint(), self.score.bar_of(time))
            elif time < self.score.length:
                self.playhead = self.snapped(time)
                self.seek.emit(self.playhead)
                self.update()
            return
        if lane == "sections":
            if event.button() == Qt.RightButton:
                self._section_menu(event.globalPosition().toPoint(), self.score.bar_of(time))
            return
        if lane == "chords":
            near = self._chord_near(pos)
            if event.button() == Qt.RightButton and near:
                menu = QMenu(self)
                menu.addAction(t("Change chord…"), lambda: self._edit_chord(near[0], near[1]))
                menu.addAction(t("Remove chord"), lambda: self._commit(lambda s: s.delete_chord(near[0])))
                menu.exec(event.globalPosition().toPoint())
            elif event.button() == Qt.LeftButton:
                if near:
                    self._edit_chord(near[0], near[1])
                elif time < self.score.length:
                    onset = self.snapped(time)
                    self._edit_chord(onset, "")
            return
        if lane == "lyrics":
            self._word_menu(time, event.globalPosition().toPoint())
            return
        if lane != "roll":
            return
        note, edge = self._note_under(pos)
        if event.button() == Qt.RightButton:
            if note:
                targets = self.selected if note in self.selected else [note]
                menu = QMenu(self)
                menu.addAction(t("Delete note"), lambda: self._delete(targets))
                menu.exec(event.globalPosition().toPoint())
            return
        if event.modifiers() & Qt.ShiftModifier and not note:
            self._drag = {"kind": "band", "start": pos, "now": pos}
            return
        if note:
            if event.modifiers() & Qt.ControlModifier:
                if note in self.selected:
                    self.selected.remove(note)
                else:
                    self.selected.append(note)
                self.update()
                return
            self.selected = [note]
            if edge:
                self._drag = {"kind": "resize", "note": note, "ghost": Note(note.onset, note.pitch, note.duration)}
            else:
                self._drag = {"kind": "move", "note": note, "offset": time - note.onset,
                              "ghost": Note(note.onset, note.pitch, note.duration)}
        else:
            if time >= self.score.length:
                return
            onset = self.snapped(time)
            self.selected = []
            self._drag = {"kind": "new", "note": None, "ghost": Note(onset, self.pitch_of(pos.y()), self.snap)}
        self.update()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        drag = self._drag
        if not drag:
            lane = self._lane(pos)
            note, edge = self._note_under(pos) if lane == "roll" else (None, False)
            self.setCursor(Qt.SizeHorCursor if edge else Qt.PointingHandCursor if note or lane == "lyrics"
                           else Qt.CrossCursor if lane == "roll" else Qt.ArrowCursor)
            return
        time = self.time_of(pos.x())
        ghost = drag.get("ghost")
        if drag["kind"] == "move":
            ghost.onset = max(F(0), min(self.score.length - ghost.duration, self.snapped(time - drag["offset"] + self.snap / 2)))
            ghost.pitch = self.pitch_of(pos.y())
        elif drag["kind"] in ("resize", "new"):
            end = max(ghost.onset + self.snap, self.snapped(time + self.snap / 2))
            ghost.duration = min(end, self.score.length) - ghost.onset
        elif drag["kind"] == "band":
            drag["now"] = pos
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        drag, self._drag = self._drag, None
        if not drag:
            return
        voice = self.voice
        if drag["kind"] == "band":
            rect = QRect(drag["start"], drag["now"]).normalized()
            self.selected = [n for n in self.score.voices[voice] if self.note_rect(n).intersects(QRectF(rect))]
            self.update()
            return
        ghost, original = drag["ghost"], drag.get("note")
        if original and (ghost.onset, ghost.pitch, ghost.duration) == (original.onset, original.pitch, original.duration):
            self.update()
            return
        self.history.checkpoint()
        if original:
            self.score.delete_note(voice, original)
        placed = self.score.add_note(voice, ghost.onset, ghost.pitch, ghost.duration)
        self.selected = [placed]
        self._after_edit()

    def mouseDoubleClickEvent(self, event) -> None:
        pos = event.position().toPoint()
        if self._lane(pos) == "sections":
            bar = self.score.bar_of(self.time_of(pos.x()))
            self._rename_section(bar)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.selected:
            self._delete(self.selected)
        elif event.key() in (Qt.Key_Up, Qt.Key_Down) and self.selected:
            step = (12 if event.modifiers() & Qt.ShiftModifier else 1) * (1 if event.key() == Qt.Key_Up else -1)
            self._shift_selection(step)
        elif event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Home):
            self.move_playhead(None if event.key() == Qt.Key_Home
                               else (-1 if event.key() == Qt.Key_Left else 1))
            event.accept()
        else:
            super().keyPressEvent(event)

    def move_playhead(self, bars: int | None) -> None:
        """Home (``None``) and ←/→ step the play line by whole bars.

        This lives on the canvas rather than the dialog because the piano roll sits
        in a QScrollArea, which claims the arrow keys to scroll itself. A handler
        further up never saw them: the view slid sideways and the play line stayed
        where it was. Accepting the key here stops that.
        """
        if bars is None:
            bar = 0
        else:
            here = self.score.bar_of(self.playhead)
            # Going back from mid-bar lands on this barline first, as a transport does.
            back_to_barline = bars < 0 and self.playhead > self.score.bar_start(here)
            bar = max(0, min(here if back_to_barline else here + bars, self.score.bars - 1))
        self.playhead = self.score.bar_start(bar)
        self.ensure_visible(self.playhead)
        self.update()
        self.seek.emit(self.playhead)

    # --- edits -----------------------------------------------------------
    def _after_edit(self) -> None:
        live = {id(n) for n in self.score.voices[self.voice]}
        self.selected = [n for n in self.selected if id(n) in live]
        self._resize()
        self.update()
        self.changed.emit()

    def _commit(self, change) -> bool:
        self.history.checkpoint()
        try:
            change(self.score)
        except ValueError as error:
            self.history.undo()
            QMessageBox.information(self, t("Score"), str(error))
            return False
        self._after_edit()
        return True

    def _delete(self, notes: list[Note]) -> None:
        targets = list(notes)
        self._commit(lambda s: [s.delete_note(self.voice, n) for n in targets])
        self.selected = []

    def _shift_selection(self, step: int) -> None:
        targets = sorted(self.selected, key=lambda n: n.onset)

        def change(score: Score) -> None:
            moved = []
            for note in targets:
                if not 0 <= note.pitch + step <= 127:
                    raise ValueError(t("That would move notes out of range."))
            for note in targets:
                score.delete_note(self.voice, note)
            for note in targets:
                moved.append(score.add_note(self.voice, note.onset, note.pitch + step, note.duration))
            self.selected = moved

        self._commit(change)

    def _edit_chord(self, onset: Fraction, current: str) -> None:
        text, ok = QInputDialog.getText(
            self, t("Chord"), t("Chord at bar {bar}, beat {beat} (for example C, Am7, F/A, G7sus4):",
                                bar=self.score.bar_of(onset) + 1,
                                beat=f"{float(onset - self.score.bar_start(self.score.bar_of(onset))) + 1:g}"),
            text=current)
        if not ok:
            return
        text = text.strip().replace("♭", "b").replace("♯", "#")
        if not text:
            if current:
                self._commit(lambda s: s.delete_chord(onset))
            return
        if not abc_dialect.CHORD.fullmatch(text):
            QMessageBox.information(self, t("Chord"), t("“{text}” is not a chord the engine understands. Use a "
                                                         "root and a type such as C, Cm, C7, Cmaj7, Cm7, Cdim, "
                                                         "Csus4, C6, with an optional bass like C/E.", text=text))
            return
        self._commit(lambda s: s.set_chord(onset, text))

    def _rename_section(self, bar: int) -> None:
        starts = dict(self.score.sections)
        current = starts.get(bar, "")
        items = [t(p) for p in SECTION_PRESETS]
        choice, ok = QInputDialog.getItem(self, t("Song part"), t("Part starting at bar {bar}:", bar=bar + 1),
                                          items, items.index(t(current)) if t(current) in items else 1, True)
        if ok and choice.strip():
            reverse = {t(p): p for p in SECTION_PRESETS}
            name = reverse.get(choice, choice)
            self._commit(lambda s: s.set_section(bar, name))

    def _section_menu(self, where: QPoint, bar: int) -> None:
        menu = QMenu(self)
        start = menu.addMenu(t("Start a part at bar {bar}", bar=bar + 1))
        for preset in SECTION_PRESETS:
            start.addAction(t(preset), lambda p=preset: self._commit(lambda s: s.set_section(bar, p)))
        start.addAction(t("Other…"), lambda: self._rename_section(bar))
        if bar in dict(self.score.sections) and bar != 0:
            menu.addAction(t("Remove this part start"), lambda: self._commit(lambda s: s.set_section(bar, "")))
        menu.exec(where)

    def _bar_menu(self, where: QPoint, bar: int) -> None:
        menu = QMenu(self)
        if bar < self.score.bars:
            menu.addAction(t("Insert a bar before bar {bar}", bar=bar + 1),
                           lambda: self._commit(lambda s: s.add_bars(1, bar)))
            menu.addAction(t("Delete bar {bar}", bar=bar + 1), lambda: self._commit(lambda s: s.delete_bar(bar)))
        menu.addAction(t("Add 4 bars at the end"), lambda: self._commit(lambda s: s.add_bars(4)))
        menu.exec(where)

    def _word_menu(self, time: Fraction, where: QPoint) -> None:
        """What is known about the word on the vocal note at ``time``, and ways to change it.

        Its notes are selected first (when the vocal line is being edited), so ↑/↓
        can fix a tone clash straight after the menu closes.
        """
        notes = self._vocal()
        index = next((i for i, n in enumerate(notes) if n.onset <= time < n.end), None)
        if index is None:
            return
        check = self.words()
        align = check.alignment
        sung, squeezed = align.sings[index], align.crammed.get(index, [])
        if sung is None and not squeezed:
            return
        menu = QMenu(self)
        syl = align.syllables[sung if sung is not None else squeezed[0]]
        if sung is not None:
            if self.voice == "Vocal":
                self.selected = [notes[i] for i in align.notes_of(sung)]
                self.update()
            reading = check.readings[sung]
            menu.addAction(f"{syl.text}   {reading}" if reading else syl.text).setEnabled(False)
            if check.fit[sung] in FIT_TEXT:
                menu.addAction(t(FIT_TEXT[check.fit[sung]])).setEnabled(False)
        if squeezed:
            extra = "".join(align.syllables[i].text for i in squeezed)
            menu.addAction(t("Squeezed onto this note: {words}", words=extra)).setEnabled(False)
        actions = self.line_actions(syl) if self.line_actions else []
        if actions:
            menu.addSeparator()
            for label, action in actions:
                menu.addAction(label, action)
        if sung is not None and syl.cjk and cantonese.available():
            menu.addSeparator()
            options = cantonese.candidates(syl.text)
            if len(options) > 1:
                pick = menu.addMenu(t("Reading for the tone check"))
                for option in options:
                    action = pick.addAction(option, lambda o=option: self._choose_reading(syl, o))
                    action.setCheckable(True)
                    action.setChecked(option == reading)
            same = menu.addMenu(t("Replace with a word that sounds the same"))
            found = cantonese.homophones(reading, exclude=syl.text) if reading else []
            for char in found:
                same.addAction(char, lambda c=char: self._set_lyrics(
                    replace(self.score.lyrics, syl.line, syl.col, len(syl.text), c)))
            if not found:
                same.addAction(t("None found among the common characters")).setEnabled(False)
        menu.addSeparator()
        menu.addAction(t("Edit this line…"), lambda: self._edit_line(syl.line))
        menu.exec(where)

    def _choose_reading(self, syl, reading: str) -> None:
        self.readings[(syl.line, syl.col)] = (syl.text, reading)
        self.update()
        self.readings_changed.emit()

    def _set_lyrics(self, lyrics: str) -> None:
        self._commit(lambda s: setattr(s, "lyrics", lyrics))

    def _edit_line(self, line: int) -> None:
        current = self.score.lyrics.split("\n")[line]
        text, ok = QInputDialog.getText(self, t("Words"), t("Line {n} of the lyrics:", n=line + 1), text=current)
        if ok and text != current:
            self._set_lyrics(replace_line(self.score.lyrics, line, text))


# ---------------------------------------------------------------------------
class PreviewRender(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, score: Score, target: Path, parent=None):
        super().__init__(parent)
        self.score, self.target = score, target

    def run(self) -> None:
        try:
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self.target.write_bytes(preview.render(self.score))
            self.done.emit(str(self.target))
        except Exception as error:  # noqa: BLE001 - reported in the window
            self.failed.emit(str(error))


# ---------------------------------------------------------------------------
class ScoreEditor(QDialog):
    """Open with ABC text; emits ``chosen(target, abc)`` for "sing" / "create" / "cover".

    Given the song's lyrics and style, a Cantonese song also gets the Words lane;
    whoever receives ``chosen`` reads ``edited_lyrics()`` for words changed there.
    """

    chosen = Signal(str, str)

    LABELS = {"sing": N("Sing it now"), "create": N("Make a song with this score"),
              "cover": N("Make a cover with this score")}

    def __init__(self, text: str, *, title: str, actions: tuple[str, ...] = ("create", "cover"),
                 primary: str = "create", pause_player=None, audio_device: str = "",
                 latency_ms: int = 0, lyrics: str = "", style: str = "", song_audio: str = "",
                 seed: int | None = None, audition=None, song_clock=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1360, 860)
        self.setMinimumSize(1000, 640)
        self.original_text = text
        self.original_lyrics = lyrics
        self.style, self.seed, self.song_audio, self.audition = style, seed, song_audio, audition
        self.song_clock = song_clock
        self._clock: Clock | None = None          # where the song really sings the score
        self._clock_failed = False
        self._waiting_to_hear: int | None = None  # a phrase asked for while the clock is found
        self._tries: dict[int, tuple[str, int]] = {}     # lyric line -> (clip, its offset in ms)
        self._singing = False
        self._closed = False
        self._audition_note = ""
        self._song_bpm = 90
        self.words_lane = bool(lyrics.strip()) and cantonese.is_cantonese(style)
        self.pause_player = pause_player
        self.audio_device = audio_device
        #: position() reports what the sound card has been given, not what has been
        #: heard, so the playhead is drawn this far behind it. See PlayerBar.
        self.latency_ms = max(0, int(latency_ms))
        self._play_from = F(0)
        self._focused = False
        self.dirty = False
        self._render: PreviewRender | None = None
        self._preview_hash = ""
        self._preview_file = ""
        try:
            self.history = History(Score.from_abc(text))
            self.history.score.lyrics = lyrics
            self._song_bpm = self.history.score.bpm     # the finished song's clock
            self.original_key = self.history.score.key
            self.visual_error = ""
        except UnsupportedScore as error:
            self.history, self.visual_error = None, t("This score can only be edited as text: {why}.", why=str(error))
        except abc_dialect.AbcError as error:
            self.history, self.visual_error = None, t("This score could not be read: {why}", why=str(error))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)

        top = QHBoxLayout()
        name = QLabel(title)
        name.setStyleSheet("font-size:18px;font-weight:800;")
        top.addWidget(name)
        top.addSpacing(12)
        self.info = QLabel("")
        self.info.setObjectName("MonoAccent")
        top.addWidget(self.info, 1)
        self.advanced = QPushButton(t("ABC text"))
        self.advanced.setCheckable(True)
        self.advanced.setToolTip(t("Show the score as the text the engine reads"))
        self.advanced.toggled.connect(self._toggle_text)
        top.addWidget(self.advanced)
        open_btn = QPushButton(t("Open .abc…"))
        open_btn.clicked.connect(self._open_file)
        top.addWidget(open_btn)
        save_btn = QPushButton(t("Save as .abc…"))
        save_btn.clicked.connect(self._save_file)
        top.addWidget(save_btn)
        # The ABC has no place for words, so edited lyrics get their own file.
        self.save_lyrics_btn = QPushButton(t("Save lyrics…"))
        self.save_lyrics_btn.setToolTip(t("Save the lyrics, with any words changed here, as a text file"))
        self.save_lyrics_btn.clicked.connect(self._save_lyrics)
        self.save_lyrics_btn.setVisible(bool(lyrics.strip()) and self.history is not None)
        top.addWidget(self.save_lyrics_btn)
        layout.addLayout(top)

        # --- toolbar -----------------------------------------------------
        self.toolbar = QWidget()
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        bar.addWidget(heading(t("Key")))
        self.key_combo = QComboBox()
        self.key_combo.setMinimumWidth(130)
        self.key_combo.activated.connect(self._key_chosen)
        bar.addWidget(self.key_combo)
        down = QPushButton("−1")
        down.setFixedWidth(40)
        down.setToolTip(t("Down a semitone"))
        down.clicked.connect(lambda: self._transpose(-1))
        bar.addWidget(down)
        up = QPushButton("+1")
        up.setFixedWidth(40)
        up.setToolTip(t("Up a semitone"))
        up.clicked.connect(lambda: self._transpose(1))
        bar.addWidget(up)
        bar.addSpacing(10)
        bar.addWidget(heading(t("Tempo")))
        self.tempo = QSpinBox()
        self.tempo.setRange(30, 240)
        self.tempo.setSuffix(" BPM")
        self.tempo.editingFinished.connect(self._tempo_changed)
        bar.addWidget(self.tempo)
        bar.addSpacing(10)
        bar.addWidget(heading(t("Edit")))
        self.voice_buttons = {}
        for voice in ("Vocal", "Ins"):
            button = QPushButton(t(VOICE_LABELS[voice]))
            button.setObjectName("Chip")
            button.setCheckable(True)
            button.clicked.connect(lambda _, v=voice: self._set_voice(v))
            bar.addWidget(button)
            self.voice_buttons[voice] = button
        bar.addSpacing(10)
        bar.addWidget(heading(t("Snap")))
        self.snap_buttons = []
        for value, label in SNAPS:
            button = QPushButton(t(label))
            button.setObjectName("Chip")
            button.setCheckable(True)
            button.clicked.connect(lambda _, v=value: self._set_snap(v))
            bar.addWidget(button)
            self.snap_buttons.append((value, button))
        bar.addSpacing(10)
        bar.addWidget(heading(t("Zoom")))
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(12, 120)
        # With the Words lane a 16th note needs ~20 px for its character to be readable.
        self.zoom.setValue(80 if self.words_lane else 36)
        self.zoom.setFixedWidth(110)
        bar.addWidget(self.zoom)
        bar.addStretch(1)
        self.undo_btn = QPushButton(t("Undo"))
        self.undo_btn.clicked.connect(self._undo)
        bar.addWidget(self.undo_btn)
        self.redo_btn = QPushButton(t("Redo"))
        self.redo_btn.clicked.connect(self._redo)
        bar.addWidget(self.redo_btn)
        self.play_btn = QPushButton(t("▶  Play"))
        self.play_btn.setMinimumWidth(110)
        self.play_btn.clicked.connect(self._toggle_play)
        bar.addWidget(self.play_btn)
        layout.addWidget(self.toolbar)

        # --- body --------------------------------------------------------
        self.stack = QStackedWidget()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setStyleSheet(f"QScrollArea{{border:1px solid {theme.LINE};border-radius:10px;}}")
        if self.history:
            self.canvas = RollCanvas(self.history, lyrics_lane=self.words_lane)
            self.canvas.changed.connect(self._changed)
            self.canvas.readings_changed.connect(self._refresh)
            self.canvas.line_actions = self._line_actions
            self.canvas.seek.connect(self._seek)
            self.zoom.valueChanged.connect(lambda v: self.canvas.set_zoom(float(v)))
            self.canvas.set_zoom(float(self.zoom.value()))
            self.scroll.setWidget(self.canvas)
            self.scroll.horizontalScrollBar().valueChanged.connect(lambda _: self.canvas.update())
            self.scroll.verticalScrollBar().valueChanged.connect(lambda _: self.canvas.update())
        else:
            self.canvas = None
        self.stack.addWidget(self.scroll)
        text_page = QWidget()
        text_layout = QVBoxLayout(text_page)
        text_layout.setContentsMargins(0, 0, 0, 0)
        self.text = QPlainTextEdit()
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setStyleSheet(f"font-family:'{theme.MONO_FONT}';font-size:12.5px;")
        text_layout.addWidget(self.text, 1)
        apply_row = QHBoxLayout()
        self.text_note = QLabel(t("Edit the text, then press Apply. The score is checked before it is used."))
        self.text_note.setObjectName("Hint")
        apply_row.addWidget(self.text_note, 1)
        apply_btn = QPushButton(t("Apply"))
        apply_btn.clicked.connect(self._apply_text)
        apply_row.addWidget(apply_btn)
        text_layout.addLayout(apply_row)
        self.stack.addWidget(text_page)
        layout.addWidget(self.stack, 1)

        self.hint = QLabel(t("Click to add a note · drag to move · drag its right edge to change length · "
                             "Delete removes · ↑/↓ change pitch · Shift-drag selects several · "
                             "click the chord lane to add a chord · click the bar numbers or press "
                             "←/→/Home to move the play line · right-click bars and parts for more"))
        if self.words_lane:
            self.hint.setText(self.hint.text() + "\n" + t(
                "Words shows the word expected on each note (an estimate - the singer places them itself). "
                "Red: the melody moves against the word's tone, so it may be heard as another word. "
                "Click a word to select its notes, choose its reading, or swap in a word that sounds the same."))
        self.hint.setObjectName("Hint")
        self.hint.setWordWrap(True)
        # The Words lane's counts get a line of their own: tucked into the grey
        # status line they were easy to miss, and they are what changes as you fix notes.
        self.words_status = QLabel("")
        self.words_status.setTextFormat(Qt.RichText)
        self.words_status.setStyleSheet(f"color:{theme.TEXT};font-size:14px;")
        self.words_status.setVisible(self.words_lane)
        layout.addWidget(self.words_status)
        layout.addWidget(self.hint)
        self.status = QLabel("")
        self.status.setObjectName("Warning")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        footer = QHBoxLayout()
        close = QPushButton(t("Close"))
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        footer.addStretch(1)
        for action in actions:
            button = QPushButton(t(self.LABELS[action]))
            if action == primary:
                button.setObjectName("Primary")
                button.setMinimumWidth(260)
            else:
                button.setMinimumHeight(40)
            button.clicked.connect(lambda _, a=action: self._use(a))
            footer.addWidget(button)
        layout.addLayout(footer)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        if self.audio_device:
            # The preview has its own player, so the device chosen in Settings has
            # to be applied here too or it would quietly use the system default.
            from PySide6.QtMultimedia import QMediaDevices

            for device in QMediaDevices.audioOutputs():
                if device.description() == self.audio_device:
                    self.audio.setDevice(device)
                    break
        self.player.setAudioOutput(self.audio)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_play_state)
        self._start_when_loaded: int | None = None     # see _play_file
        self._loading_seen = False
        self._stop_at: int | None = None
        self._offset_ms = 0
        self._follow = True
        self._play_clock: Clock | None = None
        self.player.mediaStatusChanged.connect(self._media_status)

        QShortcut(QKeySequence.Undo, self, activated=self._undo)
        QShortcut(QKeySequence.Redo, self, activated=self._redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, activated=self._redo)
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self._toggle_play)

        if self.history:
            self._set_voice("Vocal")
            self._set_snap(F(1, 2))
            self._refresh()
        else:
            self.text.setPlainText(text)
            self.advanced.setChecked(True)
            self.advanced.setEnabled(False)
            self.toolbar.setEnabled(False)
            self.hint.setVisible(False)
            self.status.setText(self.visual_error)

    # --- state -----------------------------------------------------------
    @property
    def score(self) -> Score | None:
        return self.history.score if self.history else None

    def _refresh(self) -> None:
        score = self.score
        if score is None:
            return
        self.info.setText(describe(score))
        pool = theory.MINOR_KEYS if theory.is_minor(score.key) else theory.MAJOR_KEYS
        self.key_combo.blockSignals(True)
        self.key_combo.clear()
        for key in pool:
            self.key_combo.addItem(key_name(key), key)
        self.key_combo.setCurrentIndex(max(0, self.key_combo.findData(score.key)))
        self.key_combo.blockSignals(False)
        self.tempo.blockSignals(True)
        self.tempo.setValue(int(score.bpm))
        self.tempo.blockSignals(False)
        self.undo_btn.setEnabled(self.history.can_undo)
        self.redo_btn.setEnabled(self.history.can_redo)
        notes = []
        span = score.pitch_range("Vocal")
        if span:
            notes.append(t("Vocal range {low} – {high}", low=theory.note_name(span[0], score.key),
                           high=theory.note_name(span[1], score.key)))
        if score.key_changes:
            notes.append(t("The key changes {n} times - the Key box moves the whole song", n=len(score.key_changes)))
        shift = theory.nearest_interval(self.original_key, score.key)
        if abs(shift) > 5:
            notes.append("⚠  " + t("Moved {n} semitones from the original key - the singer may strain.",
                                   n=f"{shift:+d}"))
        if not score.voices["Vocal"]:
            notes.append("⚠  " + t("The vocal line has no notes."))
        if self.words_lane:
            note = f'<br><span style="color:{theme.ACCENT_2}">{html.escape(self._audition_note)}</span>' \
                if self._audition_note else ""
            self.words_status.setText(self._words_summary() + note)
        self.status.setObjectName("Warning" if any(n.startswith("⚠") for n in notes) else "Hint")
        self.status.setStyleSheet("")
        self.status.setText("   ·   ".join(notes))
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _words_summary(self) -> str:
        """The Words lane's counts as rich text, the tone clashes in red while there are any."""
        if not cantonese.available():
            return html.escape(t("Jyutping is not available, so the words have no tones: {why}",
                                 why=cantonese.failure()))
        check = self.canvas.words()
        clashes = html.escape(plural(check.clashes, "{n} tone clash", "{n} tone clashes"))
        if check.clashes:
            clashes = f'<b style="color:{theme.BAD}">{clashes}</b>'
        return t("Words: {clashes}, {crammed}, {extra}",
                 clashes=clashes,
                 crammed=plural(check.alignment.crammed_count, "{n} word without its own note",
                                "{n} words without their own note"),
                 extra=plural(check.alignment.extra_notes, "{n} note without a new word",
                              "{n} notes without a new word"))

    def edited_lyrics(self) -> str | None:
        """The lyrics as edited in the Words lane, or None when they are unchanged."""
        if self.score is None or self.score.lyrics.strip() == self.original_lyrics.strip():
            return None
        return self.score.lyrics

    def _changed(self) -> None:
        self.dirty = True
        self._stop()
        self._refresh()

    def _set_voice(self, voice: str) -> None:
        for name, button in self.voice_buttons.items():
            button.setChecked(name == voice)
        if self.canvas:
            self.canvas.voice = voice
            self.canvas.selected = []
            self.canvas.update()

    def _set_snap(self, value: Fraction) -> None:
        for snap, button in self.snap_buttons:
            button.setChecked(snap == value)
        if self.canvas:
            self.canvas.snap = value
            self.canvas.update()

    def _undo(self) -> None:
        if self.history and self.history.undo():
            self._after_history()

    def _redo(self) -> None:
        if self.history and self.history.redo():
            self._after_history()

    def _after_history(self) -> None:
        self.canvas.history = self.history
        self.canvas.selected = []
        self.canvas._fit_range()
        self.canvas.update()
        self._changed()

    def _transpose(self, semitones: int) -> None:
        if not self.canvas or not semitones:
            return
        if self.canvas._commit(lambda s: s.transpose(semitones)):
            self.canvas._fit_range()
            self._refresh()

    def _key_chosen(self) -> None:
        target = self.key_combo.currentData()
        if target and self.score and target != self.score.key:
            self._transpose(theory.nearest_interval(self.score.key, target))

    def _tempo_changed(self) -> None:
        if self.canvas and self.score and self.tempo.value() != self.score.bpm:
            value = self.tempo.value()
            self.canvas._commit(lambda s: setattr(s, "bpm", value))

    # --- text mode -------------------------------------------------------
    def current_text(self, strip_chords: bool = False) -> str:
        """The score as validated ABC. Raises WriteError / AbcError with a readable message."""
        if self.score is None:
            text = self.text.toPlainText()
            abc_dialect.parse(text)
            if strip_chords:
                return abc_dialect.strip_chords(text)
            return text
        return to_abc(self.score, strip_chords=strip_chords)

    def _toggle_text(self, on: bool) -> None:
        if on:
            if self.score is not None:
                try:
                    self.text.setPlainText(self.current_text())
                except WriteError as error:
                    self.status.setText(str(error))
            self.stack.setCurrentIndex(1)
            self.toolbar.setEnabled(False)
        else:
            if self.score is not None and not self._apply_text(quiet=True):
                self.advanced.blockSignals(True)
                self.advanced.setChecked(True)
                self.advanced.blockSignals(False)
                return
            self.stack.setCurrentIndex(0)
            self.toolbar.setEnabled(True)

    def _apply_text(self, quiet: bool = False) -> bool:
        text = self.text.toPlainText()
        if self.score is None:
            try:
                abc_dialect.parse(text)
            except abc_dialect.AbcError as error:
                self.status.setText(t("The text is not a valid score: {why}", why=str(error)))
                return False
            self.dirty = True
            self.status.setText(t("Score checked."))
            return True
        try:
            if text.strip() == to_abc(self.score).strip():
                return True
            new = Score.from_abc(text)
        except (abc_dialect.AbcError, UnsupportedScore, WriteError, ValueError) as error:
            self.status.setText(t("The text is not a valid score: {why}", why=str(error)))
            return False
        new.lyrics = self.score.lyrics
        self.history.checkpoint()
        self.history.score = new
        self.canvas.history = self.history
        self.canvas._fit_range()
        self._changed()
        if not quiet:
            self.status.setText(t("Score checked."))
        return True

    # --- files -----------------------------------------------------------
    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, t("Open a score"), str(Path.home()), t("ABC score") + " (*.abc)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
            new = Score.from_abc(text)
        except (OSError, UnicodeDecodeError, abc_dialect.AbcError, UnsupportedScore) as error:
            QMessageBox.warning(self, t("Open a score"), t("This file is not a score Ceonggo can use: {why}", why=error))
            return
        if self.history is None:
            QMessageBox.information(self, t("Open a score"), t("Close this window and open the file from the tab."))
            return
        new.lyrics = self.score.lyrics
        self.history.checkpoint()
        self.history.score = new
        self.original_key = new.key
        self.canvas._fit_range()
        self._after_history()

    def _save_lyrics(self) -> None:
        if self.score is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, t("Save lyrics"), str(Path.home() / "lyrics.txt"),
                                              t("Text") + " (*.txt)")
        if not path:
            return
        if not path.lower().endswith(".txt"):
            path += ".txt"
        try:
            Path(path).write_text(self.score.lyrics, encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, t("Save lyrics"), t("The lyrics could not be saved: {why}", why=error))
            return
        self.status.setText(t("Saved {path}", path=path))

    def _save_file(self) -> None:
        try:
            text = self.current_text()
        except (WriteError, abc_dialect.AbcError) as error:
            QMessageBox.warning(self, t("Save score"), str(error))
            return
        path, _ = QFileDialog.getSaveFileName(self, t("Save score"), str(Path.home() / "score.abc"),
                                              t("ABC score") + " (*.abc)")
        if not path:
            return
        if not path.lower().endswith(".abc"):
            path += ".abc"
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, t("Save score"),
                                t("The score could not be saved: {why}", why=error))
            return
        self.dirty = False
        self.status.setText(t("Saved {path}", path=path))

    # --- preview ---------------------------------------------------------
    def _toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self._stop()
            return
        if self.score is None:
            return
        digest = hashlib.sha1(repr((self.score.bpm, self.score.chords, self.score.voices)).encode()).hexdigest()[:16]
        if digest == self._preview_hash and self._preview_file:
            self._play_from_playhead()
            return
        if self._render and self._render.isRunning():
            return
        self.play_btn.setText(t("Preparing…"))
        self.play_btn.setEnabled(False)
        target = paths.cache_dir() / "tmp" / f"score-preview-{digest}.wav"
        self._render = PreviewRender(self.score.copy(), target, self)
        self._render.done.connect(lambda file, d=digest: self._rendered(file, d))
        self._render.failed.connect(self._render_failed)
        self._render.start()

    def _rendered(self, file: str, digest: str) -> None:
        self._preview_hash, self._preview_file = digest, file
        self.play_btn.setEnabled(True)
        self._play_from_playhead()

    def _render_failed(self, message: str) -> None:
        self.play_btn.setEnabled(True)
        self.play_btn.setText(t("▶  Play"))
        self.status.setText(t("The preview could not be made: {why}", why=message))

    def _play_from_playhead(self) -> None:
        start = self.canvas.playhead if self.canvas else F(0)
        self._play_file(self._preview_file, int(float(start) * 60 / self.score.bpm * 1000))

    def _play_file(self, file: str, start_ms: int, *, stop_ms: int | None = None, offset_ms: int = 0,
                   follow: bool = True, clock: Clock | None = None) -> None:
        """Play ``file`` from ``start_ms``; the play line follows at file time + ``offset_ms``.

        ``follow=False`` leaves the play line alone: a sung line's clip does not keep
        the score's clock (see _sung). ``clock`` maps a song's audio to score time
        instead of ``offset_ms`` (see _hear_in_song).

        The preview, the finished song and a sung line share this one player. A seek
        on media that is still loading is silently dropped - the first Play after a
        render used to start at the top whatever the play line said - so a newly set
        file is started from _media_status once it has loaded.
        """
        if self.pause_player:
            self.pause_player()
        self._stop_at, self._offset_ms, self._follow, self._play_clock = stop_ms, offset_ms, follow, clock
        self._play_from = self._time_at(self._score_ms(start_ms))
        url = QUrl.fromLocalFile(file)
        if self.player.source() == url and self.player.mediaStatus() not in (
                QMediaPlayer.LoadingMedia, QMediaPlayer.NoMedia, QMediaPlayer.InvalidMedia):
            self.player.setPosition(start_ms)
            self.player.play()
        else:
            # Changing files, Qt can still report the old one as loaded before the new
            # one starts loading; starting on that left the player "playing" at 0:00
            # in silence. So stop first, and only start after this file has loaded.
            self.player.stop()
            self._start_when_loaded, self._loading_seen = start_ms, False
            self.player.setSource(url)

    def _score_ms(self, audio_ms: float) -> float:
        """Where in the score ``audio_ms`` into what is playing falls."""
        if self._play_clock:
            return self._play_clock.score(audio_ms / 1000) * 1000
        return audio_ms + self._offset_ms

    def _time_at(self, ms: float) -> Fraction:
        """Score time at ``ms`` into the music."""
        if self.score is None:
            return F(0)
        return F(max(0.0, ms) / 1000 * self.score.bpm / 60).limit_denominator(64)

    def _media_status(self, status) -> None:
        if self._start_when_loaded is None:
            return
        if status == QMediaPlayer.LoadingMedia:
            self._loading_seen = True
        elif status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and self._loading_seen:
            start, self._start_when_loaded = self._start_when_loaded, None
            self.player.setPosition(start)
            self.player.play()
        elif status == QMediaPlayer.InvalidMedia:
            self._start_when_loaded = None
            self.status.setText(t("The sound could not be played: {why}", why=self.player.errorString()))

    def showEvent(self, event) -> None:
        """Give the piano roll the keyboard the first time the window appears.

        setFocus() in __init__ is dropped on the floor - the widget is not visible
        yet - and without focus the scroll area receives ←/→ and scrolls the view
        instead of moving the play line.
        """
        super().showEvent(event)
        if self.canvas is not None and not self._focused:
            self._focused = True
            self.canvas.setFocus()

    def keyPressEvent(self, event) -> None:
        """←/→/Home work the play line like a transport.

        Handled here rather than as QShortcuts so the ABC text view keeps its own
        arrow keys: a dialog only sees what the focused widget did not consume.
        This is the fallback; the canvas handles them itself when it has focus.
        """
        moves = {Qt.Key_Home: None, Qt.Key_Left: -1, Qt.Key_Right: 1}
        if event.key() in moves and not self.advanced.isChecked():
            self._move_playhead(moves[event.key()])
            event.accept()
            return
        super().keyPressEvent(event)

    def _move_playhead(self, bars: int | None) -> None:
        """Fallback for when focus is somewhere that ignores the arrows; the canvas
        handles them itself when it has focus, which is the usual case."""
        if self.canvas and self.score:
            self.canvas.move_playhead(bars)

    def _stop(self) -> None:
        if self.player.playbackState() != QMediaPlayer.StoppedState:
            self.player.pause()

    def _seek(self, time: Fraction) -> None:
        # Dragging the playhead moves where playback counts from, so the latency
        # clamp in _on_position follows it. Without this a seek backwards was
        # pulled straight back to wherever Play had originally started.
        self._play_from = time
        if self.player.playbackState() == QMediaPlayer.PlayingState and self.score:
            score_ms = float(time) * 60 / self.score.bpm * 1000
            audio_ms = self._play_clock.audio(score_ms / 1000) * 1000 if self._play_clock else score_ms - self._offset_ms
            self.player.setPosition(max(0, int(audio_ms)))

    def _on_position(self, ms: int) -> None:
        if not self.canvas or not self.score or self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        # What is being heard now was handed to the sound card latency_ms ago, and
        # never earlier than where Play started, so the bar does not jump backwards.
        if self._stop_at is not None and ms >= self._stop_at:
            self._stop()                         # a line heard from the song ends with the line
            return
        if not self._follow:
            return
        time = max(self._play_from, self._time_at(self._score_ms(ms - self.latency_ms)))
        if time >= self.score.length:
            self._stop()
            return
        self.canvas.playhead = time
        self.canvas.ensure_visible(time)
        self.canvas.update()

    def _on_play_state(self, state) -> None:
        self.play_btn.setText(t("❚❚  Pause") if state == QMediaPlayer.PlayingState else t("▶  Play"))

    # --- hearing a line ----------------------------------------------------
    def _line_actions(self, syl) -> list[tuple[str, object]]:
        """Ways to hear the clicked word, each naming the words it will play.

        Hearing the song plays the phrase around the word (up to the spaces), which
        is what a listener calls the sentence; 「搭上這尾班車 靠向你那肩膊」 is one
        lyric line but two phrases. Singing takes the whole line, so YuE2 has a
        little more to go on.
        """
        line, actions = syl.line, []
        words = self.score.lyrics.split("\n")[line].strip()
        if self.song_audio:
            phrase = chunk_text(self.canvas.words().alignment, syl.chunk)
            actions.append((t("▶  Hear 「{words}」 in the song", words=phrase),
                            lambda: self._hear_in_song(syl.chunk)))
        if self.audition:
            actions.append((t("▶  Sing 「{words}」 now (takes a few minutes)", words=words),
                            lambda: self._sing_line(line)))
        if line in self._tries:
            actions.append((t("▶  Play the last try of 「{words}」 again", words=words),
                            lambda: self._play_file(self._tries[line][0], 0, follow=False)))
        return actions

    def _line_notes(self, line: int | None = None, chunk: int | None = None) -> tuple[list[Note], int, int] | None:
        """The vocal notes, and the first and last index sung on a lyric line or one phrase of it."""
        span = line_span(self.canvas.words().alignment, line, chunk=chunk)
        if span is None:
            QMessageBox.information(self, t("Words"), t("No notes are sung on this line."))
            return None
        return self.canvas._vocal(), span[0], span[1]

    def _hear_in_song(self, chunk: int) -> None:
        """Play one phrase from the finished song, where the song really sings it.

        YuE2 sings a few seconds off the score's clock (in 喺度, 2.4 s early at the
        start and 0.15 s by the end), so the song's own clock is found first - once
        per song, see MainWindow.song_clock. The clip runs from a beat before the
        phrase (never back into the one before) to the next word.
        """
        if self._clock is None and self.song_clock and not self._clock_failed:
            if self._waiting_to_hear is None:
                self._waiting_to_hear = chunk
                self._audition_status(self._finding_text())
                self.song_clock(self._clock_found, self._clock_missing)
            else:
                self._waiting_to_hear = chunk
            return
        found = self._line_notes(chunk=chunk)
        if found is None:
            return
        notes, a, b = found
        beat = 60 / self._song_bpm
        start = float(notes[a].onset) - 1
        if a > 0:
            start = max(start, float(notes[a - 1].end))
        stop = float(notes[b].end) + 0.5
        if b + 1 < len(notes):
            stop = min(stop, float(notes[b + 1].onset))
        clock = self._clock or Clock()
        self._play_file(self.song_audio, max(0, int(clock.audio(start * beat) * 1000)),
                        stop_ms=int(clock.audio(stop * beat) * 1000) + 50, clock=clock)

    @staticmethod
    def _finding_text() -> str:
        return t("Finding where the song sings each line - about half a minute, once per song…")

    def _clock_found(self, clock: Clock) -> None:
        self._clock = clock
        chunk, self._waiting_to_hear = self._waiting_to_hear, None
        if self._closed:
            return
        if self._audition_note == self._finding_text():
            self._audition_status("")
        if chunk is not None:
            self._hear_in_song(chunk)

    def _clock_missing(self, message: str) -> None:
        self._clock_failed = True
        chunk, self._waiting_to_hear = self._waiting_to_hear, None
        if self._closed:
            return
        self._audition_status(t("Could not find where the song sings each line, so it plays by the score "
                                "and may be a few seconds off: {why}", why=message))
        if chunk is not None:
            self._hear_in_song(chunk)

    def _sing_line(self, line: int) -> None:
        """Have YuE2 sing just this line, with the words and notes as they are now.

        The score sent is the line's bars plus one either side, with every other
        line's vocal notes removed so nothing is sung without words.
        """
        if self._singing:
            QMessageBox.information(self, t("Words"), t("A line is already being sung - wait for it to finish."))
            return
        found = self._line_notes(line)
        if found is None:
            return
        notes, a, b = found
        score = self.score
        first = max(0, score.bar_of(notes[a].onset) - 1)
        last = min(score.bars - 1, score.bar_of(notes[b].end - F(1, 64)) + 1)
        keep = {notes[i].onset for i in range(a, b + 1)}
        clone = score.copy()
        clone.voices["Vocal"] = [n for n in clone.voices["Vocal"] if n.onset in keep]
        try:
            abc = to_abc(clone.excerpt(first, last))
        except (WriteError, ValueError) as error:
            QMessageBox.information(self, t("Words"), t("This line cannot be sung on its own: {why}", why=str(error)))
            return
        words = audition_lyrics(score.lyrics, line, score.section_at(score.bar_of(notes[a].onset)))
        text = words.split("\n", 1)[1]
        offset = int(float(score.bar_start(first)) * 60_000 / score.bpm)
        singing = t("Singing 「{line}」 - this takes a few minutes…", line=text)
        self._singing = True
        queued = self.audition(abc=abc, lyrics=words, style=self.style, seed=self.seed,
                               on_start=lambda: self._audition_status(singing),
                               on_done=lambda path: self._sung(line, text, path, offset),
                               on_fail=self._sing_failed)
        self._audition_status(t("Waiting for the song being made to finish, then singing 「{line}」…", line=text)
                              if queued else singing)

    def _sung(self, line: int, text: str, path: str, offset: int) -> None:
        """Play the clip whole, without the play line.

        Given only a few bars, YuE2 treats them as a theme: in a test the exact
        melody came a bar late and again 13 s later, with looser variations
        around it, in a 30 s clip. So the clip is not on the score's clock - but
        the line is in it, usually more than once.
        """
        self._singing = False
        if self._closed:
            return
        self._tries[line] = (path, offset)
        self._audition_status(t("Sung 「{line}」 - the singer may sing it more than once. "
                                "Click a word in the line to play it again.", line=text))
        self._play_file(path, 0, follow=False)

    def _sing_failed(self, message: str) -> None:
        self._singing = False
        if not self._closed:
            self._audition_status(t("The line could not be sung: {why}", why=message))

    def _audition_status(self, text: str) -> None:
        self._audition_note = text
        self._refresh()

    def done(self, result: int) -> None:
        # A line still being sung finishes in the background; its result is ignored
        # rather than the job cancelled, which would stop the user's songs too.
        self._closed = True
        super().done(result)

    # --- leaving ---------------------------------------------------------
    def _use(self, action: str) -> None:
        if self.advanced.isChecked() and not self._apply_text(quiet=True):
            return
        try:
            text = self.current_text()          # chords kept; Cover removes them when it sings
        except (WriteError, abc_dialect.AbcError) as error:
            QMessageBox.warning(self, t("Score"), t("The score cannot be used yet: {why}", why=str(error)))
            return
        self.dirty = False
        self._stop()
        self.chosen.emit(action, text)
        self.accept()

    def reject(self) -> None:
        if self.dirty and QMessageBox.question(
                self, t("Discard changes?"),
                t("You changed this score. Close without using or saving it?")) != QMessageBox.Yes:
            return
        self._stop()
        super().reject()
