"""外部资源解析与启动 fail-fast（上游验收报告 §五，2026-09-22）。

报告要求三条：
1. 「明确打包资源或外置配置」——本 PR 取「包内资源真打包 + 仓库根资源显式外置配置」；
2. 「让启动检查暴露缺资源而不是静默空列表」——缺 capabilities/panels 时启动直接失败；
3. 「构建后必须在干净目录安装测试」——由 scripts/verify_wheel_install.py 承担，
   本文件覆盖解析顺序与失败语义（快、可回归）。

关键性质：
- 解析顺序 env → 仓库布局 → 安装后位置；仓库布局**只在**候选含 pyproject.toml +
  目标目录时才成立（避免 wheel 下把 site-packages 上层误判成仓库根）；
- 缺资源抛 MissingResourceError，消息含已尝试位置与修复动作（环境变量名）；
- 只有显式降级开关才允许缺资源启动，且必须留痕（resource_status.missing）。
"""
from __future__ import annotations

import pytest

from dsh_sim import resources as R
from dsh_sim.api.main import create_app

pytestmark = pytest.mark.mock


@pytest.fixture()
def isolated_resolution(tmp_path, monkeypatch):
    """把「没有仓库布局、也没有安装后资源」的环境模拟出来。"""
    monkeypatch.delenv(R.ENV_CAPABILITIES_ROOT, raising=False)
    monkeypatch.delenv(R.ENV_PANELS_ROOT, raising=False)
    monkeypatch.setattr(R, "repo_root", lambda: None)
    monkeypatch.setattr(R.sys, "prefix", str(tmp_path / "prefix"))
    return tmp_path


# ---------------------------------------------------------------------------
# 解析顺序
# ---------------------------------------------------------------------------
class TestResolutionOrder:
    def test_repo_layout_is_detected_in_source_checkout(self):
        """源码/editable 运行：解析到仓库布局（既有行为不变）。"""
        caps = R.resolve_capabilities()
        panels = R.resolve_panels()
        assert caps.source == "repo-layout" and caps.path is not None
        assert panels.source == "repo-layout" and panels.path is not None
        assert caps.path.name == "capabilities" and panels.path.name == "panels"
        assert R.capabilities_root() == caps.path
        assert R.panels_root() == panels.path

    def test_env_var_wins_over_repo_layout(self, tmp_path, monkeypatch):
        """显式配置最高优先（wheel 部署靠它指向仓库外资源）。"""
        caps_dir = tmp_path / "external-capabilities"
        caps_dir.mkdir()
        monkeypatch.setenv(R.ENV_CAPABILITIES_ROOT, str(caps_dir))
        res = R.resolve_capabilities()
        assert res.source == f"env:{R.ENV_CAPABILITIES_ROOT}"
        assert res.path == caps_dir
        assert R.capabilities_root() == caps_dir

    def test_install_share_location_is_used_when_no_repo_layout(self, tmp_path, monkeypatch):
        """安装后位置：<sys.prefix>/share/dsh-sim/<name>。"""
        monkeypatch.delenv(R.ENV_CAPABILITIES_ROOT, raising=False)
        monkeypatch.setattr(R, "repo_root", lambda: None)
        prefix = tmp_path / "prefix"
        share = prefix / R.SHARE_SUBDIR / "capabilities"
        share.mkdir(parents=True)
        monkeypatch.setattr(R.sys, "prefix", str(prefix))
        res = R.resolve_capabilities()
        assert res.source == "install-share"
        assert res.path == share

    def test_repo_layout_requires_pyproject_marker(self, tmp_path, monkeypatch):
        """只有 capabilities/ 而没有 pyproject.toml 的目录不算仓库布局。"""
        fake = tmp_path / "not-a-repo"
        (fake / "capabilities").mkdir(parents=True)
        monkeypatch.setattr(R, "repo_root", lambda: None)
        monkeypatch.delenv(R.ENV_CAPABILITIES_ROOT, raising=False)
        monkeypatch.setattr(R.sys, "prefix", str(tmp_path / "prefix"))
        assert R.resolve_capabilities().found is False

    def test_repo_root_recognises_this_checkout(self):
        assert R.repo_root() is not None
        assert (R.repo_root() / "pyproject.toml").is_file()


