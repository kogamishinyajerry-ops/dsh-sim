"""上游验收报告问题 2（2026-09-22）：MOCK 来源传播与方法包版本必须贯穿全链。

四条验收条件 → 本文件四组测试：

1. 从 TaskSpec 精确解析版本、摘要和发布状态
   （TaskSpec.method 声明的是 capability_package_id + capability_package_sha256，
   版本/状态只能由该摘要反查，绝不回退到写死的 buffer_chamber/0.1.0）；
2. 准备产物按归属（project）+ 摘要（sha256）匹配，不能仅凭 logical_path 全局 first()；
3. 上游 MOCK/UNKNOWN 不得在后续报告中被推断成全链 REAL；
4. 方法版本变化必须产生可审查的差异，不能静默替换规则。

⚠️ 数据性质声明：链路使用 MockStarAdapter 准备 + REAL-tagged 合成 Run（门夹具），
非真实求解器输出。本文件验证的是**传播与绑定语义**，不是求解器结果。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from conftest import EXECUTOR_HEADERS, make_spec

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.capabilities.registry import resolve_method_package
from dsh_sim.db.models import ArtifactRow, BundleRow, TaskRow
from dsh_sim.evidence.bundle import bundle_evidence_mode

from helpers_chain import (
    authorize_and_submit,
    build_bundle,
    create_task_with_revision,
    decide,
    ik,
    prepare_via_worker,
    submit_review,
)
from test_review_flow import _real_tagged_runs

pytestmark = [pytest.mark.mock, pytest.mark.integration]

PKG_ID = "buffer_chamber"
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _real_manifest_digest(pkg_id: str = PKG_ID, version: str = "0.1.0") -> str:
    """磁盘上该版本 manifest 的 canonical 摘要（测试用作"正确声明"）。"""
    manifest = json.loads(
        (REPO_ROOT / "capabilities" / pkg_id / version / "manifest.json").read_text(encoding="utf-8")
    )
    return sha256_hex(canonical_dumps(manifest))


def _read_summary(client, bundle_id: str) -> dict:
    with client.app.state.session_factory() as s:
        row = s.get(BundleRow, bundle_id)
        entry = next(e for e in row.manifest if e["role"] == "summary")
        art = s.get(ArtifactRow, entry["artifact_id"])
        raw = Path(art.storage_path).read_text(encoding="utf-8")
        if raw.startswith("#") and "\n" in raw:
            raw = raw.split("\n", 1)[1]
        return json.loads(raw)


def _read_report(client, bundle_id: str) -> str:
    with client.app.state.session_factory() as s:
        row = s.get(BundleRow, bundle_id)
        entry = next(e for e in row.manifest if e["role"] == "report")
        art = s.get(ArtifactRow, entry["artifact_id"])
        return Path(art.storage_path).read_text(encoding="utf-8")


def _full_chain_task(client, tmp_path, spec=None) -> tuple[str, dict, list[str]]:
    """createTask → prepare(Worker) → authorize/submit → REAL-tagged runs。"""
    task_id = create_task_with_revision(client, spec=spec)
    prep = prepare_via_worker(client, tmp_path, task_id)
    run_ids = authorize_and_submit(client, task_id, prep)
    _real_tagged_runs(client, task_id, run_ids)
    return task_id, prep, run_ids


def _spec_declaring(digest: str) -> dict:
    spec = make_spec()
    spec["method"] = {**spec["method"], "capability_package_sha256": digest}
    return spec


# ---------------------------------------------------------------------------
# 条件 1：从 TaskSpec 精确解析版本、摘要、发布状态
# ---------------------------------------------------------------------------
class TestResolveFromSpec:
    def test_resolver_picks_version_by_declared_digest(self, tmp_path):
        """摘要驱动的版本解析：声明的摘要决定冻结哪个版本，而不是某个"当前版本"。"""
        root = tmp_path / "capabilities"
        for version in ("0.1.0", "0.2.0"):
            d = root / PKG_ID / version
            d.mkdir(parents=True)
            (d / "manifest.json").write_text(
                json.dumps({"capability_package_id": PKG_ID, "version": version, "status": "DRAFT"}),
                encoding="utf-8",
            )
        v1_manifest = json.loads(
            (root / PKG_ID / "0.1.0" / "manifest.json").read_text(encoding="utf-8")
        )
        v1_digest = sha256_hex(canonical_dumps(v1_manifest))

        resolved = resolve_method_package(
            None,
            {"capability_package_id": PKG_ID, "capability_package_sha256": v1_digest},
            capabilities_root=root,
        )
        assert resolved.version == "0.1.0"  # 不是"最新"的 0.2.0
        assert resolved.digest_match is True
        assert resolved.manifest_sha256 == v1_digest
        assert resolved.declared_sha256 == v1_digest

    def test_resolver_flags_mismatch_without_silent_substitution(self, tmp_path):
        """声明摘要无命中：退回最高版本但**如实标记** digest_match=False（差异可审查）。"""
        root = tmp_path / "capabilities"
        for version in ("0.1.0", "0.2.0"):
            d = root / PKG_ID / version
            d.mkdir(parents=True)
            (d / "manifest.json").write_text(
                json.dumps({"capability_package_id": PKG_ID, "version": version, "status": "DRAFT"}),
                encoding="utf-8",
            )
        resolved = resolve_method_package(
            None,
            {"capability_package_id": PKG_ID, "capability_package_sha256": "f" * 64},
            capabilities_root=root,
        )
        assert resolved.version == "0.2.0"
        assert resolved.digest_match is False
        assert resolved.declared_sha256 == "f" * 64
        assert resolved.manifest_sha256 != "f" * 64

    def test_unknown_package_blocks_bundle_build(self, client, tmp_path):
        """spec 声明的能力包不存在 → 构建证据包显式阻塞（不写死 0.1.0 顶替）。"""
        spec = _spec_declaring(_real_manifest_digest())
        spec["method"] = {**spec["method"], "capability_package_id": "no_such_package"}
        task_id = create_task_with_revision(client, spec=spec)
        prepare_via_worker(client, tmp_path, task_id)
        r = client.post(
            f"/api/v1/tasks/{task_id}/bundles",
            json={"revision": 1},
            headers={**EXECUTOR_HEADERS, **ik()},
        )
        assert r.status_code == 503, r.text
        assert "能力包不存在" in r.json()["message"]

    def test_bundle_records_resolved_version_digest_and_status(self, client, tmp_path):
        """正确声明摘要：包内记录解析出的版本/摘要/发布状态，报告如实标注非 RELEASED。"""
        task_id, _prep, _runs = _full_chain_task(
            client, tmp_path, _spec_declaring(_real_manifest_digest())
        )
        bundle = build_bundle(client, task_id)

        mp = _read_summary(client, bundle["bundle_id"])["method_package"]
        assert mp["capability_package_id"] == PKG_ID
        assert mp["version"] == "0.1.0"
        assert mp["digest_match"] is True
        assert mp["released"] is False and mp["status"] == "DRAFT"
        assert mp["manifest_sha256"] == _real_manifest_digest()

        html = _read_report(client, bundle["bundle_id"])
        assert f"{PKG_ID}@0.1.0" in html
        assert "非 RELEASED" in html  # 未发布状态不得被静默当作已批准方法
        assert "方法版本已变化" not in html  # 摘要匹配时不出差异提示

    def test_bundle_freezes_resolved_version_files(self, client, tmp_path):
        """冻结的规则/指标文件来自解析出的版本目录（角色齐全且字节等于磁盘原文件）。"""
        task_id, _prep, _runs = _full_chain_task(
            client, tmp_path, _spec_declaring(_real_manifest_digest())
        )
        bundle = build_bundle(client, task_id)

        with client.app.state.session_factory() as s:
            row = s.get(BundleRow, bundle["bundle_id"])
            roles = {e["role"] for e in row.manifest}
            assert {"rules", "method_metrics", "method_domain", "method_manifest"} <= roles
            frozen = next(e for e in row.manifest if e["role"] == "method_metrics")
            art = s.get(ArtifactRow, frozen["artifact_id"])
            disk = (
                REPO_ROOT / "capabilities" / PKG_ID / "0.1.0" / "metric-definitions.json"
            ).read_bytes()
            assert Path(art.storage_path).read_bytes() == disk


# ---------------------------------------------------------------------------
# 条件 4：方法版本变化必须产生可审查的差异（不能静默替换规则）
# ---------------------------------------------------------------------------
class TestReviewableDifference:
    def test_declared_digest_mismatch_recorded_and_rendered(self, client, tmp_path):
        """spec 声明的摘要与冻结版本不一致 → 包内记录差异且报告渲染出差异提示。"""
        task_id, _prep, _runs = _full_chain_task(client, tmp_path, make_spec())  # 声明 FAKE_SHA
        bundle = build_bundle(client, task_id)

        mp = _read_summary(client, bundle["bundle_id"])["method_package"]
        assert mp["digest_match"] is False
        assert mp["declared_sha256"] == "a" * 64
        assert mp["manifest_sha256"] == _real_manifest_digest()  # 冻结的是真实版本，未被静默替换

        html = _read_report(client, bundle["bundle_id"])
        assert "方法版本已变化" in html
        assert mp["declared_sha256"] in html and mp["manifest_sha256"] in html

    def test_method_version_change_surfaces_distinct_bindings(self, client, tmp_path):
        """声明旧摘要的包 vs 声明当前摘要的包：已审查记录可逐项比对（版本变化可见）。"""
        stale = build_bundle(
            client, _full_chain_task(client, tmp_path, make_spec())[0]
        )
        current = build_bundle(
            client,
            _full_chain_task(client, tmp_path, _spec_declaring(_real_manifest_digest()))[0],
        )
        mp_stale = _read_summary(client, stale["bundle_id"])["method_package"]
        mp_current = _read_summary(client, current["bundle_id"])["method_package"]

        assert mp_stale["declared_sha256"] != mp_current["declared_sha256"]  # 声明变了
        assert mp_stale["digest_match"] is False and mp_current["digest_match"] is True
        # 冻结的版本摘要一致：包本体的规则未被静默替换，只有"声明是否对得上"变了
        assert mp_stale["manifest_sha256"] == mp_current["manifest_sha256"]


# ---------------------------------------------------------------------------
# 条件 2：准备产物按归属 + 摘要匹配
# ---------------------------------------------------------------------------
class TestPreparedArtifactBinding:
    def test_prepared_cases_bound_to_project_and_hash(self, client, tmp_path):
        """每个 prepared_case 条目必须 (project, logical_path, sha256) 三重一致。"""
        task_id, prep, _runs = _full_chain_task(client, tmp_path)
        bundle = build_bundle(client, task_id)

        with client.app.state.session_factory() as s:
            row = s.get(BundleRow, bundle["bundle_id"])
            task = s.get(TaskRow, task_id)
            declared = prep["prepared_artifacts"]
            assert declared  # 链路确实产生了准备产物

            entries = {
                (e["logical_path"], e["sha256"]): e
                for e in row.manifest
                if e["role"] == "prepared_case"
            }
            for logical, sha in declared.items():
                assert (logical, sha) in entries, f"准备产物未按摘要绑定: {logical}"
                art = s.get(ArtifactRow, entries[(logical, sha)]["artifact_id"])
                assert art.project_id == task.project_id  # 归属校验

    def test_same_path_different_hash_is_not_substituted(self, client, tmp_path):
        """同名路径但摘要不符的 artifact 不得被当作准备产物；
        未绑定项如实记录并阻塞 ACCEPT（不静默跳过、不用同名文件顶替）。"""
        task_id, prep, _runs = _full_chain_task(client, tmp_path)
        declared = prep["prepared_artifacts"]

        with client.app.state.session_factory() as s:
            # 移除真正的准备产物，插入"同项目 + 同路径 + 不同内容"的冒名 artifact
            s.query(ArtifactRow).filter(
                ArtifactRow.logical_path.in_(list(declared.keys()))
            ).delete(synchronize_session=False)
            impostor_ids = []
            for logical in declared:
                content = f"IMPOSTOR:{logical}:{uuid.uuid4().hex}".encode("utf-8")
                aid = f"art_{uuid.uuid4().hex[:24]}"
                s.add(
                    ArtifactRow(
                        artifact_id=aid,
                        project_id="proj_a",
                        logical_path=logical,
                        length=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                        state="COMMITTED",
                        evidence_mode="MOCK",
                    )
                )
                impostor_ids.append(aid)
            s.commit()

        bundle = build_bundle(client, task_id)
        with client.app.state.session_factory() as s:
            row = s.get(BundleRow, bundle["bundle_id"])
            bound = {e["artifact_id"] for e in row.manifest if e["role"] == "prepared_case"}
        assert not (set(impostor_ids) & bound), "冒名 artifact 被错误绑定进冻结清单"

        summary = _read_summary(client, bundle["bundle_id"])
        assert any(
            i["kind"] == "PREPARED_ARTIFACT_UNMATCHED"
            for i in summary["prepared_artifact_issues"]
        )
        assert summary["complete"] is False

        review = submit_review(client, task_id, bundle)
        r = decide(client, review, "ACCEPT")
        assert r.status_code == 503, r.text
        assert "bundle_incomplete" in r.json()["details"]


# ---------------------------------------------------------------------------
# 条件 3：上游 MOCK/UNKNOWN 不得被推断成全链 REAL
# ---------------------------------------------------------------------------
class TestUpstreamModePropagation:
    def test_mock_preparation_contaminates_real_tagged_run_bundle(self, client, tmp_path):
        """Mock 准备 + REAL-tagged 合成 Run → 整包 MOCK，绝不推断成全链 REAL。

        这正是报告点名的夹具缺陷：修复前整体模式只看 Run 产物（REAL-tagged），
        准备（MOCK）不参与传播，包被标成 REAL。
        """
        task_id, _prep, run_ids = _full_chain_task(client, tmp_path)
        bundle = build_bundle(client, task_id)

        with client.app.state.session_factory() as s:
            run_modes = {
                a.evidence_mode
                for a in s.query(ArtifactRow).filter(ArtifactRow.run_id.in_(run_ids)).all()
            }
            assert run_modes == {"REAL"}, "夹具前提：Run 证据是 REAL-tagged"
            assert bundle_evidence_mode(s, task_id, 1) == "MOCK"

        assert _read_summary(client, bundle["bundle_id"])["evidence_mode"] == "MOCK"
        assert "mode-badge mode-MOCK" in _read_report(client, bundle["bundle_id"])

    def test_unknown_upstream_not_inferred_as_real(self, client, tmp_path):
        """准备产物 evidence_mode 未知（NULL）→ 整包 UNKNOWN，不推断成 REAL。"""
        task_id, prep, _runs = _full_chain_task(client, tmp_path)
        with client.app.state.session_factory() as s:
            s.query(ArtifactRow).filter(
                ArtifactRow.logical_path.in_(list(prep["prepared_artifacts"].keys()))
            ).update({ArtifactRow.evidence_mode: None}, synchronize_session=False)
            s.commit()
            assert bundle_evidence_mode(s, task_id, 1) == "UNKNOWN"

    def test_all_real_sources_yield_real(self, client, tmp_path):
        """Run 与准备产物全部 REAL（无 MOCK/UNKNOWN）→ 整包 REAL（不误伤正常链）。"""
        task_id, prep, _runs = _full_chain_task(client, tmp_path)
        with client.app.state.session_factory() as s:
            s.query(ArtifactRow).filter(
                ArtifactRow.logical_path.in_(list(prep["prepared_artifacts"].keys()))
            ).update({ArtifactRow.evidence_mode: "REAL"}, synchronize_session=False)
            s.commit()
            assert bundle_evidence_mode(s, task_id, 1) == "REAL"
