"""Logs of the bot and mihomo containers, including earlier starts.

Docker keeps the json-file history of the current container across restarts.
Every line is prefixed with the container name and a timestamp, as in
`docker compose logs -t`. A request can take the whole journal, one run
(the current process, the previous one, or the one before that), or every
line from a chosen moment until now. The engine is reached through its unix
socket. A container that was removed and created again has no older history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from config import Config
from utils.time import format_dt, now_utc, parse_date_text, to_local, zone

logger = logging.getLogger(__name__)

# Telegram rejects documents over 50 MB. Leave room for the multipart wrapper.
_MAX_BYTES = 40 * 1024 * 1024
_FRAME_HEADER = 8
_MAX_FRAME = 16 * 1024 * 1024
_STARTED_AT = re.compile(
    r"^(?P<head>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<frac>\d+))?(?P<tz>Z|[+-]\d{2}:\d{2})$"
)


class DockerError(RuntimeError):
    pass


class DockerContainerNotFound(DockerError):
    def __init__(self, name: str) -> None:
        super().__init__(f"контейнер {name} не найден")
        self.name = name


class LogSpanMissing(DockerError):
    """The saved journal does not contain the requested run or period."""


# A new process writes one of these once. They separate runs inside one journal:
# Docker stores a single StartedAt, and a quiet process can go hours without a line.
RUN_MARKERS: tuple[str, ...] = (
    "Starting attendance bot",
    "Start initial compatible provider",
)

_MISSING_SPAN = {
    "current": "в сохранённом журнале нет строк текущего запуска",
    "previous": "в сохранённом журнале нет прошлого запуска",
    "before_previous": "в сохранённом журнале нет позапрошлого запуска",
}


@dataclass(frozen=True, slots=True)
class ContainerLogFile:
    name: str
    filename: str
    body: bytes
    started_at: datetime | None
    part: int = 1
    parts: int = 1
    error: str | None = None
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class LogQuery:
    """Which part of the saved journal to send. `all` keeps every retained line."""

    mode: str = "all"
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class LogRun:
    text: str
    started_at: datetime | None
    ended_at: datetime | None
    current: bool = False


def parse_started_at(value: str) -> datetime:
    text = value.strip()
    match = _STARTED_AT.fullmatch(text)
    if match is None:
        raise DockerError(f"непонятное время запуска: {value}")
    frac = (match.group("frac") or "0")[:6].ljust(6, "0")
    tz = match.group("tz")
    if tz == "Z":
        tz = "+00:00"
    return datetime.fromisoformat(f"{match.group('head')}.{frac}{tz}").astimezone(timezone.utc)


def started_since(value: str) -> str:
    """Unix seconds of a container start. Kept for callers that still format it."""
    return str(int(parse_started_at(value).timestamp()))


def parse_log_since(value: str, tz_name: str, *, today: date | None = None) -> datetime:
    """A local date, optionally with a time, as the UTC start of a log slice."""
    raw = " ".join((value or "").split())
    time_match = re.fullmatch(r"(.+?)[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    try:
        if time_match:
            day = parse_date_text(time_match.group(1), today=today)
            hour = int(time_match.group(2))
            minute = int(time_match.group(3))
            second = int(time_match.group(4) or 0)
            if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
                raise ValueError("time")
        else:
            day = parse_date_text(raw, today=today)
            hour = minute = second = 0
    except ValueError as exc:
        raise ValueError("date") from exc
    local = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=zone(tz_name))
    return local.astimezone(timezone.utc)


def split_log_runs(
    text: str,
    *,
    started_at: datetime | None,
    markers: tuple[str, ...] = RUN_MARKERS,
) -> list[LogRun]:
    """Split a timestamped journal into process runs, oldest first.

    A run starts at a startup marker or at the first line of the process Docker
    currently reports as running. Lines without a timestamp stay with the run
    that precedes them. The last run is marked current only when the journal
    actually reaches the running process.
    """
    chunks: list[list[str]] = []
    buf: list[str] = []
    current_opened = False
    # Docker's StartedAt and the first log line can disagree by a fraction of a second.
    current_from = started_at - timedelta(seconds=1) if started_at is not None else None

    def flush() -> None:
        nonlocal buf
        if any(line.strip() for line in buf):
            chunks.append(buf)
        buf = []

    for line in text.splitlines():
        stamped, rest = _split_stamp(line)
        marker = any(marker in rest for marker in markers)
        enters_current = (
            not current_opened
            and current_from is not None
            and stamped is not None
            and stamped >= current_from
            and bool(buf)
        )
        if (marker or enters_current) and buf:
            flush()
        if enters_current or (current_from is not None and stamped is not None and stamped >= current_from):
            current_opened = True
        buf.append(line)
    flush()
    runs = [_run_from_lines(lines) for lines in chunks]
    if not runs:
        return []
    has_current = started_at is None or current_opened or runs[-1].ended_at is None
    if not has_current:
        return runs
    last = runs[-1]
    runs[-1] = LogRun(last.text, last.started_at, last.ended_at, current=True)
    return runs


def filter_log_since(text: str, since: datetime) -> str:
    """Lines at or after `since`. A line without a timestamp follows the previous one."""
    kept: list[str] = []
    keep = False
    for line in text.splitlines():
        stamped, _rest = _split_stamp(line)
        if stamped is not None:
            keep = stamped >= since
        if keep:
            kept.append(line)
    return "\n".join(kept)


def query_label(query: LogQuery, tz_name: str) -> str:
    if query.mode == "since" and query.since is not None:
        return f"с {format_dt(query.since, tz_name)} до сейчас"
    return {
        "all": "все сохранённые",
        "current": "текущий запуск",
        "previous": "прошлый запуск",
        "before_previous": "позапрошлый запуск",
    }.get(query.mode, query.mode)


def _split_stamp(line: str) -> tuple[datetime | None, str]:
    head, sep, rest = line.partition(" ")
    if not sep:
        return None, line
    try:
        return parse_started_at(head), rest
    except DockerError:
        return None, line


def _run_from_lines(lines: list[str]) -> LogRun:
    stamps = [stamped for stamped, _rest in (_split_stamp(line) for line in lines) if stamped is not None]
    return LogRun("\n".join(lines), stamps[0] if stamps else None, stamps[-1] if stamps else None)


def _scope_label(mode: str, run: LogRun, tz_name: str) -> str:
    start = format_dt(run.started_at, tz_name) if run.started_at else "—"
    end = format_dt(run.ended_at, tz_name) if run.ended_at else "—"
    if mode == "current":
        return f"текущий запуск с {start}"
    if mode == "previous":
        return f"прошлый запуск {start} — {end}"
    return f"позапрошлый запуск {start} — {end}"


def _pick_run(runs: list[LogRun], mode: str) -> LogRun:
    current_at = next((index for index, run in enumerate(runs) if run.current), None)
    if mode == "current":
        if current_at is None:
            raise LogSpanMissing(_MISSING_SPAN[mode])
        return runs[current_at]
    past = runs[:current_at] if current_at is not None else runs
    offset = -1 if mode == "previous" else -2
    if len(past) < abs(offset):
        raise LogSpanMissing(_MISSING_SPAN[mode])
    return past[offset]


def apply_log_query(
    text: str,
    *,
    started_at: datetime | None,
    query: LogQuery,
    tz_name: str,
) -> tuple[str, str | None, datetime | None]:
    """Return the selected text, its caption scope, and the time shown as the start."""
    if query.mode == "all":
        return text, None, started_at
    if query.mode == "since":
        if query.since is None:
            raise DockerError("не указана дата начала")
        body = filter_log_since(text, query.since)
        if not body.strip():
            raise LogSpanMissing(f"С {format_dt(query.since, tz_name)} в журнале нет строк.")
        return body, f"с {format_dt(query.since, tz_name)} до сейчас", query.since
    if query.mode not in _MISSING_SPAN:
        raise DockerError("не понял, какой запуск нужен")
    run = _pick_run(split_log_runs(text, started_at=started_at), query.mode)
    return run.text, _scope_label(query.mode, run, tz_name), run.started_at or started_at


class _LogBuffer:
    """Demux the Docker log stream. The full text is kept; a later step splits it."""

    def __init__(self) -> None:
        self.pending = bytearray()
        self.text = bytearray()
        self.saw_frame = False
        self.plain = False

    def feed(self, chunk: bytes) -> None:
        if self.plain:
            self._append(chunk)
            return
        self.pending.extend(chunk)
        while True:
            if len(self.pending) < _FRAME_HEADER:
                return
            stream = self.pending[0]
            size = int.from_bytes(self.pending[4:8], "big")
            if stream not in (0, 1, 2) or size > _MAX_FRAME:
                if self.saw_frame:
                    self.pending.clear()
                    return
                self.plain = True
                self._append(bytes(self.pending))
                self.pending.clear()
                return
            if len(self.pending) < _FRAME_HEADER + size:
                return
            payload = bytes(self.pending[_FRAME_HEADER : _FRAME_HEADER + size])
            del self.pending[: _FRAME_HEADER + size]
            self.saw_frame = True
            if stream != 0:
                self._append(payload)

    def finish(self) -> str:
        if not self.saw_frame and self.pending:
            self._append(bytes(self.pending))
            self.pending.clear()
        return self.text.decode("utf-8", errors="replace")

    def _append(self, payload: bytes) -> None:
        self.text.extend(payload)


def decode_docker_logs(raw: bytes) -> str:
    buffer = _LogBuffer()
    buffer.feed(raw)
    return buffer.finish()


def compose_log_text(container: str, text: str) -> str:
    prefix = f"{container}  | "
    lines = text.splitlines() or ["(нет сохранённых строк)"]
    return "".join(f"{prefix}{line}\n" for line in lines)


def log_filename(container: str, when: datetime, tz_name: str) -> str:
    stamp = to_local(when, tz_name).strftime("%d-%m-%Y_%H-%M-%S")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", container).strip("._") or "container"
    return f"{safe}_{stamp}.log"


class DockerEngine:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self._api: str | None = None

    async def ensure(self) -> None:
        if not self.socket_path.exists():
            raise DockerError("Сокет Docker не подключён к контейнеру бота.")
        await self._api_version()

    async def inspect(self, name: str) -> dict:
        status, payload = await self._json("GET", f"/containers/{name}/json")
        if status == 404:
            raise DockerContainerNotFound(name)
        if status != 200 or not isinstance(payload, dict):
            raise DockerError(f"Docker не отдал сведения о {name} ({status})")
        return payload

    async def logs(self, name: str) -> str:
        params = {"stdout": "1", "stderr": "1", "timestamps": "1"}
        status, text = await self._stream("GET", f"/containers/{name}/logs", params=params, timeout=120)
        if status == 404:
            raise DockerContainerNotFound(name)
        if status != 200:
            raise DockerError(f"Docker не отдал логи {name} ({status}): {text[:200]}")
        return text

    async def _api_version(self) -> str:
        if self._api is not None:
            return self._api
        status, payload = await self._json("GET", "/version", versioned=False)
        api = payload.get("ApiVersion") if status == 200 and isinstance(payload, dict) else None
        if not api or not re.fullmatch(r"\d+\.\d+", str(api)):
            raise DockerError("Не удалось узнать версию Docker API")
        self._api = str(api)
        return self._api

    async def _json(self, method: str, path: str, *, versioned: bool = True) -> tuple[int, object]:
        status, raw = await self._read(method, path, versioned=versioned, timeout=15)
        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise DockerError("Docker ответил не JSON") from exc

    async def _open(self, method: str, path: str, *, params: dict | None, versioned: bool, timeout: float):
        if not self.socket_path.exists():
            raise DockerError("Сокет Docker не подключён к контейнеру бота.")
        api = await self._api_version() if versioned else ""
        url = f"http://docker{('/v' + api) if api else ''}{path}"
        connector = aiohttp.UnixConnector(path=str(self.socket_path))
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        session = aiohttp.ClientSession(connector=connector, timeout=client_timeout)
        try:
            response = await session.request(method, url, params=params)
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            await session.close()
            raise DockerError(
                "Нет доступа к сокету Docker. Проверьте, что он подключён и доступен пользователю бота."
            ) from exc
        return session, response

    async def _read(self, method: str, path: str, *, versioned: bool, timeout: float) -> tuple[int, bytes]:
        try:
            session, response = await self._open(method, path, params=None, versioned=versioned, timeout=timeout)
        except DockerError:
            raise
        try:
            return response.status, await response.read()
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise DockerError(
                "Нет доступа к сокету Docker. Проверьте, что он подключён и доступен пользователю бота."
            ) from exc
        finally:
            response.release()
            await session.close()

    async def _stream(
        self,
        method: str,
        path: str,
        *,
        params: dict,
        timeout: float,
    ) -> tuple[int, str]:
        try:
            session, response = await self._open(method, path, params=params, versioned=True, timeout=timeout)
        except DockerError:
            raise
        status = response.status
        buffer = _LogBuffer()
        try:
            async for chunk in response.content.iter_chunked(65536):
                buffer.feed(chunk)
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise DockerError(
                "Нет доступа к сокету Docker. Проверьте, что он подключён и доступен пользователю бота."
            ) from exc
        finally:
            response.release()
            await session.close()
        return status, buffer.finish()


def _zip_split_size(volume_size: int) -> str:
    if volume_size % (1024 * 1024) == 0:
        return f"{volume_size // (1024 * 1024)}m"
    if volume_size % 1024 == 0:
        return f"{volume_size // 1024}k"
    return str(volume_size)


def _volume_order(path: Path) -> tuple[int, int]:
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return (1, 0)
    number = suffix[2:] if suffix.startswith(".z") else ""
    if number.isdigit():
        return (0, int(number))
    return (2, 0)


def write_split_zip(payload: bytes, entry_name: str, volume_size: int) -> list[tuple[str, bytes]]:
    """Info-ZIP volumes: name.z01, name.z02, …, name.zip. Together they unpack into entry_name."""
    if shutil.which("zip") is None:
        raise DockerError("Не найдена команда zip, большой журнал не разрезать.")
    stem = entry_name[:-4] if entry_name.endswith(".log") else entry_name
    with tempfile.TemporaryDirectory(prefix="container-log-") as tmp:
        folder = Path(tmp)
        source = folder / Path(entry_name).name
        source.write_bytes(payload)
        archive = folder / f"{stem}.zip"
        result = subprocess.run(
            ["zip", "-q", "-s", _zip_split_size(volume_size), "-j", str(archive), str(source)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"код {result.returncode}"
            raise DockerError(f"Не удалось собрать архив: {detail}")
        paths = sorted(folder.glob(f"{stem}.z*"), key=_volume_order)
        if not paths:
            raise DockerError("zip не создал архив")
        return [(path.name, path.read_bytes()) for path in paths]


def _one(
    name: str,
    body: bytes,
    filename: str,
    *,
    started: datetime | None,
    error: str | None,
    part: int = 1,
    parts: int = 1,
    scope: str | None = None,
) -> ContainerLogFile:
    return ContainerLogFile(
        name=name,
        filename=filename,
        body=body,
        started_at=started,
        part=part,
        parts=parts,
        error=error,
        scope=scope,
    )


async def _package(
    name: str,
    text: str,
    *,
    started: datetime | None,
    error: str | None,
    tz_name: str,
    scope: str | None = None,
) -> list[ContainerLogFile]:
    filename = log_filename(name, now_utc(), tz_name)
    if error is not None:
        body = compose_log_text(name, error).encode("utf-8")
        return [_one(name, body, filename, started=started, error=error)]
    body = compose_log_text(name, text).encode("utf-8")
    if len(body) <= _MAX_BYTES:
        return [_one(name, body, filename, started=started, error=None, scope=scope)]
    volumes = await asyncio.to_thread(write_split_zip, body, filename, _MAX_BYTES)
    total = len(volumes)
    return [
        _one(name, payload, volume_name, started=started, error=None, part=index, parts=total, scope=scope)
        for index, (volume_name, payload) in enumerate(volumes, start=1)
    ]


async def read_runtime_logs(
    config: Config,
    engine: DockerEngine | None = None,
    *,
    query: LogQuery | None = None,
) -> list[ContainerLogFile]:
    client = engine or DockerEngine(config.docker_socket)
    await client.ensure()
    selected = query or LogQuery()
    names = (config.bot_container_name, config.mihomo_container_name)
    files: list[ContainerLogFile] = []
    for name in names:
        files.extend(await _read_one(client, name, config.timezone, selected))
    return files


async def _read_one(
    engine: DockerEngine,
    name: str,
    tz_name: str,
    query: LogQuery,
) -> list[ContainerLogFile]:
    try:
        info = await engine.inspect(name)
        started_raw = str((info.get("State") or {}).get("StartedAt") or "")
        started = parse_started_at(started_raw) if started_raw else None
        text = await engine.logs(name)
        body, scope, shown_start = apply_log_query(text, started_at=started, query=query, tz_name=tz_name)
    except DockerContainerNotFound:
        return await _package(name, "", started=None, error=f"контейнер {name} не найден", tz_name=tz_name)
    except LogSpanMissing as exc:
        return await _package(name, "", started=None, error=str(exc), tz_name=tz_name)
    except DockerError as exc:
        logger.warning("Container logs for %s failed: %s", name, exc)
        return await _package(name, "", started=None, error=str(exc), tz_name=tz_name)
    return await _package(name, body, started=shown_start, error=None, tz_name=tz_name, scope=scope)


def log_caption(item: ContainerLogFile, tz_name: str) -> str:
    if item.error:
        return f"📋 {item.name}\n{item.error}"
    when = format_dt(item.started_at, tz_name) if item.started_at else "—"
    if item.scope:
        lines = [f"📋 {item.name}", item.scope]
    else:
        lines = [
            f"📋 {item.name}",
            "все сохранённые запуски этого контейнера",
            f"текущий запуск: {when}",
        ]
    if item.parts > 1:
        lines.append(
            f"Часть {item.part} из {item.parts}. "
            "Сложите все части в одну папку и откройте файл .zip в 7-Zip: получится один лог."
        )
    elif item.filename.endswith(".zip"):
        lines.append("Журнал в архиве: откройте его, внутри один файл лога.")
    return "\n".join(lines)
