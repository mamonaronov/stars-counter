from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import VPN_DB_MAX_BYTES, load_config
from database.vpn_database import sqlite_bundle_bytes
from services.jobs import setup_scheduler, vpn_monitor_job_timeout
from services.vpn_monitor import VpnMonitor, ping_status_line
from tests.conftest import make_config


def test_vpn_monitor_config_defaults(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:test")
    monkeypatch.setenv("SUPERADMIN_TELEGRAM_ID", "1")
    for name in (
        "VPN_MONITOR_ENABLED",
        "VPN_MONITOR_INTERVAL_SECONDS",
        "VPN_MONITOR_TIMEOUT_SECONDS",
        "VPN_DB_PATH",
        "VPN_DB_MAX_MB",
        "MIHOMO_API_SECRET",
        "MIHOMO_API_URL",
        "MIHOMO_PROXY_GROUP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("config.load_dotenv", lambda: None)

    cfg = load_config()
    assert cfg.vpn_monitor_enabled is True
    assert cfg.vpn_monitor_interval_seconds == 10
    assert cfg.vpn_monitor_timeout_seconds == 8
    assert cfg.vpn_db_max_bytes == VPN_DB_MAX_BYTES == 500 * 1024 * 1024
    assert cfg.vpn_db_path.name == "vpn.sqlite3"
    assert cfg.mihomo_api_secret is None
    assert cfg.mihomo_proxy_group == "AUTO"

    monkeypatch.setenv("VPN_MONITOR_ENABLED", "0")
    monkeypatch.setenv("VPN_DB_MAX_MB", "128")
    monkeypatch.setenv("MIHOMO_API_SECRET", "not-a-real-secret")
    cfg = load_config()
    assert cfg.vpn_monitor_enabled is False
    assert cfg.vpn_db_max_bytes == 128 * 1024 * 1024
    assert cfg.mihomo_api_secret == "not-a-real-secret"


async def test_vpn_monitor_job_starts_after_one_interval(tmp_path):
    before = datetime.now(timezone.utc)
    scheduler = AsyncIOScheduler(timezone="UTC")
    config = make_config(tmp_path)
    setup_scheduler(scheduler, bot=object(), repo=object(), db=object(), config=config)
    job = scheduler.get_job("vpn_monitor")
    assert job is not None
    monitor = job.kwargs["monitor"]
    try:
        assert job.next_run_time is not None
        assert job.next_run_time >= before + timedelta(seconds=9)
        retention = scheduler.get_job("vpn_retention")
        assert retention is not None
        assert retention.next_run_time >= before + timedelta(minutes=14)
        assert vpn_monitor_job_timeout(monitor) == 9.0
    finally:
        await monitor._probe_bot.session.close()


async def test_vpn_monitor_tick_writes_separate_db(repo, monkeypatch):
    async def fake_now(config):
        return "s3 | Cyprus | [BL]-01", None

    monkeypatch.setattr("services.vpn_monitor.fetch_auto_now", fake_now)

    class ProbeBot:
        async def get_me(self):
            await asyncio.sleep(0.01)
            return SimpleNamespace(id=1, username="probe")

    class PollingBot:
        async def get_me(self):
            raise AssertionError("ping probe must not use the polling Bot")

    monitor = VpnMonitor(repo.db.config, probe_bot=ProbeBot())
    sample = await monitor.tick(PollingBot(), repo)
    assert sample["ok"] is True
    assert sample["subscription"] == "sub3"
    assert sample["latency_ms"] >= 10
    stored = await repo.latest_vpn_sample()
    assert stored is not None
    assert stored.node_name.startswith("s3 |")
    assert repo.db.vpn_db is not None
    assert repo.db.vpn_db.path.name == "vpn.sqlite3"
    assert repo.db.vpn_db.path != repo.db.path
    async with repo.db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vpn_latency_samples'"
    ) as cur:
        assert await cur.fetchone() is None


async def test_ping_cap_deletes_oldest_samples(repo):
    payload = "x" * 8000
    total = 40
    for i in range(total):
        await repo.insert_vpn_sample(
            f"2026-01-01T00:00:{i:02d}+00:00",
            True,
            20 + i,
            "node",
            "sub1",
            payload,
        )
    await repo.db.vpn_db._checkpoint()
    before = sqlite_bundle_bytes(repo.db.vpn_db.path)
    assert before > 80_000
    cap = before // 3
    deleted = await repo.db.vpn_db.enforce_max_bytes(cap)
    assert deleted > 0
    assert sqlite_bundle_bytes(repo.db.vpn_db.path) <= cap
    assert await repo.count_vpn_samples() < total
    latest = await repo.latest_vpn_sample()
    assert latest is not None
    assert latest.measured_at == f"2026-01-01T00:00:{total - 1:02d}+00:00"
    async with repo.db.vpn_db.conn.execute(
        "SELECT measured_at FROM vpn_latency_samples ORDER BY measured_at ASC LIMIT 1"
    ) as cur:
        oldest = await cur.fetchone()
    assert oldest is not None
    assert str(oldest[0]) != "2026-01-01T00:00:00+00:00"
    again = await repo.db.vpn_db.enforce_max_bytes(cap)
    assert again == 0
    assert sqlite_bundle_bytes(repo.db.vpn_db.path) <= cap


async def test_ping_status_line_reports_latest_sample(repo):
    text = await ping_status_line(repo, repo.db.config)
    assert "ещё нет" in text
    assert "500 МБ" in text
    await repo.insert_vpn_sample(
        "2026-10-15T12:00:00+00:00",
        True,
        142,
        "s1 | node",
        "sub1",
        None,
    )
    text = await ping_status_line(repo, repo.db.config)
    assert "142 мс" in text
    assert "s1 | node" in text
    cfg = replace(repo.db.config, vpn_monitor_enabled=False)
    assert "выключены" in await ping_status_line(repo, cfg)
