"""素材导入服务:负责从外部路径安全导入媒体到受管 uploads 目录。"""
from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from .. import config
from ..api.notes import (
    ALLOWED_IMAGE_EXT,
    ALLOWED_VIDEO_EXT,
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    _check_magic,
)
from .errors import ServiceError


def import_media_file(source_path: str | Path) -> dict:
    """将外部媒体文件复制进服务受管目录并校验有效性。
    支持宿主 AI 临时生成的图片或本地素材导入。
    """
    src = Path(source_path).resolve()
    if not src.exists() or not src.is_file():
        raise ServiceError("FILE_NOT_FOUND", f"素材文件不存在: {src}", 404)

    ext = src.suffix.lower()
    is_video = ext in ALLOWED_VIDEO_EXT
    is_image = ext in ALLOWED_IMAGE_EXT
    if not (is_video or is_image):
        allowed = ALLOWED_IMAGE_EXT | ALLOWED_VIDEO_EXT
        raise ServiceError("INVALID_MEDIA_TYPE", f"不支持的文件类型 {ext},允许:{','.join(allowed)}", 400)

    max_bytes = MAX_VIDEO_BYTES if is_video else MAX_IMAGE_BYTES
    file_size = src.stat().st_size
    if file_size > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        raise ServiceError("FILE_TOO_LARGE", f"文件大小超过限制 {limit_mb}MB", 400)

    with open(src, "rb") as f:
        head = f.read(16)
    if not _check_magic(ext, head):
        raise ServiceError("INVALID_FILE_HEADER", f"文件内容签名与扩展名 {ext} 不符", 400)

    # 复制到受管 uploads 目录
    dest_name = f"{uuid.uuid4().hex[:12]}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
    dest_path = config.UPLOADS_DIR / dest_name
    shutil.copy2(src, dest_path)

    return {
        "media_id": dest_name,
        "path": dest_name,
        "type": "video" if is_video else "image",
        "size": file_size,
    }

