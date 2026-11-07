"""数据查询 API:按账号/笔记查看指标与趋势,手动刷新。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..models import Account, Note, NoteStat, SessionLocal
from ..scheduler import enqueue

router = APIRouter(prefix="/api/stats", tags=["stats"])


def _latest_stats(s, note_id: int) -> NoteStat | None:
    return (
        s.query(NoteStat)
        .filter(NoteStat.note_id == note_id)
        .order_by(NoteStat.fetched_at.desc())
        .first()
    )


@router.get("/overview")
def overview():
    """每个账号:最新快照合计 + 笔记数。"""
    with SessionLocal() as s:
        accounts = s.query(Account).order_by(Account.id).all()
        out = []
        for a in accounts:
            notes = s.query(Note).filter(Note.account_id == a.id).all()
            total = {"impression": 0, "read": 0, "like": 0, "collect": 0, "comment": 0, "share": 0, "fans_gain": 0}
            notes_with_stats = 0
            for n in notes:
                latest = _latest_stats(s, n.id)
                if latest:
                    notes_with_stats += 1
                    for k in total:
                        total[k] += getattr(latest, k)
            out.append(
                {
                    "account_id": a.id,
                    "account_name": a.name,
                    "status": a.status,
                    "note_count": len(notes),
                    "published_count": sum(1 for n in notes if n.status == "published"),
                    "tracked_count": notes_with_stats,
                    **total,
                }
            )
        return out


@router.get("/notes")
def notes_stats(account_id: int = 0):
    """每篇已发布笔记的最新数据 + 与上次快照的增量。"""
    with SessionLocal() as s:
        q = s.query(Note).filter(Note.status == "published").order_by(Note.published_at.desc())
        if account_id:
            q = q.filter(Note.account_id == account_id)
        out = []
        for n in q.all():
            stats = (
                s.query(NoteStat)
                .filter(NoteStat.note_id == n.id)
                .order_by(NoteStat.fetched_at.desc())
                .limit(2)
                .all()
            )
            latest = stats[0] if stats else None
            prev = stats[1] if len(stats) > 1 else None
            delta = {}
            if latest and prev:
                for k in ("impression", "read", "like", "collect", "comment", "share", "fans_gain"):
                    delta[k] = getattr(latest, k) - getattr(prev, k)
            out.append(
                {
                    "note_id": n.id,
                    "account_id": n.account_id,
                    "account_name": (s.get(Account, n.account_id).name if s.get(Account, n.account_id) else "?"),
                    "title": n.title,
                    "published_at": n.published_at.isoformat(sep=" ") if n.published_at else None,
                    "xhs_url": n.xhs_url,
                    "fetched_at": latest.fetched_at.isoformat(sep=" ") if latest else None,
                    "impression": latest.impression if latest else None,
                    "read": latest.read if latest else None,
                    "like": latest.like if latest else None,
                    "collect": latest.collect if latest else None,
                    "comment": latest.comment if latest else None,
                    "share": latest.share if latest else None,
                    "fans_gain": latest.fans_gain if latest else None,
                    "delta": delta if delta else None,
                }
            )
        return out


@router.get("/notes/{note_id}/history")
def note_history(note_id: int):
    with SessionLocal() as s:
        n = s.get(Note, note_id)
        if not n:
            raise HTTPException(404, "笔记不存在")
        return [
            {
                "fetched_at": st.fetched_at.isoformat(sep=" "),
                "impression": st.impression,
                "read": st.read,
                "like": st.like,
                "collect": st.collect,
                "comment": st.comment,
                "share": st.share,
                "fans_gain": st.fans_gain,
            }
            for st in sorted(n.stats, key=lambda x: x.fetched_at)
        ]


class RefreshIn(BaseModel):
    account_id: int | None = None


@router.post("/refresh")
def refresh(body: RefreshIn):
    """手动刷新:入队采集任务(在线账号或指定账号)。"""
    with SessionLocal() as s:
        if body.account_id:
            acc = s.get(Account, body.account_id)
            if not acc:
                raise HTTPException(404, "账号不存在")
            if acc.status != "online":
                raise HTTPException(400, "账号不在线")
            targets = [acc.id]
        else:
            targets = [a.id for a in s.query(Account).filter(Account.status == "online").all()]
    if not targets:
        raise HTTPException(400, "没有在线账号可采集")
    for aid in targets:
        enqueue("collect", aid)
    return {"ok": True, "queued": len(targets), "message": "已加入采集队列(与发布任务串行错峰)"}
