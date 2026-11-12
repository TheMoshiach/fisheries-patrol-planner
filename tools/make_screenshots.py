"""Takes the screenshots used in the README, from a throwaway copy of the data.

Run with:  .venv\\Scripts\\python.exe tools\\make_screenshots.py
Needs the optional tools:  pip install -e ".[demo]"
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from app_harness import PROJECT, running_app
from browser_check import MAP, pick, settle

OUT = PROJECT / "docs" / "images"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with running_app(port=8505) as (url, _), sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 900})
        page.goto(url)
        page.frame_locator(MAP).first.locator("path.leaflet-interactive").first.wait_for(timeout=60000)
        settle(page, 3000)

        pick(page, "ramses")
        page.get_by_role("button", name="Suggest a route").click()
        settle(page, 2500)
        main_area = page.locator('[data-testid="stMain"]')
        main_area.evaluate("el => el.scrollTo(0, 150)")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "map.png"))

        main_area.evaluate("el => el.scrollTo(0, 700)")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "patrol-route.png"))
        browser.close()
    for image in sorted(OUT.glob("*.png")):
        print(f"{image.relative_to(PROJECT)}  {image.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
