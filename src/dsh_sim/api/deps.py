"""API 依赖：身份解析（显式开发模式）、DB 会话、幂等上下文。

- 身份：**只有显式声明本地开发模式**时才接受自报头
  X-Dev-Subject / X-Dev-Roles / X-Dev-Projects；其它情况（未声明、空值、
  无法识别的取值、明确的生产模式）一律 401 硬拒绝——生产用户主体必须来自
  受信 IdP（TBD-08）。上游报告问题 5：界面角色下拉框不是安全边界。
- 幂等：Idempotency-Key → idempotency_records；同 key 同摘要返回原响应，
  同 key 异摘要 → 409 CONFLICT_IDEMPOTENCY（CONVENTIONS §3.4）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.db.models import IdempotencyRecordRow
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.domain.identity import (
    DEV_HEADER_PROJECTS,
    DEV_HEADER_ROLES,
    DEV_HEADER_SUBJECT,
    Identity,
    parse_dev_identity,
)

#: 显式身份模式开关。只有取值在 DEV_MODE_VALUES 内才算开发模式。
IDENTITY_MODE_ENV = "DSH_SIM_IDENTITY_MODE"
DEV_MODE_VALUES = frozenset({"dev", "development", "local"})
DEV = "dev"
PROD = "prod"


def resolve_identity_mode(raw: str | None) -> str:
    """把开关值解析为 dev / prod。

    **fail-closed**：未设置、空串或任何无法识别的取值都按生产模式处理；
    只有显式写出 dev/development/local 才启用开发模式身份头。
    """
    return DEV if (raw or "").strip().lower() in DEV_MODE_VALUES else PROD


def get_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_identity(request: Request) -> Identity:
    mode = getattr(request.app.state, "identity_mode", PROD)
    headers = {k.lower(): v for k, v in request.headers.items()}
    if mode != DEV:
        # 上游报告问题 5：非开发环境默认拒绝自报身份头，不能先信任输入再靠角色判断兜底。
        looks_like_dev = any(
            h.lower() in headers
            for h in (DEV_HEADER_SUBJECT, DEV_HEADER_ROLES, DEV_HEADER_PROJECTS)
        )
        raise ApiError(
            ErrorCode.UNAUTHORIZED,
            "身份模式非开发模式：拒绝自报身份头（X-Dev-*）。"
            "生产环境用户主体与权限必须来自受信身份提供方（TBD-08）",
            details={
                "identity_mode": mode,
                "dev_headers_present": looks_like_dev,
                "hint": f"仅本地开发可用 {IDENTITY_MODE_ENV}=dev 显式开启开发模式",
            },
        )
    identity = parse_dev_identity(headers)
    if identity is None:
        raise ApiError(ErrorCode.UNAUTHORIZED, "缺少身份凭据（开发模式：X-Dev-Subject 头）")
    request.state.identity = identity
    return identity


def active_project(identity: Identity, request: Request) -> str:
    """当前活动项目：X-Dev-Project 头优先，否则取身份项目首项。"""
    override = request.headers.get("x-dev-project")
    if override:
        if override not in identity.project_ids:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                "X-Dev-Project 不在身份授权项目集合内",
                details={"project_id": override},
            )
        return override
    if identity.project_ids:
        return sorted(identity.project_ids)[0]
    return "-"


@dataclass
class StoredResponse:
    status_code: int
    body: dict[str, Any]
    resource_id: str | None


class IdempotencyContext:
    """单次请求的幂等上下文。用法：
        stored = idem.lookup(body)   # 命中 → 直接返回 stored
        ... 执行业务 ...
        idem.store(resource_id, status, body)
    """

    def __init__(
        self, session: Session, *, subject_id: str, project_id: str, action: str, key: str
    ) -> None:
        self._session = session
        self.subject_id = subject_id
        self.project_id = project_id
        self.action = action
        self.key = key

    def lookup(self, request_body: dict[str, Any]) -> StoredResponse | None:
        """同 key 同摘要 → 返回已存响应；同 key 异摘要 → 409。"""
        request_sha = sha256_hex(canonical_dumps(request_body))
        rec = (
            self._session.query(IdempotencyRecordRow)
            .filter_by(
                subject_id=self.subject_id,
                project_id=self.project_id,
                action=self.action,
                key=self.key,
            )
            .first()
        )
        if rec is None:
            self._pending_sha = request_sha
            return None
        if rec.request_sha256 != request_sha:
            raise ApiError(
                ErrorCode.CONFLICT_IDEMPOTENCY,
                "Idempotency-Key 已用于不同请求摘要（主体+项目+action+key 唯一）",
                details={"action": self.action, "key": self.key},
            )
        if rec.response_body is None:
            # 上次请求未完成后崩溃：允许按原摘要继续（不视为冲突）
            self._pending_sha = request_sha
            return None
        return StoredResponse(rec.response_status or 200, rec.response_body, rec.resource_id)

    def store(self, resource_id: str | None, status_code: int, body: dict[str, Any]) -> None:
        rec = (
            self._session.query(IdempotencyRecordRow)
            .filter_by(
                subject_id=self.subject_id,
                project_id=self.project_id,
                action=self.action,
                key=self.key,
            )
            .first()
        )
        if rec is None:
            rec = IdempotencyRecordRow(
                subject_id=self.subject_id,
                project_id=self.project_id,
                action=self.action,
                key=self.key,
                request_sha256=self._pending_sha,
            )
            self._session.add(rec)
        rec.resource_id = resource_id
        rec.response_body = body
        rec.response_status = status_code
        # 持久化承诺（P08 复测发现的竞态修复）：业务行 + 幂等记录必须在响应
        # 发出前同事务落盘。FastAPI yield 依赖的 commit 发生在响应发送之后，
        # SIGKILL/断电窗口内客户端拿到 job_id 但库里无作业——违反持久队列承诺。
        # 此处显式 commit（get_session 的后续 commit 为幂等 no-op）。
        self._session.commit()


def idempotency(action: str):
    """FastAPI 依赖工厂：产生副作用的 POST 必带 Idempotency-Key（8—128 字符）。"""

    def dependency(
        request: Request,
        session: Session = Depends(get_session),
        identity: Identity = Depends(get_identity),
    ) -> IdempotencyContext:
        key = request.headers.get("idempotency-key")
        if key is None or not (8 <= len(key) <= 128):
            raise ApiError(
                ErrorCode.VALIDATION,
                "产生副作用的 POST 必须携带 Idempotency-Key 头（8—128 字符）",
            )
        project_id = active_project(identity, request)
        return IdempotencyContext(
            session,
            subject_id=identity.subject_id,
            project_id=project_id,
            action=action,
            key=key,
        )

    return dependency


__all__ = [
    "DEV",
    "DEV_MODE_VALUES",
    "IDENTITY_MODE_ENV",
    "PROD",
    "IdempotencyContext",
    "StoredResponse",
    "active_project",
    "get_identity",
    "get_session",
    "idempotency",
    "resolve_identity_mode",
]
