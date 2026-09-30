"""能力包目录扫描与注册（WP-17 子集）。

规则（定义书 §能力包与最小知识体系）：
- 磁盘 `capabilities/<package_id>/<version>/manifest.json` 是登记源；
- 扫描只负责**注册为 DRAFT**；RELEASED 只能来自人工发布流程（approval.json
  引用的人工审批记录），本模块永不自动置 RELEASED；
- 重复扫描幂等：同 (capability_package_id, version) 已存在则跳过；
- manifest 缺关键字段时该行标记 status=INVALID 并带 error，不静默跳过。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ResolvedMethodPackage:
    """TaskSpec.method 的精确解析结果（上游报告问题 2 验收条件 1）。

    TaskSpec 契约（contracts/task-spec.schema.json#/$defs/method）声明
    capability_package_id + capability_package_sha256（摘要，不是版本号）。
    版本与发布状态只能由该摘要反查得到——这就是"精确解析"的含义。
    """

    capability_package_id: str
    version: str
    status: str
    manifest: dict[str, Any]
    manifest_sha256: str
    declared_sha256: str
    digest_match: bool
    released: bool
    source_dir: Path


def resolve_method_package(
    session: Session | None,
    method: dict[str, Any] | None,
    *,
    capabilities_root: Path | None = None,
) -> ResolvedMethodPackage:
    """按 TaskSpec.method 声明的包标识 + 摘要精确解析（问题 2 验收条件 1）。

    解析顺序（摘要优先，绝不静默回退到某个"当前版本"）：
    1. 磁盘扫描该包全部版本，取 manifest 摘要 == spec 声明摘要的版本（digest_match=True）；
    2. 摘要无命中时退回注册表最新版本（digest_match=False，如实记录差异）；
    3. 该包完全不存在 → ApiError(BLOCKED)：方法包与 spec 不符时不构建包，
       不允许默认 0.1.0 静默顶替（问题 2：「不能静默替换规则」）。
    """
    from dsh_sim.domain.errors import ApiError, ErrorCode

    method = method or {}
    pkg_id = method.get("capability_package_id")
    declared = str(method.get("capability_package_sha256") or "")
    if not pkg_id:
        raise ApiError(
            ErrorCode.VALIDATION,
            "TaskSpec.method 缺 capability_package_id，无法解析方法包",
            details={"method": method},
        )
    root = capabilities_root or _default_root()
    # 磁盘为登记源（status 已含 RELEASED 需人工审批的降级规则、manifest_sha256 计算）
    records = [
        rec
        for rec in scan_capabilities_root(root)
        if rec["capability_package_id"] == pkg_id
    ]
    if not records:
        raise ApiError(
            ErrorCode.BLOCKED,
            "能力包不存在：capabilities/ 下无该包任何版本，不能构建证据包",
            retryable=False,
            details={"capability_package_id": pkg_id, "declared_sha256": declared},
        )

    matched = next((r for r in records if r.get("manifest_sha256") == declared), None)
    # A stale registration must never authenticate changed bytes on disk.  The
    # registry is an index; only the current manifest content can match a digest.
    rec = matched or sorted(records, key=lambda r: r["version"])[-1]

    version = rec["version"]
    mpath = root / pkg_id / version / "manifest.json"
    if not mpath.is_file():
        raise ApiError(
            ErrorCode.BLOCKED,
            "能力包 manifest 不可读，不能构建证据包",
            retryable=False,
            details={"capability_package_id": pkg_id, "version": version},
        )
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApiError(
            ErrorCode.BLOCKED,
            f"能力包 manifest 不可解析: {exc}",
            retryable=False,
            details={"capability_package_id": pkg_id, "version": version},
        ) from exc

    # New packages bind their rules, units and boundary definitions in the
    # manifest.  Older draft skeletons without a file index remain readable; a
    # declared index is mandatory evidence and any mismatch blocks resolution.
    verify_package_contents(mpath.parent, manifest)
    current_digest = sha256_hex(canonical_dumps(manifest))

    return ResolvedMethodPackage(
        capability_package_id=pkg_id,
        version=version,
        status=rec.get("status", "INVALID"),
        manifest=manifest,
        manifest_sha256=current_digest,
        declared_sha256=declared,
        digest_match=current_digest == declared,
        released=rec.get("status") == "RELEASED",
        source_dir=root / pkg_id / version,
    )


def verify_package_contents(source_dir: Path, manifest: dict[str, Any]) -> None:
    """Verify a package's optional content_sha256 index without following links.

    These are source-file digests, not engineering tolerances or approvals.
    A manifest cannot authenticate itself, so its own entry is forbidden.
    """
    from dsh_sim.domain.errors import ApiError, ErrorCode

    declared_files = manifest.get("content_sha256")
    if declared_files is None:
        return
    if not isinstance(declared_files, dict) or not declared_files:
        raise ApiError(ErrorCode.BLOCKED, "能力包 content_sha256 必须为非空文件摘要表")
    root = source_dir.resolve()
    for logical_path, expected in declared_files.items():
        relative = Path(logical_path)
        invalid = (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(c not in "0123456789abcdef" for c in expected)
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in logical_path
            or logical_path == "manifest.json"
        )
        path = root / relative
        indexed_parts = [root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1)]
        if invalid or any(p.is_symlink() for p in indexed_parts):
            raise ApiError(ErrorCode.BLOCKED, "能力包文件索引含非法路径或摘要", details={"path": logical_path})
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ApiError(ErrorCode.BLOCKED, "能力包索引文件缺失", details={"path": logical_path})
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ApiError(
                ErrorCode.BLOCKED,
                "能力包文件内容与 manifest 冻结摘要不一致",
                details={"path": logical_path, "expected_sha256": expected, "actual_sha256": actual},
            )


def _default_root() -> Path:
    return Path(
        os.environ.get("DSH_SIM_CAPABILITIES_ROOT")
        or (Path(__file__).resolve().parents[3] / "capabilities")
    )
