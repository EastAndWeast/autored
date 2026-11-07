"""设置 + Obsidian 导入 API。"""
from __future__ import annotations

from dataclasses import asdict
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config
from ..models import Account, SessionLocal
from ..obsidian_sync import import_draft, match_account_id, scan_dir
from ..scheduler import get_collect_times, get_setting, set_setting

router = APIRouter(prefix="/api", tags=["settings"])

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# ---------- 通用设置 ----------

@router.get("/settings")
def get_settings():
    return {
        "obsidian_dir": get_setting("obsidian_dir", ""),
        "published_dir": get_setting("published_dir", ""),
        "collect_times": get_collect_times(),
        "headless": get_setting("headless", "1") == "1",
        "daily_default_limit": config.DEFAULT_DAILY_LIMIT,
        "inter_account_delay": config.INTER_ACCOUNT_DELAY,
        "schedule_jitter": config.SCHEDULE_JITTER,
        "platform_threshold_h": config.PLATFORM_SCHEDULE_THRESHOLD_H,
    }


class SettingsIn(BaseModel):
    obsidian_dir: str | None = None
    published_dir: str | None = None
    collect_times: list[str] | None = None
    headless: bool | None = None


@router.put("/settings")
def put_settings(body: SettingsIn):
    if body.collect_times is not None:
        raw_times = [t.strip() for t in body.collect_times if t and t.strip()][:4]
        valid_times: list[str] = []
        for t in raw_times:
            m = _TIME_RE.match(t)
            if not m:
                raise HTTPException(400, f"采集时间格式无效: {t}，应为 HH:MM (00:00~23:59)")
            valid_times.append(f"{int(m.group(1)):02d}:{int(m.group(2)):02d}")
        if valid_times:
            set_setting("collect_times", ",".join(valid_times))
    if body.obsidian_dir is not None:
        set_setting("obsidian_dir", body.obsidian_dir.strip())
    if body.published_dir is not None:
        set_setting("published_dir", body.published_dir.strip())
    if body.headless is not None:
        set_setting("headless", "1" if body.headless else "0")
    # 采集计划可能变了,动态重排 APScheduler 任务
    from ..scheduler import reload_collect_jobs

    reload_collect_jobs()
    return get_settings()


# ---------- Obsidian ----------

def _resolve_vault_dir(dir_path: str | None = "") -> Path:
    """解析并校验 vault 目录:必须存在且为目录(S-01 防任意目录探测,R-02 兼容 None)。"""
    path = (dir_path or "").strip() or get_setting("obsidian_dir", "")
    if not path:
        raise HTTPException(400, "未配置 Obsidian 目录")
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(400, f"目录不存在: {path}")
    return root


@router.get("/obsidian/scan")
def obsidian_scan(dir_path: str = ""):
    """扫描 Obsidian 待发布文件夹,返回解析预览。"""
    root = _resolve_vault_dir(dir_path)
    try:
        drafts = scan_dir(str(root))
    except NotADirectoryError as e:
        raise HTTPException(400, str(e))
    accounts = []
    with SessionLocal() as s:
        accounts = [{"id": a.id, "name": a.name} for a in s.query(Account).all()]
    out = []
    for d in drafts:
        item = asdict(d)
        item["matched_account_id"] = match_account_id(d.account_name)
        out.append(item)
    return {"dir": str(root), "drafts": out, "accounts": accounts}


class ObsidianImportIn(BaseModel):
    dir_path: str | None = None
    files: list[str]              # 要导入的 md 绝对路径
    account_id: int               # 统一指派的账号
    move_after: bool = True       # 导入后是否移动到已发布目录


@router.post("/obsidian/import")
def obsidian_import(body: ObsidianImportIn):
    with SessionLocal() as s:
        if not s.get(Account, body.account_id):
            raise HTTPException(404, "账号不存在")

    vault = _resolve_vault_dir(body.dir_path)
    results = []
    for f in body.files:
        # 沙箱(S-01):文件必须在 vault 内且为 .md,防止任意文件读取/移动
        try:
            f_resolved = Path(f).expanduser().resolve()
            inside = str(f_resolved).startswith(str(vault) + os.sep)
            is_md = f_resolved.suffix.lower() == ".md"
            if not (inside and is_md and f_resolved.is_file()):
                results.append(
                    {"file": f, "ok": False, "error": "路径不在 Obsidian 目录内或非 .md 文件,已拒绝"}
                )
                continue
        except Exception:
            results.append({"file": f, "ok": False, "error": "非法路径"})
            continue
        try:
            draft = scan_one(str(f_resolved), str(vault))
        except Exception as e:  # noqa: BLE001
            results.append({"file": f, "ok": False, "error": str(e)[:200]})
            continue
        note_id, warnings = import_draft(draft, body.account_id)
        moved_to = None
        if note_id and body.move_after:
            dest = get_setting("published_dir", "") or (str(vault) + "/已发布")
            Path(dest).mkdir(parents=True, exist_ok=True)
            from ..obsidian_sync import move_to_published

            moved_to = move_to_published(str(f_resolved), dest)
        results.append(
            {"file": f, "ok": bool(note_id), "note_id": note_id, "warnings": warnings, "moved_to": moved_to}
        )
    return {"results": results}


def scan_one(file_path: str, vault_root: str):
    from ..obsidian_sync import parse_markdown_file

    return parse_markdown_file(file_path, vault_root=vault_root or None)