# ---------------------------------------------------------------------------
# 缺资源：抛错 + 可执行提示（不是静默空列表）
# ---------------------------------------------------------------------------
class TestMissingResourceErrors:
    def test_missing_capabilities_raises_actionable_error(self, isolated_resolution):
        with pytest.raises(R.MissingResourceError) as excinfo:
            R.capabilities_root()
        msg = str(excinfo.value)
        assert "capabilities" in msg
        assert R.ENV_CAPABILITIES_ROOT in msg  # 修复动作：指出该设哪个变量
        assert "已尝试以下位置" in msg
        assert R.ENV_ALLOW_MISSING_RESOURCES in msg  # 降级开关也必须被说明

    def test_missing_panels_raises_actionable_error(self, isolated_resolution):
        with pytest.raises(R.MissingResourceError) as excinfo:
            R.panels_root()
        assert R.ENV_PANELS_ROOT in str(excinfo.value)

    def test_resolution_report_never_raises(self, isolated_resolution):
        report = R.resolution_report()
        assert {r.name for r in report} == {"capabilities", "panels"}
        assert all(not r.found for r in report)
        assert all(r.env_var for r in report)

    def test_allow_missing_flag_parsing(self, monkeypatch):
        for raw, expected in (
            ("1", True),
            ("true", True),
            ("YES", True),
            ("on", True),
            ("", False),
            ("0", False),
            ("no", False),
            ("maybe", False),
        ):
            monkeypatch.setenv(R.ENV_ALLOW_MISSING_RESOURCES, raw)
            assert R.allow_missing_resources() is expected, raw
        monkeypatch.delenv(R.ENV_ALLOW_MISSING_RESOURCES, raising=False)
        assert R.allow_missing_resources() is False


# ---------------------------------------------------------------------------
# 状态目录（唯一允许兜底创建的位置）
# ---------------------------------------------------------------------------
class TestStateDir:
    def test_env_var_wins(self, tmp_path, monkeypatch):
        target = tmp_path / "state"
        monkeypatch.setenv(R.ENV_STATE_DIR, str(target))
        assert R.state_dir() == target and target.is_dir()

    def test_repo_layout_uses_var(self, monkeypatch):
        monkeypatch.delenv(R.ENV_STATE_DIR, raising=False)
        root = R.repo_root()
        assert root is not None
        assert R.state_dir() == root / "var"

    def test_installed_falls_back_to_user_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv(R.ENV_STATE_DIR, raising=False)
        monkeypatch.setattr(R, "repo_root", lambda: None)
        monkeypatch.setattr(R.Path, "home", classmethod(lambda cls: tmp_path / "home"))
        assert R.state_dir() == tmp_path / "home" / ".dsh-sim"


# ---------------------------------------------------------------------------
# 启动检查
# ---------------------------------------------------------------------------
class TestStartupCheck:
    def test_create_app_fails_fast_when_resources_missing(self, isolated_resolution):
        monkeypatch_off = isolated_resolution
        with pytest.raises(RuntimeError) as excinfo:
            create_app(
                database_url=f"sqlite:///{(monkeypatch_off / 'a.db').as_posix()}",
                artifact_root=monkeypatch_off / "artifacts",
                identity_mode="dev",
            )
        msg = str(excinfo.value)
        assert "capabilities" in msg and "panels" in msg

    def test_explicit_downgrade_starts_but_records_missing_resources(
        self, isolated_resolution, monkeypatch
    ):
        monkeypatch.setenv(R.ENV_ALLOW_MISSING_RESOURCES, "1")
        app = create_app(
            database_url=f"sqlite:///{(isolated_resolution / 'b.db').as_posix()}",
            artifact_root=isolated_resolution / "artifacts",
            identity_mode="dev",
        )
        assert app.state.capability_scan == []  # 空列表必须与 resource_status 同时可见
        assert app.state.resource_status["missing"] == ["capabilities", "panels"]

    def test_create_app_records_resolved_sources(self, tmp_path, monkeypatch):
        monkeypatch.delenv(R.ENV_CAPABILITIES_ROOT, raising=False)
        monkeypatch.delenv(R.ENV_PANELS_ROOT, raising=False)
        app = create_app(
            database_url=f"sqlite:///{(tmp_path / 'c.db').as_posix()}",
            artifact_root=tmp_path / "artifacts",
            identity_mode="dev",
        )
        status = app.state.resource_status
        assert status["missing"] == []
        assert status["capabilities"]["source"] == "repo-layout"
        assert status["panels"]["source"] == "repo-layout"


# ---------------------------------------------------------------------------
# 包内资源（报告模板）经 importlib.resources 定位
# ---------------------------------------------------------------------------
class TestPackagedTemplate:
    def test_template_loads_via_importlib_resources(self):
        from dsh_sim.evidence import report

        text = report._load_template_text()
        assert text.strip()
        assert report._TEMPLATE_NAME in text or "证据" in text

    def test_jinja_env_uses_the_loaded_template(self):
        from dsh_sim.evidence import report

        template = report._env.get_template(report._TEMPLATE_NAME)
        assert template is not None
