"""能力包注册器测试（WP-17）：DRAFT 注册、RELEASED 安全网降级、幂等。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsh_sim.capabilities import register_capabilities, scan_capabilities_root
from dsh_sim.db.models import CapabilityPackageRow
from dsh_sim.db.session import init_db, make_engine, make_session_factory


def _write_package(root: Path, pkg: str, ver: str, status: str, approvals: list | None = None) -> Path:
    d = root / pkg / ver
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "capability_package_id": pkg,
        "version": ver,
        "status": status,
        "purpose": "design_screening",
        "domain_summary": "test",
        "compatibility": {"dsh": "UNCONFIRMED"},
    }), encoding="utf-8")
    (d / "approval.json").write_text(json.dumps({
        "status": status,
        "approvals": approvals or [],
    }), encoding="utf-8")
    return d


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s


@pytest.mark.mock
def test_scan_registers_draft(session, tmp_path):
    root = tmp_path / "capabilities"
    _write_package(root, "buffer_chamber", "0.1.0", "DRAFT")
    recs = register_capabilities(session, root)
    assert len(recs) == 1 and recs[0]["registered"] is True
    row = session.query(CapabilityPackageRow).one()
    assert row.status == "DRAFT"
    assert row.capability_package_id == "buffer_chamber"


@pytest.mark.mock
def test_released_without_human_approval_demoted(session, tmp_path):
    """安全网：无人工审批记录的 RELEASED 一律降级 DRAFT（定义书发布门）。"""
    root = tmp_path / "capabilities"
    _write_package(root, "evil_pkg", "9.9.9", "RELEASED", approvals=[])
    recs = register_capabilities(session, root)
    assert recs[0]["status"] == "DRAFT"
    assert recs[0]["demoted_from_released"] is True


@pytest.mark.mock
def test_scan_idempotent(session, tmp_path):
    root = tmp_path / "capabilities"
    _write_package(root, "buffer_chamber", "0.1.0", "DRAFT")
    register_capabilities(session, root)
    recs2 = register_capabilities(session, root)
    assert recs2[0]["registered"] is False
    assert session.query(CapabilityPackageRow).count() == 1


@pytest.mark.mock
def test_invalid_manifest_marked_not_silent(tmp_path):
    root = tmp_path / "capabilities"
    d = root / "broken" / "0.0.1"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{not json", encoding="utf-8")
    recs = scan_capabilities_root(root)
    assert recs[0]["status"] == "INVALID"
    assert "不可解析" in recs[0]["error"]
