"""上游验收报告问题 4（2026-09-22）：关闭问题必须保存实际关闭证据。

验收条件 → 本文件四组测试：
1. 拒绝不存在、其他项目、未提交（TEMP）或不适用的关闭证据；
2. 关闭人、问题版本、实际证据 ID/哈希、绑定包与时间在同一事务内落库
   （失败则问题保持 OPEN、版本不变、无关闭记录）；
3. 关闭记录可从历史完整回看（GET /reviews/{id} 内嵌 closures）；
4. 回复文本本身不等于关闭证据（回复后问题仍 OPEN；关闭必须自带可校验证据）。

⚠️ 数据性质声明：链路使用 MockStarAdapter 准备 + REAL-tagged 合成 Run（门夹具），
非真实求解器输出。本文件验证的是**关闭证据的校验与持久化语义**。
"""
from __future__ import annotations

import uuid

import pytest
from conftest import EXECUTOR_HEADERS, REVIEWER_HEADERS
from sqlalchemy import text

from dsh_sim.db.models import ArtifactRow, IssueClosureRow, ReviewIssueRow
from dsh_sim.domain.errors import ApiError

from helpers_chain import ik
from test_review_flow import _setup_real_review

pytestmark = [pytest.mark.mock, pytest.mark.integration]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _manifest_evidence(client, bundle_id: str, role: str = "verification") -> str:
    r = client.get(f"/api/v1/bundles/{bundle_id}/manifest", headers=EXECUTOR_HEADERS)
    assert r.status_code == 200, r.text
    return next(e["artifact_id"] for e in r.json()["manifest"] if e["role"] == role)


def _add_artifact(client, *, project_id: str, state: str = "COMMITTED") -> str:
    """直接落一条 artifact 记录（测试夹具；不写文件，校验只读记录字段）。"""
    aid = f"art_{uuid.uuid4().hex[:24]}"
    with client.app.state.session_factory() as s:
        s.add(
            ArtifactRow(
                artifact_id=aid,
                project_id=project_id,
                logical_path=f"fixtures/{aid}.csv",
                length=1,
                sha256="0" * 64,
                state=state,
                storage_path=None,
            )
        )
        s.commit()
    return aid


