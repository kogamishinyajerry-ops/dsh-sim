"""FR-18: portable, hash-verifiable export of an already frozen evidence bundle.

This module copies existing evidence. It cannot run a solver, grant approval or
change a manifest. The optional offline HTML is a separately labelled view;
the original report bytes remain in the frozen artifacts.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy.orm import Session

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.db.models import ArtifactRow, BundleRow
from dsh_sim.evidence.bundle import verify_bundle_integrity

FORMAT = "dsh-sim-evidence-export/v1"


def export_bundle(session: Session, bundle: BundleRow, destination: str | Path) -> dict[str, Any]:
    """Export to a new directory; refuse missing/changed source artifacts."""
    destination = Path(destination)
    issues = verify_bundle_integrity(session, bundle)
    if any(issues.values()):
        raise ValueError(f"Bundle integrity check failed: {issues}")
    if sha256_hex(canonical_dumps(bundle.manifest)) != bundle.bundle_digest:
        raise ValueError("Frozen manifest does not match bundle_digest")
    destination.mkdir(parents=True, exist_ok=False)
    files: list[dict[str, Any]] = []
    report_bytes: bytes | None = None
    for entry in bundle.manifest:
        artifact_id = entry["artifact_id"]
        if not re.fullmatch(r"art_[A-Za-z0-9_-]+", artifact_id):
            raise ValueError("Invalid artifact identifier in frozen manifest")
        artifact = session.get(ArtifactRow, artifact_id)
        if artifact is None or not artifact.storage_path:
            raise ValueError(f"Missing artifact: {artifact_id}")
        content = Path(artifact.storage_path).read_bytes()
        if len(content) != entry["length"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError(f"Artifact changed during export: {artifact_id}")
        name = Path(artifact.logical_path).name
        if name in ("", ".", "..") or "\\" in name:
            name = "content.bin"
        relative = Path("artifacts") / artifact_id / name
        target = destination / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(content)
        files.append({"artifact_id": artifact_id, "file": relative.as_posix(), "sha256": entry["sha256"], "length": entry["length"]})
        if entry.get("role") == "report":
            report_bytes = content
    index = {
        "format": FORMAT,
        "bundle_id": bundle.bundle_id,
        "bundle_digest": bundle.bundle_digest,
        "task_id": bundle.task_id,
        "revision": bundle.revision,
        "bundle_manifest": bundle.manifest,
        "files": files,
        "offline_report": "report.offline.html" if report_bytes is not None else None,
        "note": "Export proves frozen-byte integrity, not numerical validity or engineering acceptance. Offline HTML is a derived link view; the original report is in artifacts.",
    }
    if report_bytes is not None:
        html = report_bytes.decode("utf-8")
        for item in files:
            html = html.replace(f"/api/v1/artifacts/{item['artifact_id']}/content", quote(item["file"], safe="/"))
        banner = '<div style="padding:12px;background:#fff3cd;color:#392c00">Offline view of frozen evidence. Original report bytes and hashes are retained in artifacts.</div>'
        html = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + banner, html, count=1)
        (destination / "report.offline.html").write_text(html, encoding="utf-8")
    (destination / "export.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    verify_export(destination)
    return index


def verify_export(directory: str | Path) -> dict[str, Any]:
    """Recheck a relocated export using only its files, without a DB or Harness."""
    root = Path(directory).resolve()
    index = json.loads((root / "export.json").read_text(encoding="utf-8"))
    if index.get("format") != FORMAT:
        raise ValueError("Unsupported evidence export format")
    manifest = index["bundle_manifest"]
    if sha256_hex(canonical_dumps(manifest)) != index["bundle_digest"]:
        raise ValueError("Export manifest digest mismatch")
    expected = {item["artifact_id"]: item for item in manifest}
    if len(expected) != len(manifest):
        raise ValueError("Duplicate artifact in frozen manifest")
    seen: set[str] = set()
    for item in index["files"]:
        artifact_id = item["artifact_id"]
        if artifact_id not in expected or artifact_id in seen:
            raise ValueError("Export contains unexpected/duplicate artifact")
        seen.add(artifact_id)
        relative = Path(item["file"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in item["file"]:
            raise ValueError("Unsafe export path")
        path = root / relative
        indexed_parts = [root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1)]
        if any(p.is_symlink() for p in indexed_parts) or not path.resolve().is_relative_to(root):
            raise ValueError("Unsafe export symlink")
        content = path.read_bytes()
        frozen = expected[artifact_id]
        actual = hashlib.sha256(content).hexdigest()
        if actual != frozen["sha256"] or len(content) != frozen["length"]:
            raise ValueError(f"Export artifact digest/length mismatch: {artifact_id}")
        if item["sha256"] != frozen["sha256"] or item["length"] != frozen["length"]:
            raise ValueError("Export index differs from frozen manifest")
    if seen != set(expected):
        raise ValueError("Export is missing frozen artifacts")
    return {"bundle_id": index["bundle_id"], "bundle_digest": index["bundle_digest"], "verified_artifacts": len(seen), "integrity": "VERIFIED"}
