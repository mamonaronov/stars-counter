"""Uptime screen matches the daily-stats admin report."""

from __future__ import annotations

import html

import pytest

from config import REQUIRED_DB_VERSION
from handlers.uptime import _vpn_report
from keyboards.main import admin_vpn_kb
from utils.time import now_utc, to_iso


def test_uptime_report_lines(monkeypatch):
    import utils.uptime as uptime

    monkeypatch.setattr(uptime, "bot_uptime_seconds", lambda: 90)
    monkeypatch.setattr(uptime, "host_uptime_seconds", lambda: 26 * 3600)
    monkeypatch.setattr(uptime, "host_loadavg", lambda: (0.42, 0.31, 0.28))
    monkeypatch.setattr(uptime, "app_build_identity", lambda: ("deadbeef", "fix: uptime <commit>"))
    lines = uptime.uptime_report_lines()
    assert len(lines) == 1
    assert lines[0].startswith("<pre>")
    assert lines[0].endswith("</pre>")
    body = html.unescape(lines[0].removeprefix("<pre>").removesuffix("</pre>"))
    rows = body.splitlines()
    assert rows[0].endswith("Аптайм бота: 1 мин 30 с")
    assert rows[1].endswith("Аптайм сервера: 1 д 2 ч")
    assert rows[2].endswith("Load avg: 0.42, 0.31, 0.28")
    assert rows[3].endswith("Коммит: fix: uptime <commit> (deadbeef)")
    assert rows[4].endswith(f"Версия БД: {REQUIRED_DB_VERSION}")
    assert len({row.index(":") for row in rows}) == 1
    extra = uptime.uptime_report_lines([("Возраст сервиса", "3 д")])
    extra_body = html.unescape(extra[0].removeprefix("<pre>").removesuffix("</pre>"))
    assert extra_body.splitlines()[-1].endswith("Возраст сервиса: 3 д")
    assert "&lt;commit&gt;" in lines[0]


def test_answered_probe_stays_a_ping_when_node_lookup_fails():
    from services.vpn_charts import SIGNAL_NO_PING, classify_vpn_signal

    assert classify_vpn_signal(ok=True, latency_ms=90, error="mihomo_unreachable:ClientConnectorError") is None
    assert classify_vpn_signal(ok=True, latency_ms=90, error="mihomo_timeout") is None
    assert classify_vpn_signal(ok=False, latency_ms=8000, error="timeout") == SIGNAL_NO_PING
    assert (
        classify_vpn_signal(ok=False, latency_ms=None, error="mihomo_unreachable:ClientConnectorError")
        == SIGNAL_NO_PING
    )


def test_uptime_keyboard_matches_daily_stats_periods():
    labels = list(admin_vpn_kb("24h", "n").inline_keyboard)
    texts = [button.text for row in labels for button in row]
    assert "[сутки]" in texts
    assert "[Ноды]" in texts
    assert "Подписки" in texts
    assert "Доступность" in texts
    assert "📄 Логи за сутки" in texts
    assert "📈 Картинки за сутки" in texts
    assert "📈 Load avg за сутки" in texts
    assert "5 мин" in texts and "всё время" in texts


async def test_vpn_summary_buckets_and_report(repo, monkeypatch):
    import utils.uptime as uptime

    monkeypatch.setattr(uptime, "bot_uptime_seconds", lambda: 90)
    monkeypatch.setattr(uptime, "host_uptime_seconds", lambda: 3600)
    monkeypatch.setattr("services.load_samples.host_loadavg", lambda: (0.10, 0.20, 0.30))
    now = to_iso(now_utc())
    await repo.insert_vpn_sample(now, True, 50, "s3 | fast", "sub3", None, 100.0)
    await repo.insert_vpn_sample(now, True, 150, "s3 | fast", "sub3", None, 110.0)
    await repo.insert_vpn_sample(now, True, 1200, "s1 | slow", "sub1", None, 120.0)
    await repo.insert_vpn_sample(now, False, 8000, "s1 | slow", "sub1", "timeout", 130.0)
    await repo.insert_vpn_sample(
        now, True, 80, None, None, "mihomo_unreachable:ClientConnectorError", 140.0
    )
    start = "2000-01-01T00:00:00+00:00"
    end = "2999-01-01T00:00:00+00:00"
    summary = await repo.vpn_latency_summary(start, end)
    assert summary["total"] == 5
    assert summary["fail_count"] == 1
    assert summary["bucket_0_100"] == 2
    assert summary["bucket_100_500"] == 1
    assert summary["bucket_1000"] == 1
    assert summary["no_ping"] == 1
    assert summary["p95_ms"] == 8000
    assert summary["p95_count"] == 1

    text = await _vpn_report(repo, repo.db.config, "24h", view="n")
    assert text.startswith("🛡 <b>VPN / задержка бота</b>")
    assert "Аптайм бота" in text
    assert "Аптайм сервера" in text
    assert "Load avg" in text
    assert "Коммит" in text
    assert "Версия БД" in text
    assert "Возраст сервиса" in text
    assert "Следующий бекап" in text
    assert "<b>За последние сутки</b>" in text
    assert "Топ нод:" in text
    assert "s3 | fast" in text
    assert "0–100 мс" in text
    assert "сервер выключен" in text
    assert "сервис не запущен" in text

    subs = await _vpn_report(repo, repo.db.config, "24h", view="s")
    assert "Топ подписок:" in subs
    assert "VLESS / mobile" in subs
    assert "Топ нод:" not in subs

    availability = await _vpn_report(repo, repo.db.config, "24h", view="a")
    assert "Топ нод:" not in availability
    assert "Топ подписок:" not in availability


@pytest.fixture
async def h(tmp_path):
    from database.database import Database
    from tests.conftest import make_config
    from tests.harness import Harness

    config = make_config(tmp_path)
    db = Database(config)
    await db.initialize()
    try:
        yield Harness(config, db)
    finally:
        await db.close()


async def test_superadmin_opens_uptime_from_service(h):
    await h.text(1, "/start")
    await h.click(1, "s:srv")
    await h.press(1, "Аптайм")
    text = h.last_text()
    assert "VPN / задержка бота" in text
    assert "Аптайм бота" in text
    assert "За последние сутки" in text
    assert "[сутки]" in h.buttons()
    assert "[Ноды]" in h.buttons()
    await h.press(1, "Подписки")
    assert "[Подписки]" in h.buttons()
    await h.press(1, "Сервис")
    assert "🛠" in h.last_text() or "Сервис" in h.last_text()


async def test_guest_cannot_open_uptime(h):
    await h.text(99, "/start")
    await h.click(99, "ad:vpn")
    assert "Недостаточно прав" in h.session.last("AnswerCallbackQuery").text
