"""Host uptime, stored with each ping sample."""

from __future__ import annotations

from pathlib import Path

PROC_UPTIME = Path("/proc/uptime")


def host_uptime_seconds(path: Path = PROC_UPTIME) -> float | None:
    try:
        return float(path.read_text(encoding="utf-8").split()[0])
    except (OSError, IndexError, ValueError):
        return None
