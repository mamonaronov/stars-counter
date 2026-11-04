"""Uptime screen matches the daily-stats admin report."""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

import pytest

from config import REQUIRED_DB_VERSION
from handlers.uptime import _parse_vpn_chart, _parse_vpn_view, _vpn_report
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


def test_subscription_chart_uses_the_node_name_when_the_stored_key_is_unknown():
    from database.models import VpnLatencySample
    from services.vpn_charts import samples_to_timeline

    sample = VpnLatencySample(
        id=1,
        measured_at="2026-11-04T12:00:00+00:00",
        ok=1,
        latency_ms=40,
        node_name="Платная | 🇦🇹 №4 LTE x7 трафик",
        subscription="unknown",
        error=None,
    )
    points = samples_to_timeline([sample], color_by_sub=True)
    assert points[0].color_key == "Платная"


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
    rows = admin_vpn_kb("24h", "n").inline_keyboard
    texts = [button.text for row in rows for button in row]
    assert [[button.text for button in row] for row in rows[:4]] == [
        ["5 мин", "30 мин", "1 ч"],
        ["6 ч", "12 ч", "[сутки]"],
        ["неделя", "месяц", "С запуска", "всё время"],
        ["[Ноды]", "Подписки", "Доступность"],
    ]
    assert "📄 Логи за сутки" in texts
    assert "📈 Картинки за сутки" in texts
    assert "📈 Load avg за сутки" in texts
    assert "🗑 Очистить пинги" in texts
    assert "🛠 Сервис" in texts
    assert "🏠 Меню" in texts
    assert "[С запуска]" not in texts


def _kb_buttons(markup) -> dict[str, str]:
    return {button.text: button.callback_data for row in markup.inline_keyboard for button in row}


def test_boot_period_button_stays_with_the_view():
    buttons = _kb_buttons(admin_vpn_kb("boot", "a", rounded=True))
    assert [button.text for row in admin_vpn_kb("boot", "a", rounded=True).inline_keyboard for button in row if button.text in {"Ноды", "Подписки", "[Доступность]", "[С запуска]", "всё время"}] == [
        "[С запуска]",
        "всё время",
        "Ноды",
        "Подписки",
        "[Доступность]",
    ]
    assert buttons["Ноды"] == "adv:boot:n"
    assert buttons["Подписки"] == "adv:boot:s"
    assert buttons["[Доступность]"] == "adv:boot:a:r"
    assert buttons["[С запуска]"] == "adv:boot:a:r"
    assert buttons["5 мин"] == "adv:5m:a:r"
    assert buttons["всё время"] == "adv:all:a:r"
    assert buttons["[Округление]"] == "adv:boot:a"
    assert buttons["📄 Логи за время с запуска"] == "advl:boot"
    assert buttons["📈 Доступность за время с запуска"] == "advc:boot:a:r"
    assert buttons["📈 Load avg за время с запуска"] == "adld:boot"
    assert buttons["🗑 Очистить пинги"] == "advw:boot:a"
    assert buttons["🏠 Меню"] == "n:m"
    assert _parse_vpn_view(buttons["[Доступность]"]) == ("boot", "a", True, False)
    assert _parse_vpn_view(buttons["[С запуска]"]) == ("boot", "a", True, False)
    assert _parse_vpn_chart(buttons["📈 Доступность за время с запуска"]) == ("boot", True, True, False, False)
    assert _kb_buttons(admin_vpn_kb())["С запуска"] == "adv:boot:n"


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
    assert summary["p95_no_ping"] is True
    assert summary["p99_no_ping"] is True
    assert summary["p99_9_no_ping"] is True
    assert summary["p95_ms"] is None
    assert summary["p95_count"] == 0
    assert summary["p95_no_ping_count"] == 1
    assert summary["p100ms"] == 40
    assert summary["p300ms"] == 60
    assert summary["p500ms"] == 60
    assert summary["p1000ms"] == 60

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
    assert "p95: нет пинга (0 зам., 1 без пинга)" in html.unescape(text)
    plain = html.unescape(text)
    assert "p100мс" in plain and "p40" in plain
    assert "p300мс" in plain and "p500мс" in plain and "p1000мс" in plain
    assert "p60" in plain
    assert "p95 нет пинга (0 зам., 1 без пинга)" in text

    subs = await _vpn_report(repo, repo.db.config, "24h", view="s")
    assert "Топ подписок:" in subs
    assert "VLESS / mobile" in subs
    assert "Топ нод:" not in subs


