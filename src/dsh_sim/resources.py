"""外部资源解析：能力包根 / 面板根 / 运行状态目录（上游验收报告 §五，2026-09-22）。

问题：Python 包在 `src/`，而 `capabilities/` 与 `panels/` 在仓库根目录；运行时用
`Path(__file__).resolve().parents[3]` 推算仓库根——源码/editable 运行正常，装成 wheel
后该路径落到 site-packages 上层，资源取不到，且表现为**静默空列表 / 不挂载**。

本模块把这三个根变成**显式外置配置**，并把"解析到了什么、为什么失败"暴露出来
（而不是让调用方拿到一个不存在的路径继续跑）：

解析顺序（`resolution_report()` 可复现每一步的候选与实际来源）：
1. 环境变量——显式配置，最高优先；
2. 仓库布局——仅当候选目录同时含 `pyproject.toml` 与目标目录时才认定
   （避免在 wheel/site-packages 下把上层目录误判成仓库根）；
3. 安装后共享位置——`<sys.prefix>/share/dsh-sim/<name>`；
4. 状态目录兜底——`DSH_SIM_STATE_DIR` → 仓库 `var/` → 用户家目录 `.dsh-sim`
   （仅状态类路径，按需创建；资源类缺失则报错，不兜底造目录）。

`capabilities_root()` / `panels_root()` 在缺失时抛 `MissingResourceError`，
携带期望位置、已尝试位置与修复动作（环境变量名）。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 外部资源根（显式配置优先）
ENV_CAPABILITIES_ROOT = "DSH_SIM_CAPABILITIES_ROOT"
ENV_PANELS_ROOT = "DSH_SIM_PANELS_ROOT"
ENV_STATE_DIR = "DSH_SIM_STATE_DIR"
#: 显式降级开关：允许在缺资源时仍然启动（只用于刻意的最小化部署，且必然留痕告警）
ENV_ALLOW_MISSING_RESOURCES = "DSH_SIM_ALLOW_MISSING_RESOURCES"

#: 安装后资源位置（`pip install` 后位于解释器前缀下）
SHARE_SUBDIR = "share/dsh-sim"
_STATE_DIR_NAME = ".dsh-sim"


class MissingResourceError(RuntimeError):
    """必需外部资源缺失：启动应当失败，而不是继续用空资源跑。"""

    def __init__(
        self,
        resource: str,
        *,
        env_var: str,
        tried: tuple[Path, ...],
        hint: str | None = None,
    ) -> None:
        self.resource = resource
        self.env_var = env_var
        self.tried = tried
        tried_text = "\n".join(f"  - {p}" for p in tried) or "  - （无候选位置）"
        lines = [
            f"必需资源缺失：{resource}（已尝试以下位置，均不存在）",
            tried_text,
            f"修复：设置 {env_var}=<{resource} 目录>（wheel 安装必须显式配置）；",
            "      源码/editable 运行请在仓库根目录下执行，或将资源放到 "
            f"<sys.prefix>/{SHARE_SUBDIR}/。",
        ]
        if hint:
            lines.append(f"      说明：{hint}")
        lines.append(
            f"      仅在刻意最小化部署时才可设置 {ENV_ALLOW_MISSING_RESOURCES}=1 "
            "降级启动（会留下醒目告警）。"
        )
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class Resolution:
    """一次资源解析的结果（含候选与实际来源，供诊断/启动日志复现）。"""

    name: str
    path: Path | None
    source: str  # env:<VAR> / repo-layout / install-share / state-default / missing
    env_var: str
    tried: tuple[Path, ...] = field(default_factory=tuple)

    @property
    def found(self) -> bool:
        return self.path is not None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "path": str(self.path) if self.path is not None else None,
            "source": self.source,
            "env_var": self.env_var,
        }


def repo_root() -> Path | None:
    """仓库根目录；**仅**在候选同时含 `pyproject.toml` 与 `capabilities/` 时认定。

    这样 wheel/site-packages 安装下不会把 `site-packages`、`Lib`、`Python3x`
    之类的上层目录误判成仓库根（那正是"静默缺资源"的来源）。
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "capabilities").is_dir():
            return candidate
    return None


