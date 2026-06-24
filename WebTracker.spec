# PyInstaller spec for WebTracker (single-file Windows exe)
# Bundles: server code, manual.html, inject.js, Playwright driver.
# The browser itself is NOT bundled - tracker.py launches the system-installed
# Google Chrome via channel="chrome" (real Chrome fingerprints better against
# bot detection than the bundled Chromium). Chrome must be installed on the
# machine running the built exe.
# Build with: .venv\Scripts\pyinstaller.exe --clean --noconfirm WebTracker.spec
# Output: dist\WebTracker.exe

import os
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Pull in the entire playwright package (driver/node.exe + JS bundle).
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")

# Pull in flask and its dependencies.
flask_datas, flask_binaries, flask_hidden = collect_all("flask")
wz_datas, wz_binaries, wz_hidden = collect_all("werkzeug")

# Tcl/Tk runtime data. Building from a venv, PyInstaller's tkinter hook can miss
# these (they live in the BASE Python, not the venv) -> the exe crashes at start
# with: Tcl data directory "..._tcl_data" not found. Ship them explicitly under
# the _tcl_data / _tk_data names the tkinter runtime hook looks for.
# Layout differs by Python distribution: python.org installers put these at
# <base>/tcl/tcl8.6, while conda/Anaconda puts them at <base>/Library/lib/tcl8.6.
# Try both so the build works from either.
import sys
_TCL_CANDIDATES = [
    os.path.join(sys.base_prefix, "tcl"),
    os.path.join(sys.base_prefix, "Library", "lib"),
]
_TCL_DIR = _TK_DIR = None
for _base in _TCL_CANDIDATES:
    _tcl = os.path.join(_base, "tcl8.6")
    _tk = os.path.join(_base, "tk8.6")
    if os.path.isdir(_tcl) and os.path.isdir(_tk):
        _TCL_DIR, _TK_DIR = _tcl, _tk
        break
if not _TCL_DIR:
    raise SystemExit(
        "Tcl/Tk data not found under any of: " + ", ".join(_TCL_CANDIDATES))

extra_datas = [
    # ("manual_mockup.html", "."),   # served by server.index()  (the new 3-page manual)
    ("manual.html", "."),          # kept for fallback/reference
    ("inject.js",   "."),
    ("examples",    "examples"),
    (_TCL_DIR, "_tcl_data"),       # -> TCL_LIBRARY at runtime
    (_TK_DIR,  "_tk_data"),        # -> TK_LIBRARY  at runtime
]

# conda/Anaconda Python keeps several DLLs under Library/bin instead of next
# to python.exe, where PyInstaller's dependency walker looks -- so it neither
# bundles them nor errors out, it just silently omits them. The exe then
# works fine on THIS machine (Library/bin is still on PATH here) but breaks
# on any other PC: _ctypes/_ssl/pyexpat fail to import (DLL load failed), and
# -- the one that actually bit us -- tkinter silently fails to import, which
# is caught by ui_thread.py's try/except, so the nav toolbar and thought-card
# windows just never appear with no visible error. Fail the BUILD loudly
# instead of shipping a silently-broken exe again.
_CONDA_LIB_BIN = os.path.join(sys.base_prefix, "Library", "bin")
_conda_dlls = []
if os.path.isdir(_CONDA_LIB_BIN):
    _REQUIRED_CONDA_DLLS = (
        "ffi.dll", "libcrypto-3-x64.dll", "libssl-3-x64.dll", "libexpat.dll",
        "tcl86t.dll", "tk86t.dll", "zlib.dll",
    )
    _missing = []
    for _name in _REQUIRED_CONDA_DLLS:
        _path = os.path.join(_CONDA_LIB_BIN, _name)
        if os.path.isfile(_path):
            _conda_dlls.append((_path, "."))
        else:
            _missing.append(_name)
    if _missing:
        raise SystemExit(
            f"Required DLLs missing from {_CONDA_LIB_BIN}: {_missing}")

a = Analysis(
    ["webtracker_launcher.py"],
    pathex=["."],
    binaries=pw_binaries + flask_binaries + wz_binaries + _conda_dlls,
    datas=pw_datas + flask_datas + wz_datas + extra_datas,
    hiddenimports=pw_hidden + flask_hidden + wz_hidden + [
        "server", "tracker", "actions", "recorder", "utils", "tasks_data",
        "page_panel", "card_window", "nav_toolbar", "ui_thread",
        "refinement", "refine_utils",
        "PIL", "PIL.Image", "PIL.ImageTk",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "PyInstaller"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="WebTracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,             # UPX trips antivirus more than it saves space
    runtime_tmpdir=None,
    console=True,          # keep console so users see "serving on ..." + Ctrl+C
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
