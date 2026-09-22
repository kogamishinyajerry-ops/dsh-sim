"""能力包目录扫描与注册（WP-17 子集）。

规则（定义书 §能力包与最小知识体系）：
- 磁盘 `capabilities/<package_id>/<version>/manifest.json` 是登记源；
- 扫描只负责**注册为 DRAFT**；RELEASED 只能来自人工发布流程（approval.json
  引用的人工审批记录），本模块永不自动置 RELEASED；
- 重复扫描幂等：同 (capability_package_id, version) 已存在则跳过；
- manifest 缺关键字段时该行标记 status=INVALID 并带 error，不静默跳过。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.db.models import CapabilityPackageRow

REQUIRED_MANIFEST_FIELDS = ("capability_package_id", "version", "status")
VALID_STATUSES = {"DRAFT", "RELEASED", "RETIRED", "INVALID"}
# purpose 首版固定 design_screening（定义书 §输入契约）；manifest 可省略，
# 省略时读 domain.json 的 purpose，仍无则按固定值登记并在 note 标注。
DEFAULT_PURPOSE = "design_screening"


def scan_capabilities_root(root: Path) -> list[dict[str, Any]]:
    """扫描 capabilities/ 目录，返回登记记录列表（不落库）。"""
    records: list[dict[str, Any]] = []
    if not root.is_dir():
        return records
    for manifest_path in sorted(root.glob("*/*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            records.append({
                "capability_package_id": manifest_path.parent.parent.name,
                "version": manifest_path.parent.name,
                "status": "INVALID",
                "error": f"manifest 不可解析: {exc}",
                "manifest_sha256": None,
            })
            continue
        missing = [f for f in REQUIRED_MANIFEST_FIELDS if f not in manifest]
        status = manifest.get("status", "DRAFT")
        if status not in VALID_STATUSES:
            status = "INVALID"
        if missing:
            status = "INVALID"
        # 安全网：approval.json 无人工审批记录时，即使 manifest 写 RELEASED 也降级 DRAFT
        approval_path = manifest_path.parent / "approval.json"
        has_human_approval = False
        if approval_path.is_file():
            try:
                approval = json.loads(approval_path.read_text(encoding="utf-8"))
                has_human_approval = bool(approval.get("approvals"))
            except (OSError, json.JSONDecodeError):
                has_human_approval = False
        demoted = False
        if status == "RELEASED" and not has_human_approval:
            status = "DRAFT"
            demoted = True
        # purpose：manifest → domain.json → R0 固定值
        purpose = manifest.get("purpose")
        if purpose is None:
            domain_path = manifest_path.parent / "domain.json"
            if domain_path.is_file():
                try:
                    purpose = json.loads(domain_path.read_text(encoding="utf-8")).get("purpose")
                except (OSError, json.JSONDecodeError):
                    purpose = None
        if purpose is None:
            purpose = DEFAULT_PURPOSE
        records.append({
            "capability_package_id": manifest.get("capability_package_id", manifest_path.parent.parent.name),
            "version": str(manifest.get("version", manifest_path.parent.name)),
            "status": status,
            "purpose": purpose,
            "domain_summary": manifest.get("domain_summary"),
            "compatibility": manifest.get("compatibility"),
            "manifest_sha256": sha256_hex(canonical_dumps(manifest)),
            "source_path": str(manifest_path.parent),
            "demoted_from_released": demoted,
            "error": f"缺字段: {missing}" if missing else None,
        })
    return records


def register_capabilities(session: Session, root: Path) -> list[dict[str, Any]]:
    """扫描并幂等入库。返回本次扫描记录（含已存在标记）。"""
    out: list[dict[str, Any]] = []
    for rec in scan_capabilities_root(root):
        exists = (
            session.query(CapabilityPackageRow)
            .filter_by(
                capability_package_id=rec["capability_package_id"],
                version=rec["version"],
            )
            .one_or_none()
        )
        if exists is not None:
            rec["registered"] = False
            out.append(rec)
            continue
        session.add(CapabilityPackageRow(
            capability_package_id=rec["capability_package_id"],
            version=rec["version"],
            status=rec["status"],
            purpose=rec.get("purpose"),
            domain_summary=rec.get("domain_summary"),
            compatibility=rec.get("compatibility"),
            manifest_sha256=rec.get("manifest_sha256") or "",
        ))
        rec["registered"] = True
        out.append(rec)
    session.commit()
    return out