def _resolve(
    name: str,
    env_var: str,
    *,
    repo_subdir: str,
    install_subdir: str,
) -> Resolution:
    tried: list[Path] = []

    raw = os.environ.get(env_var)
    if raw:
        candidate = Path(raw).expanduser()
        tried.append(candidate)
        if candidate.is_dir():
            return Resolution(name, candidate, f"env:{env_var}", env_var, tuple(tried))

    root = repo_root()
    if root is not None:
        candidate = root / repo_subdir
        tried.append(candidate)
        if candidate.is_dir():
            return Resolution(name, candidate, "repo-layout", env_var, tuple(tried))

    candidate = Path(sys.prefix) / SHARE_SUBDIR / install_subdir
    tried.append(candidate)
    if candidate.is_dir():
        return Resolution(name, candidate, "install-share", env_var, tuple(tried))

    return Resolution(name, None, "missing", env_var, tuple(tried))


def resolve_capabilities() -> Resolution:
    return _resolve(
        "capabilities",
        ENV_CAPABILITIES_ROOT,
        repo_subdir="capabilities",
        install_subdir="capabilities",
    )


def resolve_panels() -> Resolution:
    return _resolve(
        "panels",
        ENV_PANELS_ROOT,
        repo_subdir="panels",
        install_subdir="panels",
    )


def capabilities_root() -> Path:
    """能力包根目录；缺失时抛 MissingResourceError（绝不返回不存在的路径）。"""
    res = resolve_capabilities()
    if res.path is None:
        raise MissingResourceError(
            "capabilities",
            env_var=ENV_CAPABILITIES_ROOT,
            tried=res.tried,
            hint="能力包 rules/metrics/domain 是数值校核与证据冻结的输入，不可缺省为空。",
        )
    return res.path


def panels_root() -> Path:
    """面板静态资源根目录；缺失时抛 MissingResourceError。"""
    res = resolve_panels()
    if res.path is None:
        raise MissingResourceError(
            "panels",
            env_var=ENV_PANELS_ROOT,
            tried=res.tried,
            hint="执行台/审查台面板由工程 API 静态挂载在 /panels 下。",
        )
    return res.path


def state_dir() -> Path:
    """运行状态目录（数据库/工件/Worker 工作目录的默认父目录）。

    顺序：`DSH_SIM_STATE_DIR` → 仓库 `var/`（源码运行）→ `~/.dsh-sim`（安装后）。
    按需创建；这是唯一允许兜底创建的路径（状态而非只读资源）。
    """
    raw = os.environ.get(ENV_STATE_DIR)
    if raw:
        target = Path(raw).expanduser()
    else:
        root = repo_root()
        target = (root / "var") if root is not None else (Path.home() / _STATE_DIR_NAME)
    target.mkdir(parents=True, exist_ok=True)
    return target


def allow_missing_resources() -> bool:
    return os.environ.get(ENV_ALLOW_MISSING_RESOURCES, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolution_report() -> list[Resolution]:
    """全部外部资源的解析结果（不抛异常；供启动日志与诊断端点复现）。"""
    return [resolve_capabilities(), resolve_panels()]


__all__ = [
    "ENV_ALLOW_MISSING_RESOURCES",
    "ENV_CAPABILITIES_ROOT",
    "ENV_PANELS_ROOT",
    "ENV_STATE_DIR",
    "SHARE_SUBDIR",
    "MissingResourceError",
    "Resolution",
    "allow_missing_resources",
    "capabilities_root",
    "panels_root",
    "repo_root",
    "resolution_report",
    "resolve_capabilities",
    "resolve_panels",
    "state_dir",
]
