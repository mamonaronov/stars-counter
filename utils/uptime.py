"""Process (bot) and host (server) uptime."""

from __future__ import annotations

import os
import time
from pathlib import Path

from config import REQUIRED_DB_VERSION
from utils.app_version import app_build_identity
from utils.formatting import colon_block, pre_html, seconds_human

PROC_UPTIME = Path("/proc/uptime")
PROC_LOADAVG = Path("/proc/loadavg")
PROC_SELF_STAT = Path("/proc/self/stat")

_started_monotonic: float | None = None


def mark_bot_started() -> None:
    global _started_monotonic
    _started_monotonic = time.monotonic()


def host_uptime_seconds(path: Path = PROC_UPTIME) -> float | None:
    try:
        return float(path.read_text(encoding="utf-8").split()[0])
    except (OSError, IndexError, ValueError):
        return None


def process_uptime_from_stat(stat_text: str, host_uptime: float, clk_tck: int) -> float:
    comm_end = stat_text.rfind(")")
    if comm_end < 0:
        raise ValueError("invalid /proc/stat")
    fields = stat_text[comm_end + 2 :].split()
    start_ticks = int(fields[19])
    if clk_tck <= 0:
        raise ValueError("clk_tck")
    return max(0.0, host_uptime - start_ticks / clk_tck)


def parse_loadavg(text: str) -> tuple[float, float, float] | None:
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return None


def host_loadavg(path: Path = PROC_LOADAVG) -> tuple[float, float, float] | None:
    try:
        return parse_loadavg(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def format_loadavg(loads: tuple[float, float, float] | None) -> str:
    if loads is None:
        return "—"
    return ", ".join(f"{value:.2f}" for value in loads)


def bot_uptime_seconds() -> float | None:
    if _started_monotonic is not None:
        return max(0.0, time.monotonic() - _started_monotonic)
    host = host_uptime_seconds()
    if host is None:
        return None
    try:
        stat = PROC_SELF_STAT.read_text(encoding="utf-8")
        clk_tck = int(os.sysconf("SC_CLK_TCK"))
        return process_uptime_from_stat(stat, host, clk_tck)
    except (OSError, IndexError, ValueError, TypeError, OverflowError):
        return None


def uptime_report_lines(
    extra: list[tuple[str, str]] | None = None,
    *,
    db_version: int | None = REQUIRED_DB_VERSION,
    load_avg: str | None = None,
) -> list[str]:
    commit, title = app_build_identity()
    rows = [
        ("Аптайм бота", seconds_human(bot_uptime_seconds())),
        ("Аптайм сервера", seconds_human(host_uptime_seconds())),
        ("Load avg", load_avg if load_avg is not None else format_loadavg(host_loadavg())),
        ("Коммит", f"{title} ({commit})"),
    ]
    if db_version is not None:
        rows.append(("Версия БД", str(db_version)))
    if extra:
        rows.extend(extra)
    return [pre_html(colon_block(rows))]