def _issue_open(client, review: dict, bundle: dict, evidence_artifact_id: str) -> dict:
    """issue DRAFT → confirm → reply(带证据)，返回 OPEN 的 issue（尚未关闭）。"""
    r = client.post(
        f"/api/v1/reviews/{review['review_id']}/issues",
        json={
            "responsible": "eng_zhang",
            "severity": "MAJOR",
            "description": "压损比较口径是否与批准截面一致？",
            "close_criteria": "提供同截面同定义的独立复算证据",
        },
        headers={**REVIEWER_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    issue = r.json()

    digest = bundle["bundle_digest"]
    r = client.post(
        "/api/v1/confirmations",
        json={"action": "confirmIssue", "target_id": issue["issue_id"], "target_digest": digest},
        headers={**REVIEWER_HEADERS, **ik()},
    )
    conf_id = r.json()["confirmation_id"]
    r = client.post(
        f"/api/v1/issues/{issue['issue_id']}/confirm",
        json={"issue_version": issue["version"], "confirmation_id": conf_id},
        headers={**REVIEWER_HEADERS, **ik()},
    )
    assert r.status_code == 200, r.text
    issue = r.json()

    r = client.post(
        f"/api/v1/issues/{issue['issue_id']}/replies",
        json={"body": "已补充同口径复算证据", "evidence_artifact_ids": [evidence_artifact_id]},
        headers={**EXECUTOR_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _close(client, issue: dict, bundle: dict, evidence_ids: list[str]):
    """人工确认 closeIssue → 关闭请求。返回响应对象（不断言状态码）。"""
    r = client.post(
        "/api/v1/confirmations",
        json={
            "action": "closeIssue",
            "target_id": issue["issue_id"],
            "target_digest": bundle["bundle_digest"],
        },
        headers={**REVIEWER_HEADERS, **ik()},
    )
    assert r.status_code == 201, r.text
    conf_id = r.json()["confirmation_id"]
    return client.post(
        f"/api/v1/issues/{issue['issue_id']}/close",
        json={
            "issue_version": issue["version"],
            "close_evidence_artifact_ids": evidence_ids,
            "confirmation_id": conf_id,
        },
        headers={**REVIEWER_HEADERS, **ik()},
    )


def _issue_row(client, issue_id: str) -> ReviewIssueRow:
    with client.app.state.session_factory() as s:
        return s.get(ReviewIssueRow, issue_id)


def _closures(client, issue_id: str) -> list[IssueClosureRow]:
    with client.app.state.session_factory() as s:
        return (
            s.query(IssueClosureRow)
            .filter_by(issue_id=issue_id)
            .order_by(IssueClosureRow.closed_at)
            .all()
        )


# ---------------------------------------------------------------------------
# 条件 2 + 3：关闭证据同事务落库，且可从历史完整回看
# ---------------------------------------------------------------------------
class TestClosePersistsEvidence:
    def test_close_records_evidence_and_is_reviewable(self, client, tmp_path):
        ctx = _setup_real_review(client, tmp_path)
        evidence = _manifest_evidence(client, ctx["bundle"]["bundle_id"])
        issue = _issue_open(client, ctx["review"], ctx["bundle"], evidence)
        version_before = issue["version"]

        r = _close(client, issue, ctx["bundle"], [evidence])
        assert r.status_code == 200, r.text
        closed = r.json()
        assert closed["status"] == "CLOSED"

        # ① 关闭时只有回复 → 问题仍 OPEN（回复文本不等于关闭证据，FR-21）
        assert issue["status"] == "OPEN"

        # ② 关闭记录：关闭人 / 问题版本 / 证据（ID+摘要+路径+角色）/ 绑定包 / 时间
        rows = _closures(client, issue["issue_id"])
        assert len(rows) == 1
        rec = rows[0]
        assert rec.closed_by == REVIEWER_HEADERS["X-Dev-Subject"]
        assert rec.issue_version == version_before
        assert rec.bundle_digest == ctx["bundle"]["bundle_digest"]
        assert rec.confirmation_id
        assert rec.closed_at is not None
        assert [e["artifact_id"] for e in rec.evidence] == [evidence]
        assert rec.evidence[0]["sha256"] and rec.evidence[0]["logical_path"]
        assert rec.evidence[0]["role"] == "verification"

        # ③ 历史回看：GET /reviews/{id} 内嵌该关闭记录（新会话读库，非内存态）
        r = client.get(f"/api/v1/reviews/{ctx['review']['review_id']}", headers=REVIEWER_HEADERS)
        assert r.status_code == 200, r.text
        issues = {i["issue_id"]: i for i in r.json()["issues"]}
        got = issues[issue["issue_id"]]
        assert got["status"] == "CLOSED"
        assert len(got["closures"]) == 1
        assert got["closures"][0]["evidence"][0]["artifact_id"] == evidence
        assert got["closures"][0]["closed_by"] == REVIEWER_HEADERS["X-Dev-Subject"]

    def test_closures_are_append_only_and_immutable(self, client, tmp_path):
        ctx = _setup_real_review(client, tmp_path)
        evidence = _manifest_evidence(client, ctx["bundle"]["bundle_id"])
        issue = _issue_open(client, ctx["review"], ctx["bundle"], evidence)
        assert _close(client, issue, ctx["bundle"], [evidence]).status_code == 200

        closure_id = _closures(client, issue["issue_id"])[0].closure_id
        with client.app.state.session_factory() as s:
            row = s.get(IssueClosureRow, closure_id)
            row.closed_by = "someone_else"
            with pytest.raises(ApiError):
                s.flush()  # before_update 守卫：关闭证据不可原位改写
            s.rollback()
            row = s.get(IssueClosureRow, closure_id)
            s.delete(row)
            with pytest.raises(ApiError):
                s.flush()  # before_delete 守卫：关闭证据不被删除
            s.rollback()

    def test_duplicate_evidence_ids_are_normalised(self, client, tmp_path):
        """重复 ID 去重保序，不重复落库（证据清单是集合语义）。"""
        ctx = _setup_real_review(client, tmp_path)
        a = _manifest_evidence(client, ctx["bundle"]["bundle_id"])
        issue = _issue_open(client, ctx["review"], ctx["bundle"], a)
        r = _close(client, issue, ctx["bundle"], [a, a, a])
        assert r.status_code == 200, r.text
        rec = _closures(client, issue["issue_id"])[0]
        assert [e["artifact_id"] for e in rec.evidence] == [a]


# ---------------------------------------------------------------------------
# 条件 1：拒绝不存在 / 其他项目 / 未提交 / 不适用的证据
# ---------------------------------------------------------------------------
class TestCloseRejectsInvalidEvidence:
    def _open_issue(self, client, tmp_path):
        ctx = _setup_real_review(client, tmp_path)
        evidence = _manifest_evidence(client, ctx["bundle"]["bundle_id"])
        issue = _issue_open(client, ctx["review"], ctx["bundle"], evidence)
        return ctx, issue, evidence

    def _assert_rejected(self, client, ctx, issue, response, *, code: int, kind: str):
        assert response.status_code == code, response.text
        assert kind in response.text or kind in str(response.json())
        # 失败不留痕：问题仍 OPEN、版本不变、无关闭记录
        row = _issue_row(client, issue["issue_id"])
        assert row.status == "OPEN"
        assert row.version == issue["version"]
        assert _closures(client, issue["issue_id"]) == []

    def test_reject_nonexistent_evidence(self, client, tmp_path):
        ctx, issue, _ = self._open_issue(client, tmp_path)
        r = _close(client, issue, ctx["bundle"], ["art_does_not_exist"])
        self._assert_rejected(client, ctx, issue, r, code=422, kind="不存在")

    def test_reject_other_project_evidence(self, client, tmp_path):
        ctx, issue, _ = self._open_issue(client, tmp_path)
        foreign = _add_artifact(client, project_id="proj_b")
        r = _close(client, issue, ctx["bundle"], [foreign])
        self._assert_rejected(client, ctx, issue, r, code=403, kind="不属于本项目")

    def test_reject_uncommitted_evidence(self, client, tmp_path):
        ctx, issue, _ = self._open_issue(client, tmp_path)
        temp = _add_artifact(client, project_id="proj_a", state="TEMP")
        r = _close(client, issue, ctx["bundle"], [temp])
        self._assert_rejected(client, ctx, issue, r, code=422, kind="已提交")

    def test_reject_evidence_outside_frozen_bundle(self, client, tmp_path):
        """项目内、已提交，但不属于本次审查冻结包 → 不适用，拒绝。"""
        ctx, issue, _ = self._open_issue(client, tmp_path)
        stray = _add_artifact(client, project_id="proj_a")
        r = _close(client, issue, ctx["bundle"], [stray])
        self._assert_rejected(client, ctx, issue, r, code=422, kind="冻结包")

    def test_reject_evidence_replaced_after_freeze(self, client, tmp_path):
        """冻结后被替换的证据（记录摘要与冻结摘要不一致）→ 409。"""
        ctx, issue, evidence = self._open_issue(client, tmp_path)
        with client.app.state.session_factory() as s:
            s.execute(
                text("UPDATE artifacts SET sha256 = :sha WHERE artifact_id = :aid"),
                {"sha": "f" * 64, "aid": evidence},
            )
            s.commit()
        r = _close(client, issue, ctx["bundle"], [evidence])
        self._assert_rejected(client, ctx, issue, r, code=409, kind="摘要不一致")

    def test_empty_evidence_list_rejected(self, client, tmp_path):
        ctx, issue, _ = self._open_issue(client, tmp_path)
        r = _close(client, issue, ctx["bundle"], [])
        self._assert_rejected(client, ctx, issue, r, code=422, kind="必须附关闭证据")


# ---------------------------------------------------------------------------
# 条件 4：回复文本不是关闭证据
# ---------------------------------------------------------------------------
class TestReplyIsNotCloseEvidence:
    def test_reply_with_evidence_does_not_close_issue(self, client, tmp_path):
        ctx = _setup_real_review(client, tmp_path)
        evidence = _manifest_evidence(client, ctx["bundle"]["bundle_id"])
        issue = _issue_open(client, ctx["review"], ctx["bundle"], evidence)
        # 回复（即便带证据）不改变状态，也不产生关闭记录
        assert issue["status"] == "OPEN"
        assert _closures(client, issue["issue_id"]) == []
        # 关闭仍必须由审查人显式给出可校验证据
        assert _close(client, issue, ctx["bundle"], [evidence]).status_code == 200
