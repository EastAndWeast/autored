"""v1 草稿 API:提供草稿创建、编辑、修订号锁与独立管理能力。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...services.drafts import create_draft, delete_draft, get_draft, update_draft
from ...services.errors import ServiceError
from ...models import Note, SessionLocal, Account

router = APIRouter(prefix="/drafts", tags=["v1-drafts"])


def _draft_dict(note: Note, s=None) -> dict:
    acc_name = "?"
    if s:
        acc = s.get(Account, note.account_id)
        if acc:
            acc_name = acc.name
    return {
        "id": note.id,
        "account_id": note.account_id,
        "account_name": acc_name,
        "title": note.title,
        "content": note.content,
        "note_type": note.note_type,
        "media_paths": note.media_paths or [],
        "tags": note.tags or [],
        "status": note.status,
        "revision": note.revision,
        "created_at": note.created_at.isoformat(sep=" "),
        "updated_at": note.updated_at.isoformat(sep=" "),
    }


class DraftCreateIn(BaseModel):
    account_id: int
    title: str = ""
    content: str = ""
    note_type: str = "image"
    media_paths: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source: str = "ai"
    idempotency_key: str | None = None


class DraftUpdateIn(BaseModel):
    title: str | None = None
    content: str | None = None
    note_type: str | None = None
    media_paths: list[str] | None = None
    tags: list[str] | None = None
    expected_revision: int | None = None


@router.post("")
def post_draft(body: DraftCreateIn):
    try:
        note = create_draft(
            account_id=body.account_id,
            title=body.title,
            content=body.content,
            note_type=body.note_type,
            media_paths=body.media_paths,
            tags=body.tags,
            source=body.source,
        )
        with SessionLocal() as s:
            return _draft_dict(note, s)
    except ServiceError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message})
    except ValueError as e:
        raise HTTPException(400, {"code": "VALIDATION_ERROR", "message": str(e)})


@router.get("/{draft_id}")
def get_draft_by_id(draft_id: int):
    try:
        with SessionLocal() as s:
            note = get_draft(draft_id, session=s)
            return _draft_dict(note, s)
    except ServiceError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message})


@router.patch("/{draft_id}")
def patch_draft(draft_id: int, body: DraftUpdateIn):
    try:
        note = update_draft(
            note_id=draft_id,
            title=body.title,
            content=body.content,
            note_type=body.note_type,
            media_paths=body.media_paths,
            tags=body.tags,
            expected_revision=body.expected_revision,
        )
        with SessionLocal() as s:
            return _draft_dict(note, s)
    except ServiceError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message})
    except ValueError as e:
        raise HTTPException(400, {"code": "VALIDATION_ERROR", "message": str(e)})


@router.delete("/{draft_id}")
def delete_draft_by_id(draft_id: int):
    try:
        delete_draft(draft_id)
        return {"ok": True, "message": "草稿已删除"}
    except ServiceError as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": e.message})

