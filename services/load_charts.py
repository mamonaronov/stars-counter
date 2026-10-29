"""One chart with the three server load-average series."""

from __future__ import annotations

import io
from datetime import datetime, tzinfo
from functools import partial

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.dates import DateFormatter

from database.load_database import LoadDatabase
from services.chart_theme import LINE, apply_dark, render_off_loop, save_png, style_legend
from utils.time import parse_iso, to_iso, zone

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

_SERIES = (
    (1, "1 мин", LINE),
    (2, "5 мин", "#4ade80"),
    (3, "15 мин", "#c084fc"),
)


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    save_png(fig, buf, format="png", dpi=140)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _time_formatter(span_seconds: float, tz: tzinfo) -> DateFormatter:
    if span_seconds <= 45 * 60:
        fmt = "%H:%M:%S"
    elif span_seconds <= 36 * 3600:
        fmt = "%H:%M"
    else:
        fmt = "%d.%m %H:%M"
    return DateFormatter(fmt, tz=tz)


def render_load_chart(
    samples: list[tuple[str, float, float, float]],
    period_title: str,
    tz_name: str,
) -> bytes | None:
    if not samples:
        return None
    times = [parse_iso(sampled_at) for sampled_at, _, _, _ in samples]
    fig, ax = plt.subplots(figsize=(10, 4.6))
    for index, label, color in _SERIES:
        ax.plot(
            times,
            [row[index] for row in samples],
            color=color,
            linewidth=1.6,
            label=label,
        )
    span = (times[-1] - times[0]).total_seconds() if len(times) > 1 else 0
    tz = zone(tz_name)
    ax.xaxis_date(tz)
    ax.xaxis.set_major_formatter(_time_formatter(span, tz))
    fig.autofmt_xdate(rotation=30)
    ax.set_ylim(bottom=0)
    ax.set_ylabel("load avg")
    ax.set_title(f"Load avg · {period_title}")
    style_legend(ax.legend(loc="upper left", fontsize=10))
    apply_dark(fig, ax, grid="both")
    return _png(fig)


async def build_load_chart(
    load_db: LoadDatabase,
    start: datetime,
    end: datetime,
    period_title: str,
    tz_name: str,
) -> tuple[str, bytes] | None:
    samples = await load_db.list_between(to_iso(start), to_iso(end))
    png = await render_off_loop(partial(render_load_chart, samples, period_title, tz_name))
    if png is None:
        return None
    return f"Load avg · {period_title}", png
