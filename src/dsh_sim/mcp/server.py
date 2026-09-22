"""dsh-sim FastMCP stdio server（Agent G / WP-11）。

工程 API 的受限投影：12 个工具，名字与 CONVENTIONS §3.5 一字不差。

强约束（定义书 §DSH接入与工具边界 / §锁版接入方式）：
- 每个工具 = 工程 API（HTTP）的受限投影；本层不产生工程数据，不做本地计算。
- 一律以 AGENT 身份调 API（X-Dev-Subject: agent-dsh-sim / X-Dev-Roles: AGENT）。
  模型侧无授权/接受能力：authorizeRuns / decideReview / confirmIssue / closeIssue
  根本不在工具清单中；即使直接调 API，服务端也会对 Agent Bearer 返回 403。
- 长操作（prepare_task / submit_runs / build_bundle）快速返回资源 id + 轮询提示，
  不阻塞等待计算结束（MCP 60s 调用上限，定义书 §锁版接入方式）。
- 服务不可达 → 结构化错误 {code:"UNAVAILABLE", message:"工程服务不可用",
  retryable:true}；禁止 fallback 到本地生成脚本或伪造数据。
- 禁止暴露：run_any_code / 任意 SQL / 任意文件路径 / approve 类工具；
  server 启动时自检工具清单并打印（stderr，stdout 是 JSON-RPC 通道）。
- get_evidence 只返回摘要 + artifact 引用，不把大文件/场数据塞进文本返回。

运行：python -m dsh_sim.mcp.server  （CONVENTIONS §6）
配置：env DSH_SIM_API_URL（默认 http://127.0.0.1:8600/api/v1）；
     env DSH_SIM_AGENT_PROJECTS（AGENT 身份项目集合，逗号分隔，默认 proj_a——
     开发模式默认值，生产身份体系属 TBD-08）。
"""
from __future__ import annotations

import os
import sys
import uuid
from typing import Any

import httpx
from fastmcp import FastMCP

from dsh_sim.canonical import canonical_dumps, sha256_hex

# ---------------------------------------------------------------------------
# 常量（CONVENTIONS §3.5：12 工具名一字不差；禁止工具清单）
# ---------------------------------------------------------------------------

EXPECTED_TOOLS: tuple[str, ...] = (
    "list_capabilities",
    "get_task",
    "create_task",
    "revise_task",
    "prepare_task",
    "get_preparation",
    "submit_runs",
    "get_run",
    "cancel_run",
    "build_bundle",
    "get_evidence",
    "draft_review_issue",
)

# 禁止暴露的工具名模式（定义书 §DSH接入与工具边界）
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "run_any_code",
    "exec",
    "sql",
    "shell",
    "approve",
    "authorize",
    "decide",
    "confirm",
    "accept",
    "file_path",
    "read_file",
    "write_file",
)

DEFAULT_API_URL = "http://127.0.0.1:8600/api/v1"
AGENT_SUBJECT = "agent-dsh-sim"
AGENT_ROLES = "AGENT"
CALL_TIMEOUT_SECONDS = 60.0  # 定义书 §锁版接入方式：MCP 默认单次调用 60 秒
EVIDENCE_MAX_ENTRIES = 20  # get_evidence manifest 摘要截断上限

