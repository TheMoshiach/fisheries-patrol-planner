"""Records a short captioned demo video of the app in Google Chrome.

Run with:  .venv\\Scripts\\python.exe tools\\make_demo.py
Needs the optional tools:  pip install -e ".[demo]"  and  python -m playwright install ffmpeg

The recording uses a throwaway copy of the data, so the demo's patrol decision
never reaches the real decision log. Output: demo/fisheries-patrol-planner-demo.mp4
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import Locator, Page, sync_playwright

from app_harness import PROJECT, running_app
from browser_check import MAP, settle

SIZE = {"width": 1600, "height": 900}
OUTPUT = PROJECT / "demo" / "fisheries-patrol-planner-demo.mp4"

OVERLAY = """
(() => {
  if (window.top !== window) return;
  const setup = () => {
    if (document.getElementById('demo-caption')) return;
    const style = document.createElement('style');
    style.textContent = `
      #demo-caption { position: fixed; left: 50%; bottom: 34px; transform: translateX(-50%);
        max-width: 1180px; padding: 16px 30px; border-radius: 14px; background: rgba(13, 27, 42, .92);
        color: #fff; font: 600 27px/1.35 "Segoe UI", Arial, sans-serif; text-align: center;
        box-shadow: 0 8px 30px rgba(0,0,0,.35); z-index: 999998; opacity: 0; transition: opacity .35s; }
      #demo-cursor { position: fixed; width: 26px; height: 26px; margin: -13px 0 0 -13px; border-radius: 50%;
        background: rgba(255, 196, 0, .55); border: 2px solid #b26a00; z-index: 999999; pointer-events: none;
        left: -100px; top: -100px; transition: transform .15s; }
      #demo-cursor.down { transform: scale(1.9); }
      #demo-card { position: fixed; inset: 0; background: linear-gradient(135deg, #0d1b2a, #1b4965);
        color: #fff; z-index: 999997; display: flex; flex-direction: column; align-items: center;
        justify-content: center; text-align: center; font-family: "Segoe UI", Arial, sans-serif;
        opacity: 1; transition: opacity .6s; }
      #demo-card h1 { font-size: 68px; margin: 0 0 22px; font-weight: 700; color: #fff; }
      #demo-card p { font-size: 32px; margin: 6px 80px; max-width: 1200px; line-height: 1.4; color: #dbe9f4; }
      #demo-card small { font-size: 22px; margin-top: 34px; color: #9fc2dc; }`;
    document.head.appendChild(style);
    for (const id of ['demo-card', 'demo-caption', 'demo-cursor']) {
      const el = document.createElement('div'); el.id = id; document.body.appendChild(el);
    }
    window.__card = (title, lines, note) => {
      const card = document.getElementById('demo-card');
      if (!title) { card.style.opacity = 0; setTimeout(() => card.style.display = 'none', 650); return; }
      card.innerHTML = `<h1>${title}</h1>` + lines.map(l => `<p>${l}</p>`).join('') + (note ? `<small>${note}</small>` : '');
      card.style.display = 'flex'; requestAnimationFrame(() => card.style.opacity = 1);
    };
    window.__caption = text => {
      const el = document.getElementById('demo-caption');
      if (!text) { el.style.opacity = 0; return; }
      el.textContent = text; el.style.opacity = 1;
    };
    window.__cursor = (x, y, down) => {
      const el = document.getElementById('demo-cursor');
      el.style.left = x + 'px'; el.style.top = y + 'px'; el.classList.toggle('down', !!down);
    };
    window.__card('Fisheries Patrol Planner',
      ['A few patrol boats. Hundreds of fishing vessels.', 'Which ones do you check first?'], '');
  };
  if (document.body) setup(); else document.addEventListener('DOMContentLoaded', setup);
})();
"""


class Director:
    """Moves a visible cursor, types and clicks at a human pace, and sets captions."""

    def __init__(self, page: Page):
        self.page = page
        self.x, self.y = 800.0, 450.0

    def wait(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))

    def caption(self, text: str) -> None:
        self.page.evaluate("t => window.__caption(t)", text)

    def card(self, title: str, lines: list[str] | None = None, note: str = "") -> None:
        self.page.evaluate("a => window.__card(a[0], a[1], a[2])", [title, lines or [], note])

    def move(self, x: float, y: float, seconds: float = 0.7) -> None:
        steps = max(int(seconds * 40), 1)
        for i in range(1, steps + 1):
            t = i / steps
            ease = t * t * (3 - 2 * t)
            px, py = self.x + (x - self.x) * ease, self.y + (y - self.y) * ease
            self.page.mouse.move(px, py)
            self.page.evaluate("p => window.__cursor(p[0], p[1], false)", [px, py])
            self.page.wait_for_timeout(18)
        self.x, self.y = x, y

    def over(self, target: Locator, seconds: float = 0.7, dx: float = 0.5, dy: float = 0.5) -> None:
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        self.move(box["x"] + box["width"] * dx, box["y"] + box["height"] * dy, seconds)

    def click(self, target: Locator | None = None, seconds: float = 0.7, dx: float = 0.5, dy: float = 0.5) -> None:
        if target is not None:
            self.over(target, seconds, dx, dy)
        self.page.evaluate("p => window.__cursor(p[0], p[1], true)", [self.x, self.y])
        self.page.mouse.click(self.x, self.y)
        self.page.wait_for_timeout(180)
        self.page.evaluate("p => window.__cursor(p[0], p[1], false)", [self.x, self.y])

    def scroll(self, pixels: int, seconds: float = 1.0) -> None:
        # Scrolled by script, not by the mouse wheel: over the map, the wheel zooms the map.
        self.page.evaluate(
            "px => document.querySelector('[data-testid=\"stMain\"]').scrollBy({top: px, behavior: 'smooth'})",
            pixels,
        )
        self.wait(seconds)

    def to_top(self) -> None:
        self.page.evaluate("() => document.querySelector('[data-testid=\"stMain\"]').scrollTo({top: 0, behavior: 'smooth'})")
        self.wait(0.8)


def record(page: Page) -> None:
    d = Director(page)
    frame = page.frame_locator(MAP).first
    frame.locator("path.leaflet-interactive").first.wait_for(timeout=60000)
    settle(page, 2500)
    d.wait(1.0)                                    # title card, while the app loads behind it
    d.card("")
    d.wait(0.9)

    # 1. the map
    d.caption("Every dot is a fishing vessel active in the patrol area: red is high risk, amber elevated, green low.")
    red = frame.locator('path[fill="#c62828"]')
    d.over(red.nth(0), 1.1)
    d.wait(1.2)
    d.over(red.nth(min(2, red.count() - 1)), 0.9)
    d.wait(1.0)
    d.caption("The outlined areas are marine reserves. The blue markers are the patrol boats.")
    d.over(frame.locator(".awesome-marker").first, 1.0, dy=0.3)
    d.wait(2.2)

    # 2. find a vessel
    d.caption("Search for any vessel by name, or just click its dot.")
    box = page.get_by_role("combobox", name="Find a vessel")
    d.click(box, 1.0)
    page.keyboard.type("ramses", delay=140)
    d.wait(1.0)
    page.keyboard.press("Enter")
    settle(page, 1200)

    # 3. the reasons
    d.caption("Each score is explained in plain words: this vessel switched its tracker off five times far from shore.")
    d.over(page.get_by_text("Why it ranks here"), 0.9, dx=0.2)
    d.wait(1.2)
    d.over(page.get_by_text("Stopped transmitting AIS").first, 0.8, dx=0.4)
    d.wait(3.0)

    # 4. events on the map
    d.caption("The map shows where it fished recently, and where it went dark or met another ship at sea.")
    events = frame.locator('path[fill="#37474f"], path[fill="#6a1b9a"]')
    if events.count():
        d.over(events.first, 1.1)
        d.wait(2.0)
        if events.count() > 1:
            d.over(events.nth(1), 0.9)
    d.wait(1.8)

    # 5. plan a patrol
    d.caption("Plan a patrol: one click suggests the highest-risk vessels a boat can reach and still get home.")
    d.scroll(330, 1.0)
    d.click(page.get_by_role("button", name="Suggest a route"), 1.0)
    settle(page, 1500)
    d.over(frame.locator(".awesome-marker").first, 0.9, dy=0.3)
    d.wait(2.4)
    d.caption("Every leg shows its distance, bearing and time, with a warning if the route is beyond the boat's range.")
    d.scroll(330, 1.0)
    d.over(page.get_by_text("Distance (nm)"), 0.8)
    d.wait(3.4)

    # 6. record the decision
    d.caption("Saving the patrol records who decided, when, and the score they saw. Nothing is ever overwritten.")
    name = page.get_by_label("Your name")
    d.click(name, 0.9)
    page.keyboard.type("Officer Reyes", delay=75)
    page.keyboard.press("Enter")
    settle(page, 500)
    d.click(page.get_by_role("button", name="Save patrol"), 1.0)
    settle(page, 1200)
    d.to_top()
    d.click(page.get_by_role("tab", name="Decision log"), 0.9)
    settle(page, 600)
    d.over(page.get_by_role("heading", name="Every decision, newest first"), 0.7, dx=0.3)
    d.wait(2.8)

    # 7. reliability
    d.caption("How far can you trust it? The ranking was tested against official lists of vessels caught fishing illegally.")
    d.click(page.get_by_role("tab", name="How reliable is this?"), 0.9)
    settle(page, 600)
    d.over(page.get_by_text("Listed fishing vessels tested"), 0.9)
    d.wait(2.0)
    d.caption("Listed vessels came out at or near the top of their groups. The page also says plainly how thin that evidence is.")
    d.scroll(220, 1.0)
    d.wait(3.0)

    # 8. settings
    d.to_top()
    d.caption("Refresh the data, edit your boats, and tune what counts as risky, all inside the app.")
    d.click(page.get_by_role("tab", name="Settings"), 0.9)
    settle(page, 600)
    panel = page.get_by_role("tabpanel", name="Settings")
    d.over(panel.get_by_role("button", name="Refresh data from Global Fishing Watch"), 0.9)
    d.wait(1.2)
    d.scroll(430, 1.2)
    d.wait(1.4)
    d.scroll(380, 1.2)
    d.wait(1.6)

    # 9. closing card
    d.caption("")
    page.evaluate("() => window.__cursor(-100, -100, false)")  # no cursor over the closing card
    d.card("Fisheries Patrol Planner",
           ["Rank the vessels. See why. Send the boat. Keep the record."],
           "Built on Global Fishing Watch data  ·  A score is a lead to check, not proof")
    d.wait(4.0)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="patrol_demo_video_"))
    with running_app(port=8504) as (url, _), sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport=SIZE, record_video_dir=str(work), record_video_size=SIZE)
        context.add_init_script(OVERLAY)
        page = context.new_page()
        page.goto(url)
        record(page)
        context.close()
        browser.close()
    recording = next(work.glob("*.webm"))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(recording),
         "-c:v", "libx264", "-preset", "slow", "-crf", "21", "-pix_fmt", "yuv420p", "-r", "25",
         "-movflags", "+faststart", str(OUTPUT)],
        check=True,
    )
    shutil.rmtree(work, ignore_errors=True)
    print(f"Saved {OUTPUT} ({OUTPUT.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
