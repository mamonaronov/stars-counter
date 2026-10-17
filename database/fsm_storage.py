"""SQLite FSM storage so drafts survive a process restart."""

from __future__ import annotations

import json
from typing import Any, Mapping

from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from database.database import Database
from utils.time import now_utc, to_iso


def _key_tuple(key: StorageKey) -> tuple:
    return (int(key.bot_id), int(key.chat_id), int(key.user_id), str(key.destiny or "default"))


def _state_value(state: StateType) -> str | None:
    if state is None:
        return None
    return state.state if hasattr(state, "state") else str(state)


class SqliteStorage(BaseStorage):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

    async def _fetchone(self, sql: str, params: tuple) -> Any:
        async with self.db.exclusive():
            async with self.db.conn.execute(sql, params) as cur:
                return await cur.fetchone()

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        bot_id, chat_id, user_id, destiny = _key_tuple(key)
        async with self.db.exclusive():
            async with self.db.conn.execute(
                """
                INSERT INTO fsm_storage (bot_id, chat_id, user_id, destiny, state, data, updated_at)
                VALUES (?, ?, ?, ?, ?, '{}', ?)
                ON CONFLICT(bot_id, chat_id, user_id, destiny) DO UPDATE SET
                    state = excluded.state, updated_at = excluded.updated_at
                """,
                (bot_id, chat_id, user_id, destiny, _state_value(state), to_iso(now_utc())),
            ):
                pass
            await self.db.conn.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        bot_id, chat_id, user_id, destiny = _key_tuple(key)
        row = await self._fetchone(
            "SELECT state FROM fsm_storage WHERE bot_id = ? AND chat_id = ? AND user_id = ? AND destiny = ?",
            (bot_id, chat_id, user_id, destiny),
        )
        return None if row is None else row["state"]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        bot_id, chat_id, user_id, destiny = _key_tuple(key)
        payload = json.dumps(dict(data), ensure_ascii=False)
        async with self.db.exclusive():
            async with self.db.conn.execute(
                """
                INSERT INTO fsm_storage (bot_id, chat_id, user_id, destiny, state, data, updated_at)
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(bot_id, chat_id, user_id, destiny) DO UPDATE SET
                    data = excluded.data, updated_at = excluded.updated_at
                """,
                (bot_id, chat_id, user_id, destiny, payload, to_iso(now_utc())),
            ):
                pass
            await self.db.conn.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        bot_id, chat_id, user_id, destiny = _key_tuple(key)
        row = await self._fetchone(
            "SELECT data FROM fsm_storage WHERE bot_id = ? AND chat_id = ? AND user_id = ? AND destiny = ?",
            (bot_id, chat_id, user_id, destiny),
        )
        if row is None or not row["data"]:
            return {}
        try:
            parsed = json.loads(row["data"])
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def close(self) -> None:
        return None
