"""FR-18: evidence remains verifiable after moving away from its host/database."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from dsh_sim.db.models import BundleRow
from dsh_sim.evidence.export import export_bundle, verify_export
from helpers_chain import full_mock_chain


@pytest.mark.mock
def test_export_relocation_and_corruption_detection(client, tmp_path):
    chain = full_mock_chain(client, tmp_path)
    original = tmp_path / "export"
    with client.app.state.session_factory() as session:
        bundle = session.get(BundleRow, chain.bundle["bundle_id"])
        exported = export_bundle(session, bundle, original)
        assert exported["bundle_digest"] == chain.bundle["bundle_digest"]
    relocated = tmp_path / "another harness" / "evidence"
    shutil.copytree(original, relocated)
    shutil.rmtree(original)
    checked = verify_export(relocated)
    assert checked["integrity"] == "VERIFIED"
    assert checked["verified_artifacts"] == len(exported["bundle_manifest"])
    assert "Offline view" in (relocated / "report.offline.html").read_text(encoding="utf-8")
    victim = relocated / exported["files"][0]["file"]
    victim.write_bytes(victim.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="digest/length"):
        verify_export(relocated)


@pytest.mark.mock
def test_export_rejects_manifest_change_and_existing_directory(client, tmp_path):
    chain = full_mock_chain(client, tmp_path)
    destination = tmp_path / "export"
    with client.app.state.session_factory() as session:
        bundle = session.get(BundleRow, chain.bundle["bundle_id"])
        export_bundle(session, bundle, destination)
        with pytest.raises(FileExistsError):
            export_bundle(session, bundle, destination)
    index_file = destination / "export.json"
    index = json.loads(index_file.read_text(encoding="utf-8"))
    index["bundle_manifest"][0]["length"] += 1
    index_file.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest digest"):
        verify_export(destination)


@pytest.mark.mock
def test_validation_requires_explicit_fixture_mode_and_preserves_existing_path(tmp_path):
    from dsh_sim.validation.openfoam import main, run_validation

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("untouched", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        main(["--output", str(tmp_path / "new")])
    assert error.value.code == 2
    assert not (tmp_path / "new").exists()
    assert main(["--output", str(existing), "--local-validation", "--quiet"]) == 2
    assert list(existing.iterdir()) == [existing / "keep.txt"]
    with pytest.raises(ValueError, match="built-in"):
        run_validation(tmp_path / "arbitrary", scenario="user supplied executable")
    assert not (tmp_path / "arbitrary").exists()


@pytest.mark.mock
def test_literal_database_path_does_not_parse_question_mark_as_url(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.engine import URL
    from dsh_sim.db.session import make_engine

    sentinel = tmp_path / "existing.db"
    original = make_engine(URL.create("sqlite", database=str(sentinel)))
    with original.begin() as connection:
        connection.execute(text("CREATE TABLE sentinel (value TEXT)"))
        connection.execute(text("INSERT INTO sentinel VALUES ('untouched')"))
    original.dispose()
    before = sentinel.read_bytes()
    directory = tmp_path / "existing.db?fixture=new"
    directory.mkdir()
    target = directory / "validation.db"
    isolated = make_engine(URL.create("sqlite", database=str(target)))
    assert isolated.url.database == str(target)
    with isolated.begin() as connection:
        connection.execute(text("CREATE TABLE validation_fixture (value TEXT)"))
    isolated.dispose()
    assert target.is_file()
    assert sentinel.read_bytes() == before