async def test_subscription_top_names_nodes_stored_as_unknown(repo):
    now = to_iso(now_utc())
    await repo.insert_vpn_sample(now, True, 40, "Платная | 🇦🇹 №4 LTE x7 трафик", "unknown", None, 1.0)
    await repo.insert_vpn_sample(
        now,
        True,
        80,
        "🏴 ЧЕРНЫЕ СПИСКИ 🏴 BLACK LISTS | Mobile-150 | 🇸🇪 Sweden | [BL]-02",
        "unknown",
        None,
        1.0,
    )
    await repo.insert_vpn_sample(
        now,
        True,
        90,
        "🏳️ БЕЛЫЕ СПИСКИ 🏳️ WHITE LISTS | CIDR | Полная • Full | 🇩🇪 Germany | [*CIDR]-04",
        "unknown",
        None,
        1.0,
    )
    text = html.unescape(await _vpn_report(repo, repo.db.config, "24h", view="s"))
    assert "Платная" in text
    assert "Чёрные списки" in text
    assert "CIDR / белые списки" in text
    assert "• unknown" not in text
    assert "• —" not in text
    assert "(—)" not in text

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


async def test_percentiles_keep_fast_failures_in_the_tail(repo):
    now = to_iso(now_utc())
    for _ in range(20):
        await repo.insert_vpn_sample(now, True, 100, "n", "sub", None, 1.0)
    await repo.insert_vpn_sample(now, False, 50, "n", "sub", "timeout", 1.0)
    start = "2000-01-01T00:00:00+00:00"
    end = "2999-01-01T00:00:00+00:00"
    summary = await repo.vpn_latency_summary(start, end)
    assert summary["p95_ms"] == 100
    assert summary["p95_no_ping"] is False
    assert summary["p95_count"] == 20
    assert summary["p95_no_ping_count"] == 1
    assert summary["p100ms"] == pytest.approx(100 * 20 / 21)
    assert summary["p300ms"] == pytest.approx(100 * 20 / 21)
    assert summary["p500ms"] == pytest.approx(100 * 20 / 21)
    assert summary["p1000ms"] == pytest.approx(100 * 20 / 21)
    assert summary["p99_no_ping"] is True
    assert summary["p99_no_ping_count"] == 1

    text = html.unescape(await _vpn_report(repo, repo.db.config, "24h", view="n"))
    assert "20 зам., 1 без пинга" in text
    assert "p95,2" in text
    assert "0 зам., 1 без пинга" in text
    assert "нет пинга" in text
    nodes = await repo.vpn_top_nodes(start, end)
    assert nodes[0]["p95_ms"] == 100
    assert nodes[0]["p95_count"] == 20
    assert nodes[0]["p95_no_ping_count"] == 1
    assert "p95 100 мс (20 зам., 1 без пинга)" in text


async def test_px_ms_steps_up_at_each_threshold(repo):
    now = to_iso(now_utc())
    for latency in (50, 250, 400, 800, 1500):
        await repo.insert_vpn_sample(now, True, latency, "n", "sub", None, 1.0)
    await repo.insert_vpn_sample(now, False, 10, "n", "sub", "timeout", 1.0)
    start = "2000-01-01T00:00:00+00:00"
    end = "2999-01-01T00:00:00+00:00"
    summary = await repo.vpn_latency_summary(start, end)
    assert summary["p100ms"] == pytest.approx(100 * 1 / 6)
    assert summary["p300ms"] == pytest.approx(100 * 2 / 6)
    assert summary["p500ms"] == pytest.approx(100 * 3 / 6)
    assert summary["p1000ms"] == pytest.approx(100 * 4 / 6)

    text = html.unescape(await _vpn_report(repo, repo.db.config, "24h", view="n"))
    assert "p100мс" in text and "p16,7" in text
    assert "p300мс" in text and "p33,3" in text
    assert "p500мс" in text and "p50" in text
    assert "p1000мс" in text and "p66,7" in text


