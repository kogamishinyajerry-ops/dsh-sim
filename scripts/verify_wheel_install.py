"""构建 wheel → 干净目录安装 → 验证资源可达性（上游验收报告 §五）。

报告原文要求：「构建后必须在干净目录安装测试……应明确打包资源或外置配置，
并让启动检查暴露缺资源而不是静默空列表。」

本脚本把这条要求变成可复跑的验收动作（源码树里跑 pytest 不能替代它）：

1. 构建 wheel，检查**包内资源**（报告模板）确实进了产物——旧产物 55 个文件全是 .py；
2. `pip install --target` 把产物装进**干净目录**，探针以 PYTHONPATH 指向该目录运行，
   并断言 `import dsh_sim` 来自这里（不是仓库 src）——否则"安装测试"是假的；
3. **未配置外部资源**：导入服务入口（等同 `uvicorn dsh_sim.api.main:app`）必须
   fail-fast，错误里带已尝试位置与修复动作；
4. **显式降级开关**：允许启动，但缺资源必须留痕（resource_status.missing）；
5. **显式配置外部资源**：启动成功，/panels 挂载、能力包注册；
6. 报告模板经 `importlib.resources` 从装好的包读出。

依赖策略：探针复用当前解释器既有环境（不联网、不重复下载），
但被验证的 `dsh_sim` 一定来自 wheel 安装产物（第 2 项断言保证）。

用法：
    <VENV>/Scripts/python.exe scripts/verify_wheel_install.py
退出码 0 = 全部通过；非 0 = 有验收项失败（逐项 PASS/FAIL）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_MEMBER_SUFFIX = "dsh_sim/evidence/templates/report.html.j2"

# 探针 A：未配置资源 —— 期望"导入即失败"，另验证模板已随包分发、降级开关留痕。
_PROBE_UNCONFIGURED = textwrap.dedent(
    '''
    import json, os

    result = {}

    # 6) 包内资源（报告模板）不依赖外部配置，应当随包可读
    from dsh_sim.evidence import report
    result["template_chars"] = len(report._load_template_text())

    import dsh_sim
    result["dsh_sim_file"] = dsh_sim.__file__

    # 3) 未配置外部资源 → 导入服务入口（= uvicorn 启动路径）必须 fail-fast
    try:
        import dsh_sim.api.main  # noqa: F401
        result["unconfigured"] = {"raised": False}
    except RuntimeError as exc:
        result["unconfigured"] = {"raised": True, "message": str(exc)}

    # 4) 显式降级开关 → 允许启动，但缺资源必须留痕
    os.environ["DSH_SIM_ALLOW_MISSING_RESOURCES"] = "1"
    import importlib
    mod = importlib.import_module("dsh_sim.api.main")
    result["degraded"] = {
        "missing": mod.app.state.resource_status["missing"],
        "capability_scan": len(mod.app.state.capability_scan),
    }

    print(json.dumps(result, ensure_ascii=False))
    '''
)

# 探针 B：显式配置外部资源 —— 期望启动成功。
_PROBE_CONFIGURED = textwrap.dedent(
    '''
    import json, os

    os.environ["DSH_SIM_CAPABILITIES_ROOT"] = "#REPO#/capabilities"
    os.environ["DSH_SIM_PANELS_ROOT"] = "#REPO#/panels"
    os.environ["DSH_SIM_STATE_DIR"] = "#TMP#/state"

    import dsh_sim
    from dsh_sim.api import main as api_main

    app = api_main.app  # 模块级 app：等同 uvicorn dsh_sim.api.main:app 的启动路径
    mounted = [getattr(r, "name", "") for r in app.routes]
    result = {
        "dsh_sim_file": dsh_sim.__file__,
        "missing": app.state.resource_status["missing"],
        "capabilities_source": app.state.resource_status["capabilities"]["source"],
        "panels_source": app.state.resource_status["panels"]["source"],
        "panels_mounted": "panels" in mounted,
        "capability_scan": len(app.state.capability_scan),
    }
    # 显式 create_app（自定义库/工件目录）同样应成功
    explicit = api_main.create_app(
        database_url="sqlite:///:memory:", artifact_root="#TMP#/artifacts", identity_mode="dev"
    )
    result["explicit_missing"] = explicit.state.resource_status["missing"]
    print(json.dumps(result, ensure_ascii=False))
    '''
)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _probe(work: Path, name: str, source: str, env: dict[str, str]) -> tuple[bool, str, dict]:
    path = work / f"{name}.py"
    path.write_text(
        source.replace("#TMP#", work.as_posix()).replace("#REPO#", REPO_ROOT.as_posix()),
        encoding="utf-8",
    )
    proc = _run([sys.executable, str(path)], env=env)
    if proc.returncode != 0:
        return False, (proc.stdout + proc.stderr)[-2000:], {}
    return True, "", json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    work = Path(tempfile.mkdtemp(prefix="dshsim-wheel-verify-"))
    try:
        # ---- 1) 构建 wheel -------------------------------------------------
        wheel_dir = work / "dist"
        wheel_dir.mkdir()
        built = _run(
            [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(wheel_dir)],
            cwd=str(REPO_ROOT),
        )
        wheels = sorted(wheel_dir.glob("*.whl"))
        checks.append(("构建 wheel", bool(wheels), built.stdout[-400:] + built.stderr[-400:]))
        if not wheels:
            return _report(checks)
        wheel = wheels[0]

        # ---- 2) 包内资源在产物中 ------------------------------------------
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
            has_template = any(n.endswith(TEMPLATE_MEMBER_SUFFIX) for n in names)
            non_py = [
                n
                for n in names
                if not n.endswith(".py") and "dist-info" not in n and not n.endswith(".pyc")
            ]
        checks.append(
            (
                "wheel 含包内资源（报告模板）",
                has_template,
                f"非 .py 资源 {len(non_py)} 个：{non_py[:5]}",
            )
        )

        # ---- 3) 安装到干净目录 --------------------------------------------
        clean = work / "clean-site"
        installed = _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--target",
                str(clean),
                str(wheel),
            ]
        )
        checks.append(
            ("安装到干净目录（pip --target）", installed.returncode == 0, installed.stderr[-400:])
        )
        if installed.returncode != 0:
            return _report(checks)

        # 探针环境：干净目录优先于仓库 src（后者来自父环境的可编辑安装 .pth）
        base_env = {
            **os.environ,
            "PYTHONPATH": str(clean),
            "PYTHONIOENCODING": "utf-8",
        }
        for var in (
            "DSH_SIM_ALLOW_MISSING_RESOURCES",
            "DSH_SIM_CAPABILITIES_ROOT",
            "DSH_SIM_PANELS_ROOT",
            "DSH_SIM_STATE_DIR",
        ):
            base_env.pop(var, None)

        # ---- 4) 探针 A：未配置资源 → 必须 fail-fast ------------------------
        ok, detail, a = _probe(work, "probe_unconfigured", _PROBE_UNCONFIGURED, base_env)
        checks.append(("干净目录探针 A 执行", ok, detail))
        if not ok:
            return _report(checks)

        imported = Path(a["dsh_sim_file"]).resolve()
        checks.append(
            (
                "干净目录内 import 的是安装产物（非仓库 src）",
                clean.resolve() in imported.parents,
                str(imported),
            )
        )
        checks.append(
            (
                "报告模板随包分发（importlib.resources）",
                a["template_chars"] > 0,
                f"{a['template_chars']} 字符",
            )
        )
        msg = a["unconfigured"].get("message", "")
        checks.append(
            (
                "未配置资源 → 服务入口 fail-fast 且提示可执行修复",
                a["unconfigured"]["raised"]
                and "capabilities" in msg
                and "panels" in msg
                and "DSH_SIM_CAPABILITIES_ROOT" in msg
                and "已尝试以下位置" in msg,
                msg.splitlines()[0] if msg else "未抛错",
            )
        )
        checks.append(
            (
                "显式降级开关才启动且缺资源留痕",
                a["degraded"]["missing"] == ["capabilities", "panels"]
                and a["degraded"]["capability_scan"] == 0,
                json.dumps(a["degraded"], ensure_ascii=False),
            )
        )

        # ---- 5) 探针 B：显式配置资源 → 必须启动成功 ------------------------
        ok, detail, b = _probe(work, "probe_configured", _PROBE_CONFIGURED, base_env)
        checks.append(("干净目录探针 B 执行", ok, detail))
        if not ok:
            return _report(checks)
        checks.append(
            (
                "显式配置外部资源后启动成功（面板挂载 + 能力包注册）",
                b["missing"] == []
                and b["explicit_missing"] == []
                and b["panels_mounted"]
                and b["capability_scan"] > 0
                and b["capabilities_source"].startswith("env:"),
                json.dumps(
                    {
                        k: b[k]
                        for k in (
                            "missing",
                            "capabilities_source",
                            "panels_source",
                            "panels_mounted",
                            "capability_scan",
                        )
                    },
                    ensure_ascii=False,
                ),
            )
        )
        return _report(checks)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _report(checks: list[tuple[str, bool, str]]) -> int:
    print("\n=== wheel 干净目录安装验收（上游验收报告 §五）===")
    failed = 0
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if detail and not ok:
            print(f"       {detail.strip()[:2500]}")
        if not ok:
            failed += 1
    print(f"\n{len(checks) - failed}/{len(checks)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
