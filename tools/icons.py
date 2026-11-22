"""Render the app's PNG icons from app/static/icon.svg (dev tool; needs requirements-dev.txt).

    python tools/icons.py

The PNGs (home-screen / install icons) fill the whole square with no rounded corners or border,
because phones cut their own shape out of them. Keep the artwork inside the middle 80% so
Android's round "maskable" crop never clips it.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

svg = (STATIC / "icon.svg").read_text(encoding="utf-8")
# full-bleed square: drop the rounded corners and the thin inner border
svg = svg.replace('rx="14" fill="url(#w)"', 'fill="url(#w)"')
svg = svg.replace('<rect x="2.5" y="2.5" width="59" height="59" rx="12" fill="none" stroke="#8a6410" stroke-width="1.5"/>', "")
# shrink the envelope a little toward the centre so the maskable crop has room
svg = svg.replace('<rect x="12"', '<g transform="translate(32 33) scale(0.8) translate(-32 -33)"><rect x="12"')
svg = svg.replace("</svg>", "</g></svg>")
assert svg.count("<g ") == 1 and 'rx="14"' not in svg

with sync_playwright() as p:
    # uses an installed Edge or Chrome if Playwright's own browser isn't downloaded
    try:
        browser = p.chromium.launch()
    except Exception:
        browser = p.chromium.launch(channel="msedge")
    for size in (192, 512):
        page = browser.new_page(viewport={"width": size, "height": size})
        sized = svg.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
        page.set_content(f'<html><body style="margin:0">{sized}</body></html>')
        page.screenshot(path=str(STATIC / f"icon-{size}.png"), omit_background=False)
        page.close()
        print(f"wrote icon-{size}.png")
    browser.close()
