"""统计查询领域服务:提供多账号数据汇总与单篇笔记追踪。

P4 数据闭环特性:
1. 区分「未采集(pending_first_fetch)」与「真实 0(verified_zero)」
2. 国内小红书与海外版 Rednote 平台适配状态清晰可见(不虚报全兼容)
3. 记录每次快照时间戳、来源通道(api/dom)及匹配方式(platform_id/title)
4. 输出标准指标术语定义,供 AI 与前端精确呈现
"""
from __future__ import annotations

from typing import Any
from ..models import Account, Note, NoteStat, SessionLocal

METRICS_DEFINITIONS = {
    "impression": "曝光量: 笔记在小红书/Rednote推荐流与搜索结果中的展现次数",
    "read": "阅读/观看量: 用户点击进入笔记正文或观看视频的完整次数",
    "like": "点赞数: 用户对笔记点击红心的互动次数",
    "collect": "收藏数: 用户将笔记加入收藏专辑的次数",
    "comment": "评论数: 笔记评论区用户发表的评论留言总数",
    "share": "分享数: 用户将笔记转发给微信、朋友圈或站内好友的次数",
    "fans_gain": "涨粉数: 用户通过浏览该篇笔记直接点击关注作者的转化数",
}


def _latest_stats(s, note_id: int) -> NoteStat | None:
    return (
        s.query(NoteStat)
        .filter(NoteStat.note_id == note_id)
        .order_by(NoteStat.fetched_at.desc())
        .first()
    )


def get_stats_overview_service() -> list[dict[str, Any]]:
    with SessionLocal() as s:
        accounts = s.query(Account).order_by(Account.id).all()
        out = []
        for a in accounts:
            notes = s.query(Note).filter(Note.account_id == a.id).all()
            total = {
                "impression": 0,
                "read": 0,
                "like": 0,
                "collect": 0,
                "comment": 0,
                "share": 0,
                "fans_gain": 0,
            }
            notes_with_stats = 0
            for n in notes:
                latest = _latest_stats(s, n.id)
                if latest:
                    notes_with_stats += 1
                    for k in total:
                        total[k] += getattr(latest, k, 0)

            # 双平台兼容状态标记(交接规范:未验证的一侧清楚标记,不虚报全兼容)
            region = a.platform_region or "unknown"
            if region in ("cn", "china"):
                compat = "fully_supported"
                compat_text = "国内版已完整验证"
            elif region in ("global", "rednote"):
                compat = "experimental_unverified"
                compat_text = "海外版实验中(待核验)"
            else:
                compat = "unknown"
                compat_text = "未知平台"

            out.append(
                {
                    "account_id": a.id,
                    "account_name": a.name,
                    "status": a.status,
                    "platform_region": region,
                    "compatibility": compat,
                    "compatibility_text": compat_text,
                    "last_stats_at": a.last_stats_at.isoformat(sep=" ") if a.last_stats_at else None,
                    "last_stats_error": a.last_stats_error,
                    "note_count": len(notes),
                    "published_count": sum(1 for n in notes if n.status == "published"),
                    "tracked_count": notes_with_stats,
                    **total,
                }
            )
        return out


def get_notes_stats_service(account_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with SessionLocal() as s:
        q = s.query(Note).filter(Note.status == "published").order_by(Note.published_at.desc())
        if account_id:
            q = q.filter(Note.account_id == account_id)
        notes = q.limit(limit).all()
        out = []
        for n in notes:
            acc = s.get(Account, n.account_id)
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
                    delta[k] = getattr(latest, k, 0) - getattr(prev, k, 0)

            # 区分: 未采集 vs 真实读数为 0 vs 正常追踪
            if latest is None:
                tracking_status = "pending_first_fetch"
                tracking_status_text = "未采集(等待定时采集任务)"
            else:
                is_all_zero = (
                    latest.impression == 0
                    and latest.read == 0
                    and latest.like == 0
                    and latest.collect == 0
                )
                if is_all_zero:
                    tracking_status = "verified_zero"
                    tracking_status_text = "已采集(平台读数为 0)"
                else:
                    tracking_status = "tracked"
                    tracking_status_text = "已追踪"

            out.append(
                {
                    "note_id": n.id,
                    "account_id": n.account_id,
                    "account_name": acc.name if acc else "?",
                    "title": n.title,
                    "published_at": n.published_at.isoformat(sep=" ") if n.published_at else None,
                    "xhs_url": n.xhs_url,
                    "fetched_at": latest.fetched_at.isoformat(sep=" ") if latest else None,
                    "tracking_status": tracking_status,
                    "tracking_status_text": tracking_status_text,
                    "source": latest.source if latest else None,
                    "matched_by": latest.raw_matched_by if latest else None,
                    "impression": latest.impression if latest else 0,
                    "read": latest.read if latest else 0,
                    "like": latest.like if latest else 0,
                    "collect": latest.collect if latest else 0,
                    "comment": latest.comment if latest else 0,
                    "share": latest.share if latest else 0,
                    "fans_gain": latest.fans_gain if latest else 0,
                    "delta": delta if delta else None,
                }
            )
        return out
