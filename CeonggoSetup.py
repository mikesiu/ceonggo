"""Ceonggo Setup - downloads the music engine and models Ceonggo needs.

Start it with:   python CeonggoSetup.py   (or double-click "Ceonggo Setup.exe")

A separate program from Ceonggo, as in EasyAI. It only writes inside its own
folder: runtime/, models/, cache/, setup_state.json and language.json.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _make_console_utf8_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _report_fatal(error: BaseException) -> None:
    """Show a startup failure - a windowed build has no console to print to."""
    import traceback

    details = "".join(traceback.format_exception(error))
    try:
        print(details, file=sys.stderr)
    except Exception:
        pass
    message = (
        "Ceonggo Setup could not start.\n\n"
        f"{type(error).__name__}: {error}\n\n"
        "If this keeps happening, run CeonggoSetup.bat from a Command Prompt to "
        "see the full message."
    )
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])
        box = QMessageBox(QMessageBox.Critical, "Ceonggo Setup", message)
        box.setDetailedText(details)
        box.exec()
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "Ceonggo Setup", 0x10)
        except Exception:
            pass


def _smoke_shot(app, window) -> None:
    """CEONGGO_SMOKE_SHOT=<png>: save a screenshot of the window, then quit.

    Lets an automated check confirm a windowed build really draws its window.
    """
    import os

    target = os.environ.get("CEONGGO_SMOKE_SHOT")
    if not target:
        return
    from PySide6.QtCore import QTimer

    QTimer.singleShot(3000, lambda: (window.grab().save(target), app.quit()))


def main() -> int:
    _make_console_utf8_safe()

    from PySide6.QtWidgets import QApplication, QMessageBox

    from app import i18n
    from app.i18n import t
    from app.ui import theme
    from setup.catalog import CATALOG_PATH, Catalog
    from setup.ui import SetupWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Ceonggo Setup")
    app.setOrganizationName("Ceonggo")
    i18n.start()
    theme.apply(app, icon_name="CeonggoSetup")

    if not CATALOG_PATH.is_file():
        QMessageBox.critical(
            None, t("Ceonggo Setup"),
            t("The download list is missing:\n{path}\n\nRe-download the Ceonggo folder.",
              path=CATALOG_PATH))
        return 1

    window = SetupWindow(Catalog())
    window.show()
    _smoke_shot(app, window)
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as fatal:      # noqa: BLE001 - last line of defence
        _report_fatal(fatal)
        raise SystemExit(1)
