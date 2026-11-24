"""Colours and map styling for the app's dark theme.

Kept in one place so the app, the browser checks and the demo script agree.
The page itself is themed in .streamlit/config.toml; this module covers the map,
which lives in its own frame and does not see that theme.
"""

from __future__ import annotations

from branca.element import MacroElement
from jinja2 import Template

BACKGROUND = "#0b1320"
PANEL = "#16233a"
BORDER = "#2b3a55"
TEXT = "#e6edf3"
MUTED = "#9fb3c8"
ACCENT = "#4fc3f7"

TIER_COLOURS = {"High": "#ff5252", "Elevated": "#ffab40", "Low": "#66bb6a"}
EVENT_COLOURS = {"gap": "#b0bec5", "encounter": "#ce93d8", "fishing": "#ff5252"}

VESSEL_OUTLINE = "#0b1320"
SELECTED_OUTLINE = "#ffffff"
PATROL_AREA = "#9fb3c8"
RESERVE = "#4db6ac"
TRAIL = ACCENT
ROUTE = "#ffffff"

# OpenStreetMap only offers light tiles. Inverting them and turning the hue back
# round gives a dark sea and land with light labels, with no extra tile service
# or API key. Only the tiles are filtered, so the coloured dots keep their colours.
MAP_CSS = f"""
.leaflet-container {{ background: {BACKGROUND}; }}
.leaflet-tile-pane {{ filter: invert(100%) hue-rotate(180deg) brightness(95%) contrast(90%); }}
.leaflet-bar a, .leaflet-bar a:hover {{ background-color: {PANEL}; color: {TEXT}; border-color: {BORDER}; }}
.leaflet-draw-toolbar a, .leaflet-draw-toolbar a:hover {{ background-color: {TEXT}; }}
.leaflet-control-attribution {{ background: rgba(11, 19, 32, .8) !important; color: {MUTED}; }}
.leaflet-control-attribution a {{ color: {ACCENT}; }}
.leaflet-control-scale-line {{ background: rgba(11, 19, 32, .75); color: {TEXT}; border-color: {MUTED}; }}
.leaflet-tooltip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER};
  box-shadow: 0 2px 8px rgba(0, 0, 0, .5); }}
.leaflet-tooltip-top:before {{ border-top-color: {PANEL}; }}
.leaflet-tooltip-bottom:before {{ border-bottom-color: {PANEL}; }}
.leaflet-tooltip-left:before {{ border-left-color: {PANEL}; }}
.leaflet-tooltip-right:before {{ border-right-color: {PANEL}; }}
"""


class DarkMapStyle(MacroElement):
    """Adds the dark map styling from the map's own script, which every renderer runs."""

    _template = Template(
        """
        {% macro script(this, kwargs) %}
        (function () {
            if (document.getElementById("patrol-planner-dark-map")) { return; }
            var style = document.createElement("style");
            style.id = "patrol-planner-dark-map";
            style.textContent = {{ this.css | tojson }};
            document.head.appendChild(style);
        })();
        {% endmacro %}
        """
    )

    def __init__(self):
        super().__init__()
        self._name = "DarkMapStyle"
        self.css = MAP_CSS


def stop_marker_html(number: int) -> str:
    """A numbered circle marking a stop on a planned patrol route."""
    return (
        f'<div style="background:{BACKGROUND};border:2px solid {ACCENT};border-radius:50%;'
        f"width:22px;height:22px;line-height:18px;text-align:center;font-weight:700;"
        f'color:{TEXT};margin:-5px 0 0 -5px">{number}</div>'
    )
