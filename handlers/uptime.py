"""Superadmin uptime screen: the same report as the daily-stats «Аптайм» button."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery

from config import Config
from database.queries import Repo
from handlers.common import require_level, respond
from keyboards.main import admin_vpn_kb, confirm_kb
from services.audit import audit
from services.load_charts import build_load_chart
from services.load_samples import capture_server_load
from services.roles import LEVEL_SUPERADMIN, Actor
from services.telegram_backup import last_telegram_backup_at, next_backup_value
from services.vpn_charts import (
    build_vpn_availability_charts,
    build_vpn_charts,
    downtime_ticks,
    expected_vpn_ticks,
)
from services.vpn_monitor import format_vpn_log, subscription_label, vpn_samples_as_dicts
from utils.formatting import colon_block, pre_html, seconds_human
from utils.telegram import answer_callback, png_file, safe_send, text_file
from utils.time import now_utc, parse_iso, to_iso
from utils.uptime import bot_uptime_seconds, host_uptime_seconds, uptime_report_lines

router = Router(name="uptime")
logger = logging.getLogger(__name__)

VPN_PERIODS = {
    "5m": (timedelta(minutes=5), "последние 5 минут"),
    "30m": (timedelta(minutes=30), "последние 30 минут"),
    "1h": (timedelta(hours=1), "последний час"),
    "6h": (timedelta(hours=6), "последние 6 часов"),
    "12h": (timedelta(hours=12), "последние 12 часов"),
    "24h": (timedelta(hours=24), "последние сутки"),
    "7d": (timedelta(days=7), "последнюю неделю"),
    "30d": (timedelta(days=30), "последний месяц"),
    "boot": (None, "время с запуска бота"),
    "all": (None, "всё время"),
}


def _boot_start(end: datetime) -> tuple[datetime | None, str]:
    """Start of the current process, or None when uptime is unknown."""
    uptime = bot_uptime_seconds()
    if uptime is None:
        return None, "время с запуска бота, запуск неизвестен"
    return end - timedelta(seconds=uptime), "время с запуска бота"


_VPN_LOG_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

_VPN_BUCKET_KEYS = (
    ("0–100 мс", "bucket_0_100"),
    ("100–500 мс", "bucket_100_500"),
    ("500–1000 мс", "bucket_500_1000"),
    ("> 1000 мс", "bucket_1000"),
    ("Нет пинга/соединения", "no_ping"),
    ("сервис не запущен", "service_down"),
    ("сервер выключен", "server_off"),
)


def _ms(value) -> str:
    if value is None:
        return "—"
    return str(int(round(value)))


def _tail_text(row: dict, name: str) -> str:
    """Latency tail, counting successful pings and samples with no ping."""
    no_ping_count = int(row.get(f"{name}_no_ping_count") or 0)
    ping_count = int(row.get(f"{name}_count") or 0)
    if row.get(f"{name}_no_ping"):
        return f"нет пинга ({ping_count} зам., {no_ping_count} без пинга)"
    ms = row.get(f"{name}_ms")
    if ms is None:
        return "— мс (0 зам.)"
    if no_ping_count:
        return f"{_ms(ms)} мс ({ping_count} зам., {no_ping_count} без пинга)"
    return f"{_ms(ms)} мс ({ping_count} зам.)"


def _p100ms_text(value) -> str:
    """Digits of x in px, where that percentile is the latency mark."""
    if value is None:
        return "—"
    rounded = round(float(value), 1)
    if rounded <= 0:
        return "p0"
    if rounded >= 100:
        return "p100"
    text = f"{rounded:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"p{text}"


def _pct(count: int, total: int) -> str:
    if total <= 0:
        return "—"
    return f"{(count / total * 100):.1f}%".replace(".", ",")


def _kv_pre(rows: list[tuple[str, str]]) -> str:
    return pre_html(colon_block(rows))


def _vpn_bucket_lines(summary: dict, interval: int, expected: int = 0) -> list[str]:
    rows = [(label, int(summary.get(key, 0))) for label, key in _VPN_BUCKET_KEYS]
    observed = sum(count for _, count in rows)
    total = max(expected, observed) if expected else observed
    table = [
        (label, f"{seconds_human(count * interval)} ({_pct(count, total)})")
        for label, count in rows
    ]
    return [
        f"Время в диапазонах (тик {interval} с, должно быть {expected or total} зам.):",
        _kv_pre(table),
    ]


def _subscription_title(row: dict) -> str:
    key = row.get("subscription")
    label = html.escape(subscription_label(key))
    if isinstance(key, str) and key.startswith("sub") and key[3:].isdigit():
        return f"{label} (<code>{html.escape(key)}</code>)"
    return label


def _vpn_top_item_lines(row: dict, title_html: str) -> list[str]:
    return [
        f"• {title_html}",
        f"\t{int(row['samples'])} раз, avg {_ms(row['avg_ms'])} мс,",
        f"\tp95 {_tail_text(row, 'p95')}",
    ]


def _append_vpn_top(lines: list[str], heading: str, rows: list[dict], title_html) -> None:
    if not rows:
        return
    lines.append("")
    lines.append(heading)
    for i, row in enumerate(rows):
        if i:
            lines.append("")
        lines.extend(_vpn_top_item_lines(row, title_html(row)))


def _service_age_seconds(started_iso: str | None, now: datetime | None = None) -> float | None:
    if not started_iso:
        return None
    try:
        started = parse_iso(started_iso)
    except ValueError:
        return None
    return max(0.0, ((now or now_utc()) - started).total_seconds())


async def _admin_status_pairs(repo: Repo, config: Config, now: datetime | None = None) -> list[tuple[str, str]]:
    now = now or now_utc()
    started = await repo.service_started_at()
    last_backup = await last_telegram_backup_at(repo.db)
    return [
        ("Возраст сервиса", seconds_human(_service_age_seconds(started, now))),
        (
            "Следующий бекап",
            next_backup_value(last_backup, config.telegram_backup_interval_minutes, now),
        ),
    ]


def _vpn_period(period_key: str | None) -> tuple[str, timedelta | None, str]:
    key = period_key if period_key in VPN_PERIODS else "24h"
    delta, title = VPN_PERIODS[key]
    return key, delta, title


async def _vpn_window(
    repo: Repo,
    period_key: str,
    now: datetime | None = None,
    *,
    since_boot: bool = False,
) -> tuple[str, datetime, datetime, str]:
    key, delta, title = _vpn_period(period_key)
    end = now or now_utc()
    if key == "boot":
        started, title = _boot_start(end)
        if started is None:
            earliest = await repo.earliest_vpn_measured_at()
            start = parse_iso(earliest) if earliest else end
        else:
            start = started
        return key, start, end, title
    if delta is None:
        earliest = await repo.earliest_vpn_measured_at()
        start = parse_iso(earliest) if earliest else end
    else:
        start = end - delta
    if since_boot:
        uptime = bot_uptime_seconds()
        if uptime is None:
            title = f"{title}, запуск бота неизвестен"
        else:
            started = end - timedelta(seconds=uptime)
            if started > start:
                start = started
            title = f"{title}, с запуска бота"
    return key, start, end, title


async def _vpn_report(
    repo: Repo,
    config: Config,
    period_key: str,
    *,
    now=None,
    view: str = "n",
    since_boot: bool = False,
) -> str:
    _key, start, end, title = await _vpn_window(repo, period_key, now, since_boot=since_boot)
    start_iso, end_iso = to_iso(start), to_iso(end)
    summary = await repo.vpn_latency_summary(start_iso, end_iso)
    interval = max(1, config.vpn_monitor_interval_seconds)
    heartbeats = await repo.list_vpn_heartbeats(start_iso, end_iso)
    service_down, server_off = downtime_ticks(
        heartbeats,
        window_start=start,
        window_end=end,
        interval_seconds=interval,
        now_host_uptime_s=host_uptime_seconds(),
    )
    summary["service_down"] = service_down
    summary["server_off"] = server_off
    extra = await _admin_status_pairs(repo, config, end)
    load_avg = await capture_server_load(repo.db)

    lines = ["🛡 <b>VPN / задержка бота</b>", ""]
    lines.extend(uptime_report_lines(extra, load_avg=load_avg))

    total = summary["total"]
    fail = summary["fail_count"]
    expected = expected_vpn_ticks(start, end, interval)
    if expected:
        samples_line = f"Замеров: {total} из {expected} · ошибок: {fail} ({_pct(fail, total)})"
    else:
        samples_line = f"Замеров: {total} · ошибок: {fail} ({_pct(fail, total)})"
    lines.extend(
        [
            "",
            f"<b>За {title}</b>",
            samples_line,
            _kv_pre(
                [
                    ("Средняя", f"{_ms(summary['avg_ms'])} мс"),
                    ("Минимум", f"{_ms(summary['min_ms'])} мс"),
                    ("Максимум", f"{_ms(summary['max_ms'])} мс"),
                    ("p95", _tail_text(summary, "p95")),
                    ("p99", _tail_text(summary, "p99")),
                    ("p99.9", _tail_text(summary, "p99_9")),
                    ("p100мс", _p100ms_text(summary.get("p100ms"))),
                    ("p300мс", _p100ms_text(summary.get("p300ms"))),
                    ("p500мс", _p100ms_text(summary.get("p500ms"))),
                    ("p1000мс", _p100ms_text(summary.get("p1000ms"))),
                ]
            ),
            "",
            *_vpn_bucket_lines(summary, interval, expected),
        ]
    )
    if view == "s":
        top_subs = await repo.vpn_top_subscriptions(start_iso, end_iso, limit=5)
        _append_vpn_top(lines, "Топ подписок:", top_subs, _subscription_title)
    elif view != "a":
        top_nodes = await repo.vpn_top_nodes(start_iso, end_iso, limit=5)
        _append_vpn_top(
            lines,
            "Топ нод:",
            top_nodes,
            lambda row: f"<code>{html.escape(row['node_name'] or '—')}</code>",
        )
    if not config.vpn_monitor_enabled:
        lines.append("")
        lines.append("Монитор выключен (VPN_MONITOR_ENABLED=0).")
    return "\n".join(lines)


def _parse_vpn_view(data: str | None) -> tuple[str, str, bool, bool]:
    period, view, rounded, since_boot = "24h", "n", False, False
    if data and data.startswith("adv:"):
        parts = data.split(":")
        if len(parts) >= 2:
            period = parts[1] if parts[1] in VPN_PERIODS else "24h"
        if len(parts) >= 3 and parts[2] in {"n", "s", "a"}:
            view = parts[2]
        flags = parts[3:]
        rounded = view == "a" and "r" in flags
        since_boot = "b" in flags
    return period, view, rounded, since_boot


def _parse_vpn_chart(data: str | None) -> tuple[str, bool, bool, bool, bool]:
    period, availability, rounded, color_by_sub, since_boot = "24h", False, False, False, False
    if data and data.startswith("advc:"):
        parts = data.split(":")
        if len(parts) >= 2:
            period = parts[1] if parts[1] in VPN_PERIODS else "24h"
        flags = parts[2:]
        if "a" in flags:
            availability = True
            rounded = "r" in flags
        elif "s" in flags:
            color_by_sub = True
        since_boot = "b" in flags
    return period, availability, rounded, color_by_sub, since_boot


async def _load_window(load_db, period_key: str):
    key, delta, title = _vpn_period(period_key)
    end = now_utc()
    if key == "boot":
        started, title = _boot_start(end)
        if started is None:
            earliest = await load_db.earliest_at()
            start = parse_iso(earliest) if earliest else end
        else:
            start = started
        return key, start, end, title
    if delta is None:
        earliest = await load_db.earliest_at()
        start = parse_iso(earliest) if earliest else end
    else:
        start = end - delta
    return key, start, end, title


@router.callback_query(F.data == "ad:vpn")
@router.callback_query(F.data.startswith("adv:"))
async def admin_vpn(cb: CallbackQuery, config: Config, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    period, view, rounded, since_boot = _parse_vpn_view(cb.data)
    text = await _vpn_report(repo, config, period, view=view, since_boot=since_boot)
    await respond(cb, text, admin_vpn_kb(period, view, rounded=rounded))


def _parse_vpn_wipe(data: str | None) -> tuple[str, str, bool]:
    period, view, since_boot = "24h", "n", False
    parts = (data or "").split(":")
    if len(parts) >= 2 and parts[1] in VPN_PERIODS:
        period = parts[1]
    if len(parts) >= 3 and parts[2] in {"n", "s", "a"}:
        view = parts[2]
    since_boot = "b" in parts[3:]
    return period, view, since_boot


@router.callback_query(F.data.startswith("advw:"))
async def admin_vpn_wipe_ask(cb: CallbackQuery, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    period, view, since_boot = _parse_vpn_wipe(cb.data)
    row = await repo.vpn_fetchone("SELECT COUNT(*) AS n FROM vpn_latency_samples")
    count = int(row["n"]) if row else 0
    boot = ":b" if since_boot else ""
    await respond(
        cb,
        "Удалить <b>все</b> замеры пинга?\n\n"
        f"Сейчас их {count}. Пропадут графики, логи и статистика задержки за любой период. "
        "Load avg и остальные данные бота останутся.",
        confirm_kb(f"advwc:{period}:{view}{boot}", f"adv:{period}:{view}{boot}", yes_text="🗑 Очистить"),
    )


@router.callback_query(F.data.startswith("advwc:"))
async def admin_vpn_wipe(cb: CallbackQuery, config: Config, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    assert actor is not None
    period, view, since_boot = _parse_vpn_wipe(cb.data)
    vpn = repo.db.vpn_db
    if vpn is None:
        await answer_callback(cb, "Нет базы замеров пинга", show_alert=True)
        return
    await vpn.wipe_samples()
    await audit(repo, actor, "vpn.wipe", entity_type="vpn")
    text = await _vpn_report(repo, config, period, view=view, since_boot=since_boot)
    await respond(cb, text, admin_vpn_kb(period, view), notice="Замеры пинга удалены")


@router.callback_query(F.data.startswith("adld:"))
async def admin_load_chart(cb: CallbackQuery, config: Config, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    raw = cb.data.split(":", 1)[1] if cb.data else "24h"
    load_db = repo.db.load_db
    if load_db is None:
        await answer_callback(cb, "Нет замеров load avg", show_alert=True)
        return
    _key, start, end, title = await _load_window(load_db, raw)
    await answer_callback(cb, "Строю график")
    if cb.message is None:
        return
    try:
        chart = await build_load_chart(load_db, start, end, title, config.timezone)
    except Exception:
        logger.exception("Load chart failed")
        await safe_send(cb.message.answer, "Не удалось построить график load avg.")
        return
    if chart is None:
        await safe_send(cb.message.answer, "Нет замеров load avg за этот период.")
        return
    caption, png = chart
    await safe_send(
        cb.message.answer_document,
        png_file(png, "load-avg.png"),
        caption=caption[:1024],
    )


@router.callback_query(F.data.startswith("advc:"))
async def admin_vpn_charts(cb: CallbackQuery, config: Config, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    period, availability, rounded, color_by_sub, since_boot = _parse_vpn_chart(cb.data)
    _period, start, end, title = await _vpn_window(repo, period, since_boot=since_boot)
    await answer_callback(cb, "Строю графики")
    if cb.message is None:
        return
    try:
        if availability:
            charts = await build_vpn_availability_charts(
                repo, to_iso(start), to_iso(end), title, rounded=rounded, tz_name=config.timezone
            )
        else:
            charts = await build_vpn_charts(
                repo, to_iso(start), to_iso(end), title, color_by_sub=color_by_sub, tz_name=config.timezone
            )
    except Exception:
        logger.exception("VPN charts failed")
        await safe_send(cb.message.answer, "Не удалось построить графики.")
        return
    if not charts:
        await safe_send(cb.message.answer, "Нет данных для графиков.")
        return
    for caption, png in charts:
        if caption.startswith("Доступность"):
            filename = "vpn-availability.png"
        elif caption.startswith("Пинг по времени"):
            filename = "vpn-timeline.png"
        else:
            filename = "vpn-distribution.png"
        await safe_send(
            cb.message.answer_document,
            png_file(png, filename),
            caption=caption[:1024],
        )


@router.callback_query(F.data.startswith("advl:"))
@router.callback_query(F.data == "ad:vpnlog")
async def admin_vpn_log(cb: CallbackQuery, config: Config, repo: Repo, actor: Actor | None) -> None:
    if not await require_level(cb, actor, LEVEL_SUPERADMIN):
        return
    period, since_boot = "24h", False
    if cb.data and cb.data.startswith("advl:"):
        parts = cb.data.split(":")
        if len(parts) >= 2 and parts[1] in VPN_PERIODS:
            period = parts[1]
        since_boot = "b" in parts[2:]
    period, start, end, title = await _vpn_window(repo, period, since_boot=since_boot)
    if period == "all" and start == end:
        start = _VPN_LOG_EPOCH
    start_iso, end_iso = to_iso(start), to_iso(end)
    db_samples = await repo.list_vpn_samples(start_iso, end_iso)
    payload = vpn_samples_as_dicts(db_samples)
    if not payload:
        await answer_callback(cb, f"За {title} логов нет", show_alert=True)
        return
    text = format_vpn_log(payload, start_iso, end_iso, tz_name=config.timezone)
    filename = f"vpn-{period}-{end.strftime('%Y%m%d-%H%M')}.txt"
    await answer_callback(cb, "Отправляю файл")
    if cb.message is None:
        return
    sent = await safe_send(
        cb.message.answer_document,
        text_file(text, filename),
        caption=f"VPN-логи за {title} · {len(payload)} записей",
    )
    if sent is None:
        await respond(cb, "Не удалось отправить файл логов.", admin_vpn_kb(period))
