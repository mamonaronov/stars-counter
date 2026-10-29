"""Follow a basic group when Telegram upgrades it to a supergroup.

The upgrade keeps the members and the history and issues a new chat id.
Bindings of the markers group and the backup group are rewritten. A short map
is also stored, so an id that still lives only in MARKERS_GROUP_ID resolves
to the supergroup.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramMigrateToChat, TelegramNetworkError

from database.database import Database

logger = logging.getLogger(__name__)

MIGRATIONS_KEY = "chat_id_migrations"
_HOPS = 8

T = TypeVar("T")


def parse_chat_migrations(raw: str | None) -> dict[int, int]:
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid %s value", MIGRATIONS_KEY)
        return {}
    if not isinstance(data, dict):
        return {}
    mapping: dict[int, int] = {}
    for key, value in data.items():
        try:
            old_id = int(key)
            new_id = int(value)
        except (TypeError, ValueError):
            continue
        if old_id != new_id:
            mapping[old_id] = new_id
    return mapping


def follow_chat_id(mapping: dict[int, int], chat_id: int) -> int:
    seen: set[int] = set()
    current = chat_id
    for _ in range(_HOPS):
        nxt = mapping.get(current)
        if nxt is None or nxt == current or nxt in seen:
            return current
        seen.add(current)
        current = nxt
    return current


def _dump(mapping: dict[int, int]) -> str:
    return json.dumps({str(old): new for old, new in sorted(mapping.items())}, separators=(",", ":"))


def _bound_chat_keys() -> tuple[str, str]:
    from services.group_export import KEY_GROUP
    from services.telegram_backup import CHAT_ID_KEY

    return KEY_GROUP, CHAT_ID_KEY


async def resolve_chat_id(db: Database, chat_id: int | None) -> int | None:
    if chat_id is None:
        return None
    mapping = parse_chat_migrations(await db.get_system(MIGRATIONS_KEY))
    return follow_chat_id(mapping, chat_id)


async def _rewrite_bound_chats(db: Database, mapping: dict[int, int]) -> None:
    for key in _bound_chat_keys():
        raw = await db.get_system(key)
        if not raw or not raw.strip():
            continue
        try:
            current = int(raw.strip())
        except ValueError:
            continue
        resolved = follow_chat_id(mapping, current)
        if resolved != current:
            await db.set_system(key, str(resolved))
            logger.info("Updated %s from %s to %s after group upgrade", key, current, resolved)


async def remember_chat_migration(db: Database, old_id: int, new_id: int) -> int:
    """Store old → new and point saved group ids at the supergroup. Returns the id to use."""
    if old_id == new_id:
        return old_id
    mapping = parse_chat_migrations(await db.get_system(MIGRATIONS_KEY))
    mapping[old_id] = new_id
    if mapping.get(new_id) == old_id:
        mapping.pop(new_id, None)
    final = follow_chat_id(mapping, old_id)
    serialized = _dump(mapping)
    if await db.get_system(MIGRATIONS_KEY) != serialized:
        await db.set_system(MIGRATIONS_KEY, serialized)
        logger.warning("Telegram group %s was upgraded to supergroup %s", old_id, final)
    await _rewrite_bound_chats(db, mapping)
    return final


async def note_group_migration(
    db: Database,
    *,
    chat_id: int,
    migrate_to_chat_id: int | None,
    migrate_from_chat_id: int | None,
) -> int | None:
    """Apply a service message about a group becoming a supergroup."""
    if migrate_to_chat_id is not None:
        return await remember_chat_migration(db, chat_id, int(migrate_to_chat_id))
    if migrate_from_chat_id is not None:
        return await remember_chat_migration(db, int(migrate_from_chat_id), chat_id)
    return None


async def send_following_migration(
    db: Database,
    chat_id: int,
    send: Callable[[int], Awaitable[T]],
) -> tuple[T, int]:
    """Send to chat_id. If Telegram reports an upgrade, save the new id and send once more."""
    try:
        return await send(chat_id), chat_id
    except TelegramMigrateToChat as exc:
        new_id = int(exc.migrate_to_chat_id)
        if new_id == chat_id:
            raise
        adopted = await remember_chat_migration(db, chat_id, new_id)
        return await send(adopted), adopted


async def adopt_if_migrated(bot: Bot, db: Database, chat_id: int) -> bool:
    """Keep a group binding when the bot left because the group was upgraded.

    True means the caller must not drop the binding. A network failure is treated
    the same way: the next send will either follow the upgrade or see a real kick.
    """
    try:
        await bot.get_chat(chat_id)
    except TelegramMigrateToChat as exc:
        new_id = int(exc.migrate_to_chat_id)
        if new_id != chat_id:
            await remember_chat_migration(db, chat_id, new_id)
        return True
    except (TelegramNetworkError, TimeoutError):
        logger.warning("Could not check whether chat %s was upgraded; keeping the saved binding", chat_id)
        return True
    except Exception:
        return False
    return False
