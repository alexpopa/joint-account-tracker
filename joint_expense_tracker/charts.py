"""Geometry for the server-rendered SVG charts. Templates only draw what this computes."""

from __future__ import annotations

import math
from typing import Any

BAR_MAX_WIDTH = 24
SEGMENT_GAP = 2
CORNER_RADIUS = 4


def nice_ticks(maximum: float, count: int = 4) -> list[float]:
    """Round, evenly spaced axis ticks from 0 up to at least `maximum`."""
    if maximum <= 0:
        return [0.0, 1.0]
    raw_step = maximum / count
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw_step)
    return [step * i for i in range(math.ceil(maximum / step) + 1)]


def money_tick(value: float) -> str:
    if value >= 10_000:
        return f"${value / 1000:,.0f}K"
    if value >= 1_000:
        return f"${value / 1000:,.1f}K".replace(".0K", "K")
    return f"${value:,.0f}"


def _rounded_top(x: float, y: float, width: float, height: float) -> str:
    """Bar path with rounded top corners and a square base."""
    r = min(CORNER_RADIUS, width / 2, height)
    return (
        f"M{x:.1f},{y + height:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
        f"H{x + width - r:.1f} Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + r:.1f} V{y + height:.1f} Z"
    )


def stacked_columns(
    points: list[dict[str, Any]],
    keys: list[str],
    *,
    width: int = 760,
    height: int = 240,
    margin_left: int = 52,
    margin_bottom: int = 34,
    margin_top: int = 10,
) -> dict[str, Any]:
    """Lay out stacked columns, one per point, stacking `keys` bottom-up."""
    plot_width = width - margin_left - 4
    plot_height = height - margin_top - margin_bottom
    baseline = margin_top + plot_height
    ticks = nice_ticks(max((sum(point[key] for key in keys) for point in points), default=0))
    top_value = ticks[-1]
    band = plot_width / max(len(points), 1)
    bar_width = min(BAR_MAX_WIDTH, band * 0.6)

    columns = []
    for index, point in enumerate(points):
        center = margin_left + band * index + band / 2
        x = center - bar_width / 2
        filled = [key for key in keys if point[key] > 0]
        segments = []
        stack_top = baseline
        for key in filled:
            full = point[key] / top_value * plot_height
            # The gap is carved from the base of every segment above the first, in the surface color.
            gap = SEGMENT_GAP if segments else 0
            y = stack_top - full
            drawn = max(full - gap, 0.5)
            path = (
                _rounded_top(x, y, bar_width, drawn)
                if key == filled[-1]
                else f"M{x:.1f},{y + drawn:.1f} V{y:.1f} H{x + bar_width:.1f} V{y + drawn:.1f} Z"
            )
            segments.append({"key": key, "path": path})
            stack_top = y
        columns.append(
            {
                "point": point,
                "center": round(center, 1),
                "segments": segments,
                "hit": {"x": round(center - band / 2, 1), "width": round(band, 1)},
            }
        )
    return {
        "width": width,
        "height": height,
        "baseline": baseline,
        "plot_left": margin_left,
        "plot_right": width - 4,
        "plot_top": margin_top,
        "ticks": [
            {"y": round(baseline - value / top_value * plot_height, 1), "label": money_tick(value)} for value in ticks
        ],
        "columns": columns,
    }
