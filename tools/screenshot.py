#!/usr/bin/env python3
"""Screenshot a running Kestrel COP with headless Chromium (Playwright) and report browser errors.

    pip install playwright && playwright install chromium
    python tools/screenshot.py --url http://127.0.0.1:8000 --out docs/screenshot.png --wait 12

Used to produce the README image and as a smoke test that the page boots without console errors.
"""

from __future__ import annotations

import argparse
import json
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/")
    ap.add_argument("--out", default="docs/screenshot.png")
    ap.add_argument("--wait", type=float, default=10.0, help="seconds to let tracks and the map settle")
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--click", default=None, help="CSS selector to click before the screenshot (e.g. a suggestion chip)")
    ap.add_argument("--ask", default=None, help="type this question into the watch-officer box and submit it")
    ap.add_argument("--select", default=None, help="UID of a track to select (opens the track card)")
    ap.add_argument("--js", default=None, help="JavaScript to run in the page before the screenshot")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    console: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height}, device_scale_factor=1)
        page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(args.url, wait_until="domcontentloaded")
        time.sleep(args.wait)
        if args.select:
            page.evaluate(f"window.kestrel && window.kestrel.select({json.dumps(args.select)})")
            time.sleep(1.0)
        if args.click:
            page.click(args.click)
            time.sleep(2.5)
        if args.ask:
            page.fill("#ask-input", args.ask)
            page.press("#ask-input", "Enter")
            time.sleep(4.0)
        if args.js:
            page.evaluate(args.js)
            time.sleep(1.5)
        page.screenshot(path=args.out, full_page=False)
        browser.close()

    print(f"saved {args.out}")
    noise = ("net::ERR", "Failed to load resource", "arcgisonline", "fonts.googleapis")
    for line in console:
        if not any(n in line for n in noise):
            print("console:", line[:300])
    for err in errors:
        print("PAGE ERROR:", err[:500])
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
