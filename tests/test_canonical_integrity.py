"""Input-integrity regressions; no solver data or engineering thresholds."""
from __future__ import annotations

import pytest

from dsh_sim.canonical import CanonicalizationError, canonical_dumps, prepared_digest, spec_sha256

pytestmark = pytest.mark.mock


@pytest.mark.parametrize("values", [(1, 2), (1, 1)])
@pytest.mark.parametrize("reverse", [False, True])
def test_nfc_key_collision_is_rejected(values, reverse):
    pairs = [("é", values[0]), ("e\u0301", values[1])]
    if reverse:
        pairs.reverse()
    with pytest.raises(CanonicalizationError, match="NFC"):
        canonical_dumps(dict(pairs))


def test_nested_collision_is_not_hidden_by_hashing():
    with pytest.raises(CanonicalizationError, match="NFC"):
        spec_sha256({"conditions": [{"é": 1, "e\u0301": 2}]})


def test_prepared_artifact_path_collision_is_rejected():
    with pytest.raises(CanonicalizationError, match="NFC"):
        prepared_digest("0" * 64, {"é.sim": "1" * 64, "e\u0301.sim": "2" * 64}, "3" * 64, "adapter/1")


@pytest.mark.parametrize("bad_key", [1, None, ("key",)])
@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_key_types_raise_contract_error_not_sorting_error(bad_key, reverse):
    pairs = [("valid", 1), (bad_key, 2)]
    if reverse:
        pairs.reverse()
    with pytest.raises(CanonicalizationError, match="字符串"):
        canonical_dumps(dict(pairs))


@pytest.mark.parametrize("value", ["\ud800", "\udfff", "prefix\ud800suffix"])
@pytest.mark.parametrize("as_key", [False, True])
def test_invalid_utf8_is_rejected_at_canonicalization(value, as_key):
    obj = {value: 1} if as_key else {"value": value}
    with pytest.raises(CanonicalizationError, match="Unicode"):
        canonical_dumps(obj)


def test_unambiguous_unicode_equivalence_is_preserved():
    assert spec_sha256({"é": "café"}) == spec_sha256({"e\u0301": "cafe\u0301"})


def test_existing_valid_digest_is_unchanged():
    assert spec_sha256({"é": 1}) == "ddcfcf4765da163969972bb20660092ca2787782d9352d1d8a38e93f70acf3bf"
