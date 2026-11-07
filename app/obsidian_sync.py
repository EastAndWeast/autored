"""Obsidian 集成:扫描 vault 里的待发布文件夹,一个 .md = 一篇笔记。

约定:
- frontmatter 支持:title / tags / account / scheduled_at / cover
- 图片引用:![[图片名.png]](Obsidian wikilink)或 ![](相对路径)
  按文件名在 vault 内递归查找(自动适配 Obsidian 附件目录)
- 正文中的图片行会被移除(图片改为上传),其余 markdown 轻度转纯文本
- frontmatter 里的 account 按账号备注名匹配
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config
from .models import Account, SessionLocal


@dataclass
class ObsidianDraft:
    path: str                      # md 绝对路径
    title: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    account_name: str = ""         # frontmatter 指定的账号名(可为空)
    scheduled_at: datetime | None = None
    images: list[str] = field(default_factory=list)  # 图片绝对路径
    cover: str = ""                # 指定封面(图片名)
    warnings: list[str] = field(default_factory=list)


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_WIKILINK_IMG_RE = re.compile(r"!\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_TAG_FM_RE = re.compile(r"^tags:\s*(.+)$", re.M)
_KV_RE = re.compile(r"^(\w+):\s*(.*)$", re.M)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm_raw, body = m.group(1), text[m.end():]
    fm: dict = {}
    for k, v in _KV_RE.findall(fm_raw):
        fm[k.strip().lower()] = v.strip().strip("\"'")
    return fm, body


def _strip_markdown(body: str) -> str:
    """轻度转纯文本:去标题#/列表标记/粗斜体/行内代码,保留换行。"""
    lines = []
    for line in body.splitlines():
        s = line.rstrip()
        if not s.strip():
            lines.append("")
            continue
        # 图片引用整行移除
        if _WIKILINK_IMG_RE.search(s) or _MD_IMG_RE.search(s):
            s = _WIKILINK_IMG_RE.sub("", s)
            s = _MD_IMG_RE.sub("", s)
            if not s.strip():
                continue
        s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s)          # 标题
        s = re.sub(r"^\s*[-*+]\s+", "", s)               # 无序列表
        s = re.sub(r"^\s*\d+\.\s+", "", s)               # 有序列表
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)           # 粗体
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", s)
        s = re.sub(r"`([^`]+)`", r"\1", s)               # 行内代码
        s = re.sub(r"\[\^?\d+\]", "", s)                 # 脚注引用
        lines.append(s)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _find_image(vault: Path, name: str) -> Path | None:
    """按文件名递归找图(支持不带扩展名的 wikilink)。

    沙箱限制(S-01):名称不得含路径分隔符或 ..,结果必须位于 vault 内。
    """
    name = name.strip().replace("\\", "/")
    if not name or name.startswith(".") or "/" in name or ".." in name:
        return None
    leaf = Path(name).name
    vault_root = str(vault.resolve())
    patterns = [leaf] if Path(leaf).suffix else [
        leaf + ext for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")
    ]
    for pattern in patterns:
        for p in vault.rglob(pattern):
            if p.is_file() and not p.name.startswith("."):
                resolved = str(p.resolve())
                if resolved == vault_root or resolved.startswith(vault_root + os.sep):
                    return p
    return None


def parse_markdown_file(path: str, vault_root: str | None = None) -> ObsidianDraft:
    p = Path(path)
    vault = Path(vault_root) if vault_root else p.parent
    draft = ObsidianDraft(path=str(p))
    text = p.read_text(encoding="utf-8", errors="replace")
    fm, body = _parse_frontmatter(text)

    # 标题:frontmatter > 首个 # 标题 > 文件名
    draft.title = fm.get("title", "")
    if not draft.title:
        m = re.search(r"^#\s+(.+)$", body, re.M)
        draft.title = m.group(1).strip() if m else p.stem

    # tags:列表或逗号分隔;另收集正文 #行内标签(标题行除外,保守不做)
    tags_raw = fm.get("tags", "")
    if tags_raw:
        draft.tags = [t.strip().lstrip("#") for t in re.split(r"[,，\s]+", tags_raw) if t.strip()]
    draft.account_name = fm.get("account", "")
    draft.cover = fm.get("cover", "")
    sa = fm.get("scheduled_at", "") or fm.get("发布时间", "")
    if sa:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
            try:
                draft.scheduled_at = datetime.strptime(sa, fmt)
                break
            except ValueError:
                continue
        if draft.scheduled_at is None:
            draft.warnings.append(f"无法解析 scheduled_at: {sa}")

    # 正文清理
    if draft.title and re.search(rf"^#\s+{re.escape(draft.title)}\s*$", body, re.M):
        body = re.sub(rf"^#\s+{re.escape(draft.title)}\s*\n?", "", body, count=1, flags=re.M)
    draft.content = _strip_markdown(body)

    # 图片解析
    seen: set[str] = set()
    for name in _WIKILINK_IMG_RE.findall(body) + _MD_IMG_RE.findall(body):
        name = name.split("|")[0].strip()
        if name.startswith("http"):
            draft.warnings.append(f"网络图片暂不支持,已跳过: {name[:60]}")
            continue
        if name in seen:
            continue
        seen.add(name)
        img = _find_image(vault, name)
        if img:
            draft.images.append(str(img))
        else:
            draft.warnings.append(f"找不到图片文件: {name}")

    if draft.cover:
        img = _find_image(vault, draft.cover)
        if img:
            img_str = str(img)
            if img_str in draft.images:
                draft.images.remove(img_str)  # 同类型比较,避免 Path/str 永假(F-07.1)
            draft.images.insert(0, img_str)  # 封面放第一张
        else:
            draft.warnings.append(f"找不到封面图: {draft.cover}")

    return draft


