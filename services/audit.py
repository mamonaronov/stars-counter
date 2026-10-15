"""Audit log: who did what, when, with which rights."""

from __future__ import annotations

import json
import logging
from typing import Any

from database.models import AuditEntry
from database.queries import Repo
from services.roles import Actor, ROLE_TITLES
from utils.formatting import esc
from utils.time import format_dt_iso

logger = logging.getLogger(__name__)

ACTION_TITLES: dict[str, str] = {
    "attendance.mark": "отметил присутствие",
    "attendance.unmark": "снял отметку присутствия",
    "event.create": "создал занятие",
    "event.update": "изменил занятие",
    "event.delete": "удалил занятие",
    "month.close": "закрыл месяц",
    "month.reopen": "открыл месяц обратно",
    "month.move": "перенёс конец месяца",
    "month.rename": "переименовал месяц",
    "template.create": "создал шаблон",
    "template.update": "изменил шаблон",
    "template.delete": "удалил шаблон",
    "student.add": "добавил студента",
    "student.import": "импортировал студентов",
    "student.update": "изменил данные студента",
    "student.remove": "удалил студента",
    "student.restore": "восстановил студента",
    "student.unlink": "отвязал Telegram студента",
    "verification.link": "создал ссылку верификации",
    "verification.done": "подтвердил аккаунт студента",
    "role.assign": "назначил роль",
    "role.revoke": "снял роль",
    "role.transfer": "передал роль",
    "excuse.add": "добавил больничный/отпрос",
    "excuse.cancel": "отменил больничный/отпрос",
    "request.create": "отправил запрос отмечающему",
    "request.approve": "одобрил запрос",
    "request.reject": "отклонил запрос",
    "report.export": "выгрузил отчёт",
    "backup.manual": "сделал бэкап",
}


async def audit(
    repo: Repo,
    actor: Actor,
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    **details: Any,
) -> None:
    try:
        await repo.add_audit(
            actor_telegram_id=actor.telegram_id,
            actor_student_id=actor.student.id if actor.student else None,
            actor_name=actor.display_name,
            actor_role=actor.role_name,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details={k: v for k, v in details.items() if v is not None} or None,
        )
    except Exception:
        logger.exception("Audit write failed for %s", action)


def _details_text(details: str | None) -> str:
    if not details:
        return ""
    try:
        data = json.loads(details)
    except (TypeError, ValueError):
        return esc(details)
    if not isinstance(data, dict):
        return esc(details)
    parts = [f"{esc(key)}: {esc(value)}" for key, value in data.items()]
    return "; ".join(parts)


def format_audit_entry(entry: AuditEntry, tz_name: str) -> str:
    when = format_dt_iso(entry.created_at, tz_name)
    role = ROLE_TITLES.get(entry.actor_role, entry.actor_role)
    action = ACTION_TITLES.get(entry.action, entry.action)
    head = f"<b>{when}</b> — {esc(entry.actor_name or entry.actor_telegram_id)} ({esc(role)}) {esc(action)}"
    tail = _details_text(entry.details)
    return f"{head}\n<i>{tail}</i>" if tail else head
