"""FR-18: stale registry rows cannot authenticate changed method files."""
from __future__ import annotations

import hashlib
import json

import pytest

from dsh_sim.canonical import canonical_dumps, sha256_hex
from dsh_sim.capabilities.registry import register_capabilities, resolve_method_package
from dsh_sim.db.session import init_db, make_engine, make_session_factory
from dsh_sim.domain.errors import ApiError


def _package(tmp_path, *, indexed=True):
    root = tmp_path / "capabilities"
    package = root / "test_flow" / "0.1.0"
    package.mkdir(parents=True)
    rules = b'{"rules": [{"threshold": null}]}'
    (package / "rules.json").write_bytes(rules)
    manifest = {"capability_package_id": "test_flow", "version": "0.1.0", "status": "DRAFT"}
    if indexed:
        manifest["content_sha256"] = {"rules.json": hashlib.sha256(rules).hexdigest()}
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    method = {"capability_package_id": "test_flow", "capability_package_sha256": sha256_hex(canonical_dumps(manifest))}
    return root, package, manifest, method


@pytest.mark.mock
def test_stale_registry_digest_does_not_match_modified_manifest(tmp_path):
    root, package, manifest, method = _package(tmp_path)
    engine = make_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    init_db(engine)
    with make_session_factory(engine)() as session:
        register_capabilities(session, root)
        assert resolve_method_package(session, method, capabilities_root=root).digest_match
        manifest["description"] = "changed after registration"
        (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        resolved = resolve_method_package(session, method, capabilities_root=root)
        assert resolved.digest_match is False
        assert resolved.manifest_sha256 != method["capability_package_sha256"]


@pytest.mark.mock
def test_changed_rule_bytes_block_even_when_manifest_unchanged(tmp_path):
    root, package, _, method = _package(tmp_path)
    (package / "rules.json").write_text('{"rules": []}', encoding="utf-8")
    with pytest.raises(ApiError, match="内容与 manifest"):
        resolve_method_package(None, method, capabilities_root=root)


@pytest.mark.mock
@pytest.mark.parametrize("logical", ["../outside.json", "/outside.json", "rules\\outside.json", "manifest.json"])
def test_package_index_rejects_unsafe_or_self_referential_paths(tmp_path, logical):
    root, package, manifest, method = _package(tmp_path)
    manifest["content_sha256"] = {logical: "0" * 64}
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ApiError):
        resolve_method_package(None, method, capabilities_root=root)


@pytest.mark.mock
def test_package_index_rejects_symlinks(tmp_path):
    root, package, _, method = _package(tmp_path)
    original = (package / "rules.json").read_bytes()
    (tmp_path / "outside.json").write_bytes(original)
    (package / "rules.json").unlink()
    (package / "rules.json").symlink_to(tmp_path / "outside.json")
    with pytest.raises(ApiError, match="非法路径"):
        resolve_method_package(None, method, capabilities_root=root)


@pytest.mark.mock
def test_legacy_draft_without_content_index_remains_readable(tmp_path):
    root, _, _, method = _package(tmp_path, indexed=False)
    assert resolve_method_package(None, method, capabilities_root=root).digest_match