def scan_dir(dir_path: str) -> list[ObsidianDraft]:
    """扫描目录下所有顶层 .md(不递归子目录,子目录当分类用)。"""
    root = Path(dir_path).expanduser()
    if not root.is_dir():
        raise NotADirectoryError(f"目录不存在: {dir_path}")
    drafts = []
    for md in sorted(root.glob("*.md")):
        if md.name.startswith((".", "_")):
            continue
        try:
            drafts.append(parse_markdown_file(str(md), vault_root=str(root)))
        except Exception as e:  # noqa: BLE001
            d = ObsidianDraft(path=str(md), title=md.stem)
            d.warnings.append(f"解析失败: {e}")
            drafts.append(d)
    return drafts


def import_draft(draft: ObsidianDraft, account_id: int) -> tuple[int, list[str]]:
    """把草稿复制到 uploads 并创建笔记记录,返回 (note_id, warnings)。"""
    from .models import Note, SessionLocal, now as _now

    warnings = list(draft.warnings)
    media: list[str] = []
    for img in draft.images:
        if len(media) >= 18:
            warnings.append("图片超过 18 张,已截断")
            break
        dest_name = f"{_now().strftime('%Y%m%d%H%M%S%f')}_{Path(img).name}"
        dest = config.UPLOADS_DIR / dest_name
        shutil.copy2(img, dest)
        media.append(dest_name)

    if not media:
        return 0, warnings + ["无配图,已跳过(图文笔记必须至少一张图片)"]
    if not draft.content.strip():
        warnings.append("正文为空,仅有图片")

    scheduled = draft.scheduled_at
    if scheduled:
        from .scheduler import apply_jitter
        gap_h = (scheduled - _now().replace(microsecond=0)).total_seconds() / 3600
        mode = "platform" if gap_h >= config.PLATFORM_SCHEDULE_THRESHOLD_H else "local"
        if mode == "local":
            scheduled = apply_jitter(scheduled)  # 本地定时加 0~10 分钟抖动
    else:
        mode = "immediate"

    title = draft.title.strip()
    if len(title) > 20:
        warnings.append(f"标题 {len(title)} 字超过平台 20 字限制,已截断")
        title = title[:20]

    with SessionLocal() as s:
        note = Note(
            account_id=account_id,
            title=title,
            content=draft.content,
            note_type="image",
            media_paths=media,
            tags=draft.tags[:10],
            status="pending" if mode != "immediate" else "pending",
            schedule_mode=mode,
            scheduled_at=scheduled,
            source="obsidian",
        )
        s.add(note)
        s.commit()
        return note.id, warnings


def match_account_id(name: str) -> int | None:
    if not name:
        return None
    with SessionLocal() as s:
        acc = s.query(Account).filter(Account.name == name).first()
        return acc.id if acc else None


def move_to_published(md_path: str, dest_dir: str) -> str | None:
    """发布成功后把 md 挪到「已发布」文件夹;返回新路径。"""
    src = Path(md_path)
    dest = Path(dest_dir).expanduser()
    if not src.exists() or not dest.is_dir():
        return None
    target = dest / src.name
    i = 1
    while target.exists():
        target = dest / f"{src.stem}_{i}{src.suffix}"
        i += 1
    shutil.move(str(src), str(target))
    return str(target)
