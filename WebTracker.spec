# PyInstaller spec for WebTracker (single-file Windows exe)
# Bundles: server code, manual.html, inject.js, Playwright driver, Chromium browser.
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

# Locate the installed Chromium so we can ship it inside the exe.
LOCAL_APPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
CHROMIUM_SRC = os.path.join(LOCAL_APPDATA, "ms-playwright", "chromium-1223")
if not os.path.isdir(CHROMIUM_SRC):
    raise SystemExit(
        f"Chromium not found at {CHROMIUM_SRC}.\n"
        "Run `.venv\\Scripts\\playwright install chromium` first."
    )

extra_datas = [
    # ("manual_mockup.html", "."),   # served by server.index()  (the new 3-page manual)
    ("manual.html", "."),          # kept for fallback/reference
    ("inject.js",   "."),
    ("examples",    "examples"),
    (CHROMIUM_SRC,  "ms-playwright/chromium-1223"),
]

a = Analysis(
    ["webtracker_launcher.py"],
    pathex=["."],
    binaries=pw_binaries + flask_binaries + wz_binaries,
    datas=pw_datas + flask_datas + wz_datas + extra_datas,
    hiddenimports=pw_hidden + flask_hidden + wz_hidden + [
        "server", "tracker", "actions", "recorder", "utils", "tasks_data",
        "page_panel",
        "refinement", "refine_utils",
        "PIL", "PIL.Image",
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
