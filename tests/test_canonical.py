"""canonical-json-v1 测试向量（与契约文档 contracts/canonical-json-v1.md 同步发版）。

向量规则：每条 (输入对象, 期望 canonical 字符串)。哈希向量由参考实现生成后冻结，
修改实现前必须先更新向量并在 PR 说明原因。
"""
from __future__ import annotations

import hashlib

import pytest

from dsh_sim.canonical import (
    CanonicalizationError,
    canonical_dumps,
    prepared_digest,
    sha256_hex,
    spec_sha256,
)

VECTORS = [
    ({"b": 1, "a": 2}, '{"a":2,"b":1}'),
    ({"z": {"y": [3, 1, 2], "x": None}}, '{"z":{"x":null,"y":[3,1,2]}}'),
    # 数组顺序有意义：不排序
    ([3, 1, 2], "[3,1,2]"),
    ({"arr": [{"b": 1, "a": 0}]}, '{"arr":[{"a":0,"b":1}]}'),
    # NFC：e + combining acute → é 单码点
    ({"café": "value"}, '{"café":"value"}'),
    ("é", '"é"'),  # NFC 单码点 é；裸字符串序列化带 JSON 引号
    ("é", '"é"'),  # 分解形式 e+U+0301 → NFC 单码点 é
    # 数字
    ({"n": 1.5, "i": -3}, '{"i":-3,"n":1.5}'),
    ({"t": True, "f": False}, '{"f":false,"t":true}'),
    # 无多余空白
    ({"a b": "c d"}, '{"a b":"c d"}'),
    # 契约文档新增向量 V-10~V-12：嵌套数组对象、中文、负数浮点
    ({"items": [{"v": [1, [2, 3]], "k": "x"}]}, '{"items":[{"k":"x","v":[1,[2,3]]}]}'),
    ({"工况": "压损", "方案": ["A", "B"]}, '{"工况":"压损","方案":["A","B"]}'),
    ({"delta": -0.125}, '{"delta":-0.125}'),
]

REJECTED = [
    {"x": float("nan")},
    {"x": float("inf")},
    {"x": float("-inf")},
    {1: "non-str-key"},
    {"x": object()},
    {"x": {1, 2, 3}},
]


@pytest.mark.mock
@pytest.mark.parametrize("obj,expected", VECTORS)
def test_canonical_vectors(obj, expected):
    assert canonical_dumps(obj) == expected


@pytest.mark.mock
@pytest.mark.parametrize("obj", REJECTED)
def test_canonical_rejects(obj):
    with pytest.raises(CanonicalizationError):
        canonical_dumps(obj)


@pytest.mark.mock
def test_sha256_known_vector():
    # RFC 4231 风格自检：sha256("abc") 的公开已知值
    assert sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.mark.mock
def test_spec_sha256_stable_across_key_order():
    a = {"conditions": [{"id": "c1"}], "variants": ["A", "B"]}
    b = {"variants": ["A", "B"], "conditions": [{"id": "c1"}]}
    assert spec_sha256(a) == spec_sha256(b)


@pytest.mark.mock
def test_spec_sha256_sensitive_to_array_order():
    a = {"variants": ["A", "B"]}
    b = {"variants": ["B", "A"]}
    assert spec_sha256(a) != spec_sha256(b)


@pytest.mark.mock
def test_prepared_digest_shape():
    d1 = prepared_digest(
        spec_sha="0" * 64,
        artifacts={"cases/A_c1.sim": "1" * 64},
        readback_sha="2" * 64,
        adapter_build="starccm-mock-0.1.0",
    )
    assert len(d1) == 64
    d2 = prepared_digest("0" * 64, {"cases/A_c1.sim": "1" * 64}, "2" * 64, "starccm-mock-0.1.0")
    assert d1 == d2
    # 适配器构建变化必须改变摘要（版本基线 FR-27）
    d3 = prepared_digest("0" * 64, {"cases/A_c1.sim": "1" * 64}, "2" * 64, "other-build")
    assert d1 != d3
