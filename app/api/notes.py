"""笔记管理 API:创建(图文/视频+媒体上传)、编辑、立即/定时发布、列表。"""
from __future__ import annotations

import os
import uuid
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

import random

from .. import config
from ..models import Account, Note, SessionLocal, log
from ..scheduler import enqueue, apply_jitter

router = APIRouter(prefix="/api/notes", tags=["notes"])

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
MAX_IMAGES = 18
MAX_IMAGE_BYTES = 20 * 1024 * 1024   # 20MB
MAX_VIDEO_BYTES = 1 * 1024 * 1024 * 1024  # 1GB

# 文件头签名(前 16 字节内);None 表示第 4~8 字节应为 b"ftyp"(mp4/mov/m4v)
MAGIC_BYTES = {
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG"],
    ".gif": [b"GIF8"],
    ".webp": [b"RIFF"],
    ".webm": [b"\x1a\x45\xdf\xa3"],
    ".mp4": [None],
    ".m4v": [None],
    ".mov": [None, b"moov", b"mdat", b"wide", b"skip"],  # QuickTime 容器变体
}


def _check_magic(ext: str, head: bytes) -> bool:
    sigs = MAGIC_BYTES.get(ext)
    if sigs is None:
        return True  # 未登记的扩展名不校验
    for sig in sigs:
        if sig is None:
            if head[4:8] == b"ftyp":
                return True
        elif head.startswith(sig):
            return True
    return False


def _note_dict(s, n: Note) -> dict:
    acc = s.get(Account, n.account_id)
    return {
        "id": n.id,
        "account_id": n.account_id,
        "account_name": acc.name if acc else "?",
        "title": n.title,
        "content": n.content,
        "note_type": n.note_type,
        "media_paths": n.media_paths or [],
        "tags": n.tags or [],
        "status": n.status,
        "revision": n.revision,
        "schedule_mode": n.schedule_mode,
        "scheduled_at": n.scheduled_at.isoformat(sep=" ") if n.scheduled_at else None,
        "submitted_at": n.submitted_at.isoformat(sep=" ") if n.submitted_at else None,
        "confirmed_at": n.confirmed_at.isoformat(sep=" ") if n.confirmed_at else None,
        "published_at": n.published_at.isoformat(sep=" ") if n.published_at else None,
        "platform_status": n.platform_status,
        "verification_status": n.verification_status,
        "xhs_note_id": n.xhs_note_id,
        "xhs_url": n.xhs_url,
        "xhs_publish_url": n.xhs_publish_url,
        "error": n.error,
        "source": n.source,
        "created_at": n.created_at.isoformat(sep=" "),
        "updated_at": n.updated_at.isoformat(sep=" "),
    }


@router.get("")
def list_notes(status: str = "", account_id: int = 0, limit: int = 200):
    with SessionLocal() as s:
        q = s.query(Note).order_by(Note.id.desc())
        if status:
            q = q.filter(Note.status == status)
        if account_id:
            q = q.filter(Note.account_id == account_id)
        return [_note_dict(s, n) for n in q.limit(limit).all()]


@router.get("/{note_id}")
def get_note(note_id: int):
    from ..models import NoteStat

    with SessionLocal() as s:
        n = s.get(Note, note_id)
        if not n:
            raise HTTPException(404, "笔记不存在")
        d = _note_dict(s, n)
        d["stats_history"] = [
            {
                "fetched_at": st.fetched_at.isoformat(sep=" "),
                "impression": st.impression,
                "read": st.read,
                "like": st.like,
                "collect": st.collect,
                "comment": st.comment,
                "share": st.share,
            }
            for st in n.stats
        ]
        return d


def _save_upload(file: UploadFile, note_type: str) -> str:
    ext = os.path.splitext(file.filename or "")[1].lower()
    allowed = ALLOWED_VIDEO_EXT if note_type == "video" else ALLOWED_IMAGE_EXT
    if ext not in allowed:
        raise HTTPException(400, f"不支持的文件类型 {ext},允许:{','.join(allowed)}")
    max_bytes = MAX_VIDEO_BYTES if note_type == "video" else MAX_IMAGE_BYTES
    name = f"{uuid.uuid4().hex[:12]}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
    dest = config.UPLOADS_DIR / name
    size = 0
    head = b""
    with open(dest, "wb") as f:
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > max_bytes:
                f.close()
                dest.unlink(missing_ok=True)
                limit_mb = max_bytes // (1024 * 1024)
                raise HTTPException(400, f"文件超过大小限制 {limit_mb}MB")
            if len(head) < 16:
                head += chunk[: 16 - len(head)]
            f.write(chunk)
    if size == 0 or not _check_magic(ext, head):
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"文件内容与扩展名 {ext} 不符或为空")
    return name


