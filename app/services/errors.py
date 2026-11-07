"""业务领域异常定义。"""
from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class AccountNotFoundError(ServiceError):
    def __init__(self, message: str = "账号不存在"):
        super().__init__("ACCOUNT_NOT_FOUND", message, 404)


class NoteNotFoundError(ServiceError):
    def __init__(self, message: str = "笔记不存在"):
        super().__init__("NOTE_NOT_FOUND", message, 404)


class JobNotFoundError(ServiceError):
    def __init__(self, message: str = "发布任务不存在"):
        super().__init__("JOB_NOT_FOUND", message, 404)


class DraftNotEditableError(ServiceError):
    def __init__(self, message: str = "当前笔记状态不可编辑"):
        super().__init__("DRAFT_NOT_EDITABLE", message, 409)


class RevisionConflictError(ServiceError):
    def __init__(self, current_rev: int, expected_rev: int):
        super().__init__(
            "REVISION_CONFLICT",
            f"内容已被修改(当前修订号: {current_rev}, 传入期望号: {expected_rev})",
            409,
        )


class IdempotencyConflictError(ServiceError):
    def __init__(self, message: str = "相同幂等键携带了不同的请求参数"):
        super().__init__("IDEMPOTENCY_CONFLICT", message, 409)


class JobConflictError(ServiceError):
    def __init__(self, message: str = "该笔记已有正在进行中的发布任务"):
        super().__init__("JOB_BUSY", message, 409)


class PublishNotReadyError(ServiceError):
    def __init__(self, missing_fields: list[str]):
        super().__init__(
            "MEDIA_MISSING" if "media_paths" in missing_fields else "PUBLISH_NOT_READY",
            f"草稿缺少必要发布要素: {', '.join(missing_fields)}",
            400,
        )
        self.missing_fields = missing_fields


class CannotCancelError(ServiceError):
    def __init__(self, message: str = "任务处于不可取消的状态"):
        super().__init__("CANNOT_CANCEL", message, 409)


class AccountNotOnlineError(ServiceError):
    def __init__(self, message: str = "账号未登录或登录已失效"):
        super().__init__("LOGIN_REQUIRED", message, 400)

