"""VPN latency charts: central tendency and ping-over-time with node spans."""

from __future__ import annotations

import io
import math
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo
from functools import partial
from statistics import mean, median

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import hsv_to_rgb
from matplotlib.dates import DateFormatter
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator

from database.models import VpnLatencySample
from database.queries import Repo
from services.chart_theme import (
    AXIS as _AXIS,
    GRID as _GRID,
    apply_dark as _apply_dark,
    render_off_loop,
    style_legend as _style_legend,
)
from services.vpn_monitor import SUBSCRIPTION_LABELS, subscription_label
from utils.time import parse_iso, zone
from utils.uptime import host_uptime_seconds

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False

_MAX_TIMELINE_OK = 1800
_MAX_TIMELINE_DOWN = 1500
_MAX_LEGEND_NODES = 16
_CHART_DPI = 620
_DIST_DPI = 800
_MAX_PNG_BYTES = 1_000_000
_DEFAULT_STEP = timedelta(seconds=10)
_GAP_FACTOR = 3.0
_CURVE_STEPS = 20
_AVG_LINE = "#e879f9"
_PING_DISPLAY_MAX = 2000.0
_PING_CEILINGS = (50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0)
_PING_MAJORS = (
    0.0,
    10.0,
    25.0,
    50.0,
    75.0,
    100.0,
    150.0,
    200.0,
    250.0,
    300.0,
    400.0,
    500.0,
    750.0,
    1000.0,
)
_PING_MINOR_STEPS = (
    (50.0, 5.0),
    (100.0, 10.0),
    (200.0, 25.0),
    (500.0, 50.0),
    (1000.0, 100.0),
    (float("inf"), 1000.0),
)
# Safe hue arc: green → cyan → blue → violet. Avoids red / orange / yellow signals.
_HUE_LO = 0.36
_HUE_HI = 0.80
# Cap on how wide one subscription's hue band is. Fewer subs keep this width;
# more subs shrink it so the gap between bands stays at least as wide as a band.
_BAND_HALF = 0.015
_OTHER_SUB = "_other"

SIGNAL_SERVER_OFF = "server_off"
SIGNAL_SERVICE_DOWN = "service_down"
SIGNAL_NO_PING = "no_ping"
_SIGNAL_KEYS = frozenset({SIGNAL_SERVER_OFF, SIGNAL_SERVICE_DOWN, SIGNAL_NO_PING})
_GAP_SIGNALS = frozenset({SIGNAL_SERVER_OFF, SIGNAL_SERVICE_DOWN})
_SIGNAL_ORDER = (SIGNAL_NO_PING, SIGNAL_SERVICE_DOWN, SIGNAL_SERVER_OFF)
_SIGNAL_COLORS = {
    SIGNAL_SERVER_OFF: (0.98, 0.84, 0.12),
    SIGNAL_SERVICE_DOWN: (0.96, 0.50, 0.10),
    SIGNAL_NO_PING: (0.90, 0.16, 0.20),
}
_SIGNAL_LABELS = {
    SIGNAL_SERVER_OFF: "сервер выключен",
    SIGNAL_SERVICE_DOWN: "сервис не запущен",
    SIGNAL_NO_PING: "нет пинга",
}
PING_0_100 = "ping_0_100"
PING_100_500 = "ping_100_500"
PING_500_1000 = "ping_500_1000"
PING_1000 = "ping_1000"
_PING_BUCKET_KEYS = (PING_0_100, PING_100_500, PING_500_1000, PING_1000)
_PING_BUCKET_SET = frozenset(_PING_BUCKET_KEYS)
_PING_BUCKET_LABELS = {
    PING_0_100: "0–100 мс",
    PING_100_500: "100–500 мс",
    PING_500_1000: "500–1000 мс",
    PING_1000: "> 1000 мс",
}
_AVAIL_SLOTS = 64
_AVAIL_WINDOW_CAP = timedelta(hours=2)
# Mihomo / VPN path failures — counted as no ping, not a crashed bot.
_VPN_ISSUE_MARKERS = (
    "mihomo_unreachable",
    "mihomo_timeout",
    "mihomo_http_",
    "mihomo_no_now",
)


@dataclass(slots=True)
class CentralTendency:
    mean: float
    median: float
    mode: float
    mode_count: int
    mode_binned: bool


@dataclass(slots=True)
class TimelinePoint:
    time: datetime
    ping_ms: float
    node: str
    signal: str | None
    color_key: str
    host_uptime_s: float | None = None

    @property
    def down(self) -> bool:
        return self.signal is not None


def classify_vpn_signal(*, ok: bool, latency_ms: int | None, error: str | None) -> str | None:
    err = error or ""
    if any(marker in err for marker in _VPN_ISSUE_MARKERS):
        return SIGNAL_NO_PING
    if not ok or latency_ms is None:
        return SIGNAL_NO_PING
    return None


def _is_subscription_prefix(part: str) -> bool:
    return len(part) >= 2 and part[0] == "s" and part[1:].isdigit()


def short_node_name(name: str | None) -> str:
    """Legend label: sN subscription plus server, so nodes stay distinct."""
    if not name or not str(name).strip():
        return "нет ноды"
    parts = [part.strip() for part in str(name).split("|") if part.strip()]
    prefix = None
    if len(parts) >= 2 and _is_subscription_prefix(parts[0]):
        prefix = parts[0]
        parts = parts[1:]
    if len(parts) >= 2:
        tail = parts[-1]
        if len(tail) > 28:
            tail = tail[:27] + "…"
        body = f"{parts[0]} · {tail}"
    else:
        body = parts[0] if parts else ""
        if len(body) > 32:
            body = body[:31] + "…"
    if not body:
        return prefix or "нет ноды"
    if prefix:
        return f"{prefix} · {body}"
    return body


def latency_central_tendency(values: list[int]) -> CentralTendency | None:
    if not values:
        return None
    counts = Counter(values)
    mode_val, mode_count = counts.most_common(1)[0]
    binned = False
    if mode_count == 1 and len(values) > 1:
        rounded = [int(round(v / 10) * 10) for v in values]
        mode_val, mode_count = Counter(rounded).most_common(1)[0]
        binned = True
    return CentralTendency(
        mean=float(mean(values)),
        median=float(median(values)),
        mode=float(mode_val),
        mode_count=mode_count,
        mode_binned=binned,
    )


