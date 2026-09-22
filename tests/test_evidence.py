"""证据包与报告（FR-17/18；WP-13）。

- manifest 冻结：长度 + sha256，落库不可原位改写（before_update 硬守卫）；
- bundle_digest = canonical(manifest) 的 sha256，可复算；
- 缺必需工况标 incomplete（不靠减清单通过）；
- 报告 Jinja 确定性渲染：MOCK 整页斜纹水印 + 页眉 evidence_mode + 缺项/限制单独一节；
- Claim 绑 metric_id+artifact_id；数字与独立复算一致才 CONFIRMED。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import EXECUTOR_HEADERS

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.db.models import BundleRow, ClaimRow
from dsh_sim.domain.errors import ApiError
from dsh_sim.evidence.bundle import compute_completeness

from helpers_chain import full_mock_chain

pytestmark = pytest.mark.mock


def _bundle_row(client, bundle_id: str) -> BundleRow:
    s = client.app.state.session_factory()
    try:
        return s.get(BundleRow, bundle_id)
    finally:
        s.close()


class TestBundleFreeze:
    def test_digest_matches_canonical_manifest(self, client, tmp_path):
        chain = full_mock_chain(client, tmp_path)
        row = _bundle_row(client, chain.bundle["bundle_id"])
        assert row.bundle_digest == sha256_hex(canonical_dumps(row.manifest))
        for entry in row.manifest:
            assert entry["length"] >= 0 and len(entry["sha256"]) == 64

    def test_manifest_immutable(self, client, tmp_path):
        chain = full_mock_chain(client, tmp_path)
        s = client.app.state.session_factory()
        try:
            row = s.get(BundleRow, chain.bundle["bundle_id"])
            row.manifest = []  # 尝试原位改写 → 硬守卫拒绝
            with pytest.raises(ApiError):
                s.flush()
        finally:
            s.rollback()
            s.close()

    def test_artifact_content_matches_manifest_sha(self, client, tmp_path):
        chain = full_mock_chain(client, tmp_path)
        s = client.app.state.session_factory()
        try:
            from dsh_sim.db.models import ArtifactRow
            import hashlib

            row = s.get(BundleRow, chain.bundle["bundle_id"])
            entry = next(e for e in row.manifest if e["role"] == "task_spec")
            art = s.get(ArtifactRow, entry["artifact_id"])
            content = Path(art.storage_path).read_bytes()
            assert len(content) == entry["length"]
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        finally:
            s.close()

    def test_incomplete_bundle_marked(self, client, tmp_path):
        chain = full_mock_chain(client, tmp_path, execute_max_jobs=5)  # 缺 1 Run
        s = client.app.state.session_factory()
        try:
            completeness = compute_completeness(s, chain.task_id, 1)
            assert completeness["complete"] is False
            assert any(m["kind"] == "RUN_NOT_SUCCEEDED" for m in completeness["missing"])
            # 摘要 artifact 内含 incomplete_items（缺项不靠减清单通过）
            row = s.get(BundleRow, chain.bundle["bundle_id"])
            entry = next(e for e in row.manifest if e["role"] == "summary")
            from dsh_sim.db.models import ArtifactRow

            art = s.get(ArtifactRow, entry["artifact_id"])
            body = Path(art.storage_path).read_text(encoding="utf-8")
            summary = json.loads(body.split("\n", 1)[-1])
            assert summary["complete"] is False
            assert summary["incomplete_items"]
            assert summary["evidence_mode"] == "MOCK"
        finally:
            s.close()


class TestClaims:
    def test_claims_binding_and_states(self, client, tmp_path):
        chain = full_mock_chain(client, tmp_path)
        s = client.app.state.session_factory()
        try:
            claims = (
                s.query(ClaimRow).filter_by(bundle_id=chain.bundle["bundle_id"]).all()
            )
            assert claims
            for c in claims:
                assert c.metric_id and c.artifact_id  # 每条绑 metric_id + artifact_id
            confirmed = {c.metric_id for c in claims if c.state == "CONFIRMED"}
            drafts = {c.metric_id for c in claims if c.state == "DRAFT"}
            # 原始边界量双源一致 → CONFIRMED
            assert "boundary_mass_flow@inlet" in confirmed
            assert "total_pressure@inlet" in confirmed
            # null 阈值指标/派生量无双源交叉 → DRAFT，不冒充已确认
            assert "steady_mass_imbalance" in drafts
            assert "monitor_stability" in drafts
        finally:
            s.close()


class TestReport:
    def _read_report(self, client, bundle_id: str) -> str:
        s = client.app.state.session_factory()
        try:
            from dsh_sim.db.models import ArtifactRow

            row = s.get(BundleRow, bundle_id)
            entry = next(e for e in row.manifest if e["role"] == "report")
            art = s.get(ArtifactRow, entry["artifact_id"])
            assert ".mock." in art.logical_path  # mock 文件名中缀
            return Path(art.storage_path).read_text(encoding="utf-8")
        finally:
            s.close()

    def test_mock_watermark_and_header(self, client, tmp_path):
        chain = full_mock_chain(client, tmp_path)
        html = self._read_report(client, chain.bundle["bundle_id"])
        assert 'class="mock-words"' in html  # 整页 MOCK 字样网格
        assert "mock-stripes" in html  # 斜纹水印
        assert 'mode-badge mode-MOCK' in html  # 页眉常驻 evidence_mode
        assert "非真实求解器输出" in html

    def test_sections_and_claims_rendered(self, client, tmp_path):
        chain = full_mock_chain(client, tmp_path)
        html = self._read_report(client, chain.bundle["bundle_id"])
        assert "缺项与限制" in html  # 缺项/限制单独一节
        assert "多维状态" in html
        assert "total_pressure_loss" in html
        assert "CONFIRMED" in html and "DRAFT" in html
        assert "仅限内部方案筛选用途" in html
        assert chain.bundle["bundle_id"] in html

    def test_deterministic_render(self, client, tmp_path):
        """同一数据渲染两次字节一致（确定性模板，无时间戳/随机）。"""
        chain = full_mock_chain(client, tmp_path)
        html1 = self._read_report(client, chain.bundle["bundle_id"])
        s = client.app.state.session_factory()
        try:
            from dsh_sim.db.models import RunRow, TaskRevisionRow, TaskRow
            from dsh_sim.evidence.report import render_report

            row = s.get(BundleRow, chain.bundle["bundle_id"])
            task = s.get(TaskRow, chain.task_id)
            rev = s.query(TaskRevisionRow).filter_by(task_id=chain.task_id, revision=1).first()
            runs = s.query(RunRow).filter_by(task_id=chain.task_id, revision=1).all()
            claims = s.query(ClaimRow).filter_by(bundle_id=row.bundle_id).all()
            # 方法包信息是冻结事实：从 summary 产物读回，证明渲染可由冻结数据复现
            from dsh_sim.db.models import ArtifactRow

            summary_entry = next(e for e in row.manifest if e["role"] == "summary")
            summary_art = s.get(ArtifactRow, summary_entry["artifact_id"])
            summary = json.loads(
                Path(summary_art.storage_path).read_text(encoding="utf-8").split("\n", 1)[-1]
            )
            html2 = render_report(
                s,
                bundle_id=row.bundle_id,
                manifest_digest=sha256_hex(canonical_dumps(row.manifest[:-1])),
                task=task,
                rev=rev,
                runs=runs,
                claims=claims,
                evidence_mode="MOCK",
                completeness=compute_completeness(s, chain.task_id, 1),
                manifest=row.manifest[:-1],
                method_package=summary["method_package"],
            )
            assert html1 == html2
        finally:
            s.close()
