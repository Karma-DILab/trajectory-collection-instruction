"""Entry point for the PyInstaller-built single exe.

Splits two paths:
  WEBTRACKER_ASSETS_DIR -> read-only: manual.html, inject.js (bundled inside exe)
  WEBTRACKER_STATE_DIR  -> writable : users/, browser_profile/ (next to the exe)
Also points Playwright at the bundled Chromium so no `playwright install` is
needed on the target machine.
"""

import os
import sys
import threading


def _force_utf8_console():
    # Korean Windows console defaults to cp949 which mangles non-ASCII output
    # (em-dash, user-folder paths with Korean characters, etc.). Force UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _bootstrap():
    if getattr(sys, "frozen", False):
        bundle = sys._MEIPASS  # PyInstaller extract dir (read-only)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(bundle, "ms-playwright")
        os.environ["WEBTRACKER_ASSETS_DIR"]    = bundle
        os.environ["WEBTRACKER_STATE_DIR"]     = os.path.dirname(os.path.abspath(sys.executable))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        os.environ.setdefault("WEBTRACKER_ASSETS_DIR", here)
        os.environ.setdefault("WEBTRACKER_STATE_DIR",  here)


_force_utf8_console()
_bootstrap()

from server import app, USERS_ROOT, PORT, _open_browser  # noqa: E402


def main():
    os.makedirs(USERS_ROOT, exist_ok=True)
    print(f"Web Tracker - serving on http://localhost:{PORT}/")
    print(f"Data folder: {os.environ['WEBTRACKER_STATE_DIR']}")
    print("Ctrl+C to quit.")
    threading.Timer(1.0, _open_browser).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