def ping_bucket_key(ping_ms: float) -> str:
    """Map a successful ping to the availability color bucket."""
    if math.isnan(ping_ms) or ping_ms < 100:
        return PING_0_100
    if ping_ms < 500:
        return PING_100_500
    if ping_ms < 1000:
        return PING_500_1000
    return PING_1000


def samples_to_timeline(
    samples: list[VpnLatencySample],
    *,
    color_by_sub: bool,
    color_by_ping: bool = False,
) -> list[TimelinePoint]:
    points: list[TimelinePoint] = []
    for sample in samples:
        signal = classify_vpn_signal(ok=bool(sample.ok), latency_ms=sample.latency_ms, error=sample.error)
        no_ping = not bool(sample.ok) or sample.latency_ms is None
        ping = float("nan") if no_ping else float(sample.latency_ms)
        node = short_node_name(sample.node_name)
        if signal is not None:
            color_key = signal
        elif color_by_ping:
            color_key = ping_bucket_key(ping)
        elif color_by_sub:
            color_key = subscription_label(sample.subscription)
        else:
            color_key = node
        points.append(
            TimelinePoint(
                time=parse_iso(sample.measured_at),
                ping_ms=ping,
                node=node,
                signal=signal,
                color_key=color_key,
                host_uptime_s=float(sample.host_uptime_s) if sample.host_uptime_s is not None else None,
            )
        )
    return points


