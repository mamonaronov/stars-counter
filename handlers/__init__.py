"""Router assembly. One dispatcher, one bot."""

from __future__ import annotations

from aiogram import Router

from handlers import (
    attendance,
    audit,
    events,
    excuses,
    fallback,
    months,
    requests,
    roles,
    service,
    start,
    students,
    templates,
    wipe,
)


def setup_routers() -> Router:
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(attendance.router)
    root.include_router(events.router)
    root.include_router(templates.router)
    root.include_router(months.router)
    root.include_router(requests.router)
    root.include_router(excuses.router)
    root.include_router(students.router)
    root.include_router(roles.router)
    root.include_router(audit.router)
    root.include_router(service.router)
    root.include_router(wipe.router)
    root.include_router(fallback.router)
    return root
