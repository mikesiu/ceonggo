"""Tools → Extract Vocals from Audio…: one recording split into its vocal and its instrumental.

The window picks the files and checks the separator is installed; this dialog
runs the job on the shared Engine (so it waits its turn behind a song) and shows
its progress. It is not modal, so the player and the tabs stay usable. Closing
it, like Stop, ends this job only - a song queued in a tab carries on.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from app.i18n import t
from app.jobs import requests as rq
from app.jobs.engine import Job
from app.ui.progress_card import ProgressCard
from app.ui.widgets import open_folder


class ExtractDialog(QDialog):
    def __init__(self, window, source: Path, vocals: Path, instrumental: Path):
        super().__init__(window)
        self.window_ = window
        self.source = source
        self.setWindowTitle(t("Extract vocals"))
        self.resize(560, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(10)

        name = QLabel(source.name)
        name.setStyleSheet("font-size:15px;font-weight:700;")
        layout.addWidget(name)
        targets = QLabel("\n".join(f"→ {path}" for path in (vocals, instrumental)))
        targets.setObjectName("Mono")
        targets.setWordWrap(True)
        layout.addWidget(targets)

        self.progress = ProgressCard(rq.EXTRACT_STAGES, rq.EXTRACT_WEIGHT)
        self.progress.cancel_clicked.connect(lambda: self.window_.engine.cancel(self.job))
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        show = QPushButton(t("Show folder"))
        show.clicked.connect(lambda: open_folder(vocals.parent))
        buttons.addWidget(show)
        close = QPushButton(t("Close"))
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.running = True
        self.job = Job("extract", rq.extract_job(audio=source, vocals=vocals, instrumental=instrumental),
                       on_start=self.progress.start, on_stage=self.progress.on_stage,
                       on_done=self._done, on_fail=self._failed)
        self.progress.start(t("Waiting for the engine…") if window.engine.busy else "")
        window.engine.submit(self.job)

    def _done(self, _event: dict) -> None:
        self.progress.finish(t("Done - {title}", title=self.source.stem))
        self._ended()

    def _failed(self, event: dict) -> None:
        if event.get("kind") == "cancelled":
            self.progress.fail(t("Stopped"))
        else:
            self.progress.fail(t("Could not extract the vocals"))
            self.window_.show_error(t("Extract vocals"), rq.friendly_error(event), event)
        self._ended()

    def _ended(self) -> None:
        self.running = False
        if not self.isVisible():             # closed while it ran: nothing left to show
            self.deleteLater()

    def reject(self) -> None:
        """Close, Esc and the title bar's ✕ all come here. Hidden first: a queued
        job's on_fail comes straight back from cancel, a running one's later, and
        either way it tidies up."""
        super().reject()
        if self.running:
            self.window_.engine.cancel(self.job)
        else:
            self.deleteLater()
