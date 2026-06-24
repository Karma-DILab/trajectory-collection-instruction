r"""Isolation diagnostic.

Launches Chromium with the tracker's EXACT launch config (same flags, viewport,
device-scale, --app mode) but with NO inject.js, NO bindings, NO screenshot loop,
NO window guard, NO toolbar — i.e. just the browser, nothing of ours running
inside it.

Then YOU manually test the problem click (Amazon "Buy Now", a hover dropdown
menu item, etc.):

  * If the click WORKS here (navigates) -> our injected machinery (inject.js /
    CDP loops / bindings) is what breaks it, not the browser setup.
  * If the click STILL fails here -> it's the launch config (flags / viewport /
    --app / device-scale) or the site itself, not our injected JS.

Run:
    .\.venv\Scripts\python.exe test_plain.py

Uses a SEPARATE profile dir so it won't clash with the running tool's profile
lock (so it starts logged out — Amazon "Buy Now" should go to the sign-in page,
which still counts as "navigated").
"""

import asyncio
import os

from playwright.async_api import async_playwright

from tracker import VIEWPORT_W, VIEWPORT_H, NAV_BAR_H, _detect_scale_factor

START_URL = "https://www.amazon.com/"


async def main():
    here = os.path.dirname(os.path.abspath(__file__))
    user_data_dir = os.path.join(here, "browser_profile_plaintest")
    os.makedirs(user_data_dir, exist_ok=True)

    scale = _detect_scale_factor(reserve_w=0, reserve_h=NAV_BAR_H)
    win_w = int(VIEWPORT_W * scale)
    win_h = int(VIEWPORT_H * scale)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            channel="chrome",
            locale="en-US",
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            device_scale_factor=scale,
            args=[
                "--no-default-browser-check",
                "--no-first-run",
                "--lang=en-US",
                "--disable-blink-features=AutomationControlled",
                f"--app={START_URL}",
                f"--window-size={win_w},{win_h}",
                f"--window-position=0,{NAV_BAR_H}",
                f"--force-device-scale-factor={scale}",
                "--disable-features=PaintHolding",
                "--disable-infobars",
                "--disable-extensions",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-backgrounding-occluded-windows",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
            ],
            ignore_default_args=["--enable-automation"],
        )

        print("=" * 64)
        print(" PLAIN browser — NO inject.js, NO recording, NO CDP loops.")
        print(" Same flags/viewport/--app as the tracker.")
        print(" -> Test the click that failed (Buy Now / hover menu) HERE.")
        print(" -> Close the window when done.")
        print("=" * 64)

        closed = asyncio.Event()
        context.on("close", lambda *a: closed.set())
        page = context.pages[0] if context.pages else await context.new_page()
        page.on("close", lambda *a: closed.set())
        await closed.wait()
        try:
            await context.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