mcp = FastMCP("dsh-sim")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """懒构造 API 客户端。测试可替换模块级 _client 注入 ASGI transport。

    trust_env=False：工程服务是本机回环地址，必须直连。若走系统代理
    （如用户开启 Clash 等），代理对 127.0.0.1 的请求可能返回 502/拒绝，
    把"服务不可达"误判为"服务返回异常响应"（实测案例：端口 9 探活被
    代理拦成 HTTP_502 而非 ConnectError→UNAVAILABLE）。
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=os.environ.get("DSH_SIM_API_URL", DEFAULT_API_URL),
            timeout=httpx.Timeout(CALL_TIMEOUT_SECONDS),
            trust_env=False,
            headers={
                "X-Dev-Subject": AGENT_SUBJECT,
                "X-Dev-Roles": AGENT_ROLES,
                "X-Dev-Projects": os.environ.get("DSH_SIM_AGENT_PROJECTS", "proj_a"),
            },
        )
    return _client


def _local_error(code: str, message: str, *, retryable: bool, details: dict | None = None) -> dict:
    """桥层本地产生的错误（服务未给出 Error 模型时）。trace_id 本地生成并标记来源。"""
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "trace_id": str(uuid.uuid4()),
        "details": {"source": "mcp-bridge", **(details or {})},
    }


def _idem_key(tool: str, args: dict[str, Any], explicit: str | None) -> str:
    """幂等键：显式传入优先；否则按 工具名+规范化参数 确定性派生。

    同参数重试 → 同 key → 服务端返回原对象（CONVENTIONS §3.4 幂等语义的客户端侧落实）。
    """
    if explicit:
        return explicit
    digest = sha256_hex(f"mcp-dsh-sim:{tool}:{canonical_dumps(args)}")
    return f"mcp-{digest[:48]}"


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    """统一 API 调用：错误模型透传 / 不可达 → UNAVAILABLE。绝不伪造成功。"""
    headers = {"Idempotency-Key": idem_key} if idem_key else {}
    try:
        resp = await _get_client().request(
            method, path, json=json_body, params=params, headers=headers
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
            httpx.PoolTimeout, httpx.NetworkError, OSError) as exc:
        return _local_error(
            "UNAVAILABLE",
            "工程服务不可用",
            retryable=True,
            details={"reason": type(exc).__name__},
        )
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "code" in body:
            return body  # 服务端 Error 模型原样透传（含 403 越权拒绝，如实呈现）
        return _local_error(
            f"HTTP_{resp.status_code}",
            f"工程服务返回非错误模型响应（{resp.status_code}）",
            retryable=resp.status_code in (429, 503),
            details={"http_status": resp.status_code},
        )
    return resp.json()


def _with_hint(body: dict[str, Any], hint: str) -> dict[str, Any]:
    """长操作受理响应附轮询提示（请求返回不是计算结束，定义书 §工程API）。"""
    if "code" in body:
        return body
    return {**body, "hint": hint}


# ---------------------------------------------------------------------------
# 12 个受限工具（名字一字不差，勿改）
# ---------------------------------------------------------------------------


@mcp.tool
async def list_capabilities(cursor: str | None = None, limit: int = 50) -> dict:
    """查询当前域已发布（RELEASED）能力包目录（游标分页）。

    只读操作。模型侧无授权/接受能力：本工具集不含 authorizeRuns/decideReview 等
    人工批准动作，Agent 身份调用服务端亦会被 403 拒绝。
    """
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    return await _request("GET", "/capabilities", params=params)


@mcp.tool
async def get_task(task_id: str) -> dict:
    """查询任务当前多维状态（task_state/review_state/blockers 等）。

    只读操作。模型侧无授权/接受能力。阻塞不是静默跳过：缺输入/回读失败在
    blockers 中表达。
    """
    return await _request("GET", f"/tasks/{task_id}")


@mcp.tool
async def create_task(draft: dict, idempotency_key: str | None = None) -> dict:
    """创建任务草稿（符合 task-draft.schema.json；缺失输入以 open_questions 显式阻塞，禁止补猜）。

    草稿不是批准：模型侧无授权/接受能力，后续 prepare/authorize 仍需人工确认链。
    幂等：同参数重试返回同一任务。
    """
    args = {"draft": draft}
    return await _request(
        "POST", "/tasks", json_body=args,
        idem_key=_idem_key("create_task", args, idempotency_key),
    )


@mcp.tool
async def revise_task(
    task_id: str,
    expected_revision: int,
    spec: dict,
    idempotency_key: str | None = None,
) -> dict:
    """创建新修订（完整 TaskSpec）；expected_revision 防并发覆盖。

    新修订使旧授权/旧证据包对新输入失效（FR-23）。模型侧无授权/接受能力。
    输入变化必须走新修订，不能原位改写。
    """
    args = {"expected_revision": expected_revision, "spec": spec}
    return await _request(
        "POST", f"/tasks/{task_id}/revisions", json_body=args,
        idem_key=_idem_key("revise_task", {"task_id": task_id, **args}, idempotency_key),
    )


@mcp.tool
async def prepare_task(task_id: str, revision: int, idempotency_key: str | None = None) -> dict:
    """发起异步准备作业（PREPARE）：创建副本、白名单写入、真实回读；不在该动作开始正式求解。

    快速返回 preparation_id + job_id；准备可能等待软件资源。模型侧无授权/接受能力。
    """
    args = {"revision": revision}
    body = await _request(
        "POST", f"/tasks/{task_id}/prepare", json_body=args,
        idem_key=_idem_key("prepare_task", {"task_id": task_id, **args}, idempotency_key),
    )
    return _with_hint(
        body,
        "长作业已受理（202≠完成）。用 get_preparation(preparation_id) 轮询真实回读与差异；"
        "授权必须由工程师在受信面板人工确认，模型无法代办。",
    )


@mcp.tool
async def get_preparation(preparation_id: str) -> dict:
    """读取准备结果：实际设置回读、申请/回读差异、blockers。

    只读操作。READY 仅表示准备完成且回读一致，不表示工程结果已通过。
    """
    return await _request("GET", f"/preparations/{preparation_id}")


@mcp.tool
async def submit_runs(
    task_id: str,
    authorization_id: str,
    prepared_digest: str,
    idempotency_key: str | None = None,
) -> dict:
    """依据已存在的人工授权持久入队；准备摘要变化 409 拒绝。

    模型侧无授权能力：authorization_id 只能来自工程师受信面板的 authorizeRuns，
    Agent 身份无法自行取得（服务端 403）。快速返回 run_ids；请求返回不是计算结束。
    """
    args = {"authorization_id": authorization_id, "prepared_digest": prepared_digest}
    body = await _request(
        "POST", f"/tasks/{task_id}/submissions", json_body=args,
        idem_key=_idem_key("submit_runs", {"task_id": task_id, **args}, idempotency_key),
    )
    return _with_hint(
        body,
        "已受理入队（202≠完成）。用 get_run(run_id) 轮询多维状态；"
        "SUCCEEDED 仅证明执行与文件收集成功，不等于数值 PASS。",
    )


@mcp.tool
async def get_run(run_id: str, after_seq: int = 0) -> dict:
    """查询 Run 多维状态（execution/numerical/applicability）与增量事件。

    只读操作。程序成功 ≠ 数值 PASS；无证据不报成功。
    """
    return await _request("GET", f"/runs/{run_id}", params={"after_seq": after_seq})


@mcp.tool
async def cancel_run(run_id: str, reason: str | None = None, idempotency_key: str | None = None) -> dict:
    """提出受控取消请求（异步）；取消请求不等于已停止。

    全部受控子进程退出后才显示 CANCELLED；无法证实退出标记 LOST。
    """
    args: dict[str, Any] = {"reason": reason} if reason else {}
    body = await _request(
        "POST", f"/runs/{run_id}/cancel", json_body=args,
        idem_key=_idem_key("cancel_run", {"run_id": run_id, **args}, idempotency_key),
    )
    return _with_hint(body, "取消请求已入库；用 get_run(run_id) 轮询直到现场确认 CANCELLED/LOST。")


@mcp.tool
async def build_bundle(task_id: str, revision: int, idempotency_key: str | None = None) -> dict:
    """异步构建冻结证据集合（bundle）；检查必需 Run 与定义一致性。

    快速返回 bundle_id + job_id。模型侧无接受能力：提交审查与 ACCEPT 均需人工。
    """
    args = {"revision": revision}
    body = await _request(
        "POST", f"/tasks/{task_id}/bundles", json_body=args,
        idem_key=_idem_key("build_bundle", {"task_id": task_id, **args}, idempotency_key),
    )
    return _with_hint(body, "构建作业已受理（202≠完成）。用 get_evidence(bundle_id) 查询冻结清单与摘要。")


@mcp.tool
async def get_evidence(bundle_id: str, max_entries: int = EVIDENCE_MAX_ENTRIES) -> dict:
    """读取冻结证据包摘要与 artifact 引用清单。

    只返回摘要 + artifact 引用（artifact_id/logical_path/sha256），不把大文件/场数据
    塞进文本返回；原始内容经受权 artifact 接口另行流式读取。manifest 超长时截断并
    标记 truncated。
    """
    body = await _request("GET", f"/bundles/{bundle_id}")
    if "code" in body:
        return body
    manifest = body.get("manifest") or []
    total = len(manifest)
    shown = manifest[: max(0, max_entries)]
    return {
        "bundle_id": body.get("bundle_id"),
        "task_id": body.get("task_id"),
        "revision": body.get("revision"),
        "bundle_digest": body.get("bundle_digest"),
        "validity": body.get("validity"),
        "created_at": body.get("created_at"),
        "manifest_total": total,
        "manifest": shown,
        "manifest_truncated": total > len(shown),
        "hint": "manifest 仅含 artifact 引用；大文件内容请经受权 artifact 接口流式读取，不在此返回。",
    }


@mcp.tool
async def draft_review_issue(
    review_id: str,
    responsible: str,
    severity: str,
    description: str,
    close_criteria: str,
    related_artifact_ids: list[str] | None = None,
    related_run_ids: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """把质疑整理为审查问题草稿（DRAFT）。

    模型创建的问题永远是 DRAFT：转 OPEN 需指定审查人 confirmIssue 人工确认，
    关闭需审查人核对证据后 closeIssue；模型侧无确认/关闭/接受能力。
    """
    args: dict[str, Any] = {
        "responsible": responsible,
        "severity": severity,
        "description": description,
        "close_criteria": close_criteria,
        "related_artifact_ids": related_artifact_ids,
        "related_run_ids": related_run_ids,
    }
    payload = {k: v for k, v in args.items() if v is not None}
    return await _request(
        "POST", f"/reviews/{review_id}/issues", json_body=payload,
        idem_key=_idem_key("draft_review_issue", {"review_id": review_id, **payload}, idempotency_key),
    )


# ---------------------------------------------------------------------------
# 启动自检：工具清单 == 12 期望；禁止模式扫描。打印走 stderr（stdout 是 JSON-RPC）。
# ---------------------------------------------------------------------------


async def _registered_tool_names() -> list[str]:
    tools = await mcp.list_tools()
    return sorted(t.name for t in tools)


def selfcheck(tool_names: list[str]) -> list[str]:
    """返回违规信息列表；空列表 = 通过。"""
    problems: list[str] = []
    names = sorted(tool_names)
    expected = sorted(EXPECTED_TOOLS)
    if names != expected:
        missing = [n for n in expected if n not in names]
        extra = [n for n in names if n not in expected]
        problems.append(f"工具清单不匹配：missing={missing} extra={extra}")
    for name in names:
        lowered = name.lower()
        for pat in FORBIDDEN_PATTERNS:
            if pat in lowered:
                problems.append(f"禁止模式命中：{name} ~ {pat}")
    return problems


def main() -> None:
    import asyncio

    names = asyncio.run(_registered_tool_names())
    problems = selfcheck(names)
    print(f"[dsh-sim-mcp] 工具清单自检：{len(names)} 个工具", file=sys.stderr)
    for n in names:
        print(f"[dsh-sim-mcp]   - {n}", file=sys.stderr)
    if problems:
        for p in problems:
            print(f"[dsh-sim-mcp] 自检失败：{p}", file=sys.stderr)
        raise SystemExit(2)
    print("[dsh-sim-mcp] 自检通过：无禁止工具（run_any_code/SQL/文件路径/approve 类）", file=sys.stderr)
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
