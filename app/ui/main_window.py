"""The Ceonggo window: Create, Cover and Library tabs above a player bar."""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QPushButton, QStackedWidget, QTabBar,
    QVBoxLayout, QWidget,
)

from app import gpu, paths
from app.config import Config
from app.i18n import t
from app.install_state import InstallState
from app.jobs import requests as rq
from app.jobs.engine import Engine, Job
from app.score import abc_dialect
from app.score.model import Score
from app.score.timing import Clock, fit_clock
from app.songs import Library, Song, copy_records
from app.ui import theme
from app.ui.cover_tab import CoverTab
from app.ui.create_tab import CreateTab
from app.ui.extract_dialog import ExtractDialog
from app.ui.library_tab import LibraryTab
from app.ui.player_bar import PlayerBar
from app.ui.settings_dialog import SettingsDialog
from app.ui.song_card import SongCard


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, install: InstallState):
        super().__init__()
        self.cfg = cfg
        self.install = install
        self.library = Library(cfg.songs_dir())
        # The library and the transcription cache hold every song and score; a job
        # may only ever empty a folder inside them, never one of them.
        self.engine = Engine(self, protect=[self.library.root, paths.cache_dir() / "scores",
                                            paths.cache_dir() / "auditions", paths.root()])
        self.gpu = gpu.detect()
        self.setWindowTitle("Ceonggo")
        self.resize(1480, 920)
        self.setMinimumSize(1180, 760)
        self._build()
        self.engine.busy_changed.connect(self._busy)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._run_current)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self._run_current)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self._settings)
        QShortcut(QKeySequence(Qt.Key_Space | Qt.ControlModifier), self, activated=self.player.toggle)

    def _build(self) -> None:
        self.player = PlayerBar()       # first: the tabs' song cards connect to it
        self.player.set_output_device(self.cfg.get("audio_device", ""))
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(20, 6, 16, 0)
        logo = QLabel(f"<span style='font-size:21px;font-weight:900;color:{theme.ACCENT}'>●</span>"
                      f"<span style='font-size:21px;font-weight:900'>&nbsp;Ceonggo</span>")
        header.addWidget(logo)
        header.addSpacing(18)

        self.create_tab = CreateTab(self)
        self.cover_tab = CoverTab(self)
        self.library_tab = LibraryTab(self)
        self.tabs = QStackedWidget()
        self.tab_bar = QTabBar()
        self.tab_bar.setExpanding(False)
        for widget, label in ((self.create_tab, t("Create")), (self.cover_tab, t("Cover")),
                              (self.library_tab, t("Library"))):
            self.tabs.addWidget(widget)
            self.tab_bar.addTab(label)
        self.tab_bar.currentChanged.connect(self._switch)
        header.addWidget(self.tab_bar, 0, Qt.AlignBottom)

        header.addStretch(1)
        pill_text = f"{self.gpu.name} · {round(self.gpu.vram_gib)} GB" if self.gpu else t("No NVIDIA GPU")
        self.pill = QLabel(pill_text)
        self.pill.setObjectName("EnginePill")
        header.addWidget(self.pill, 0, Qt.AlignVCenter)
        self.status = QLabel(t("Ready"))
        self.status.setObjectName("EnginePill")
        self.status.setMinimumWidth(96)
        self.status.setAlignment(Qt.AlignCenter)
        header.addWidget(self.status, 0, Qt.AlignVCenter)
        tools = QPushButton(t("Tools"))
        tools_menu = QMenu(tools)
        tools_menu.addAction(t("Extract Vocals from Audio…"), self.extract_vocals)
        tools.setMenu(tools_menu)
        header.addWidget(tools, 0, Qt.AlignVCenter)
        settings = QPushButton(t("Settings"))
        settings.clicked.connect(self._settings)
        header.addWidget(settings, 0, Qt.AlignVCenter)

        header_widget = QWidget()
        header_widget.setLayout(header)
        outer.addWidget(header_widget)
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{theme.LINE};")
        outer.addWidget(line)
        outer.addWidget(self.tabs, 1)

        outer.addWidget(self.player)
        self.setCentralWidget(page)

    # --- shared services for the tabs --------------------------------------
    def make_card(self, song: Song, compact: bool = False) -> SongCard:
        card = SongCard(song, compact=compact)
        card.play.connect(self.player.load)
        card.open_score.connect(self.show_score)
        card.use_score.connect(self._use_song_score)
        card.reuse.connect(self._reuse)
        card.replace_vocal.connect(self.replace_vocal)
        card.delete.connect(self._delete)
        return card

    def edit_score(self, text: str, *, title: str, origin: str, actions=("create", "cover"),
                   primary: str = "create", on_sing=None, lyrics: str = "", style: str = "",
                   on_lyrics=None, song_audio: str = "", seed: int | None = None, audition=None,
                   song_clock=None) -> None:
        """Open the score editor; whatever the user chooses is routed from here.

        ``lyrics`` and ``style`` give a Cantonese song its Words lane. Words edited
        there travel with the score: ``on_sing(abc, lyrics)`` and the target tab
        receive them, or None when they were left alone. ``on_lyrics(lyrics)`` is
        the opening tab's hook: when the editor is left through Sing it now or a
        Use button with edited words, it gets them first, so its lyrics box shows
        what is about to be sung. Closing the editor changes nothing.

        ``audition`` (normally ``self.audition``) lets the editor have one line sung
        with ``seed``; ``song_audio`` is the finished song, to hear a line as it was,
        and ``song_clock(on_done, on_fail)`` finds where that song really sings it.
        """
        from app.ui.score_editor import ScoreEditor

        override = self.cfg.get("playback_latency_ms")
        dialog = ScoreEditor(text, title=title, actions=actions, primary=primary,
                             pause_player=self.player.player.pause,
                             audio_device=self.cfg.get("audio_device", ""),
                             latency_ms=self.player.latency_ms if override is None else int(override),
                             lyrics=lyrics, style=style, song_audio=song_audio, seed=seed,
                             audition=audition, song_clock=song_clock,
                             openutau_exe=self.cfg.get("openutau_exe", ""), parent=self)

        def chosen(action: str, abc: str) -> None:
            words = dialog.edited_lyrics()
            if words is not None and on_lyrics:
                on_lyrics(words)
            if action == "sing" and on_sing:
                on_sing(abc, words)
            else:
                self.use_score(abc, action, origin, lyrics=words)

        dialog.chosen.connect(chosen)
        dialog.exec()

    def audition(self, *, abc: str, lyrics: str, style: str, seed: int | None,
                 on_start, on_done, on_fail) -> bool:
        """Have YuE2 sing a short score (one line from the editor); True if it waits behind another job.

        The clip goes to the cache, never the library: it is a try, not a song.
        ``on_done`` gets the audio file, ``on_fail`` a message to show.
        """
        folder = paths.cache_dir() / "auditions" / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.perf_counter_ns() % 10**6}"
        data = rq.song_job(self.cfg, style=style, lyrics=lyrics, cot="full", seed=seed or rq.new_seed(),
                           output_dir=folder, abc=abc)
        queued = self.engine.busy

        def failed(event: dict) -> None:
            on_fail(t("Stopped") if event.get("kind") == "cancelled" else rq.friendly_error(event))

        self.engine.submit(Job("yue2", data, on_start=on_start,
                               on_done=lambda event: on_done(event.get("audio") or str(folder / "audio.flac")),
                               on_fail=failed))
        return queued

    #: Beside each song: where its audio really sings the score (see app/score/timing.py).
    CLOCK_FILE = "clock.json"

    def song_clock(self, song: Song, on_done, on_fail) -> bool:
        """Give ``on_done`` the song's clock, score seconds -> audio seconds.

        YuE2 sings a few seconds off the score's clock. The first time, the song is
        transcribed (SheetSage2, about half a minute) and its sung notes lined up
        with the score; the clock is kept beside the song, so later it is instant.
        Returns True when it was known already (``on_done`` has then been called).
        """
        saved = song.folder / self.CLOCK_FILE
        try:
            on_done(Clock.from_dict(json.loads(saved.read_text(encoding="utf-8"))))
            return True
        except (OSError, ValueError, KeyError, TypeError):
            pass
        folder = paths.cache_dir() / "scores" / f"clock-{time.strftime('%Y%m%d-%H%M%S')}"

        def done(event: dict) -> None:
            try:
                playback = json.loads((Path(event.get("dir") or folder) / "playback.json").read_text(encoding="utf-8"))
                score = Score.from_abc(song.score.read_text(encoding="utf-8"))
            except (OSError, ValueError, abc_dialect.AbcError) as error:
                on_fail(str(error))
                return
            heard = [(note["start"], note["pitch"]) for track in playback.get("tracks", [])
                     if track.get("name") == "Vocal" for note in track.get("notes", [])]
            written = [(float(n.onset) * 60 / score.bpm, n.pitch) for n in score.voices["Vocal"]]
            clock = fit_clock(written, heard)
            if clock is None:
                on_fail(t("The singing could not be matched to the score."))
                return
            try:
                saved.write_text(json.dumps(clock.to_dict()), encoding="utf-8")
            except OSError:
                pass                                   # still usable now; found again next time
            on_done(clock)

        self.engine.submit(Job("sheetsage", rq.transcribe_job(audio=song.audio, output_dir=folder), on_done=done,
                               on_fail=lambda event: on_fail(rq.friendly_error(event))))
        return False

    def replace_vocal(self, song: Song) -> None:
        """Keep a song's band and put a vocal sung elsewhere (OpenUtau) over it, as a new song.

        The original is never touched. The vocal is expected on the song's clock -
        the editor's OpenUtau export puts it there - so the song's clock shift
        places it, found first if this song has none yet.
        """
        title = t("Replace vocal")
        if not song.has_audio:
            return
        if not self._separator_ready():
            QMessageBox.information(self, title, t("Replace vocal needs the Cover maker with its vocal separator. "
                                                   "Open Settings → Repair / add components… to add it."))
            return
        vocal, _ = QFileDialog.getOpenFileName(self, t("Import tuned vocal"), str(Path.home()),
                                               t("Audio") + " (*.wav *.flac *.mp3)")
        if not vocal:
            return
        progress = self.library_tab.progress
        self.show_tab(self.library_tab)
        known = (song.folder / self.CLOCK_FILE).is_file()
        progress.start(t("Waiting for the engine…") if known
                       else t("Finding where the song sings each line - about half a minute, once per song…"))

        def separate(clock: Clock | None, note: str = "") -> None:
            folder = self.library.new_folder(song.title, "-new-vocal")
            data = rq.remix_job(song_audio=song.audio, vocal=vocal, output_dir=folder,
                                vocal_offset=clock.shift if clock else 0.0)
            self.engine.submit(Job(
                "remix", data,
                on_start=lambda: progress.start(t("Replacing the vocal of “{title}”", title=song.title)),
                on_stage=progress.on_stage,
                on_done=lambda event: self._vocal_replaced(song, folder, vocal, clock, note),
                on_fail=lambda event: self._vocal_failed(folder, event)))

        def no_clock(message: str) -> None:
            if not progress.cancel.isEnabled():        # Stop was pressed while the clock was being found
                progress.fail(t("Stopped"))
                return
            separate(None, t("The song's clock could not be found ({why}), so the vocal starts with the song "
                             "and may be a few seconds off.", why=message))

        self.song_clock(song, separate, no_clock)

    def _vocal_replaced(self, original: Song, folder: Path, vocal: str, clock: Clock | None, note: str) -> None:
        song = Song(folder, title=f"{original.title} · {t('new vocal')}", kind=original.kind, style=original.style,
                    lyrics=original.lyrics, seed=original.seed, cot=original.cot,
                    source_audio=original.source_audio, parent=original.folder.name)
        song.extra.update({key: value for key, value in original.extra.items() if key in ("style_builder", "source_path")})
        song.extra["new_vocal"] = Path(vocal).name
        song.created = datetime.now().isoformat(timespec="seconds")
        # The band, and so the clock, is the original's: Score and ↻ keep working.
        copy_records(original, song)
        song.save()
        song = Song.load(folder) or song
        self.library_tab.progress.finish(t("Done - {title}", title=song.title))
        if note:
            self.show_error(t("Replace vocal"), note, {})
        self.library_changed()
        self.player.load(song)

    def _vocal_failed(self, folder: Path, event: dict) -> None:
        self.discard_folder(folder)
        if event.get("kind") == "cancelled":
            self.library_tab.progress.fail(t("Stopped"))
            return
        self.library_tab.progress.fail(t("Could not replace the vocal"))
        self.show_error(t("Replace vocal"), rq.friendly_error(event), event)

    def _separator_ready(self) -> bool:
        """Replace vocal and Extract vocals both need the Cover maker's separator and FFmpeg."""
        return (self.install.has("cover") and paths.separator_model().is_file()
                and (paths.ffmpeg_bin() / "ffmpeg.exe").is_file())

    def extract_vocals(self) -> None:
        """Tools → Extract Vocals from Audio…: any recording split into a vocal and an instrumental file."""
        title = t("Extract vocals")
        if not self._separator_ready():
            QMessageBox.information(self, title, t("Extract vocals needs the Cover maker with its vocal separator. "
                                                   "Open Settings → Repair / add components… to add it."))
            return
        source, _ = QFileDialog.getOpenFileName(self, t("Choose a recording"), str(Path.home()),
                                                t("Audio") + " (*.mp3 *.wav *.flac)")
        if not source:
            return
        source = Path(source)
        folder = QFileDialog.getExistingDirectory(self, t("Save the vocals and instrumental in"), str(source.parent))
        if not folder:
            return
        vocals = Path(folder) / f"{source.stem} (Vocals).flac"
        instrumental = Path(folder) / f"{source.stem} (Instrumental).flac"
        existing = [path.name for path in (vocals, instrumental) if path.exists()]
        if existing and QMessageBox.question(self, title, t("Replace these files?\n{files}",
                                                            files="\n".join(existing))) != QMessageBox.Yes:
            return
        ExtractDialog(self, source, vocals, instrumental).show()

    def use_score(self, text: str, target: str, origin: str, lyrics: str | None = None) -> None:
        """Hand a score (and any lyrics edited with it) to the Create or Cover tab, and show that tab."""
        tab = self.cover_tab if target == "cover" else self.create_tab
        tab.set_score(text, origin)
        if lyrics is not None:
            tab.lyrics.replace_all(lyrics)
        self.show_tab(tab)

    def show_score(self, song: Song) -> None:
        try:
            abc = song.score.read_text(encoding="utf-8")
        except OSError:
            return
        self.edit_score(abc, title=t("Score - {title}", title=song.title), origin=song.title,
                        lyrics=song.lyrics, style=song.style, seed=song.seed, audition=self.audition,
                        song_audio=str(song.audio) if song.audio.is_file() else "",
                        song_clock=lambda done, fail: self.song_clock(song, done, fail))

    def _use_song_score(self, song: Song, target: str) -> None:
        try:
            abc = song.score.read_text(encoding="utf-8")
        except OSError:
            return
        self.use_score(abc, target, song.title)

    def _switch(self, index: int) -> None:
        self.tabs.setCurrentIndex(index)
        if self.tabs.currentWidget() is self.library_tab:
            self.library_tab.refresh()

    def show_tab(self, widget) -> None:
        self.tab_bar.setCurrentIndex(self.tabs.indexOf(widget))

    def _reuse(self, song: Song) -> None:
        """Fill the tab a song came from with everything used to make it."""
        settings = song.settings()
        if song.kind == "cover":
            self.cover_tab.load_settings(settings)
            self.show_tab(self.cover_tab)
        else:
            self.create_tab.load_settings(settings)
            self.show_tab(self.create_tab)

    def _delete(self, song: Song) -> None:
        if QMessageBox.question(self, t("Move to trash"),
                                t("Move “{title}” to the trash folder?\nIt stays in songs/_trash until you "
                                  "delete it there.", title=song.title)) != QMessageBox.Yes:
            return
        self.player.release(song)
        try:
            self.library.trash(song)
        except OSError as exc:
            QMessageBox.warning(self, t("Move to trash"), str(exc))
        self.library_changed()

    def show_error(self, title: str, message: str, event: dict) -> None:
        """A plain-words message, with the engine's own text under Show Details.

        Shown on the next event-loop turn: callers are inside an engine callback,
        and a modal dialog there would hold the engine "busy" (and any queued
        takes) until the user closed it.
        """
        details = event.get("message") or ""
        if self.engine.last_log:
            details += f"\n\n{t('Log file')}: {self.engine.last_log}"

        def show() -> None:
            box = QMessageBox(QMessageBox.Warning, title, message, QMessageBox.Ok, self)
            if details.strip():
                box.setDetailedText(details.strip())
            box.exec()

        QTimer.singleShot(0, show)

    def library_changed(self) -> None:
        if self.tabs.currentWidget() is self.library_tab:
            self.library_tab.refresh()

    def discard_folder(self, folder) -> None:
        """Remove what a failed or cancelled run left behind."""
        if folder and folder.is_dir() and not (folder / "song.json").exists():
            shutil.rmtree(folder, ignore_errors=True)

    # --- window ----------------------------------------------------------
    def _busy(self, busy: bool) -> None:
        self.status.setText(t("Working…") if busy else t("Ready"))
        self.status.setStyleSheet(f"color:{theme.ACCENT};" if busy else "")
        self.create_tab.set_busy(busy)
        self.cover_tab.set_busy(busy)

    def _run_current(self) -> None:
        current = self.tabs.currentWidget()
        if current is self.create_tab:
            self.create_tab.create()
        elif current is self.cover_tab:
            self.cover_tab.create()

    def _settings(self) -> None:
        dialog = SettingsDialog(self.cfg, self, busy=lambda: self.engine.busy)
        accepted = dialog.exec()
        if accepted:
            self.player.set_output_device(self.cfg.get("audio_device", ""))
        if accepted and dialog.language_changed:
            QMessageBox.information(self, t("Settings"), t("The new language is used the next time Ceonggo starts."))

    def closeEvent(self, event) -> None:
        if self.engine.busy:
            if QMessageBox.question(self, t("Quit Ceonggo?"),
                                    t("A song is still being made. Stop it and quit?")) != QMessageBox.Yes:
                event.ignore()
                return
            self.engine.cancel_all()
            if self.engine.process:
                self.engine.process.proc.kill()
                self.engine.process.proc.waitForFinished(5000)
        self.create_tab.remember()
        self.cover_tab.remember()
        self.cfg.save()
        event.accept()
