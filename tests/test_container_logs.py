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
    LogQuery,
    _LogBuffer,
    _package,
    compose_log_text,
    decode_docker_logs,
    log_caption,
    parse_log_since,
    parse_started_at,
    read_runtime_logs,
    split_log_runs,
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
        self.logged: list[str] = []

    async def ensure(self) -> None:
        return None

    async def inspect(self, name: str) -> dict:
        if name == self.missing:
            raise DockerContainerNotFound(name)
        return {"State": {"StartedAt": "2026-11-04T20:00:00.123456789Z"}}

    async def logs(self, name: str) -> str:
        self.logged.append(name)
        return "hello\n"


async def test_runtime_logs_include_earlier_starts_of_the_same_container(config: Config):
    engine = _Engine()
    cfg = replace(config, timezone="Europe/Moscow")
    files = await read_runtime_logs(cfg, engine)
    assert [item.name for item in files] == ["stars-counter-bot", "mihomo-proxy"]
    assert engine.logged == ["stars-counter-bot", "mihomo-proxy"]
    assert files[0].body.decode() == "stars-counter-bot  | hello\n"
    assert files[0].filename.endswith(".log")
    assert "stars-counter-bot_" in files[0].filename
    caption = log_caption(files[0], "Europe/Moscow")
    assert "все сохранённые запуски" in caption
    assert "04.11.2026 23:00" in caption
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


_JOURNAL = "\n".join(
    [
        "2026-11-01T10:00:00.000000Z Starting attendance bot",
        "2026-11-01T10:05:00.000000Z alpha",
        "stuck-with-alpha",
        "2026-11-02T10:00:00.000000Z Starting attendance bot",
        "2026-11-02T11:00:00.000000Z beta",
        "2026-11-03T10:00:00.000000Z Starting attendance bot",
        "2026-11-03T12:00:00.000000Z gamma",
    ]
)
_CURRENT_START = "2026-11-03T10:00:00.000000000Z"


class _TextEngine(_Engine):
    def __init__(self, text: str, started: str = _CURRENT_START) -> None:
        super().__init__()
        self.text = text
        self.started = started

    async def inspect(self, name: str) -> dict:
        if name == self.missing:
            raise DockerContainerNotFound(name)
        return {"State": {"StartedAt": self.started}}

    async def logs(self, name: str) -> str:
        self.logged.append(name)
        return self.text


def test_runs_split_on_startup_marker_and_keep_unstamped_lines():
    runs = split_log_runs(_JOURNAL, started_at=parse_started_at(_CURRENT_START))
    assert [run.current for run in runs] == [False, False, True]
    assert "alpha" in runs[0].text and "stuck-with-alpha" in runs[0].text
    assert "beta" in runs[1].text and "alpha" not in runs[1].text
    assert "gamma" in runs[2].text and "beta" not in runs[2].text


def test_started_at_splits_a_journal_without_markers():
    text = "\n".join(
        [
            "2026-11-01T10:00:00.000000Z first",
            "2026-11-02T10:00:00.000000Z second",
            "2026-11-03T10:00:01.000000Z third",
        ]
    )
    runs = split_log_runs(text, started_at=parse_started_at(_CURRENT_START))
    assert len(runs) == 2
    assert runs[0].current is False and "first" in runs[0].text and "second" in runs[0].text
    assert runs[1].current is True and runs[1].text.endswith("third")


def test_journal_that_ends_before_the_current_process_has_no_current_run():
    text = "\n".join(
        [
            "2026-11-01T10:00:00.000000Z Starting attendance bot",
            "2026-11-01T10:05:00.000000Z alpha",
        ]
    )
    runs = split_log_runs(text, started_at=parse_started_at(_CURRENT_START))
    assert len(runs) == 1
    assert runs[0].current is False


def test_parse_log_since_reads_local_date_and_time():
    moment = parse_log_since("04.11.2026 15:30", "Europe/Moscow")
    assert moment == datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc)
    yesterday = parse_log_since("вчера", "Europe/Moscow", today=datetime(2026, 11, 4).date())
    assert yesterday == datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_log_since("не дата", "Europe/Moscow")


async def test_previous_and_before_previous_runs_are_packaged(config: Config):
    engine = _TextEngine(_JOURNAL)
    cfg = replace(config, timezone="Europe/Moscow")
    previous = await read_runtime_logs(cfg, engine, query=LogQuery(mode="previous"))
    body = previous[0].body.decode()
    assert "beta" in body and "alpha" not in body and "gamma" not in body
    caption = log_caption(previous[0], "Europe/Moscow")
    assert "прошлый запуск 02.11.2026 13:00 — 02.11.2026 14:00" in caption

    older = await read_runtime_logs(cfg, _TextEngine(_JOURNAL), query=LogQuery(mode="before_previous"))
    older_body = older[0].body.decode()
    assert "alpha" in older_body and "stuck-with-alpha" in older_body
    assert "beta" not in older_body and "gamma" not in older_body
    assert "позапрошлый запуск" in log_caption(older[0], "Europe/Moscow")


async def test_since_keeps_lines_from_that_moment_until_now(config: Config):
    since = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)
    files = await read_runtime_logs(
        replace(config, timezone="Europe/Moscow"),
        _TextEngine(_JOURNAL),
        query=LogQuery(mode="since", since=since),
    )
    body = files[0].body.decode()
    assert "beta" in body and "gamma" in body
    assert "alpha" not in body and "stuck-with-alpha" not in body
    assert "с 02.11.2026 14:00 до сейчас" in log_caption(files[0], "Europe/Moscow")


async def test_all_saved_logs_stay_unfiltered(config: Config):
    files = await read_runtime_logs(replace(config, timezone="Europe/Moscow"), _TextEngine(_JOURNAL))
    body = files[0].body.decode()
    assert "alpha" in body and "beta" in body and "gamma" in body
    caption = log_caption(files[0], "Europe/Moscow")
    assert "все сохранённые запуски" in caption
    assert "03.11.2026 13:00" in caption


async def test_missing_run_is_an_error_file_and_the_other_container_still_returns(config: Config):
    text = "\n".join(
        [
            "2026-11-03T10:00:00.000000Z Starting attendance bot",
            "2026-11-03T12:00:00.000000Z gamma",
        ]
    )
    files = await read_runtime_logs(config, _TextEngine(text), query=LogQuery(mode="previous"))
    assert files[0].error == "в сохранённом журнале нет прошлого запуска"
    assert files[1].error == "в сохранённом журнале нет прошлого запуска"
    current = await read_runtime_logs(config, _TextEngine(text), query=LogQuery(mode="current"))
    assert current[0].error is None and "gamma" in current[0].body.decode()


async def test_restart_without_new_lines_offers_the_previous_journal(config: Config):
    text = "\n".join(
        [
            "2026-11-01T10:00:00.000000Z Starting attendance bot",
            "2026-11-01T10:05:00.000000Z alpha",
        ]
    )
    files = await read_runtime_logs(config, _TextEngine(text), query=LogQuery(mode="current"))
    assert files[0].error == "в сохранённом журнале нет строк текущего запуска"
    previous = await read_runtime_logs(config, _TextEngine(text), query=LogQuery(mode="previous"))
    assert previous[0].error is None and "alpha" in previous[0].body.decode()
