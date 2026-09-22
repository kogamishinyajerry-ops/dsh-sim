"""e2e 自检门禁（幂等可重复跑；任何一步失败 exit 1）。

步骤：
1. default env pytest 全量（--rootdir，136+1 skip 基线）
2. stdio_e2e_check.py（dsh-sim 独立 venv，需工程服务在线；不在线先临时起一个，用完杀干净）
3. 汇总打印 PASS/FAIL 门禁结论

用法: python scripts/selfcheck.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PY = Path(
    r"<PYTHON_ENV>\Scripts\python.exe"
)
DSHSIM_PY = Path(
    r"<VENV_DSH_SIM>\Scripts\python.exe"
)
API_PORT = 8600
API_URL = f"http://127.0.0.1:{API_PORT}/api/v1"


def api_online(timeout_seconds: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{API_URL}/capabilities", timeout=timeout_seconds) as r:
            return r.status in (200, 401, 403)
    except urllib.error.HTTPError:
        return True  # 服务在响应（身份/权限拒绝也算在线）
    except Exception:
        return False


def start_temp_api() -> subprocess.Popen:
    """临时起工程服务（独立临时库，不动 var/ 主库）；返回进程句柄。"""
    tmpdir = tempfile.mkdtemp(prefix="dsh_sim_selfcheck_")
    env = dict(os.environ)
    env["DSH_SIM_DATABASE_URL"] = f"sqlite:///{tmpdir}/selfcheck.db"
    env["DSH_SIM_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")
    return subprocess.Popen(
        [
            str(DEFAULT_PY), "-m", "uvicorn", "dsh_sim.api.main:app",
            "--port", str(API_PORT), "--log-level", "error",
        ],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop_temp_api(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def step1_pytest() -> bool:
    print("=== 步骤 1：default env pytest 全量（--rootdir）===")
    r = subprocess.run(
        [str(DEFAULT_PY), "-m", "pytest", "tests", "-q", "--rootdir=tests"],
        cwd=str(REPO),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tail = (r.stdout or "").strip().splitlines()
    print("\n".join("  " + line for line in tail[-3:]))
    return r.returncode == 0


def step2_stdio_e2e() -> bool:
    print("=== 步骤 2：stdio_e2e_check.py（dsh-sim venv）===")
    spawned: subprocess.Popen | None = None
    try:
        if not api_online():
            print("  工程服务不在线，临时起一个（临时库，用完杀干净）...")
            spawned = start_temp_api()
            deadline = time.time() + 40
            while time.time() < deadline:
                if api_online():
                    print("  临时服务已在线")
                    break
                if spawned.poll() is not None:
                    print("  临时服务启动失败（进程已退出）")
                    return False
                time.sleep(0.5)
            else:
                print("  临时服务 40s 内未就绪")
                return False
        else:
            print("  工程服务已在线（复用现有 8600 服务）")
        r = subprocess.run(
            [str(DSHSIM_PY), str(REPO / "tests" / "stdio_e2e_check.py")],
            cwd=str(REPO),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        for line in (r.stdout or "").strip().splitlines():
            print("  " + line)
        if r.returncode != 0 and r.stderr:
            for line in r.stderr.strip().splitlines()[-5:]:
                print("  [stderr] " + line)
        return r.returncode == 0
    finally:
        if spawned is not None:
            stop_temp_api(spawned)
            print("  临时服务已停止")


def main() -> int:
    ok1 = step1_pytest()
    ok2 = step2_stdio_e2e()
    print("=== 门禁结论 ===")
    print(f"  步骤 1 pytest 全量     : {'PASS' if ok1 else 'FAIL'}")
    print(f"  步骤 2 stdio e2e 终验  : {'PASS' if ok2 else 'FAIL'}")
    if ok1 and ok2:
        print("SELF CHECK: PASS")
        return 0
    print("SELF CHECK: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
