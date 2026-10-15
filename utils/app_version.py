"""Identity of the running build: commit baked into the image or passed via env."""

from __future__ import annotations

import os
import re
from pathlib import Path

BUILD_GIT_PATH = Path("/app/.build-git")
_REPO_BUILD_GIT = Path(__file__).resolve().parent.parent / ".build-git"


def parse_build_git(text: str) -> tuple[str, str]:
    commit = "unknown"
    title = "unknown"
    for line in text.splitlines():
        if line.startswith("commit="):
            commit = line[7:].strip() or "unknown"
        elif line.startswith("title="):
            title = line[6:].strip() or "unknown"
    return commit, title


def _from_file(path: Path) -> tuple[str, str] | None:
    try:
        if path.is_file():
            return parse_build_git(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return None


def app_build_identity() -> tuple[str, str]:
    commit = os.getenv("APP_GIT_COMMIT", "").strip()
    title = os.getenv("APP_GIT_COMMIT_TITLE", "").strip()
    if commit in {"", "unknown"} or title in {"", "unknown"}:
        parsed = _from_file(BUILD_GIT_PATH) or _from_file(_REPO_BUILD_GIT)
        if parsed:
            file_commit, file_title = parsed
            if commit in {"", "unknown"}:
                commit = file_commit
            if title in {"", "unknown"}:
                title = file_title
    return commit or "unknown", title or "unknown"


def slugify_commit_title(title: str, max_len: int = 40) -> str:
    text = " ".join(title.split())
    text = re.sub(r"[^\w.+-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-._")
    return text or "unknown"
