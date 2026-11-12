"""Clicks through every feature of the app in Google Chrome and reports what works.

Run with:  .venv\\Scripts\\python.exe tools\\browser_check.py
Needs the optional tools:  pip install -e ".[demo]"
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

from app_harness import running_app

MAP = 'iframe[title="streamlit_folium.st_folium"]'
results: list[tuple[str, bool, str]] = []
SHOTS = Path(__file__).resolve().parents[1] / "demo" / "check"


def check(name: str):
    def wrap(function):
        def run(page: Page, *args):
            try:
                function(page, *args)
                results.append((name, True, ""))
            except Exception as error:  # report and carry on with the next feature
                SHOTS.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(SHOTS / f"{name.replace(' ', '_')}.png"))
                lines = str(error).splitlines() or traceback.format_exc(limit=1).splitlines()[-3:-1]
                detail = " ".join(line.strip() for line in lines[:6])[:420]
                results.append((name, False, detail))
        return run
    return wrap


def settle(page: Page, extra_ms: int = 400) -> None:
    """Wait until the app has finished re-running."""
    page.wait_for_timeout(300)
    page.wait_for_function(
        "() => !document.querySelector('[data-testid=\"stStatusWidget\"]')", timeout=60000
    )
    page.wait_for_timeout(extra_ms)


def tab(page: Page, name: str) -> None:
    page.get_by_role("tab", name=name).click()
    settle(page)


def heading(page: Page) -> str:
    return page.locator("h3").first.inner_text()


def pick(page: Page, text: str) -> None:
    box = page.get_by_role("combobox", name="Find a vessel")
    box.click()
    box.fill(text)
    page.wait_for_timeout(600)
    box.press("Enter")
    settle(page)


def open_decision_form(page: Page) -> None:
    if not page.get_by_text("Dismiss (no action needed)").is_visible():
        page.get_by_text("Watchlist or dismiss").click()
        page.wait_for_timeout(500)


@check("page loads with the map and vessels")
def loads(page):
    expect(page.get_by_role("heading", name="Fisheries Patrol Planner")).to_be_visible()
    frame = page.frame_locator(MAP).first
    expect(frame.locator("path.leaflet-interactive").first).to_be_visible(timeout=30000)
    assert frame.locator("path.leaflet-interactive").count() > 100
    assert frame.locator("img.leaflet-tile").count() > 0, "no background map tiles"


@check("search box finds a vessel by name")
def search(page):
    pick(page, "ramses")
    assert "RAMSES" in heading(page), heading(page)
    expect(page.get_by_text("Why it ranks here")).to_be_visible()
    frame = page.frame_locator(MAP).first
    assert frame.locator('path[stroke="#1565c0"]').count() > 0, "no trail drawn"


@check("clicking a dot on the map selects that vessel")
def click_map(page):
    before = heading(page)
    frame = page.frame_locator(MAP).first
    dots = frame.locator('path[fill="#c62828"][stroke="#ffffff"]')
    for index in range(min(dots.count(), 6)):
        dots.nth(index).click(force=True)
        settle(page, 1200)
        if heading(page) != before:
            return
    raise AssertionError("selection did not change after clicking red dots")


@check("ticking a row in the ranked list selects that vessel")
def click_list(page):
    before = heading(page)
    expander = page.get_by_text("vessels, highest risk first")
    grid = page.locator('[data-testid="stDataFrame"]').first
    if not grid.is_visible():
        expander.click()
        settle(page)
    box = grid.bounding_box()
    page.mouse.click(box["x"] + 18, box["y"] + 36 + 35 * 4 + 17)  # checkbox of the fifth row
    settle(page, 1200)
    assert heading(page) != before, "selection did not change"


@check("risk filter narrows the list and the map")
def filters(page):
    frame = page.frame_locator(MAP).first
    before = frame.locator('path[stroke="#ffffff"], path[stroke="#111111"]').count()
    page.get_by_label("Remove Low", exact=False).or_(page.get_by_label("Low, close by backspace")).first.click()
    settle(page, 1500)
    after = frame.locator('path[stroke="#ffffff"], path[stroke="#111111"]').count()
    assert 0 < after < before, (before, after)


@check("layer switches hide patrol boats and protected areas")
def layers(page):
    frame = page.frame_locator(MAP).first
    boats, reserves = frame.locator(".awesome-marker"), frame.locator('path[stroke="#1b7f5a"]')
    assert boats.count() >= 3 and reserves.count() >= 1
    sidebar = page.get_by_test_id("stSidebarUserContent")
    sidebar.get_by_text("Patrol boats", exact=True).click()
    sidebar.get_by_text("Protected areas", exact=True).click()
    settle(page, 1500)
    assert boats.count() == 0 and reserves.count() == 0
    sidebar.get_by_text("Patrol boats", exact=True).click()
    sidebar.get_by_text("Protected areas", exact=True).click()
    settle(page, 1500)
    assert boats.count() >= 3 and reserves.count() >= 1


@check("add to patrol route draws a route with legs")
def add_to_route(page):
    pick(page, "ramses")
    page.get_by_role("button", name="Add to patrol route").click()
    settle(page, 1500)
    expect(page.get_by_role("button", name="Remove from patrol route")).to_be_visible()
    expect(page.get_by_text("Distance (nm)")).to_be_visible()
    page.get_by_role("button", name="Remove from patrol route").click()
    settle(page)


@check("saving a patrol without an officer name is refused")
def save_needs_name(page):
    page.get_by_role("button", name="Suggest a route").click()
    settle(page, 1500)
    page.get_by_role("button", name="Save patrol").click()
    settle(page, 1000)
    expect(page.get_by_text("Enter the officer's name")).to_be_visible()


@check("suggested route can be saved and the boat released")
def save_patrol(page):
    name = page.get_by_label("Your name")
    name.fill("Officer Reyes")
    name.press("Enter")
    settle(page)
    page.get_by_role("button", name="Suggest a route").click()
    settle(page, 1500)
    page.get_by_role("button", name="Save patrol").click()
    settle(page, 1500)
    expect(page.get_by_text("Decision saved.")).to_be_visible()
    expect(page.get_by_text("Patrols under way")).to_be_visible()
    expect(page.get_by_text("1 of 3")).to_be_visible()
    page.get_by_role("button", name="Release").click()
    settle(page, 1500)
    expect(page.get_by_text("2 of 3")).to_be_visible()


@check("dismiss needs a reason, then hides the vessel")
def dismiss(page):
    pick(page, "guria")
    target = heading(page)
    open_decision_form(page)
    page.get_by_text("Dismiss (no action needed)").click()
    page.get_by_role("button", name="Save decision").click()
    settle(page, 1000)
    expect(page.get_by_text("Give a reason")).to_be_visible()
    pick(page, "guria")
    open_decision_form(page)
    page.get_by_text("Dismiss (no action needed)").click()
    page.get_by_placeholder("Why this decision?").fill("Licensed local vessel, inspected last week")
    page.get_by_role("button", name="Save decision").click()
    settle(page, 1500)
    expect(page.get_by_text("Decision saved.")).to_be_visible()
    assert target not in heading(page) if page.locator("h3").count() else True


@check("decision log lists the decisions and offers a download")
def decision_log(page):
    tab(page, "Decision log")
    expect(page.get_by_role("heading", name="Every decision, newest first")).to_be_visible()
    expect(page.get_by_text("Download as CSV")).to_be_visible()
    expect(page.get_by_text("Rows are only ever added")).to_be_visible()


@check("reliability page explains the test")
def reliability(page):
    tab(page, "How reliable is this?")
    expect(page.get_by_text("Listed fishing vessels tested")).to_be_visible()
    expect(page.get_by_text("thin evidence")).to_be_visible()


@check("settings: refresh without a token explains what to do")
def refresh_button(page):
    tab(page, "Settings")
    panel = page.get_by_role("tabpanel", name="Settings")
    panel.get_by_role("button", name="Refresh data from Global Fishing Watch").click()
    settle(page, 1000)
    expect(panel.get_by_text("No Global Fishing Watch token found")).to_be_visible()
    page.wait_for_timeout(6000)  # still there after the status panel has updated itself
    expect(panel.get_by_text("No Global Fishing Watch token found")).to_be_visible()


@check("settings: saving weights rebuilds the ranking")
def save_weights(page, work):
    tab(page, "Settings")
    field = page.get_by_label("Going dark (AIS gaps)")
    field.fill("60")
    field.press("Enter")
    settle(page)
    page.get_by_role("button", name="Save weights").click()
    settle(page, 2500)
    expect(page.get_by_text("Weights saved and the ranking rebuilt.")).to_be_visible(timeout=60000)
    weights = json.loads((work / "overrides.json").read_text())["scoring"]["weights"]
    assert abs(weights["going_dark"] - 60 / 130) < 1e-6, weights


@check("settings: editing and saving a patrol boat")
def save_boats(page, work):
    tab(page, "Settings")
    grids = page.locator('[data-testid="stDataFrame"]')
    grid = [grids.nth(i) for i in range(grids.count()) if grids.nth(i).is_visible()][-1]
    grid.scroll_into_view_if_needed()
    page.wait_for_timeout(1200)  # let the page stop moving before measuring
    box = grid.bounding_box()
    page.mouse.dblclick(box["x"] + box["width"] * 0.84, box["y"] + 36 + 17)  # Range of the first boat
    page.wait_for_timeout(500)
    page.keyboard.press("Control+A")
    page.keyboard.type("650")
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    page.get_by_role("button", name="Save boats").click()
    settle(page, 2500)
    expect(page.get_by_text("Boats saved.")).to_be_visible(timeout=60000)
    boats = json.loads((work / "overrides.json").read_text())["patrol_boats"]
    assert len(boats) == 3 and boats[0]["range_nm"] == 650.0, boats[0]
    assert [b["patrol_boat_id"] for b in boats] == ["PB-01", "PB-02", "PB-03"]


@check("settings: protected-area name search")
def area_search(page):
    tab(page, "Settings")
    field = page.get_by_label("Search protected-area names")
    field.fill("Cocos")
    field.press("Enter")
    settle(page, 1000)
    expect(page.get_by_role("listitem").filter(has_text="Cocos").first).to_be_visible()


@check("settings: drawing a rectangle sets the patrol area edges")
def draw_area(page):
    tab(page, "Settings")
    page.get_by_text("Draw the area on a map", exact=True).click()
    settle(page, 2500)
    frame_element = page.locator(MAP).last
    frame_element.scroll_into_view_if_needed()
    page.wait_for_timeout(1500)
    frame = page.frame_locator(MAP).last
    # The map must open on the patrol area, not on the whole world.
    zoom = frame.locator(".leaflet-tile-loaded").first.get_attribute("src")
    assert int(zoom.split("/")[-3]) >= 4, f"map opened too far out: {zoom}"
    frame.locator(".leaflet-draw-draw-rectangle").click()
    box = frame_element.bounding_box()
    x, y = box["x"] + box["width"] * 0.35, box["y"] + box["height"] * 0.35
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 120, y + 90, steps=12)
    page.mouse.up()
    settle(page, 1500)
    expect(page.get_by_text("Rectangle drawn.")).to_be_visible()


@check("settings: saving a new patrol area")
def save_area(page, work):
    tab(page, "Settings")
    panel = page.get_by_role("tabpanel", name="Settings")
    name = panel.get_by_role("textbox", name="Name", exact=True)
    name.scroll_into_view_if_needed()
    name.fill("Test area")
    name.press("Enter")
    settle(page)
    panel.get_by_role("button", name="Save patrol area").click()
    settle(page, 1500)
    expect(page.get_by_text("Patrol area saved.")).to_be_visible()
    area = json.loads((work / "overrides.json").read_text())["patrol_area"]
    west, south, east, north = area["bbox"]
    assert area["name"] == "Test area" and west < east and south < north, area
    # The rectangle was drawn near the Galapagos, so the saved edges must be there too.
    assert -110 < west < east < -70 and -20 < south < north < 20, area


def main() -> int:
    with running_app() as (url, work), sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(url)
        settle(page, 3000)
        for step in (loads, search, click_map, click_list, filters, layers, add_to_route,
                     save_needs_name, save_patrol, dismiss, decision_log, reliability):
            step(page)
        for step in (save_boats, save_weights):
            step(page, work)
        refresh_button(page)
        area_search(page)
        draw_area(page)
        save_area(page, work)
        browser.close()
    width = max(len(name) for name, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - failed} of {len(results)} checks passed in Google Chrome")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