def sample_step(points: list[TimelinePoint]) -> timedelta:
    deltas = [(b.time - a.time).total_seconds() for a, b in zip(points, points[1:]) if b.time > a.time]
    if not deltas:
        return _DEFAULT_STEP
    deltas.sort()
    typical = median(deltas[: max(1, (len(deltas) + 1) // 2)])
    typical = min(max(float(typical), 5.0), 30.0)
    return timedelta(seconds=typical)


def _gap_bounds(
    left: datetime,
    right: datetime,
    *,
    step: timedelta,
    skip_first_step: bool,
) -> tuple[datetime, datetime] | None:
    if right - left <= step * _GAP_FACTOR:
        return None
    start = left + step if skip_first_step else left
    if right - start <= timedelta(0):
        return None
    return start, right


def split_downtime_gap(
    start: datetime,
    end: datetime,
    *,
    host_uptime_s: float | None,
    step: timedelta,
) -> list[tuple[datetime, datetime, str]]:
    """Split a silent interval using host uptime measured at `end`.

    Host still up → bot/service was down.
    Host uptime shorter than the gap → machine was off, then came back.
    Missing uptime (old rows) → treat as service down.
    """
    duration = (end - start).total_seconds()
    if duration <= 0:
        return []
    tolerance = max(step.total_seconds() * _GAP_FACTOR, 5.0)
    if host_uptime_s is None or host_uptime_s + tolerance >= duration:
        return [(start, end, SIGNAL_SERVICE_DOWN)]
    host_back = end - timedelta(seconds=host_uptime_s)
    if host_back <= start:
        return [(start, end, SIGNAL_SERVICE_DOWN)]
    spans: list[tuple[datetime, datetime, str]] = [(start, host_back, SIGNAL_SERVER_OFF)]
    if end > host_back:
        spans.append((host_back, end, SIGNAL_SERVICE_DOWN))
    return spans


def _downtime_spans(
    samples: list[tuple[datetime, float | None]],
    *,
    window_start: datetime | None,
    window_end: datetime | None,
    step: timedelta,
    now_host_uptime_s: float | None,
) -> list[tuple[datetime, datetime, str]]:
    if not samples:
        if window_start is None or window_end is None:
            return []
        return split_downtime_gap(
            window_start, window_end, host_uptime_s=now_host_uptime_s, step=step
        )
    spans: list[tuple[datetime, datetime, str]] = []

    def add(left: datetime, right: datetime, host_up: float | None, *, skip_first_step: bool) -> None:
        bounds = _gap_bounds(left, right, step=step, skip_first_step=skip_first_step)
        if bounds is None:
            return
        spans.extend(split_downtime_gap(bounds[0], bounds[1], host_uptime_s=host_up, step=step))

    first_time, first_up = samples[0]
    if window_start is not None:
        add(window_start, first_time, first_up, skip_first_step=False)
    for (left_time, _), (right_time, right_up) in zip(samples, samples[1:]):
        add(left_time, right_time, right_up, skip_first_step=True)
    if window_end is not None:
        add(samples[-1][0], window_end, now_host_uptime_s, skip_first_step=True)
    return spans


def expected_vpn_ticks(
    window_start: datetime,
    window_end: datetime,
    interval_seconds: int,
) -> int:
    """How many monitor ticks fit in [window_start, window_end)."""
    step = max(1, int(interval_seconds))
    seconds = max(0.0, (window_end - window_start).total_seconds())
    return int(seconds // step)


def downtime_ticks(
    heartbeats: list[tuple[str, float | None]],
    *,
    window_start: datetime,
    window_end: datetime,
    interval_seconds: int,
    now_host_uptime_s: float | None = None,
) -> tuple[int, int]:
    """Return (service_down_ticks, server_off_ticks) for silent gaps in the window."""
    step_sec = max(1, int(interval_seconds))
    step = timedelta(seconds=step_sec)
    samples = [(parse_iso(ts), host_up) for ts, host_up in heartbeats]
    spans = _downtime_spans(
        samples,
        window_start=window_start,
        window_end=window_end,
        step=step,
        now_host_uptime_s=now_host_uptime_s,
    )
    down_sec = 0.0
    off_sec = 0.0
    for start, end, signal in spans:
        seconds = (end - start).total_seconds()
        if signal == SIGNAL_SERVER_OFF:
            off_sec += seconds
        else:
            down_sec += seconds
    return max(0, int(round(down_sec / step_sec))), max(0, int(round(off_sec / step_sec)))


def fill_downtime_gaps(
    points: list[TimelinePoint],
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    step: timedelta | None = None,
    now_host_uptime_s: float | None = None,
) -> list[TimelinePoint]:
    if not points:
        return points
    step = step or sample_step([point for point in points if point.signal not in _GAP_SIGNALS] or points)
    spans = _downtime_spans(
        [(point.time, point.host_uptime_s) for point in points],
        window_start=window_start,
        window_end=window_end,
        step=step,
        now_host_uptime_s=now_host_uptime_s,
    )

    def mark(when: datetime, signal: str) -> TimelinePoint:
        return TimelinePoint(
            time=when,
            ping_ms=float("nan"),
            node="",
            signal=signal,
            color_key=signal,
        )

    def markers(start: datetime, end: datetime, signal: str) -> list[TimelinePoint]:
        items = [mark(start, signal)]
        if end - start > step:
            marker = end - timedelta(microseconds=1)
            if marker > start:
                items.append(mark(marker, signal))
        return items

    out: list[TimelinePoint] = []
    span_i = 0
    for point in points:
        while span_i < len(spans) and spans[span_i][0] < point.time:
            out.extend(markers(*spans[span_i]))
            span_i += 1
        out.append(point)
    while span_i < len(spans):
        out.extend(markers(*spans[span_i]))
        span_i += 1
    return out


def availability_round_window(span: timedelta, step: timedelta) -> timedelta:
    """Coarsen ping-bucket flicker on long charts; keep a few samples on short ones."""
    span_s = max(span.total_seconds(), 1.0)
    step_s = max(step.total_seconds(), 1.0)
    by_span = span_s / _AVAIL_SLOTS
    window_s = max(step_s * 3.0, min(by_span, _AVAIL_WINDOW_CAP.total_seconds()))
    return timedelta(seconds=window_s)


def _rekey(point: TimelinePoint, color_key: str) -> TimelinePoint:
    if point.color_key == color_key:
        return point
    return replace(point, color_key=color_key)


def _run_duration(points: list[TimelinePoint], start: int, end: int, step: timedelta) -> timedelta:
    if end < len(points):
        return points[end].time - points[start].time
    return points[end - 1].time - points[start].time + step


def debounce_ping_buckets(
    points: list[TimelinePoint],
    min_duration: timedelta,
    *,
    step: timedelta,
) -> list[TimelinePoint]:
    """Keep an OK ping bucket until a different one lasts `min_duration`. Signals stay."""
    if not points:
        return points
    out = list(points)
    current_key: str | None = None
    pending_key: str | None = None
    pending_start: int | None = None

    def hold(index: int, key: str) -> None:
        out[index] = _rekey(out[index], key)

    for index, point in enumerate(points):
        if point.signal is not None:
            current_key = None
            pending_key = None
            pending_start = None
            continue
        key = point.color_key
        if current_key is None or key == current_key:
            current_key = key
            pending_key = None
            pending_start = None
            hold(index, current_key)
            continue
        if pending_key != key:
            pending_key = key
            pending_start = index
        assert pending_start is not None and pending_key is not None
        if _run_duration(points, pending_start, index + 1, step) < min_duration:
            hold(index, current_key)
            continue
        current_key = pending_key
        for held in range(pending_start, index + 1):
            if points[held].signal is None:
                hold(held, current_key)
        pending_key = None
        pending_start = None
    return out


def round_availability_colors(
    points: list[TimelinePoint],
    *,
    window: timedelta | None = None,
    step: timedelta | None = None,
) -> list[TimelinePoint]:
    """Majority ping-bucket in a time window, then ignore brief leftover flicker."""
    if not points:
        return points
    colored = [_rekey(point, point.signal if point.signal else ping_bucket_key(point.ping_ms)) for point in points]
    step = step or sample_step([point for point in colored if point.signal not in _GAP_SIGNALS] or colored)
    span = colored[-1].time - colored[0].time
    window = window or availability_round_window(span, step)
    ok_idx = [i for i, point in enumerate(colored) if point.signal is None]
    if len(ok_idx) >= 2:
        times = [colored[i].time.timestamp() for i in ok_idx]
        raw_keys = [colored[i].color_key for i in ok_idx]
        half = window.total_seconds() / 2.0
        start = 0
        n = len(ok_idx)
        new_keys: list[str] = []
        for i, t0 in enumerate(times):
            while start < n and times[start] < t0 - half:
                start += 1
            end = i
            while end < n and times[end] <= t0 + half:
                end += 1
            window_keys = raw_keys[start:end]
            counts = Counter(window_keys)
            best = counts.most_common(1)[0][1]
            winners = {key for key, count in counts.items() if count == best}
            if raw_keys[i] in winners:
                new_keys.append(raw_keys[i])
            else:
                new_keys.append(counts.most_common(1)[0][0])
        for index, key in zip(ok_idx, new_keys):
            colored[index] = _rekey(colored[index], key)
    return debounce_ping_buckets(colored, window, step=step)


def downsample_timeline(points: list[TimelinePoint], max_ok: int = _MAX_TIMELINE_OK, max_down: int = _MAX_TIMELINE_DOWN) -> list[TimelinePoint]:
    if len(points) <= max_ok + max_down:
        return points
    ok_idx = [i for i, point in enumerate(points) if point.signal is None]
    down_idx = [i for i, point in enumerate(points) if point.signal is not None]
    if len(ok_idx) > max_ok:
        step = math.ceil(len(ok_idx) / max_ok)
        ok_idx = ok_idx[::step]
    if len(down_idx) > max_down:
        step = math.ceil(len(down_idx) / max_down)
        down_idx = down_idx[::step]
    run_ends: set[int] = {0, len(points) - 1}
    for i, point in enumerate(points):
        prev_break = i == 0 or points[i - 1].color_key != point.color_key or points[i - 1].signal != point.signal
        next_break = (
            i == len(points) - 1
            or points[i + 1].color_key != point.color_key
            or points[i + 1].signal != point.signal
        )
        if prev_break or next_break:
            run_ends.add(i)
    keep = sorted(set(ok_idx) | set(down_idx) | run_ends)
    return [points[i] for i in keep]


def _densify_curve(
    xs: list[float],
    ys: list[float],
    steps: int = _CURVE_STEPS,
    *,
    lerp_x: bool = False,
) -> tuple[list[float], list[float]]:
    """Catmull-Rom interpolation so polylines read as round curves, not corners."""
    n = len(xs)
    if n < 3 or steps < 2:
        return xs, ys
    out_x: list[float] = []
    out_y: list[float] = []
    for i in range(n - 1):
        p0x, p0y = (xs[i - 1], ys[i - 1]) if i > 0 else (xs[i], ys[i])
        p1x, p1y = xs[i], ys[i]
        p2x, p2y = xs[i + 1], ys[i + 1]
        p3x, p3y = (xs[i + 2], ys[i + 2]) if i + 2 < n else (xs[i + 1], ys[i + 1])
        count = steps + 1 if i == n - 2 else steps
        for s in range(count):
            t = s / steps
            t2 = t * t
            t3 = t2 * t
            if lerp_x:
                x = p1x + (p2x - p1x) * t
            else:
                x = 0.5 * (
                    (2.0 * p1x)
                    + (-p0x + p2x) * t
                    + (2.0 * p0x - 5.0 * p1x + 4.0 * p2x - p3x) * t2
                    + (-p0x + 3.0 * p1x - 3.0 * p2x + p3x) * t3
                )
            y = 0.5 * (
                (2.0 * p1y)
                + (-p0y + p2y) * t
                + (2.0 * p0y - 5.0 * p1y + 4.0 * p2y - p3y) * t2
                + (-p0y + 3.0 * p1y - 3.0 * p2y + p3y) * t3
            )
            out_x.append(x)
            out_y.append(max(0.0, y))
    return out_x, out_y


def _chaikin(xs: list[float], ys: list[float], iterations: int = 4) -> tuple[list[float], list[float]]:
    """Corner-cutting: turns a polyline into a strongly rounded curve."""
    if len(xs) < 3 or iterations <= 0:
        return xs, ys
    for _ in range(iterations):
        nx = [xs[0]]
        ny = [ys[0]]
        for i in range(len(xs) - 1):
            nx.append(0.75 * xs[i] + 0.25 * xs[i + 1])
            ny.append(0.75 * ys[i] + 0.25 * ys[i + 1])
            nx.append(0.25 * xs[i] + 0.75 * xs[i + 1])
            ny.append(0.25 * ys[i] + 0.75 * ys[i + 1])
        nx.append(xs[-1])
        ny.append(ys[-1])
        xs, ys = nx, ny
    return xs, [max(0.0, y) for y in ys]


def _curve_times(times: list[datetime], values: list[float], steps: int = _CURVE_STEPS) -> tuple[list[datetime], list[float]]:
    if len(times) < 3:
        return times, values
    tz = times[0].tzinfo
    xs, ys = _chaikin([item.timestamp() for item in times], values, iterations=4)
    return [datetime.fromtimestamp(x, tz=tz) for x in xs], ys


def _plot_ping_polyline(ax, xs: list[datetime], ys: list[float]):
    """Every ping sample as a vertex; straight segments, no dots or Chaikin rounding."""
    return ax.plot(
        xs,
        ys,
        color="#e2e8f0",
        linewidth=0.5,
        marker="None",
        alpha=0.92,
        zorder=6,
        solid_capstyle="butt",
        solid_joinstyle="miter",
    )[0]


def _finite_ping_segments(points: list[TimelinePoint]) -> list[list[TimelinePoint]]:
    """Split ping only on missing samples. Downsampled OK runs stay one polyline."""
    segments: list[list[TimelinePoint]] = []
    current: list[TimelinePoint] = []
    for point in points:
        if math.isnan(point.ping_ms):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(point)
    if current:
        segments.append(current)
    return segments


def smooth_ping_series(times: list[datetime], values: list[float]) -> list[float]:
    """Median then Gaussian-in-time: follows typical ping, not spikes."""
    n = len(values)
    if n == 0:
        return []
    if n < 5:
        return list(values)
    span = max((times[-1] - times[0]).total_seconds(), 1.0)
    sigma = min(20 * 60.0, max(30.0, span * 0.045))
    ts = [item.timestamp() for item in times]
    med_half = sigma * 0.7
    robust: list[float] = []
    start = 0
    for i, t0 in enumerate(ts):
        while start < n and ts[start] < t0 - med_half:
            start += 1
        end = i
        while end < n and ts[end] <= t0 + med_half:
            end += 1
        window = values[start:end]
        window.sort()
        robust.append(window[len(window) // 2] if window else values[i])
    out: list[float] = []
    start = 0
    limit = 3.0 * sigma
    for i, t0 in enumerate(ts):
        while start < n and ts[start] < t0 - limit:
            start += 1
        acc = 0.0
        wsum = 0.0
        for j in range(start, n):
            delta = ts[j] - t0
            if delta > limit:
                break
            weight = math.exp(-0.5 * (delta / sigma) ** 2)
            acc += robust[j] * weight
            wsum += weight
        out.append(acc / wsum if wsum else robust[i])
    return out


def clip_ping_display(value: float) -> float:
    """Keep plotted ping on the visible axis; NaN stays NaN."""
    if math.isnan(value):
        return value
    return min(max(0.0, float(value)), _PING_DISPLAY_MAX)


def _clip_ping_ys(values: list[float]) -> list[float]:
    return [clip_ping_display(value) for value in values]


def nice_ping_ymax(raw: float) -> float:
    """Snap the axis top to 50/100/200/500/1000/2000 ms; never above 2000."""
    raw = min(max(float(raw), 50.0), _PING_DISPLAY_MAX)
    for cap in _PING_CEILINGS:
        if raw <= cap + 1e-6:
            return cap
    return _PING_DISPLAY_MAX


def ping_y_ticks(ymax: float) -> tuple[list[float], list[float]]:
    """Labeled familiar ping values; above 1000 ms majors are every 1000."""
    ymax = nice_ping_ymax(ymax)
    skip: set[float] = set()
    if ymax > 200:
        skip.update({10.0, 75.0, 150.0, 250.0, 300.0, 400.0, 750.0})
    if ymax > 500:
        skip.add(25.0)
    majors = [tick for tick in _PING_MAJORS if tick <= min(ymax, 1000.0) + 1e-6 and tick not in skip]
    extra = 2000.0
    while extra <= ymax + 1e-6:
        majors.append(extra)
        extra += 1000.0
    if ymax not in {round(v, 6) for v in majors}:
        majors.append(ymax)
        majors.sort()
    major_set = {round(v, 6) for v in majors}

    minors = [0.0]
    y = 0.0
    idx = 0
    while y < ymax - 1e-9:
        while idx < len(_PING_MINOR_STEPS) - 1 and y >= _PING_MINOR_STEPS[idx][0]:
            idx += 1
        y = round(y + _PING_MINOR_STEPS[idx][1], 6)
        if y <= ymax + 1e-6 and round(y, 6) not in major_set:
            minors.append(y)
        elif y > ymax + 1e-6:
            break
    minors = [tick for tick in minors if round(tick, 6) not in major_set]
    return majors, minors


def _apply_ping_ticks(ax, ymax: float, *, axis: str = "y") -> None:
    """Familiar ping majors plus finer minors, matching the timeline Y axis."""
    ymax = nice_ping_ymax(ymax)
    majors, minors = ping_y_ticks(ymax)
    locator_axis = ax.yaxis if axis == "y" else ax.xaxis
    locator_axis.set_major_locator(FixedLocator(majors))
    locator_axis.set_minor_locator(FixedLocator(minors))
    ax.tick_params(axis=axis, which="major", length=6)
    ax.tick_params(axis=axis, which="minor", length=3.4, colors=_AXIS)
    ax.grid(True, axis=axis, which="major", color=_GRID, alpha=0.75)
    ax.grid(True, axis=axis, which="minor", color=_GRID, alpha=0.28, linewidth=0.55)
    if axis == "y":
        ax.set_ylim(bottom=0, top=ymax)
    else:
        ax.set_xlim(left=0, right=ymax)


def _fit_png(data: bytes, max_bytes: int = _MAX_PNG_BYTES) -> bytes:
    if len(data) <= max_bytes:
        return data
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    if image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True, compress_level=9)
    blob = buf.getvalue()
    if len(blob) <= max_bytes:
        return blob
    scale = min(0.98, math.sqrt(max_bytes / max(len(blob), 1)) * 0.99)
    for _ in range(8):
        width = max(64, int(image.width * scale))
        height = max(64, int(image.height * scale))
        buf = io.BytesIO()
        image.resize((width, height), Image.Resampling.LANCZOS).save(
            buf, format="PNG", optimize=True, compress_level=9
        )
        blob = buf.getvalue()
        if len(blob) <= max_bytes:
            return blob
        scale *= math.sqrt(max_bytes / max(len(blob), 1)) * 0.98
    return blob


def _png(fig, *, dpi: int = _CHART_DPI) -> bytes:
    buf = io.BytesIO()
    fig.savefig(
        buf,
        format="png",
        dpi=dpi,
        bbox_inches="tight",
        pad_inches=0.28,
        facecolor=fig.get_facecolor(),
        edgecolor="none",
        pil_kwargs={"compress_level": 9},
    )
    plt.close(fig)
    return _fit_png(buf.getvalue())


def _hsv_rgb(hue: float, sat: float, val: float) -> tuple[float, float, float]:
    hue = min(_HUE_HI, max(_HUE_LO, hue))
    rgb = hsv_to_rgb((hue, sat, val))
    return (float(rgb[0]), float(rgb[1]), float(rgb[2]))


def _server_colors(count: int) -> list[tuple[float, float, float]]:
    """Spread `count` colors on the safe hue arc.

    Few colors sit on opposite ends (strongly different). More colors pack
    closer along the same arc; saturation/value also stagger so neighbours
    still separate when hues get close.
    """
    if count <= 0:
        return []
    span = _HUE_HI - _HUE_LO
    out: list[tuple[float, float, float]] = []
    for index in range(count):
        if count == 1:
            hue = _HUE_LO + span * 0.5
        else:
            hue = _HUE_LO + span * index / (count - 1)
        if count <= 4:
            sat, val = 0.80, 0.96
        else:
            band = index % 3
            sat = 0.58 + 0.14 * band
            val = 0.98 - 0.10 * band
        out.append(_hsv_rgb(hue, sat, val))
    return out


def _sub_number(sub: str) -> int | None:
    if len(sub) >= 2 and sub[0] == "s" and sub[1:].isdigit():
        return int(sub[1:])
    return None


def _configured_sub_numbers() -> set[int]:
    numbers: set[int] = set()
    for subscription in SUBSCRIPTION_LABELS:
        if subscription.startswith("sub") and subscription[3:].isdigit():
            numbers.add(int(subscription[3:]))
    return numbers


def _band_half_for_count(count: int) -> float:
    """Keep the empty gap between bands at least as wide as a band itself."""
    if count <= 1:
        return _BAND_HALF
    span = _HUE_HI - _HUE_LO
    return min(_BAND_HALF, span / (4 * count - 2))


def _layout_centers(max_n: int) -> tuple[dict[str, float], float]:
    """Even centers for s1..sN. s1 stays on green; the highest number stays on violet."""
    half = _band_half_for_count(max_n)
    lo = _HUE_LO + half
    hi = _HUE_HI - half
    centers: dict[str, float] = {}
    for number in range(1, max_n + 1):
        if max_n == 1:
            hue = lo
        else:
            hue = lo + (hi - lo) * (number - 1) / (max_n - 1)
        centers[f"s{number}"] = hue
    return centers, half


def _label_to_prefix() -> dict[str, str]:
    mapped: dict[str, str] = {}
    for subscription, label in SUBSCRIPTION_LABELS.items():
        number = subscription[3:] if subscription.startswith("sub") else ""
        if number.isdigit():
            prefix = f"s{int(number)}"
            mapped[label] = prefix
            mapped[subscription] = prefix
    return mapped


def _key_subscription(key: str) -> str | None:
    """Subscription family for a legend key, or None when the key has no sub."""
    mapped = _label_to_prefix().get(key)
    if mapped:
        return mapped
    head = key.split("·", 1)[0].strip()
    if _is_subscription_prefix(head):
        return head
    if head.startswith("sub") and head[3:].isdigit():
        return f"s{int(head[3:])}"
    return None


def _band_colors(count: int, center: float, half: float) -> list[tuple[float, float, float]]:
    """Shades inside one subscription hue. Saturation/value keep servers apart."""
    if count <= 0:
        return []
    sat_steps = (0.74, 0.96, 0.84, 0.68)
    val_steps = (0.98, 0.84, 0.74)
    out: list[tuple[float, float, float]] = []
    for index in range(count):
        if count == 1:
            hue = center
            sat, val = 0.84, 0.96
        else:
            hue = center - half + 2 * half * index / (count - 1)
            sat = sat_steps[index % len(sat_steps)]
            val = val_steps[(index // len(sat_steps)) % len(val_steps)]
        out.append(_hsv_rgb(hue, sat, val))
    return out


def _overflow_bands(
    centers: dict[str, float],
    named_half: float,
    overflow: list[str],
) -> dict[str, tuple[float, float]]:
    """Put subs outside s1..sN into the gaps, each on its own hue."""
    ordered = [centers[f"s{number}"] for number in range(1, len(centers) + 1)]
    if len(ordered) < 2:
        anchor = ordered[0] if ordered else (_HUE_LO + _HUE_HI) / 2
        gaps = [(_HUE_LO, anchor - named_half), (anchor + named_half, _HUE_HI)]
    else:
        gaps = [(left + named_half, right - named_half) for left, right in zip(ordered, ordered[1:])]
    gaps = [(left, right) for left, right in gaps if right - left > 1e-6]
    if not gaps:
        return {sub: ((_HUE_LO + _HUE_HI) / 2, 0.0) for sub in overflow}
    counts = [0] * len(gaps)
    for index in range(len(overflow)):
        counts[index % len(gaps)] += 1
    placed: dict[str, tuple[float, float]] = {}
    cursor = 0
    for (left, right), count in zip(gaps, counts):
        if count == 0:
            continue
        width = right - left
        for slot in range(count):
            hue = left + width * (slot + 1) / (count + 1)
            half = min(0.008, width / (count + 1) * 0.25)
            placed[overflow[cursor]] = (hue, half)
            cursor += 1
    return placed


def _subscription_bands(subs: list[str]) -> dict[str, tuple[float, float]]:
    """Map each subscription to (hue center, half-width).

    Slots follow subscription number up to the highest configured id, so a
    missing middle id leaves a gap and the others keep their hues. Ids above
    that (or servers with no subscription) sit in the gaps and do not move
    the configured bands.
    """
    numbers = _configured_sub_numbers()
    if not numbers:
        numbers = {number for sub in subs if (number := _sub_number(sub)) is not None}
    max_n = max(numbers) if numbers else 0
    if max_n <= 0:
        mid = (_HUE_LO + _HUE_HI) / 2
        return {sub: (mid, _BAND_HALF) for sub in subs}
    centers, half = _layout_centers(max_n)
    bands = {sub: (centers[sub], half) for sub in subs if sub in centers}
    overflow = sorted(sub for sub in subs if sub not in centers)
    bands.update(_overflow_bands(centers, half, overflow))
    return bands


_PING_BUCKET_COLORS = dict(zip(_PING_BUCKET_KEYS, _server_colors(4)))


def _palette(keys: list[str]) -> dict[str, tuple]:
    """Colors for timeline legend keys.

    Servers stay inside their subscription's hue band. Bands are laid out by
    subscription number across the safe arc: s1 is green, the highest
    configured id is violet, and the gap between bands is at least as wide as
    a band. Adding or removing a subscription rebuilds that layout. Keys with
    no subscription — and the ping buckets — keep the even spread on the full
    safe arc.
    """
    server_keys = [key for key in keys if key not in _SIGNAL_KEYS and key not in _PING_BUCKET_SET]
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    has_sub = False
    for key in server_keys:
        sub = _key_subscription(key)
        if sub is None:
            sub = _OTHER_SUB
        else:
            has_sub = True
        if sub not in grouped:
            grouped[sub] = []
            order.append(sub)
        grouped[sub].append(key)
    colors: dict[str, tuple] = {}
    if not has_sub:
        assigned = _server_colors(len(server_keys))
        colors = {key: assigned[i] for i, key in enumerate(server_keys)}
    else:
        bands = _subscription_bands(order)
        for sub, sub_keys in grouped.items():
            center, half = bands[sub]
            ordered = sorted(sub_keys)
            for key, color in zip(ordered, _band_colors(len(ordered), center, half)):
                colors[key] = color
    colors.update(_SIGNAL_COLORS)
    colors.update(_PING_BUCKET_COLORS)
    return colors


def _smooth_density_curve(values: list[int]) -> tuple[list[float], list[float]]:
    values = [min(int(v), int(_PING_DISPLAY_MAX)) for v in values]
    lo = float(min(values))
    hi = float(max(values))
    n = len(values)
    if hi <= lo:
        return [lo - 2.0, lo, lo + 2.0], [0.0, float(n), 0.0]
    unique = len(set(values))
    n_bins = min(96, max(20, unique * 2 if unique < 40 else int(math.sqrt(n) * 2)))
    span = hi - lo
    pad = max(span * 0.12, 1.0)
    xmin, xmax = lo - pad, min(hi + pad, _PING_DISPLAY_MAX)
    n_full = max(n_bins * 12, 420)
    width = (xmax - xmin) / n_full
    counts = [0.0] * n_full
    for value in values:
        idx = min(n_full - 1, max(0, int((float(value) - xmin) / width)))
        counts[idx] += 1.0
    sigma = 5.2
    radius = 18
    kernel = [math.exp(-0.5 * (i / sigma) ** 2) for i in range(-radius, radius + 1)]
    ksum = sum(kernel) or 1.0
    kernel = [k / ksum for k in kernel]
    smooth = [0.0] * n_full
    for i in range(n_full):
        acc = 0.0
        for j, weight in enumerate(kernel):
            src = i + j - radius
            if 0 <= src < n_full:
                acc += counts[src] * weight
        smooth[i] = acc
    xs = [xmin + width * (i + 0.5) for i in range(n_full)]
    xs, smooth = _densify_curve(xs, smooth, steps=8)
    return _clip_density_to_ping_max(xs, smooth)


def _clip_density_to_ping_max(xs: list[float], ys: list[float]) -> tuple[list[float], list[float]]:
    """Stop the distribution curve at 2000 ms even if raw pings were higher."""
    cap = _PING_DISPLAY_MAX
    out_x: list[float] = []
    out_y: list[float] = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        if x <= cap + 1e-9:
            out_x.append(x)
            out_y.append(y)
            continue
        if out_x and out_x[-1] < cap:
            prev_x, prev_y = xs[i - 1], ys[i - 1]
            span = x - prev_x
            t = (cap - prev_x) / span if span else 1.0
            out_x.append(cap)
            out_y.append(max(0.0, prev_y + (y - prev_y) * t))
        break
    return out_x, out_y


def _merged_spans(points: list[TimelinePoint], step: timedelta) -> list[tuple[datetime, datetime, str, str | None]]:
    if not points:
        return []
    gap_limit = step * _GAP_FACTOR
    spans: list[tuple[datetime, datetime, str, str | None]] = []
    start = points[0].time
    key = points[0].color_key
    signal = points[0].signal
    for prev, current in zip(points, points[1:]):
        # Same color after downsample is sparse on purpose. Keep one band, not stripes.
        # Real holes are filled as down/off markers before this runs.
        same_run = current.color_key == key and current.signal == signal
        jumped = (not same_run) and current.time - prev.time > gap_limit
        if not same_run or jumped:
            end = current.time if not jumped else prev.time + step
            spans.append((start, end, key, signal))
            start = current.time
            key = current.color_key
            signal = current.signal
    spans.append((start, points[-1].time + step, key, signal))
    return spans


def _time_formatter(span_seconds: float, tz: tzinfo | None = None) -> DateFormatter:
    if span_seconds <= 45 * 60:
        fmt = "%H:%M:%S"
    elif span_seconds <= 36 * 3600:
        fmt = "%H:%M"
    else:
        fmt = "%d.%m %H:%M"
    return DateFormatter(fmt, tz=tz)


def render_central_chart(values: list[int], period_title: str) -> bytes:
    stats = latency_central_tendency(values)
    if stats is None:
        raise ValueError("no latencies")
    mode_note = " (~10 мс)" if stats.mode_binned else ""
    xs, ys = _smooth_density_curve(values)
    fig, ax = plt.subplots(figsize=(16.0, 8.0))
    _apply_dark(fig, ax)
    ax.fill_between(xs, ys, color="#3b82f6", alpha=0.32, linewidth=0, zorder=1)
    ax.plot(xs, ys, color="#7dd3fc", linewidth=1.4, solid_capstyle="round", zorder=2)
    markers = (
        (stats.mean, "#60a5fa", "-", f"среднее {stats.mean:.0f} мс"),
        (stats.median, "#4ade80", "--", f"медиана {stats.median:.0f} мс"),
        (stats.mode, "#c084fc", ":", f"мода {stats.mode:.0f} мс ({stats.mode_count}×){mode_note}"),
    )
    for x, color, style, label in markers:
        if x > _PING_DISPLAY_MAX:
            continue
        ax.axvline(x, color=color, linewidth=1.2, linestyle=style, label=label, zorder=3)
    ax.set_xlabel("Пинг, мс")
    ax.set_ylabel("замеров")
    ax.set_title(f"Распределение пинга · среднее / медиана / мода · {period_title}")
    ax.set_ylim(bottom=0)
    _apply_ping_ticks(ax, max(values), axis="x")
    _style_legend(ax.legend(loc="upper right", fontsize=10))
    return _png(fig, dpi=_DIST_DPI)


def render_timeline_chart(
    points: list[TimelinePoint],
    period_title: str,
    *,
    color_by_sub: bool,
    color_by_ping: bool = False,
    rounded: bool = False,
    tz_name: str = "Europe/Moscow",
) -> bytes:
    fig, ax = plt.subplots(figsize=(18.5, 8.2))
    _apply_dark(fig, ax)
    keys = []
    for point in points:
        if point.color_key not in keys:
            keys.append(point.color_key)
    colors = _palette(keys)
    step = sample_step([point for point in points if point.signal not in _GAP_SIGNALS] or points)
    span_points = downsample_timeline(points)
    for start, end, key, signal in _merged_spans(span_points, step):
        if signal:
            color = _SIGNAL_COLORS[signal]
            alpha = 0.62
        else:
            color = colors.get(key, _SIGNAL_COLORS[SIGNAL_NO_PING])
            alpha = 0.50
        ax.axvspan(start, end, color=color, alpha=alpha, linewidth=0, antialiased=False, zorder=1)

    ping_segments = _finite_ping_segments(points)
    ping_handles: list = []
    for segment in ping_segments:
        line = _plot_ping_polyline(
            ax,
            [point.time for point in segment],
            _clip_ping_ys([point.ping_ms for point in segment]),
        )
        if not ping_handles:
            ping_handles.append(line)

    finite = [point for point in points if not math.isnan(point.ping_ms)]
    avg_source = [point for point in span_points if not math.isnan(point.ping_ms)]
    avg_handle = None
    if len(avg_source) >= 2:
        avg_times = [point.time for point in avg_source]
        avg_values = _clip_ping_ys(smooth_ping_series(avg_times, [point.ping_ms for point in avg_source]))
        avg_by_time = {time: value for time, value in zip(avg_times, avg_values)}

        def _plot_avg(seg_t: list[datetime], seg_y: list[float]):
            nonlocal avg_handle
            if len(seg_t) < 2:
                return
            if len(seg_t) >= 3:
                seg_t, seg_y = _curve_times(seg_t, seg_y, steps=12)
                seg_y = _clip_ping_ys(seg_y)
            handle = ax.plot(
                seg_t,
                seg_y,
                color=_AVG_LINE,
                linewidth=0.5,
                alpha=0.95,
                zorder=7,
                solid_capstyle="round",
            )[0]
            if avg_handle is None:
                avg_handle = handle

        for segment in _finite_ping_segments(span_points):
            _plot_avg(
                [point.time for point in segment],
                [avg_by_time[point.time] for point in segment],
            )

    ping_fail_x = [point.time for point in points if point.signal == SIGNAL_NO_PING]
    if ping_fail_x:
        ax.scatter(ping_fail_x, [0] * len(ping_fail_x), marker="|", s=110, color=_SIGNAL_COLORS[SIGNAL_NO_PING], zorder=4)

    ping_values = [point.ping_ms for point in points if not math.isnan(point.ping_ms)]
    _apply_ping_ticks(ax, max(ping_values) if ping_values else 80.0, axis="y")
    span = (points[-1].time - points[0].time).total_seconds() if len(points) > 1 else 0
    tz = zone(tz_name)
    ax.xaxis_date(tz)
    ax.xaxis.set_major_formatter(_time_formatter(span, tz))
    ax.set_ylabel("Пинг, мс")
    ax.set_xlabel("Время")
    if color_by_ping:
        round_note = " · округление" if rounded else ""
        color_note = f"цвет фона — пинг{round_note}"
        ax.set_title(f"Доступность · {period_title}\n{color_note}")
        legend_keys = [key for key in _PING_BUCKET_KEYS if key in keys]
    else:
        color_note = "цвет фона — подписка" if color_by_sub else "цвет фона — сервер, оттенок — подписка"
        ax.set_title(f"Пинг по времени · {period_title}\n{color_note}")
        legend_keys = [key for key in keys if key not in _SIGNAL_KEYS][:_MAX_LEGEND_NODES]
    handles = [
        Patch(
            facecolor=colors[key],
            edgecolor="none",
            alpha=0.85,
            label=_PING_BUCKET_LABELS.get(key, key),
        )
        for key in legend_keys
    ]
    present_signals = {point.signal for point in points if point.signal}
    for signal in _SIGNAL_ORDER:
        if signal in present_signals:
            handles.append(
                Patch(
                    facecolor=_SIGNAL_COLORS[signal],
                    edgecolor="none",
                    alpha=0.90,
                    label=_SIGNAL_LABELS[signal],
                )
            )
    if ping_handles:
        ping_handles[0].set_label("пинг")
        handles.append(ping_handles[0])
    if avg_handle is not None:
        avg_handle.set_label("сглаженный пинг")
        handles.append(avg_handle)
    elif len(finite) >= 2:
        handles.append(Line2D([0], [0], color=_AVG_LINE, linewidth=0.5, label="сглаженный пинг"))
    if handles:
        _style_legend(
            ax.legend(
                handles=handles,
                loc="upper center",
                bbox_to_anchor=(0.5, -0.18),
                ncol=min(4, len(handles)),
                fontsize=9,
            )
        )
    fig.autofmt_xdate(rotation=35)
    return _png(fig)


def render_vpn_charts(
    samples: list[VpnLatencySample],
    period_title: str,
    *,
    color_by_sub: bool = False,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    tz_name: str = "Europe/Moscow",
) -> list[tuple[str, bytes]]:
    if not samples:
        return []
    ok_latencies = [int(sample.latency_ms) for sample in samples if sample.ok and sample.latency_ms is not None]
    points = samples_to_timeline(samples, color_by_sub=color_by_sub)
    points = fill_downtime_gaps(
        points,
        window_start=window_start,
        window_end=window_end,
        now_host_uptime_s=host_uptime_seconds(),
    )
    charts: list[tuple[str, bytes]] = []
    if ok_latencies:
        stats = latency_central_tendency(ok_latencies)
        caption = (
            f"Среднее {stats.mean:.0f} мс · медиана {stats.median:.0f} · "
            f"мода {stats.mode:.0f} ({stats.mode_count}×) · {period_title}"
        )
        charts.append((caption, render_central_chart(ok_latencies, period_title)))
    if points:
        charts.append(
            (
                f"Пинг по времени · {period_title}",
                render_timeline_chart(points, period_title, color_by_sub=color_by_sub, tz_name=tz_name),
            )
        )
    return charts


def render_availability_charts(
    samples: list[VpnLatencySample],
    period_title: str,
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    rounded: bool = False,
    tz_name: str = "Europe/Moscow",
) -> list[tuple[str, bytes]]:
    if not samples:
        return []
    points = samples_to_timeline(samples, color_by_sub=False, color_by_ping=True)
    points = fill_downtime_gaps(
        points,
        window_start=window_start,
        window_end=window_end,
        now_host_uptime_s=host_uptime_seconds(),
    )
    if rounded:
        points = round_availability_colors(points)
    if not points:
        return []
    note = " · округление" if rounded else ""
    caption = f"Доступность · {period_title}{note}"
    return [
        (
            caption,
            render_timeline_chart(
                points,
                period_title,
                color_by_sub=False,
                color_by_ping=True,
                rounded=rounded,
                tz_name=tz_name,
            ),
        )
    ]


async def build_vpn_charts(
    repo: Repo,
    start: str,
    end: str,
    period_title: str,
    *,
    color_by_sub: bool = False,
    tz_name: str = "Europe/Moscow",
) -> list[tuple[str, bytes]]:
    samples = await repo.list_vpn_samples(start, end)
    return await render_off_loop(
        partial(
            render_vpn_charts,
            samples,
            period_title,
            color_by_sub=color_by_sub,
            window_start=parse_iso(start),
            window_end=parse_iso(end),
            tz_name=tz_name,
        )
    )


async def build_vpn_availability_charts(
    repo: Repo,
    start: str,
    end: str,
    period_title: str,
    *,
    rounded: bool = False,
    tz_name: str = "Europe/Moscow",
) -> list[tuple[str, bytes]]:
    samples = await repo.list_vpn_samples(start, end)
    return await render_off_loop(
        partial(
            render_availability_charts,
            samples,
            period_title,
            window_start=parse_iso(start),
            window_end=parse_iso(end),
            rounded=rounded,
            tz_name=tz_name,
        )
    )
