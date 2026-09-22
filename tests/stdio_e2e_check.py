"""stdio 端到端终验脚本：与 dsh mcp-client 桥接同路径（command/args/env 与 cordis.patch.yml 一致）。

用法: envs/dsh-sim/Scripts/python.exe tests/stdio_e2e_check.py
前提: 工程 API 在 127.0.0.1:8600 运行。
"""
from __future__ import annotations

import asyncio
import json

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

EXPECTED = sorted([
    "build_bundle", "cancel_run", "create_task", "draft_review_issue",
    "get_evidence", "get_preparation", "get_run", "get_task",
    "list_capabilities", "prepare_task", "revise_task", "submit_runs",
])

PYTHON = r"<VENV_DSH_SIM>\Scripts\python.exe"


async def main() -> None:
    transport = StdioTransport(
        PYTHON,
        ["-m", "dsh_sim.mcp.server"],
        env={"DSH_SIM_API_URL": "http://127.0.0.1:8600/api/v1", "PYTHONIOENCODING": "utf-8"},
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools)
        assert names == EXPECTED, f"工具清单不符: {names}"
        print(f"ListTools PASS: {len(names)} tools, 与定义书 §DSH接入 12 工具一字不差")
        result = await client.call_tool("list_capabilities", {})
        text = result.content[0].text if result.content else ""
        payload = json.loads(text)
        # 工具只暴露 RELEASED（定义书：草稿知识不进正式建议）；当前无 RELEASED
        # 包（阈值 TBD），items 为空是唯一诚实结果；DRAFT 不可被模型侧查询。
        assert "items" in payload, payload
        assert payload["items"] == [], payload
        print("CallTool PASS: list_capabilities 真实往返 API，RELEASED 为空（诚实：阈值 TBD，无已发布能力）")


if __name__ == "__main__":
    asyncio.run(main())
