"""FastAPI 应用工厂（CONVENTIONS §6：uvicorn dsh_sim.api.main:app --port 8600）。

- 统一错误处理：ApiError → 定义书错误模型 {code,message,retryable,trace_id,details}。
- trace_id 注入：每请求 uuid4，响应头 X-Trace-Id。
- 静态挂载 /panels（目录可能为空，容忍缺失）。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from dsh_sim.db.session import init_db, make_engine, make_session_factory
from dsh_sim.domain.errors import ApiError, ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[3]
PANELS_DIR = REPO_ROOT / "panels"
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "var" / "artifacts"
DEFAULT_CAPABILITIES_ROOT = REPO_ROOT / "capabilities"


def create_app(
    database_url: str | None = None,
    artifact_root: str | Path | None = None,
) -> FastAPI:
    app = FastAPI(title="DSH 工业仿真智能体 工程 API", version="0.1.0")

    engine = make_engine(database_url)
    init_db(engine)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.artifact_root = Path(
        artifact_root or os.environ.get("DSH_SIM_ARTIFACT_ROOT") or DEFAULT_ARTIFACT_ROOT
    )

    @app.middleware("http")
    async def trace_id_middleware(request: Request, call_next):  # noqa: ANN001, ANN202
        request.state.trace_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
        return JSONResponse(exc.to_body(trace_id), status_code=exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
        body = ApiError(
            ErrorCode.VALIDATION,
            "请求结构不合法",
            details={"errors": list(exc.errors())},
        ).to_body(trace_id)
        return JSONResponse(body, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
        body = ApiError(
            ErrorCode.UNAVAILABLE,
            "服务内部错误（未分类异常，按暂不可用处 理）",
            retryable=True,
            details={"type": type(exc).__name__},
        ).to_body(trace_id)
        return JSONResponse(body, status_code=503)

    from dsh_sim.api.routes import artifacts, nodes, projections, reviews, runs, tasks, verifications

    app.include_router(tasks.router, prefix="/api/v1", tags=["tasks"])
    app.include_router(runs.router, prefix="/api/v1", tags=["runs"])
    app.include_router(reviews.router, prefix="/api/v1", tags=["reviews"])
    app.include_router(nodes.router, prefix="/api/v1", tags=["jobs"])
    app.include_router(artifacts.router, prefix="/api/v1", tags=["artifacts"])
    # Agent E 增量：读取投影（x-extension: read-projection）与独立复算
    app.include_router(projections.router, prefix="/api/v1", tags=["projections"])
    app.include_router(verifications.router, prefix="/api/v1", tags=["verifications"])

    # /panels 静态挂载：目录可能为空或不存在，容忍（Agent F 后续填充）
    if PANELS_DIR.is_dir():
        app.mount("/panels", StaticFiles(directory=PANELS_DIR), name="panels")

    # WP-17：启动时扫描 capabilities/ 注册能力包（只注册为 DRAFT；
    # RELEASED 必须来自人工发布流程，注册器内置安全网降级）。
    caps_root = Path(
        os.environ.get("DSH_SIM_CAPABILITIES_ROOT") or DEFAULT_CAPABILITIES_ROOT
    )
    if caps_root.is_dir():
        from dsh_sim.capabilities import register_capabilities

        with app.state.session_factory() as s:
            app.state.capability_scan = register_capabilities(s, caps_root)
    else:
        app.state.capability_scan = []

    return app


app = create_app()

__all__ = ["app", "create_app"]