@router.post("")
async def create_note(
    account_id: int = Form(...),
    title: str = Form(...),
    content: str = Form(""),
    note_type: str = Form("image"),
    tags: str = Form(""),
    scheduled_at: str = Form(""),
    save_as_draft: bool = Form(False),
    files: list[UploadFile] = File(default=[]),
):
    """创建笔记或保存草稿。"""
    with SessionLocal() as s:
        acc = s.get(Account, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")

    title = title.strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    if len(title) > 20:
        raise HTTPException(400, f"标题超长({len(title)}/20)")
    note_type = note_type if note_type in ("image", "video") else "image"

    media: list[str] = []
    for f in files or []:
        name = _save_upload(f, note_type)
        media.append(name)
    if note_type == "video" and len(media) > 1:
        media = media[:1]
    if note_type == "image" and len(media) > MAX_IMAGES:
        media = media[:MAX_IMAGES]

    tag_list = [t.strip().lstrip("#") for t in re_split_tags(tags) if t.strip()][:10]

    # 若仅保存草稿: 无需强制媒体, 直接保存为 draft
    if save_as_draft:
        with SessionLocal() as s:
            note = Note(
                account_id=account_id,
                title=title,
                content=content.strip(),
                note_type=note_type,
                media_paths=media,
                tags=tag_list,
                status="draft",
                schedule_mode="immediate",
                scheduled_at=None,
                revision=1,
            )
            s.add(note)
            s.commit()
            log("info", "note", f"新建草稿笔记#{note.id}[{title}] (所属账号#{account_id})")
            return _note_dict(s, note)

    # 安排发布流程
    if not media:
        raise HTTPException(400, "安排发布必须至少上传一个媒体文件(若仅记录灵感请选择存为草稿)")

    when = None
    mode = "immediate"
    if scheduled_at.strip():
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
            try:
                when = datetime.strptime(scheduled_at.strip(), fmt)
                break
            except ValueError:
                continue
        if when is None:
            raise HTTPException(400, "时间格式应为 YYYY-MM-DD HH:MM")
        if when <= datetime.now():
            raise HTTPException(400, "定时时间必须晚于现在")
        gap_h = (when - datetime.now()).total_seconds() / 3600
        mode = "platform" if gap_h >= config.PLATFORM_SCHEDULE_THRESHOLD_H else "local"

    # 先保存为笔记, 然后通过 schedule_publish 可靠入队
    with SessionLocal() as s:
        note = Note(
            account_id=account_id,
            title=title,
            content=content.strip(),
            note_type=note_type,
            media_paths=media,
            tags=tag_list,
            status="draft",
            schedule_mode=mode,
            scheduled_at=when,
            revision=1,
        )
        s.add(note)
        s.commit()
        note_id = note.id

    from ..services.publish import schedule_publish
    try:
        schedule_publish(
            note_id=note_id,
            mode="schedule" if when else "immediate",
            scheduled_at=when,
        )
    except Exception as e:
        with SessionLocal() as s:
            n = s.get(Note, note_id)
            if n:
                n.status = "draft"
                n.error = str(e)
                s.commit()
        raise HTTPException(400, f"排期失败: {e}")

    with SessionLocal() as s:
        n = s.get(Note, note_id)
        return _note_dict(s, n)


@router.post("/{note_id}/publish-now")
def publish_now(note_id: int):
    """待发布/失败/草稿(有媒体的)立即发布。"""
    from ..services.publish import schedule_publish
    from ..services.errors import ServiceError

    try:
        schedule_publish(note_id=note_id, mode="immediate")
        return {"ok": True, "message": "已加入发布队列(会与其它任务错峰执行)"}
    except ServiceError as e:
        raise HTTPException(e.status_code, e.message)
    except ValueError as e:
        raise HTTPException(400, str(e))


class NotePatch(BaseModel):
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    scheduled_at: str | None = None


@router.patch("/{note_id}")
def patch_note(note_id: int, body: NotePatch):
    with SessionLocal() as s:
        n = s.get(Note, note_id)
        if not n:
            raise HTTPException(404, "笔记不存在")
        if n.status in ("publishing", "queued", "published", "platform_scheduled"):
            raise HTTPException(409, f"当前状态({n.status})不可编辑")
        changed = False
        if body.title is not None:
            t = body.title.strip()
            if not t or len(t) > 20:
                raise HTTPException(400, "标题需 1~20 字")
            if n.title != t:
                n.title = t
                changed = True
        if body.content is not None:
            c = body.content.strip()
            if n.content != c:
                n.content = c
                changed = True
        if body.tags is not None:
            new_tags = [t.strip().lstrip("#") for t in body.tags if t.strip()][:10]
            if n.tags != new_tags:
                n.tags = new_tags
                changed = True
        if body.scheduled_at is not None:
            if not body.scheduled_at.strip():
                n.scheduled_at = None
                n.schedule_mode = "immediate"
            else:
                when = None
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
                    try:
                        when = datetime.strptime(body.scheduled_at.strip(), fmt)
                        break
                    except ValueError:
                        continue
                if when is None or when <= datetime.now():
                    raise HTTPException(400, "时间无效或已过去")
                gap_h = (when - datetime.now()).total_seconds() / 3600
                n.schedule_mode = (
                    "platform" if gap_h >= config.PLATFORM_SCHEDULE_THRESHOLD_H else "local"
                )
                n.scheduled_at = when
            changed = True
        if changed:
            n.revision += 1
            n.updated_at = datetime.now()
        s.commit()
        return _note_dict(s, n)


@router.delete("/{note_id}")
def delete_note(note_id: int):
    from .. import config as _config

    with SessionLocal() as s:
        n = s.get(Note, note_id)
        if not n:
            raise HTTPException(404, "笔记不存在")
        if n.status in ("publishing", "queued"):
            raise HTTPException(409, "正在发布中,稍后再删")
        media = list(n.media_paths or [])
        s.delete(n)  # 级联删除 note_stats(F-02)
        s.commit()
    for rel in media:
        try:
            f = _config.UPLOADS_DIR / rel
            if f.exists():
                f.unlink()
        except Exception:
            pass
    return {"ok": True}


def re_split_tags(tags: str) -> list[str]:
    import re as _re

    return _re.split(r"[,，#\s]+", tags or "")
