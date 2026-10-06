# -*- mode: python ; coding: utf-8 -*-
"""Ceonggo and Ceonggo Setup, built into one portable folder.

    python tools/build_release.py        (or: pyinstaller --noconfirm Ceonggo.spec)

Two programs share one _internal folder, so PySide6 is shipped once. Neither
contains torch: the engines are downloaded by Setup into runtime/.

Shipped as plain files beside the code, because they are read (or run) from
disk rather than imported: the language catalogue, the icons, the download
catalogue and its lock files, and the worker scripts that the downloaded
Pythons execute.
"""
import sys

from PyInstaller.utils.hooks import collect_data_files

sys.path.insert(0, SPECPATH)
from build_common import EXCLUDES, strip_unused

DATAS = [
    ("lang", "lang"),
    ("assets/icons", "assets/icons"),
    ("setup/catalog.json", "setup"),
    ("setup/locks", "setup/locks"),
    ("workers", "workers"),
]


def analysis(script, extra_excludes=(), extra_datas=()):
    a = Analysis(
        [script],
        pathex=[SPECPATH],
        binaries=[],
        datas=DATAS + list(extra_datas),
        hiddenimports=[],
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=EXCLUDES + list(extra_excludes),
        noarchive=False,
        optimize=0,
    )
    a.binaries = strip_unused(a.binaries)
    return a


def program(a, name, icon):
    return EXE(
        PYZ(a.pure),
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=[icon],
    )


# ToJyutping reads its pronunciation data (trie.txt) from beside its own code.
app = analysis("Ceonggo.py", extra_datas=collect_data_files("ToJyutping"))
setup = analysis("CeonggoSetup.py", extra_excludes=["PySide6.QtMultimedia"])

app_exe = program(app, "Ceonggo", "assets/icons/Ceonggo.ico")
setup_exe = program(setup, "Ceonggo Setup", "assets/icons/CeonggoSetup.ico")

coll = COLLECT(
    app_exe, app.binaries, app.datas,
    setup_exe, setup.binaries, setup.datas,
    strip=False,
    upx=False,
    name="Ceonggo",
)