async def test_since_boot_cuts_the_window_at_process_start(repo, monkeypatch):
    import handlers.uptime as screen

    end = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(screen, "bot_uptime_seconds", lambda: 3600)
    await repo.insert_vpn_sample(to_iso(end - timedelta(hours=2)), True, 40, "old-node", "sub", None, 1.0)
    await repo.insert_vpn_sample(
        to_iso(end - timedelta(minutes=10)), False, 20, "fresh-node", "sub", "timeout", 1.0
    )
    full = await _vpn_report(repo, repo.db.config, "all", now=end)
    cut = await _vpn_report(repo, repo.db.config, "boot", now=end)
    assert "Замеров: 2" in full
    assert "old-node" in full and "fresh-node" in full
    assert "За время с запуска бота" in cut
    assert "Замеров: 1" in cut
    assert "fresh-node" in cut
    assert "old-node" not in cut
    assert "ошибок: 1" in cut

    monkeypatch.setattr(screen, "bot_uptime_seconds", lambda: None)
    unknown = await _vpn_report(repo, repo.db.config, "boot", now=end)
    assert "запуск неизвестен" in unknown
    assert "Замеров: 2" in unknown


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
    await h.press(1, "Аптайм")
    await h.press(1, "Меню")
    assert "Посещаемость" in h.last_text()
    assert "🛠 Сервис" in h.buttons()


async def test_superadmin_wipes_ping_samples_only(h):
    now = to_iso(now_utc())
    await h.repo.insert_vpn_sample(now, True, 40, "s1 | node", "sub1", None, 1.0)
    await h.repo.db.load_db.record(now, 0.1, 0.2, 0.3)
    await h.text(1, "/start")
    await h.click(1, "s:srv")
    await h.press(1, "Аптайм")
    await h.press(1, "Очистить пинги")
    assert "Удалить" in h.last_text() and "Load avg" in h.last_text()
    await h.press(1, "Отмена")
    assert await h.repo.vpn_fetchone("SELECT COUNT(*) AS n FROM vpn_latency_samples")
    assert int((await h.repo.vpn_fetchone("SELECT COUNT(*) AS n FROM vpn_latency_samples"))["n"]) == 1
    await h.press(1, "Очистить пинги")
    await h.press(1, "Очистить")
    assert int((await h.repo.vpn_fetchone("SELECT COUNT(*) AS n FROM vpn_latency_samples"))["n"]) == 0
    assert "Замеров: 0" in h.last_text()
    kept = await h.repo.db.load_db.list_between("2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00")
    assert any(row[0] == now and row[1:] == (0.1, 0.2, 0.3) for row in kept)


async def test_boot_period_filters_the_open_screen(h, monkeypatch):
    import handlers.uptime as screen

    monkeypatch.setattr(screen, "bot_uptime_seconds", lambda: 30 * 60)
    old = to_iso(now_utc() - timedelta(hours=2))
    fresh = to_iso(now_utc())
    await h.repo.insert_vpn_sample(old, True, 40, "old-node", "sub", None, 1.0)
    await h.repo.insert_vpn_sample(fresh, True, 80, "fresh-node", "sub", None, 1.0)
    await h.text(1, "/start")
    await h.click(1, "s:srv")
    await h.press(1, "Аптайм")
    assert "Замеров: 2" in h.last_text()
    assert h.buttons()["С запуска"] == "adv:boot:n"
    assert "[сутки]" in h.buttons()
    await h.press(1, "С запуска")
    assert "[С запуска]" in h.buttons()
    assert "[сутки]" not in h.buttons()
    assert "За время с запуска бота" in h.last_text()
    assert "Замеров: 1" in h.last_text()
    assert "fresh-node" in h.last_text()
    assert "old-node" not in h.last_text()
    await h.press(1, "всё время")
    assert "[всё время]" in h.buttons()
    assert "[С запуска]" not in h.buttons()
    assert "За всё время" in h.last_text()
    assert "с запуска бота" not in h.last_text()
    assert "old-node" in h.last_text() and "fresh-node" in h.last_text()


async def test_guest_cannot_open_uptime(h):
    await h.text(99, "/start")
    await h.click(99, "ad:vpn")
    assert "Недостаточно прав" in h.session.last("AnswerCallbackQuery").text
