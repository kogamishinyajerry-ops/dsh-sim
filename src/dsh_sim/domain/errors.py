"""统一错误模型（定义书 §错误与幂等；CONVENTIONS §3.3）。

错误对象：{code, message, retryable, trace_id, details}。
HTTP 映射：401 未认证 / 403 越权 / 409 摘要·修订·幂等冲突 / 422 工程输入不成立 / 429 配额 / 503 暂不可用。
客户端只能对 retryable 与已定义状态采取有界重试。
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    VALIDATION = "VALIDATION"  # 结构/契约校验失败（422）
    UNAUTHORIZED = "UNAUTHORIZED"  # 未认证（401）
    FORBIDDEN = "FORBIDDEN"  # 越权（403）
    CONFLICT_DIGEST = "CONFLICT_DIGEST"  # 摘要冲突（409）
    CONFLICT_REVISION = "CONFLICT_REVISION"  # 修订/fencing 版本冲突（409）
    CONFLICT_IDEMPOTENCY = "CONFLICT_IDEMPOTENCY"  # 同 key 异摘要（409）
    ENGINEERING_INPUT = "ENGINEERING_INPUT"  # 工程输入不成立（422）
    QUOTA = "QUOTA"  # 配额限制（429）
    UNAVAILABLE = "UNAVAILABLE"  # 暂不可用（503）
    BLOCKED = "BLOCKED"  # 明确阻塞：缺前置能力/证据，非故障（503，不可重试自动成功）


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION: 422,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.CONFLICT_DIGEST: 409,
    ErrorCode.CONFLICT_REVISION: 409,
    ErrorCode.CONFLICT_IDEMPOTENCY: 409,
    ErrorCode.ENGINEERING_INPUT: 422,
    ErrorCode.QUOTA: 429,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.BLOCKED: 503,
}


class ApiError(Exception):
    """业务异常。api 层统一翻译为定义书错误模型。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details: dict[str, Any] = details or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    def to_body(self, trace_id: str) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "trace_id": trace_id,
            "details": self.details,
        }


__all__ = ["ApiError", "ErrorCode", "HTTP_STATUS"]
