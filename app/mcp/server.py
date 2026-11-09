"""小红书 AI 运营助手 - 标准 Model Context Protocol (MCP) Stdio 适配器。

使宿主 AI(Codex, WorkBuddy 等)可以通过标准工具协议接入本地发布助手:
- 多宿主共用同一常驻后台,生命周期独立,不重复启动发布 worker
- 所有文案与图片默认仅保存草稿,明确排期才执行
- 严格绑定 account_id
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from ..models import Account, Note, PublishJob, SessionLocal
from ..services.accounts import (
    get_account_service,
    get_login_status_service,
    list_accounts_service,
    trigger_login_service,
)
from ..services.drafts import create_draft, delete_draft, get_draft, update_draft
from ..services.errors import ServiceError
from ..services.media import import_media_file
from ..services.publish import (
    cancel_publish_job,
    schedule_publish,
    validate_publish_ready,
)
from ..services.runtime import get_runtime_status_service
from ..services.stats import get_notes_stats_service, get_stats_overview_service

TOOLS_REGISTRY = [
    {
        "name": "list_accounts",
        "description": "查询本地所有已配置的小红书/海外版Rednote账号列表及当前登录态状态(online/expired/pending)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "按登录状态过滤: all / online / expired / pending",
                    "enum": ["all", "online", "expired", "pending"],
                    "default": "all",
                }
            },
        },
    },
    {
        "name": "start_login",
        "description": "发起扫码登录流程(可为新账号命名,或为已有账号重新扫码)。返回账号ID与浏览器扫码页面指引。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "新建账号备注名(如 '美妆穿搭号')",
                },
                "account_id": {
                    "type": "integer",
                    "description": "已有账号ID(用于重新扫码登录)",
                },
            },
        },
    },
    {
        "name": "get_login_task",
        "description": "轮询检查指定账号当前的扫码登录状态与指引信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "integer",
                    "description": "账号ID",
                }
            },
            "required": ["account_id"],
        },
    },
    {
        "name": "create_draft",
        "description": "保存文案与素材为本地草稿。严格保存为草稿(status=draft),绝不自动入队或发布。无配图也可保存纯文字草稿。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "integer",
                    "description": "归属账号ID",
                },
                "title": {
                    "type": "string",
                    "description": "笔记标题(1~20字)",
                },
                "content": {
                    "type": "string",
                    "description": "正文内容描述(支持换行与emoji)",
                    "default": "",
                },
                "note_type": {
                    "type": "string",
                    "description": "笔记类型: image (图文) 或 video (视频)",
                    "enum": ["image", "video"],
                    "default": "image",
                },
                "media_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "已导入的受管素材文件名列表(通过 import_media 获取)",
                    "default": [],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "话题标签列表(无需带#,如 ['穿搭', '日常'])",
                    "default": [],
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "可选幂等标识符",
                },
            },
            "required": ["account_id", "title"],
        },
    },
    {
        "name": "update_draft",
        "description": "更新草稿内容(标题/正文/配图/话题)。每次更新自动递增 revision 保持版本一致性,仍保持 draft 状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "integer",
                    "description": "草稿笔记ID",
                },
                "title": {
                    "type": "string",
                    "description": "新标题(最多20字)",
                },
                "content": {
                    "type": "string",
                    "description": "新正文内容",
                },
                "media_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "新素材文件列表",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "新标签列表",
                },
                "expected_revision": {
                    "type": "integer",
                    "description": "期望的当前修订号(防并发修改错乱)",
                },
            },
            "required": ["note_id"],
        },
    },
    {
        "name": "delete_draft",
        "description": "删除指定的本地草稿及其关联的图片素材。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "integer",
                    "description": "笔记ID",
                }
            },
            "required": ["note_id"],
        },
    },
    {
        "name": "import_media",
        "description": "将外部本地图片或视频文件安全导入至助手受管存储目录,返回托管 media_id。支持临时生图文件持久化。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "宿主本地生成的图片或视频文件绝对路径",
                }
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "list_notes",
        "description": "按账号或状态查询草稿与笔记列表(含状态、发布时间、小红书线上链接)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "integer",
                    "description": "可选按账号ID筛选",
                },
                "status": {
                    "type": "string",
                    "description": "可选按状态筛选(draft/scheduled/queued/publishing/published/failed/verifying)",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量限制",
                    "default": 30,
                },
            },
        },
    },
    {
        "name": "get_note",
        "description": "获取单篇草稿/笔记详情,含配图列表、最新修订号、发布证据与核验状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "integer",
                    "description": "笔记ID",
                }
            },
            "required": ["note_id"],
        },
    },
    {
        "name": "schedule_publish",
        "description": "明确安排草稿发布(立即发布或指定时间定时发布)。对草稿做发布前要素校验,固化内容快照,加入后台可靠任务队列。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "integer",
                    "description": "要发布的草稿笔记ID",
                },
                "mode": {
                    "type": "string",
                    "description": "发布模式: immediate(排队立即发) 或 schedule(定时发布)",
                    "enum": ["immediate", "schedule"],
                    "default": "immediate",
                },
                "scheduled_at": {
                    "type": "string",
                    "description": "定时时间(格式 YYYY-MM-DD HH:MM,mode=schedule 时必填)",
                },
                "timezone": {
                    "type": "string",
                    "description": "目标时区(默认 Asia/Shanghai)",
                    "default": "Asia/Shanghai",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "可选幂等请求键,相同 key 重复提交不会重复排发",
                },
                "expected_revision": {
                    "type": "integer",
                    "description": "期望的当前草稿修订号",
                },
            },
            "required": ["note_id"],
        },
    },
    {
        "name": "get_publish_job",
        "description": "查询发布任务当前的执行阶段、重试次数、错误原因或平台发布成功详情。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "integer",
                    "description": "发布任务ID",
                }
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "cancel_publish_job",
        "description": "取消尚未提交到平台的排期中任务,将对应笔记安全回退为草稿状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "integer",
                    "description": "发布任务ID",
                }
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "get_stats",
        "description": "查询各账号整体数据表现(曝光、观看、点赞、收藏、评论、分享)及单篇笔记最新追踪数据。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "integer",
                    "description": "可选按账号ID筛选",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回笔记明细数量限制",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "get_runtime_status",
        "description": "查询本地助手服务运行状态、版本、在线账号数及当前活跃任务数。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def handle_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """分发工具调用到服务层。"""
    try:
        if name == "list_accounts":
            data = list_accounts_service(status=args.get("status"))
            return {
                "accounts": data,
                "count": len(data),
                "next_action": "请选择用于创作发布的 account_id",
            }

        elif name == "start_login":
            res = trigger_login_service(name=args.get("name"), account_id=args.get("account_id"))
            return {
                **res,
                "next_action": "提示用户打开网页并用小红书手机 APP 扫码确认",
            }

        elif name == "get_login_task":
            res = get_login_status_service(account_id=args["account_id"])
            return res

        elif name == "create_draft":
            note = create_draft(
                account_id=args["account_id"],
                title=args.get("title", ""),
                content=args.get("content", ""),
                note_type=args.get("note_type", "image"),
                media_paths=args.get("media_ids", []),
                tags=args.get("tags", []),
                source="ai",
            )
            is_ready, missing = validate_publish_ready(note)
            return {
                "note_id": note.id,
                "account_id": note.account_id,
                "title": note.title,
                "status": note.status,
                "revision": note.revision,
                "media_count": len(note.media_paths or []),
                "publish_ready": is_ready,
                "missing_for_publish": missing,
                "message": "草稿已安全保存。默认不发布,用户确认后可调用 schedule_publish 排期。",
                "next_action": "向用户展示草稿预览;若要素齐全且用户明确要求发布,方可调用 schedule_publish",
            }

        elif name == "update_draft":
            note = update_draft(
                note_id=args["note_id"],
                title=args.get("title"),
                content=args.get("content"),
                media_paths=args.get("media_ids"),
                tags=args.get("tags"),
                expected_revision=args.get("expected_revision"),
            )
            is_ready, missing = validate_publish_ready(note)
            return {
                "note_id": note.id,
                "title": note.title,
                "status": note.status,
                "revision": note.revision,
                "publish_ready": is_ready,
                "missing_for_publish": missing,
                "message": f"草稿已更新,当前修订号 rev:{note.revision}",
            }

        elif name == "delete_draft":
            delete_draft(note_id=args["note_id"])
            return {"ok": True, "message": f"草稿 #{args['note_id']} 已删除"}

        elif name == "import_media":
            res = import_media_file(args["file_path"])
            return {
                **res,
                "message": "素材已安全复制进发布助手受管目录,可在 create_draft 中传入 media_ids 使用",
            }

        elif name == "list_notes":
            with SessionLocal() as s:
                q = s.query(Note).order_by(Note.id.desc())
                if args.get("account_id"):
                    q = q.filter(Note.account_id == args["account_id"])
                if args.get("status"):
                    q = q.filter(Note.status == args["status"])
                limit = args.get("limit", 30)
                notes = q.limit(limit).all()
                data = [
                    {
                        "id": n.id,
                        "account_id": n.account_id,
                        "title": n.title,
                        "status": n.status,
                        "revision": n.revision,
                        "schedule_mode": n.schedule_mode,
                        "scheduled_at": n.scheduled_at.isoformat(sep=" ") if n.scheduled_at else None,
                        "published_at": n.published_at.isoformat(sep=" ") if n.published_at else None,
                        "media_count": len(n.media_paths or []),
                        "xhs_url": n.xhs_url,
                        "error": n.error,
                    }
                    for n in notes
                ]
                return {"notes": data, "count": len(data)}

        elif name == "get_note":
            note_id = args["note_id"]
            note = get_draft(note_id)
            with SessionLocal() as s:
                acc = s.get(Account, note.account_id)
                acc_name = acc.name if acc else "?"
                latest_job = (
                    s.query(PublishJob)
                    .filter(PublishJob.note_id == note.id)
                    .order_by(PublishJob.id.desc())
                    .first()
                )
                job_info = None
                if latest_job:
                    job_info = {
                        "job_id": latest_job.id,
                        "status": latest_job.status,
                        "mode": latest_job.mode,
                        "effective_at": latest_job.effective_at.isoformat(sep=" ") if latest_job.effective_at else None,
                        "last_error": latest_job.last_error_message,
                    }

            is_ready, missing = validate_publish_ready(note)
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
                "publish_ready": is_ready,
                "missing_for_publish": missing,
                "latest_job": job_info,
                "xhs_note_id": note.xhs_note_id,
                "xhs_url": note.xhs_url,
                "verification_status": note.verification_status,
                "error": note.error,
            }

        elif name == "schedule_publish":
            job = schedule_publish(
                note_id=args["note_id"],
                mode=args.get("mode", "immediate"),
                scheduled_at=args.get("scheduled_at"),
                timezone=args.get("timezone", "Asia/Shanghai"),
                idempotency_key=args.get("idempotency_key"),
                expected_revision=args.get("expected_revision"),
            )
            return {
                "job_id": job.id,
                "note_id": job.note_id,
                "account_id": job.account_id,
                "status": job.status,
                "mode": job.mode,
                "scheduled_at": job.scheduled_at.isoformat(sep=" ") if job.scheduled_at else None,
                "effective_at": job.effective_at.isoformat(sep=" ") if job.effective_at else None,
                "message": f"发布任务已进入后台队列(状态: {job.status}),由本地常驻后台错峰执行",
                "next_action": "可通过 get_publish_job 轮询状态,或通过 get_note 查看发布链接",
            }

        elif name == "get_publish_job":
            job_id = args["job_id"]
            with SessionLocal() as s:
                job = s.get(PublishJob, job_id)
                if not job:
                    raise ServiceError("JOB_NOT_FOUND", f"任务 #{job_id} 不存在", 404)
                note = s.get(Note, job.note_id)
                return {
                    "job_id": job.id,
                    "note_id": job.note_id,
                    "account_id": job.account_id,
                    "status": job.status,
                    "mode": job.mode,
                    "attempt_count": job.attempt_count,
                    "scheduled_at": job.scheduled_at.isoformat(sep=" ") if job.scheduled_at else None,
                    "effective_at": job.effective_at.isoformat(sep=" ") if job.effective_at else None,
                    "last_error_code": job.last_error_code,
                    "last_error_message": job.last_error_message,
                    "note_status": note.status if note else "?",
                    "xhs_url": note.xhs_url if note else None,
                    "verification_status": note.verification_status if note else None,
                }

        elif name == "cancel_publish_job":
            job = cancel_publish_job(args["job_id"])
            return {
                "job_id": job.id,
                "status": job.status,
                "message": f"任务 #{job.id} 已取消,关联笔记已安全回退为草稿",
            }

        elif name == "get_stats":
            overview = get_stats_overview_service()
            notes_stats = get_notes_stats_service(account_id=args.get("account_id"), limit=args.get("limit", 20))
            return {
                "accounts_overview": overview,
                "notes_performance": notes_stats,
            }

        elif name == "get_runtime_status":
            return get_runtime_status_service()

        else:
            raise ServiceError("UNKNOWN_TOOL", f"未知的工具名称: {name}", 404)

    except ServiceError as e:
        return {
            "isError": True,
            "error_code": e.code,
            "message": e.message,
        }
    except Exception as e:
        return {
            "isError": True,
            "error_code": "INTERNAL_ERROR",
            "message": str(e),
        }


def process_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """处理单条 JSON-RPC 请求。"""
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": "xhs-ai-assistant",
                    "version": "0.2.0",
                },
            },
        }

    elif method == "notifications/initialized":
        # 客户端初始化完成通知,无需回复响应
        return None

    elif method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {},
        }

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": TOOLS_REGISTRY,
            },
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result_payload = handle_tool_call(tool_name, arguments)
        is_error = result_payload.get("isError", False)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result_payload, ensure_ascii=False, indent=2),
                    }
                ],
                "isError": is_error,
            },
        }

    else:
        if msg_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }
        return None


def run_stdio_server() -> None:
    """标准输入输出事件主循环。"""
    if sys.platform == "win32":
        # Windows 管道默认 charmap,中文工具描述会 UnicodeEncodeError
        for _s in (sys.stdout, sys.stderr, sys.stdin):
            try:
                _s.reconfigure(encoding="utf-8")
            except Exception:
                pass
    sys.stderr.write("[MCP] 小红书 AI 运营助手 Stdio MCP Server 已启动...\n")
    sys.stderr.flush()

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        stripped = line.strip()
        if not stripped:
            continue

        try:
            req = json.loads(stripped)
            resp = process_message(req)
            if resp is not None:
                out_line = json.dumps(resp, ensure_ascii=True)  # wire 层 ASCII 转义,任何管道编码安全
                sys.stdout.write(out_line + "\n")
                sys.stdout.flush()
        except Exception as e:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32700,
                    "message": f"Parse error: {e}",
                },
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=True) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()

