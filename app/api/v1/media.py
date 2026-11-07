"""v1 媒体管理 API:素材上传、安全校验与托管。"""
from __future__ import annotations

import os
import uuid
from datetime import datetime

from fastapi import APIRouter, File, HTTPException, UploadFile

from ... import config
from ..notes import ALLOWED_IMAGE_EXT, ALLOWED_VIDEO_EXT, MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, _check_magic

router = APIRouter(prefix="/media", tags=["v1-media"])


@router.post("")
def upload_media(file: UploadFile = File(...)):
    """上传并托管媒体素材(图片/视频)。返回托管文件相对路径名。"""
    ext = os.path.splitext(file.filename or "")[1].lower()
    is_video = ext in ALLOWED_VIDEO_EXT
    is_image = ext in ALLOWED_IMAGE_EXT
    if not (is_video or is_image):
        allowed = ALLOWED_IMAGE_EXT | ALLOWED_VIDEO_EXT
        raise HTTPException(400, f"不支持的文件类型 {ext},允许:{','.join(allowed)}")

    max_bytes = MAX_VIDEO_BYTES if is_video else MAX_IMAGE_BYTES
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

    return {
        "media_id": name,
        "path": name,
        "type": "video" if is_video else "image",
        "size": size,
    }

