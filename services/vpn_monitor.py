"""Measure Telegram Bot API RTT and the current mihomo AUTO node."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote

import aiohttp
from aiogram import Bot

from config import Config
from database.models import VpnLatencySample
from database.queries import Repo
from database.vpn_database import sqlite_bundle_bytes
from utils.formatting import esc
from utils.logging import TOKEN_RE, log_extra
from utils.telegram_session import make_telegram_session
from utils.time import format_dt_iso, now_utc, to_iso
from utils.timeouts import await_or_abandon, reset_bot_session
from utils.uptime import host_uptime_seconds

logger = logging.getLogger(__name__)

NODE_PREFIX_RE = re.compile(r"^(s\d+)\s+\|")

# Labels match mihomo-proxy subscription prefixes (s1 | …).
SUBSCRIPTION_LABELS = {
    "sub1": "VLESS / все конфиги",
    "sub2": "Shadowsocks+All",
    "sub3": "VLESS / mobile",
    "sub4": "CIDR / белые списки",
    "sub5": "SNI / белые списки",
}

_SUMMARY_EVERY = 30  # 30 × 10s ≈ 5 minutes
_GROUP_NOW_HOPS = 5


def parse_node(now: str | None) -> tuple[str | None, str | None]:
    """Return (node_name, subscription like sub3) from mihomo AUTO.now."""
    if not now or not str(now).strip():
        return None, None
    name = str(now).strip()
    match = NODE_PREFIX_RE.match(name)
    if not match:
        return name, "unknown"
    prefix = match.group(1)
    return name, f"sub{int(prefix[1:])}"


def subscription_label(subscription: str | None) -> str:
    if not subscription:
        return "неизвестно"
    return SUBSCRIPTION_LABELS.get(subscription, subscription)


def sanitize_error(exc: BaseException) -> str:
    text = TOKEN_RE.sub("***", f"{type(exc).__name__}: {exc}")
    return text[:300]


def format_byte_size(n: int) -> str:
    mb = n / (1024 * 1024)
    if mb >= 10:
        return f"{mb:.0f} МБ"
    if mb >= 1:
        return f"{mb:.1f} МБ"
    kb = n / 1024
    if kb >= 1:
        return f"{kb:.0f} КБ"
    return f"{n} Б"


def make_probe_bot(config: Config) -> Bot:
    """Separate Bot/session for getMe probes so a hung ping cannot kill polling."""
    session = make_telegram_session(
        config.telegram_proxy_url,
        float(config.vpn_monitor_timeout_seconds),
    )
    return Bot(token=config.bot_token, session=session)


async def measure_bot_latency(bot: Bot, timeout: float) -> tuple[bool, int, str | None]:
    started = time.monotonic()
    try:
        await await_or_abandon(bot.get_me(), timeout, name="bot.get_me")
        latency_ms = int((time.monotonic() - started) * 1000)
        return True, latency_ms, None
    except TimeoutError:
        latency_ms = int((time.monotonic() - started) * 1000)
        await reset_bot_session(bot)
        return False, latency_ms, "timeout"
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return False, latency_ms, sanitize_error(exc)


async def _mihomo_proxy_payload(
    session: aiohttp.ClientSession, base: str, headers: dict[str, str], name: str
) -> tuple[dict | None, str | None]:
    url = f"{base}/proxies/{quote(name, safe='')}"
    async with session.get(url, headers=headers) as resp:
        if resp.status == 401:
            return None, "mihomo_unauthorized"
        if resp.status != 200:
            return None, f"mihomo_http_{resp.status}"
        data = await resp.json(content_type=None)
    if not isinstance(data, dict):
        return None, "mihomo_no_now"
    return data, None


async def fetch_auto_now(config: Config) -> tuple[str | None, str | None]:
    """Current AUTO node from mihomo. Second value is an error, not a subscription.

    Follows nested groups (AUTO → FAST/BACKUP → leaf) so the sample stores the node,
    not the inner group name.
    """
    if not config.mihomo_api_secret:
        return None, "mihomo_secret_missing"
    base = config.mihomo_api_url.rstrip("/")
    name = config.mihomo_proxy_group.strip()
    timeout = aiohttp.ClientTimeout(total=3)
    headers = {"Authorization": f"Bearer {config.mihomo_api_secret}"}
    last_now: str | None = None
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for _ in range(_GROUP_NOW_HOPS):
                data, error = await _mihomo_proxy_payload(session, base, headers, name)
                if error:
                    return (last_now, None) if last_now else (None, error)
                now = data.get("now")
                if not isinstance(now, str) or not now.strip():
                    return (last_now, None) if last_now else (None, "mihomo_no_now")
                last_now = now.strip()
                if last_now == name or not isinstance(data.get("all"), list):
                    return last_now, None
                name = last_now
    except asyncio.TimeoutError:
        return None, "mihomo_timeout"
    except aiohttp.ClientError as exc:
        return None, f"mihomo_unreachable:{type(exc).__name__}"
    except Exception as exc:
        return None, sanitize_error(exc)
    return last_now, None


class VpnMonitor:
    def __init__(self, config: Config, probe_bot: Bot | None = None) -> None:
        self.config = config
        self._probe_bot = probe_bot
        self._ticks = 0
        self._ok = 0
        self._fail = 0
        self._latencies: list[int] = []

    def _probe(self, bot: Bot) -> Bot:
        return self._probe_bot if self._probe_bot is not None else bot

    async def reset_probe(self) -> None:
        """Replace the probe Bot so the next tick does not reuse a hung SOCKS session."""
        old = self._probe_bot
        self._probe_bot = make_probe_bot(self.config)
        if old is not None:
            await reset_bot_session(old)

    def _flush_summary(self) -> None:
        if self._ticks % _SUMMARY_EVERY != 0:
            return
        avg = round(sum(self._latencies) / len(self._latencies)) if self._latencies else None
        log_extra(
            logger,
            logging.INFO,
            "vpn_monitor_summary",
            ticks=self._ticks,
            ok=self._ok,
            fail=self._fail,
            avg_ms=avg,
        )
        self._ok = 0
        self._fail = 0
        self._latencies = []

    async def tick(self, bot: Bot, repo: Repo) -> dict:
        measured_at = to_iso(now_utc())
        node_raw, mihomo_error = await fetch_auto_now(self.config)
        node_name, subscription = parse_node(node_raw)
        ok, latency_ms, bot_error = await measure_bot_latency(
            self._probe(bot), self.config.vpn_monitor_timeout_seconds
        )
        error_parts = [part for part in (mihomo_error, bot_error) if part]
        error = "; ".join(error_parts) if error_parts else None
        sample = {
            "measured_at": measured_at,
            "ok": ok,
            "latency_ms": latency_ms,
            "node_name": node_name,
            "subscription": subscription,
            "error": error,
        }
        await repo.insert_vpn_sample(
            measured_at, ok, latency_ms, node_name, subscription, error, host_uptime_seconds()
        )
        self._ticks += 1
        if ok:
            self._ok += 1
            self._latencies.append(latency_ms)
        else:
            self._fail += 1
        self._flush_summary()
        return sample


async def ping_status_line(repo: Repo, config: Config) -> str:
    """One HTML line for the service panel."""
    vpn = repo.db.vpn_db
    size = sqlite_bundle_bytes(vpn.path) if vpn is not None else 0
    tail = f"база {format_byte_size(size)} / {format_byte_size(config.vpn_db_max_bytes)}"
    if not config.vpn_monitor_enabled:
        return f"Замеры пинга: выключены · {tail}"
    sample: VpnLatencySample | None = await repo.latest_vpn_sample()
    every = f"каждые {config.vpn_monitor_interval_seconds} с"
    if sample is None:
        return f"Замеры пинга: ещё нет · {every} · {tail}"
    ping = f"{sample.latency_ms} мс" if sample.ok and sample.latency_ms is not None else "нет ответа"
    node = esc(sample.node_name) if sample.node_name else "нода неизвестна"
    when = format_dt_iso(sample.measured_at, config.timezone)
    return f"Пинг: {ping} · {node} · {when} · {every} · {tail}"
