from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from config import Config
from services.container_logs import (
    DockerContainerNotFound,
    _LogBuffer,
    _package,
    compose_log_text,
    decode_docker_logs,
    log_caption,
    parse_started_at,
    read_runtime_logs,
    started_since,
    write_split_zip,
)


def _frame(stream: int, payload: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def test_docker_stream_joins_stdout_and_stderr_and_skips_stdin():
    raw = _frame(1, b"out\n") + _frame(0, b"hidden\n") + _frame(2, b"err\n")
    split = raw[:6]
    buffer = _LogBuffer()
    buffer.feed(split)
    buffer.feed(raw[6:])
    assert buffer.finish() == "out\nerr\n"
    assert decode_docker_logs(b'{"ts":"2026"}\n') == '{"ts":"2026"}\n'


def test_started_at_accepts_nanoseconds():
    started = parse_started_at("2026-11-04T20:00:00.123456789Z")
    assert started == datetime(2026, 11, 4, 20, 0, 0, 123456, tzinfo=timezone.utc)
    assert started_since("2026-11-04T20:00:00.123456789Z") == str(int(started.timestamp()))


class _Engine:
    def __init__(self, missing: str | None = None) -> None:
        self.missing = missing
        self.since: dict[str, str] = {}

    async def ensure(self) -> None:
        return None

    async def inspect(self, name: str) -> dict:
        if name == self.missing:
            raise DockerContainerNotFound(name)
        return {"State": {"StartedAt": "2026-11-04T20:00:00.123456789Z"}}

    async def logs(self, name: str, since: str) -> str:
        self.since[name] = since
        return "hello\n"


async def test_runtime_logs_are_two_files_since_container_start(config: Config):
    engine = _Engine()
    cfg = replace(config, timezone="Europe/Moscow")
    files = await read_runtime_logs(cfg, engine)
    assert [item.name for item in files] == ["stars-counter-bot", "mihomo-proxy"]
    assert engine.since["stars-counter-bot"] == started_since("2026-11-04T20:00:00.123456789Z")
    assert files[0].body.decode() == "stars-counter-bot  | hello\n"
    assert files[0].filename.endswith(".log")
    assert "stars-counter-bot_" in files[0].filename
    assert "04.11.2026 23:00" in log_caption(files[0], "Europe/Moscow")
    assert files[0].error is None


async def test_missing_mihomo_still_returns_a_second_file(config: Config):
    files = await read_runtime_logs(config, _Engine(missing="mihomo-proxy"))
    assert files[0].error is None
    assert b"hello" in files[0].body
    assert files[1].error == "контейнер mihomo-proxy не найден"
    assert "mihomo-proxy  | контейнер mihomo-proxy не найден" in files[1].body.decode()
    assert "не найден" in log_caption(files[1], config.timezone)


def test_split_zip_parts_unpack_into_the_original_file(tmp_path):
    if shutil.which("zip") is None or shutil.which("7z") is None:
        pytest.skip("нужны zip и 7z")
    payload = os.urandom(300_000)
    parts = write_split_zip(payload, "journal.log", 64 * 1024)
    assert len(parts) > 1
    assert parts[0][0] == "journal.z01"
    assert parts[-1][0] == "journal.zip"
    assert all(len(body) <= 64 * 1024 for _, body in parts)
    for name, body in parts:
        (tmp_path / name).write_bytes(body)
    out = tmp_path / "out"
    subprocess.run(["7z", "x", str(tmp_path / "journal.zip"), f"-o{out}"], check=True, capture_output=True)
    assert (out / "journal.log").read_bytes() == payload


async def test_oversized_log_is_sent_as_split_zip(monkeypatch, tmp_path):
    if shutil.which("zip") is None or shutil.which("7z") is None:
        pytest.skip("нужны zip и 7z")
    monkeypatch.setattr("services.container_logs._MAX_BYTES", 64 * 1024)
    text = os.urandom(250_000).decode("latin-1")
    files = await _package("bot", text, started=None, error=None, tz_name="Europe/Moscow")
    assert len(files) > 1
    assert files[0].part == 1
    assert files[-1].part == files[-1].parts == len(files)
    assert "Часть 1 из" in log_caption(files[0], "Europe/Moscow")
    for item in files:
        (tmp_path / item.filename).write_bytes(item.body)
    out = tmp_path / "out"
    subprocess.run(["7z", "x", str(tmp_path / files[-1].filename), f"-o{out}"], check=True, capture_output=True)
    restored = next(out.rglob("*.log")).read_bytes()
    assert restored == compose_log_text("bot", text).encode("utf-8")
