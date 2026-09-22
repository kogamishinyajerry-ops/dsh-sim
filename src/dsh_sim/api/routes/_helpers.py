"""路由共用助手：幂等回放/存储的统一模式。"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from dsh_sim.api.deps import IdempotencyContext, StoredResponse


def replay_or_none(stored: StoredResponse | None) -> JSONResponse | None:
    if stored is None:
        return None
    return JSONResponse(stored.body, status_code=stored.status_code)


def respond(
    idem: IdempotencyContext,
    *,
    resource_id: str | None,
    status_code: int,
    body: BaseModel | dict[str, Any],
) -> JSONResponse:
    payload = body.model_dump(mode="json") if isinstance(body, BaseModel) else body
    idem.store(resource_id, status_code, payload)
    return JSONResponse(payload, status_code=status_code)


__all__ = ["replay_or_none", "respond"]
