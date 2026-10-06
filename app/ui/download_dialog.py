"""Tools → Download Audio from URL…: the audio of one link, saved as FLAC or MP3.

The window checks yt-dlp is installed; this dialog asks for the link, the folder
and the format, runs the job on the shared Engine (so it waits its turn behind a
song) and shows its progress. It is not modal, so the player and the tabs stay
usable. Closing it, like Stop, ends this job only - a song queued in a tab
carries on. After a download the form is free for the next link.

Update downloader installs the newest yt-dlp, for when a site changes and
downloads that used to work start failing. It runs on the Engine too, so it can
never change yt-dlp under a download.
"""
from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from app import paths
from app.i18n import t
from app.jobs import requests as rq
from app.jobs.engine import Job
from app.ui.progress_card import ProgressCard
from app.ui.widgets import open_folder

#: The progress card's stages: a download skips Update downloader's, and an update the download's.
DOWNLOAD_SKIPS = {"update"}
UPDATE_SKIPS = {"connect", "download", "convert"}


def downloads_folder() -> Path:
    """Windows' Downloads folder, wherever the user has moved it."""
    found = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation)
    return Path(found) if found and Path(found).is_dir() else Path.home()


def downloader_version() -> str:
    """The yt-dlp in the Cover maker's runtime, from its dist-info folder; "" when there is none."""
    for info in paths.sheetsage_packages().glob("yt_dlp-*.dist-info"):
        return info.name.removeprefix("yt_dlp-").removesuffix(".dist-info")
    return ""


class DownloadDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window_ = window
        self.cfg = window.cfg
        self.job: Job | None = None
        self.work_dir: Path | None = None
        self.saved: Path | None = None
        self.running = False
        self.setWindowTitle(t("Download audio"))
        self.resize(600, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(10)

        layout.addWidget(QLabel(t("Link")))
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://www.youtube.com/watch?v=…")
        layout.addWidget(self.url)

        layout.addWidget(QLabel(t("Save in")))
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(self.cfg.get("download_dir") or str(downloads_folder()))
        folder_row.addWidget(self.folder, 1)
        self.browse = QPushButton(t("Browse…"))
        self.browse.clicked.connect(self._browse)
        folder_row.addWidget(self.browse)
        layout.addLayout(folder_row)

        format_row = QHBoxLayout()
        format_row.addWidget(QLabel(t("Format")))
        self.format = QComboBox()
        self.format.addItem(t("FLAC - lossless"), "flac")
        self.format.addItem("MP3", "mp3")
        self.format.setCurrentIndex(max(0, self.format.findData(self.cfg.get("download_format", "flac"))))
        format_row.addWidget(self.format)
        format_row.addStretch(1)
        layout.addLayout(format_row)
        note = QLabel(t("YouTube and most other video and music sites work. A file already in the folder "
                        "is never replaced. Only download what you have the right to use."))
        note.setObjectName("Hint")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.progress = ProgressCard(rq.DOWNLOAD_STAGES, rq.DOWNLOAD_WEIGHT)
        self.progress.title.setWordWrap(True)  # "Saved - <a long video title>" must not widen the dialog
        self.progress.cancel_clicked.connect(lambda: self.window_.engine.cancel(self.job))
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        self.update_button = QPushButton(t("Update downloader"))
        self.update_button.setToolTip(t("Installs the newest yt-dlp from the internet. Try it when downloads "
                                        "that used to work start failing."))
        self.update_button.clicked.connect(self._update)
        buttons.addWidget(self.update_button)
        self.version = QLabel("")
        self.version.setObjectName("Hint")
        buttons.addWidget(self.version)
        buttons.addStretch(1)
        self.show_folder = QPushButton(t("Show folder"))
        self.show_folder.clicked.connect(lambda: open_folder(self.saved.parent))
        self.show_folder.setVisible(False)
        buttons.addWidget(self.show_folder)
        self.start = QPushButton(t("Download"))
        self.start.setObjectName("Primary")
        self.start.setDefault(True)
        self.start.clicked.connect(self._download)
        buttons.addWidget(self.start)
        self.close_button = QPushButton(t("Cancel"))
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self._show_version()

    # --- the form ----------------------------------------------------------
    def _browse(self) -> None:
        start = self.folder.text().strip() or str(downloads_folder())
        folder = QFileDialog.getExistingDirectory(self, t("Save downloads in"), start)
        if folder:
            self.folder.setText(str(Path(folder)))

    def _show_version(self) -> None:
        version = downloader_version()
        self.version.setText(f"yt-dlp {version}" if version else "")

    def _set_running(self, running: bool) -> None:
        self.running = running
        for widget in (self.url, self.folder, self.browse, self.format, self.start, self.update_button):
            widget.setEnabled(not running)

    # --- jobs --------------------------------------------------------------
    def _download(self) -> None:
        title = t("Download audio")
        url = self.url.text().strip()
        if not url.lower().startswith(("http://", "https://")) or any(c.isspace() for c in url):
            QMessageBox.information(self, title, t("Paste the link of one video or track, starting with https://."))
            return
        folder = Path(self.folder.text().strip() or downloads_folder())
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QMessageBox.warning(self, title, t("This folder cannot be used:\n{folder}\n\n{reason}",
                                               folder=str(folder), reason=str(error)))
            return
        output_format = self.format.currentData()
        self.cfg.set("download_dir", str(folder))
        self.cfg.set("download_format", output_format)
        self.work_dir = paths.cache_dir() / "downloads" / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        data = rq.download_job(url=url, save_dir=folder, work_dir=self.work_dir, output_format=output_format)
        self._run(Job("download", data, on_start=lambda: self.progress.start("", DOWNLOAD_SKIPS),
                      on_stage=self.progress.on_stage, on_done=self._downloaded,
                      on_fail=lambda event: self._failed(event, t("Could not download the audio"))),
                  DOWNLOAD_SKIPS)

    def _update(self) -> None:
        self.work_dir = None
        self._run(Job("download", rq.downloader_update_job(),
                      on_start=lambda: self.progress.start("", UPDATE_SKIPS),
                      on_stage=self.progress.on_stage, on_done=self._updated,
                      on_fail=lambda event: self._failed(event, t("Could not update the downloader"))),
                  UPDATE_SKIPS)

    def _run(self, job: Job, skip: set[str]) -> None:
        self.job = job
        self._set_running(True)
        self.show_folder.setVisible(False)
        self.progress.setVisible(True)
        self.progress.start(t("Waiting for the engine…") if self.window_.engine.busy else "", skip)
        self.window_.engine.submit(job)

    def _downloaded(self, event: dict) -> None:
        self.saved = Path(event.get("file") or "")
        self.progress.finish(t("Saved - {name}", name=self.saved.name))
        self.show_folder.setVisible(True)
        self.close_button.setText(t("Close"))
        self._ended()
        self.url.selectAll()                   # pasting the next link replaces this one
        self.url.setFocus()

    def _updated(self, event: dict) -> None:
        before, after = event.get("before") or "?", event.get("after") or "?"
        self.progress.finish(t("The downloader is up to date ({version})", version=after) if before == after
                             else t("Downloader updated: {old} → {new}", old=before, new=after))
        self._show_version()
        self._ended()

    def _failed(self, event: dict, headline: str) -> None:
        if self.work_dir is not None:           # the worker removes it too, unless it was killed
            shutil.rmtree(self.work_dir, ignore_errors=True)
        if event.get("kind") == "cancelled":
            self.progress.fail(t("Stopped"))
        else:
            self.progress.fail(headline)
            self.window_.show_error(t("Download audio"), rq.friendly_error(event), event)
        self._ended()

    def _ended(self) -> None:
        self._set_running(False)
        if not self.isVisible():             # closed while it ran: nothing left to show
            self.deleteLater()

    def reject(self) -> None:
        """Cancel, Esc and the title bar's ✕ all come here. Hidden first: a queued
        job's on_fail comes straight back from cancel, a running one's later, and
        either way it tidies up."""
        super().reject()
        if self.running:
            self.window_.engine.cancel(self.job)
        else:
            self.deleteLater()
