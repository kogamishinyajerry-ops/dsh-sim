"""FastAPI 应用工厂（CONVENTIONS §6：uvicorn dsh_sim.api.main:app --port 8600）。

- 统一错误处理：ApiError → 定义书错误模型 {code,message,retryable,trace_id,details}。
- trace_id 注入：每请求 uuid4，响应头 X-Trace-Id。
- 静态挂载 /panels（目录可能为空，容忍缺失）。
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from dsh_sim.api.deps import IDENTITY_MODE_ENV, PROD, resolve_identity_mode
from dsh_sim.db.session import init_db, make_engine, make_session_factory
from dsh_sim.domain.errors import ApiError, ErrorCode
from dsh_sim.resources import (
    ENV_ALLOW_MISSING_RESOURCES,
    MissingResourceError,
    allow_missing_resources,
    resolve_capabilities,
    resolve_panels,
    state_dir,
)

logger = logging.getLogger("dsh_sim.api")


def create_app(
    database_url: str | None = None,
    artifact_root: str | Path | None = None,
    identity_mode: str | None = None,
) -> FastAPI:
    """应用工厂。

    identity_mode：显式身份模式；缺省读环境变量 DSH_SIM_IDENTITY_MODE。
    **fail-closed**：未显式声明开发模式即按生产模式处理（拒绝自报身份头）。
    """
    app = FastAPI(title="DSH 工业仿真智能体 工程 API", version="0.1.0")

    engine = make_engine(database_url)
    init_db(engine)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.artifact_root = Path(
        artifact_root or os.environ.get("DSH_SIM_ARTIFACT_ROOT") or (state_dir() / "artifacts")
    )
    app.state.identity_mode = resolve_identity_mode(
        identity_mode if identity_mode is not None else os.environ.get(IDENTITY_MODE_ENV)
    )
    if app.state.identity_mode == PROD:
        logger.warning(
            "身份模式=prod：拒绝 X-Dev-* 自报身份头（生产用户主体须来自受信 IdP，TBD-08）。"
            "本地联调请显式设置 %s=dev。",
            IDENTITY_MODE_ENV,
        )
    else:
        logger.warning(
            "身份模式=dev：接受 X-Dev-* 自报身份头，仅供本地/隔离环境，不得用于生产。"
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

    # ------------------------------------------------------------------
    # 外部资源解析与启动检查（上游验收报告 §五：明确外置配置，缺资源必须暴露，
    # 不能像旧实现那样把 site-packages 当仓库根、静默地挂空面板/注册空能力包）。
    # ------------------------------------------------------------------
    missing: list[MissingResourceError] = []
    cap_res = resolve_capabilities()
    panel_res = resolve_panels()
    if not cap_res.found:
        missing.append(
            MissingResourceError(
                "capabilities",
                env_var=cap_res.env_var,
                tried=cap_res.tried,
                hint="能力包 rules/metrics/domain 是数值校核与证据冻结的输入。",
            )
        )
    if not panel_res.found:
        missing.append(
            MissingResourceError(
                "panels",
                env_var=panel_res.env_var,
                tried=panel_res.tried,
                hint="执行台/审查台面板由工程 API 静态挂载在 /panels 下。",
            )
        )
    if missing and not allow_missing_resources():
        # fail-fast：缺资源时拒绝启动，错误里带已尝试位置与可执行的修复动作
        raise RuntimeError("\n\n".join(str(exc) for exc in missing))
    if missing:
        for exc in missing:
            logger.error("资源缺失但已按 %s 降级启动：%s", ENV_ALLOW_MISSING_RESOURCES, exc)
    for res in (cap_res, panel_res):
        logger.warning(
            "外部资源 %s → %s（来源 %s）",
            res.name,
            res.path if res.found else "<缺失>",
            res.source,
        )

    # /panels 静态挂载
    if panel_res.path is not None:
        app.mount("/panels", StaticFiles(directory=panel_res.path), name="panels")

    # WP-17：启动时扫描 capabilities/ 注册能力包（只注册为 DRAFT；
    # RELEASED 必须来自人工发布流程，注册器内置安全网降级）。
    if cap_res.path is not None:
        from dsh_sim.capabilities import register_capabilities

        with app.state.session_factory() as s:
            app.state.capability_scan = register_capabilities(s, cap_res.path)
    else:
        app.state.capability_scan = []

    app.state.resource_status = {
        "capabilities": cap_res.as_dict(),
        "panels": panel_res.as_dict(),
        "missing": [res.name for res in (cap_res, panel_res) if not res.found],
    }

    return app


app = create_app()

__all__ = ["app", "create_app"]
