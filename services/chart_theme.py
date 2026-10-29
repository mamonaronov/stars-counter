"""Shared dark theme for matplotlib charts in the bot."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

BG = "#111318"
FG = "#e8eaed"
GRID = "#3a3f4b"
AXIS = "#8b919a"
LEGEND_BG = "#1c1f26"
LINE = "#7dd3fc"
BAR = "#3b82f6"
BAR_ALT = "#2dd4bf"

T = TypeVar("T")

# pyplot keeps one global figure state, so only one draw may run at a time.
_render_lock = asyncio.Lock()


async def render_off_loop(draw: Callable[[], T]) -> T:
    """Draw a chart on a worker thread so button handlers keep running."""
    async with _render_lock:
        return await asyncio.to_thread(draw)


def apply_dark(fig, *axes, grid: str | bool | None = "y") -> None:
    fig.patch.set_facecolor(BG)
    axis = None if not grid else ("y" if grid is True else grid)
    for ax in axes:
        ax.set_facecolor(BG)
        ax.tick_params(colors=FG, labelsize=10)
        ax.xaxis.label.set_color(FG)
        ax.yaxis.label.set_color(FG)
        ax.title.set_color(FG)
        for spine in ax.spines.values():
            spine.set_color(AXIS)
        if axis == "both":
            ax.grid(True, color=GRID, alpha=0.75)
        elif axis:
            ax.grid(True, axis=axis, color=GRID, alpha=0.75)


def style_legend(legend) -> None:
    if legend is None:
        return
    frame = legend.get_frame()
    frame.set_facecolor(LEGEND_BG)
    frame.set_edgecolor(GRID)
    for text in legend.get_texts():
        text.set_color(FG)


def save_png(fig, buf, **kwargs) -> None:
    kwargs.setdefault("facecolor", fig.get_facecolor())
    kwargs.setdefault("edgecolor", "none")
    fig.savefig(buf, **kwargs)
