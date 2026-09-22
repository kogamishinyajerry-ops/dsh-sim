"""上游验收报告问题 1（2026-09-22）：冻结包必须成为验收对象。

场景驱动的 API 级回归（REAL-tagged 合成门夹具，非求解器验证）：
1. 先缺项提交诊断包，之后补算齐全 → 旧包 ACCEPT 仍被阻塞（不能"补绿"）；
2. 冻结包文件被改一字节 → bundle_artifact_corrupted 阻塞；
3. manifest 引用的 artifact 被删（来源缺失）→ 阻塞；
4. 冻结后 attempt 更换 → 阻塞（旧包绑定旧尝试）；
5. manifest/digest 被篡改 → bundle_digest_mismatch 阻塞；
6. 冻结后新增证据（新 Verification/新 artifact）不进旧包验收，也不阻塞正常 ACCEPT。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.db.models import ArtifactRow, BundleRow, RunRow, VerificationRow
from helpers_chain import build_bundle, decide, submit_review
from test_review_flow import _setup_real_review

pytestmark = [pytest.mark.mock, pytest.mark.integration]


def _decide_expect_blocked(client, review: dict, *codes: str) -> dict:
    r = decide(client, review, "ACCEPT")
    assert r.status_code == 503, r.text
    details = r.json()["details"]
    for code in codes:
        assert code in details, f"{code} not in {details}"
    return details


class TestNoGreeningByBackfill:
    def test_incomplete_bundle_stays_incomplete_after_backfill(
        self, client, tmp_path, monkeypatch
    ):
        """缺 Run 的诊断包提交审查后，补算该 Run 成功并带 REAL 证据与 PASS 校验——
        旧包 ACCEPT 仍必须阻塞 bundle_incomplete（补算只对新包有效）。"""
        from helpers_chain import (
            authorize_and_submit,
            create_task_with_revision,
            prepare_via_worker,
        )

        task_id = create_task_with_revision(client)
        prep = prepare_via_worker(client, tmp_path, task_id)
        run_ids = authorize_and_submit(client, task_id, prep)
        # 只把 run[0] 落成完成态；run[1] 保持 QUEUED → 包缺项
        from test_review_flow import _real_tagged_runs

        _real_tagged_runs(client, task_id, run_ids[:1])
        bundle = build_bundle(client, task_id)
        review = submit_review(client, task_id, bundle)

        # 冻结后补算：run[1] 也转完成态 + REAL 证据 + PASS 校验
        _real_tagged_runs(client, task_id, run_ids[1:])
        # 当前 DB 状态已"齐全"，但旧包验收必须仍按冻结集合判定
        _decide_expect_blocked(client, review, "bundle_incomplete")

    def test_new_bundle_after_backfill_can_accept(self, client, tmp_path):
        """补算后构建**新**包重新提交 → ACCEPT 放行（正确出路是新包重审）。"""
        from helpers_chain import (
            authorize_and_submit,
            create_task_with_revision,
            prepare_via_worker,
        )
        from test_review_flow import _real_tagged_runs

        task_id = create_task_with_revision(client)
        prep = prepare_via_worker(client, tmp_path, task_id)
        run_ids = authorize_and_submit(client, task_id, prep)
        _real_tagged_runs(client, task_id, run_ids[:1])
        build_bundle(client, task_id)  # 旧包（缺项诊断包）

        _real_tagged_runs(client, task_id, run_ids[1:])  # 补算
        bundle2 = build_bundle(client, task_id)  # 新包
        review2 = submit_review(client, task_id, bundle2)
        r = decide(client, review2, "ACCEPT")
        assert r.status_code == 201, r.text


class TestFrozenBytesIntegrity:
    def test_tampered_file_byte_blocks_accept(self, client, tmp_path):
        """冻结后文件改一字节 → bundle_artifact_corrupted。"""
        ctx = _setup_real_review(client, tmp_path)
        with client.app.state.session_factory() as s:
            bundle = s.get(BundleRow, ctx["bundle"]["bundle_id"])
            entry = next(e for e in bundle.manifest if e["role"] == "raw")
            art = s.get(ArtifactRow, entry["artifact_id"])
            p = Path(art.storage_path)
            data = bytearray(p.read_bytes())
            data[0] ^= 0x01  # 改一字节
            p.write_bytes(bytes(data))
            s.commit()
        _decide_expect_blocked(client, ctx["review"], "bundle_artifact_corrupted")

    def test_missing_artifact_source_blocks_accept(self, client, tmp_path):
        """manifest 引用的 artifact 记录被删（来源缺失）→ 阻塞。"""
        ctx = _setup_real_review(client, tmp_path)
        with client.app.state.session_factory() as s:
            bundle = s.get(BundleRow, ctx["bundle"]["bundle_id"])
            entry = next(e for e in bundle.manifest if e["role"] == "raw")
            s.execute(delete(ArtifactRow).where(ArtifactRow.artifact_id == entry["artifact_id"]))
            s.commit()
        _decide_expect_blocked(client, ctx["review"], "bundle_source_missing")

    def test_tampered_manifest_digest_blocks_accept(self, client, tmp_path):
        """manifest 被篡改（增删条目）→ 重算摘要不一致 → bundle_digest_mismatch。"""
        ctx = _setup_real_review(client, tmp_path)
        with client.app.state.session_factory() as s:
            bundle = s.get(BundleRow, ctx["bundle"]["bundle_id"])
            # 绕过 ORM 事件守卫直接 UPDATE（模拟外部篡改）：manifest 多出一条幽灵条目
            from sqlalchemy import text

            ghost = dict(bundle.manifest[0])
            ghost["artifact_id"] = f"art_{uuid.uuid4().hex[:24]}"
            tampered = bundle.manifest + [ghost]
            s.execute(
                text("UPDATE bundles SET manifest = :m WHERE bundle_id = :b"),
                {"m": json.dumps(tampered), "b": bundle.bundle_id},
            )
            s.commit()
        _decide_expect_blocked(client, ctx["review"], "bundle_digest_mismatch")


class TestAttemptBinding:
    def test_attempt_switch_blocks_old_bundle(self, client, tmp_path):
        """冻结后 run 换了 current_attempt_id → 旧包验收按冻结 attempt 判定，
        新 attempt 的证据不在冻结集合内 → selected_attempt_evidence_missing。"""
        ctx = _setup_real_review(client, tmp_path)
        with client.app.state.session_factory() as s:
            run = s.get(RunRow, ctx["run_ids"][0])
            run.current_attempt_id = "attempt-new-after-freeze"
            s.commit()
        _decide_expect_blocked(client, ctx["review"], "selected_attempt_evidence_missing")


class TestNewEvidenceNotInOldBundle:
    def test_post_freeze_evidence_does_not_block_clean_accept(self, client, tmp_path):
        """冻结后新增的独立 Verification/新 artifact 不进旧包验收；
        正常链路的 ACCEPT 不被无关新证据误伤。"""
        ctx = _setup_real_review(client, tmp_path)
        with client.app.state.session_factory() as s:
            # 冻结后冒出的新 verification（不属于 manifest）
            s.add(
                VerificationRow(
                    verification_id=f"ver_{uuid.uuid4().hex[:24]}",
                    run_id=ctx["run_ids"][0],
                    attempt_id="attempt-ghost",
                    rule_set_sha256="c" * 64,
                    conclusion="PASS",
                    source_artifact_ids=[],
                )
            )
            s.commit()
        r = decide(client, ctx["review"], "ACCEPT")
        assert r.status_code == 201, r.text
