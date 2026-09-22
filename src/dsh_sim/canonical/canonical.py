"""canonical-json-v1 —— dsh-sim 规范化序列化与哈希参考实现（冻结层）。

定义书 §规范化与哈希：
- 严格 JSON，拒绝 NaN/Infinity
- 字符串 NFC 规范化
- 对象键排序；数组顺序有意义，禁止排序
- UTF-8；无多余空白
- 哈希仅由服务计算；时间/会话文本/展示格式不进工程输入摘要

对应冻结接口（CONVENTIONS §3.1）：
    canonical_dumps(obj) -> str
    sha256_hex(s) -> str
    spec_sha256(task_spec_dict) -> str
    prepared_digest(spec_sha, artifacts, readback_sha, adapter_build) -> str
"""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from typing import Any

__all__ = [
    "CanonicalizationError",
    "canonical_dumps",
    "sha256_hex",
    "spec_sha256",
    "prepared_digest",
    "CANONICAL_VERSION",
]

CANONICAL_VERSION = "canonical-json-v1"


class CanonicalizationError(ValueError):
    """对象无法按 canonical-json-v1 规范化时抛出。"""


def _normalize_string(value: str, path: str) -> str:
    """Normalize without admitting strings that cannot be hashed as UTF-8."""
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError(f"{path}: 字符串包含非法 Unicode 代理码点") from exc
    return normalized


def _normalize(obj: Any, path: str = "$") -> Any:
    """递归规范化：NFC 字符串、排序对象键、拒绝 NaN/Infinity 与非 JSON 原生类型。"""
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise CanonicalizationError(f"{path}: NaN/Infinity 不允许出现在规范化输入中")
        return obj
    if isinstance(obj, str):
        return _normalize_string(obj, path)
    if isinstance(obj, (list, tuple)):
        # 数组顺序有意义：只规范化元素，绝不排序
        return [_normalize(v, f"{path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k in obj:
            if not isinstance(k, str):
                raise CanonicalizationError(f"{path}: 对象键必须是字符串，得到 {type(k).__name__}")
            nk = _normalize_string(k, path)
            if nk in out:
                raise CanonicalizationError(f"{path}: NFC 规范化后存在重复对象键 {nk!r}")
            out[nk] = _normalize(obj[k], f"{path}.{nk}")
        return out
    raise CanonicalizationError(f"{path}: 不支持类型 {type(obj).__name__}（仅允许 JSON 原生类型）")


def canonical_dumps(obj: Any) -> str:
    """把 JSON 原生对象序列化为 canonical-json-v1 字符串。"""
    normalized = _normalize(obj)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def spec_sha256(task_spec_dict: dict[str, Any]) -> str:
    """spec_sha256 覆盖完整规范化业务输入。"""
    return sha256_hex(canonical_dumps(task_spec_dict))


def prepared_digest(
    spec_sha: str,
    artifacts: dict[str, str],
    readback_sha: str,
    adapter_build: str,
) -> str:
    """prepared_digest 覆盖 spec 摘要 + 准备产物摘要 + 真实回读摘要 + 适配器/软件构建。

    artifacts: {logical_path: artifact_sha256} —— 准备产物的逐文件摘要。
    """
    envelope = {
        "version": CANONICAL_VERSION,
        "spec_sha256": spec_sha,
        "prepared_artifacts": artifacts,
        "readback_sha256": readback_sha,
        "adapter_build": adapter_build,
    }
    return sha256_hex(canonical_dumps(envelope))
