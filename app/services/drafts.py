"""草稿应用服务:负责草稿 CRUD、修订号控制与草稿/发布分离。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import Account, Note, SessionLocal, log
from .errors import (
    AccountNotFoundError,
    DraftNotEditableError,
    NoteNotFoundError,
    RevisionConflictError,
)


def create_draft(
    account_id: int,
    title: str = "",
    content: str = "",
    note_type: str = "image",
    media_paths: list[str] | None = None,
    tags: list[str] | None = None,
    source: str = "web",
    session: Session | None = None,
) -> Note:
    """创建草稿:允许无媒体或无完整标题,严格置为 draft,绝不自动入队发布。"""
    title = (title or "").strip()
    if len(title) > 20:
        raise ValueError(f"标题最多20字(当前{len(title)}字)")

    note_type = note_type if note_type in ("image", "video") else "image"
    media_paths = list(media_paths or [])
    tags = [t.strip().lstrip("#") for t in (tags or []) if t.strip()][:10]

    def _do(s: Session) -> Note:
        acc = s.get(Account, account_id)
        if not acc:
            raise AccountNotFoundError(f"账号 #{account_id} 不存在")

        note = Note(
            account_id=account_id,
            title=title,
            content=content.strip(),
            note_type=note_type,
            media_paths=media_paths,
            tags=tags,
            status="draft",
            revision=1,
            schedule_mode="immediate",
            source=source,
        )
        s.add(note)
        s.commit()
        s.refresh(note)
        log("info", "draft", f"新建草稿#{note.id} [{title or '(未命名)'}] 归属账号[{acc.name}] (rev:1)")
        return note

    if session is not None:
        return _do(session)
    with SessionLocal() as s:
        return _do(s)


def update_draft(
    note_id: int,
    title: str | None = None,
    content: str | None = None,
    note_type: str | None = None,
    media_paths: list[str] | None = None,
    tags: list[str] | None = None,
    expected_revision: int | None = None,
    session: Session | None = None,
) -> Note:
    """更新草稿:仅在草稿或失败状态下允许修改,每次修改递增 revision,永不隐式触发发布。"""

    def _do(s: Session) -> Note:
        note = s.get(Note, note_id)
        if not note:
            raise NoteNotFoundError(f"笔记 #{note_id} 不存在")

        if note.status not in ("draft", "failed"):
            raise DraftNotEditableError(f"当前笔记处于[{note.status}]状态,不可编辑")

        if expected_revision is not None and note.revision != expected_revision:
            raise RevisionConflictError(current_rev=note.revision, expected_rev=expected_revision)

        changed = False
        if title is not None:
            t = title.strip()
            if len(t) > 20:
                raise ValueError(f"标题最多20字(当前{len(t)}字)")
            if note.title != t:
                note.title = t
                changed = True

        if content is not None and note.content != content.strip():
            note.content = content.strip()
            changed = True

        if note_type is not None and note_type in ("image", "video") and note.note_type != note_type:
            note.note_type = note_type
            changed = True

        if media_paths is not None:
            new_paths = list(media_paths)
            if note.media_paths != new_paths:
                note.media_paths = new_paths
                changed = True

        if tags is not None:
            new_tags = [t.strip().lstrip("#") for t in tags if t.strip()][:10]
            if note.tags != new_tags:
                note.tags = new_tags
                changed = True

        if changed:
            note.revision += 1
            note.updated_at = datetime.now()
            s.commit()
            s.refresh(note)
            log("info", "draft", f"更新草稿#{note.id} [{note.title}] 修订号流转至 rev:{note.revision}")

        return note

    if session is not None:
        return _do(session)
    with SessionLocal() as s:
        return _do(s)


def get_draft(note_id: int, session: Session | None = None) -> Note:
    def _do(s: Session) -> Note:
        note = s.get(Note, note_id)
        if not note:
            raise NoteNotFoundError(f"笔记 #{note_id} 不存在")
        return note

    if session is not None:
        return _do(session)
    with SessionLocal() as s:
        return _do(s)


def delete_draft(note_id: int, session: Session | None = None) -> None:
    from .. import config

    def _do(s: Session) -> None:
        note = s.get(Note, note_id)
        if not note:
            raise NoteNotFoundError(f"笔记 #{note_id} 不存在")
        if note.status in ("publishing", "queued"):
            raise DraftNotEditableError("正在发布中的笔记不可删除")

        media = list(note.media_paths or [])
        s.delete(note)
        s.commit()

        for rel in media:
            try:
                f = config.UPLOADS_DIR / rel
                if f.exists():
                    f.unlink()
            except Exception:
                pass
        log("info", "draft", f"删除笔记/草稿#{note_id}")

    if session is not None:
        _do(session)
    else:
        with SessionLocal() as s:
            _do(s)

