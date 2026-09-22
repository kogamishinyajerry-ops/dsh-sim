# canonical-json-v1 规范

> 状态：冻结。本规范与参考实现 `src/dsh_sim/canonical/canonical.py` 逐字一致，与测试向量 `tests/test_canonical.py` 同步发版。
> 依据：定义书 §规范化与哈希、CONVENTIONS §3.1。修改规则 = 修改协议版本号并全量回归。

版本标识：`canonical-json-v1`（`CANONICAL_VERSION` 常量）。

## 1. 序列化规则（强制）

| # | 规则 | 说明 |
|---|---|---|
| 1 | 严格 JSON | 仅允许 JSON 原生类型：null、boolean、number、string、array、object。任何其他类型（set、自定义对象等）必须拒绝，抛出 `CanonicalizationError`。 |
| 2 | 拒绝 NaN/Infinity | 浮点数出现 `NaN`、`+Infinity`、`-Infinity` 必须拒绝，抛出 `CanonicalizationError`。 |
| 3 | 字符串 NFC | 所有字符串（含对象键）先做 Unicode NFC 规范化（`unicodedata.normalize("NFC", s)`）。 |
| 4 | 对象键排序 | 对象键按排序后的顺序输出（`sort_keys=True`，NFC 之后排序）。对象键必须是字符串，非字符串键拒绝。 |
| 5 | 数组顺序有意义 | 数组**只规范化元素，绝不排序**。方案（variants）与工况（conditions）的顺序是业务输入的一部分，禁止重排。 |
| 6 | UTF-8 | 输出为 UTF-8 文本；非 ASCII 字符不转义（`ensure_ascii=False`）。 |
| 7 | 无多余空白 | 分隔符为 `separators=(",", ":")`，即键值、元素之间无任何空格/换行。 |

参考序列化调用（与实现一致）：

```python
json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
```

## 2. 哈希规则（强制）

- `sha256_hex(s)`：对 canonical 字符串的 UTF-8 编码计算 SHA-256，输出 64 位小写十六进制。
- `spec_sha256(task_spec_dict)` = `sha256_hex(canonical_dumps(task_spec_dict))`，覆盖**完整规范化业务输入**。
- `prepared_digest(spec_sha, artifacts, readback_sha, adapter_build)` 覆盖信封对象：

```json
{
  "version": "canonical-json-v1",
  "spec_sha256": "<spec_sha>",
  "prepared_artifacts": {"<logical_path>": "<artifact_sha256>"},
  "readback_sha256": "<readback_sha>",
  "adapter_build": "<adapter_build>"
}
```

- **哈希仅由服务计算**；客户端提交的任何摘要字段都以服务端重算为准。
- **时间、会话文本、展示格式不作为工程输入摘要**。相同文件名不能替代内容身份。

## 3. 测试向量

以下向量从 `tests/test_canonical.py` 的 `VECTORS` 抄录并扩展（V-10～V-12 为本版新增：嵌套数组对象、中文、负数浮点）。每条为 `(输入对象 → 期望 canonical 字符串)`。

| # | 输入（Python 字面量） | 期望 canonical 字符串 |
|---|---|---|
| V-01 | `{"b": 1, "a": 2}` | `{"a":2,"b":1}` |
| V-02 | `{"z": {"y": [3, 1, 2], "x": None}}` | `{"z":{"x":null,"y":[3,1,2]}}` |
| V-03 | `[3, 1, 2]` | `[3,1,2]` |
| V-04 | `{"arr": [{"b": 1, "a": 0}]}` | `{"arr":[{"a":0,"b":1}]}` |
| V-05 | `{"café": "value"}`（键为 e+组合尖音符） | `{"café":"value"}`（NFC 单码点 é） |
| V-06 | `"é"`（分解形式） | `"é"`（NFC 单码点） |
| V-07 | `{"n": 1.5, "i": -3}` | `{"i":-3,"n":1.5}` |
| V-08 | `{"t": True, "f": False}` | `{"f":false,"t":true}` |
| V-09 | `{"a b": "c d"}` | `{"a b":"c d"}` |
| V-10 | `{"items": [{"v": [1, [2, 3]], "k": "x"}]}` | `{"items":[{"k":"x","v":[1,[2,3]]}]}` |
| V-11 | `{"工况": "压损", "方案": ["A", "B"]}` | `{"工况":"压损","方案":["A","B"]}` |
| V-12 | `{"delta": -0.125}` | `{"delta":-0.125}` |

### 与 tests/test_canonical.py 的已知差异（如实记录，未修改任何一侧）

- 本表 V-02/V-06 的期望值以**冻结参考实现实际输出**为准（`canonical_dumps` 对外层对象保留包装键、对裸字符串输出带引号的 JSON 文本）。
- `tests/test_canonical.py` 中对应两条向量（`{"x":null,"y":[3,1,2]}`、`é` 无引号）与冻结实现不一致，`pytest tests/test_canonical.py` 现有 2 条**先存失败**（非本任务引入；新增的 V-10～V-12 全部通过）。按诚实红线不删除、不改实现，留待编排层裁定向量归属（BLOCKED 请求）。

### 必须拒绝的输入（`CanonicalizationError`）

- `{"x": float("nan")}`
- `{"x": float("inf")}`
- `{"x": float("-inf")}`
- `{1: "non-str-key"}`（非字符串对象键）
- `{"x": object()}`（非 JSON 原生类型）
- `{"x": {1, 2, 3}}`（set）

### 已知哈希自检向量

- `sha256_hex("abc")` = `ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad`

### 行为不变量（实现必须满足）

- 键序无关：`spec_sha256(a) == spec_sha256(b)` 当 a、b 仅对象键顺序不同。
- 数组序敏感：`spec_sha256({"variants":["A","B"]}) != spec_sha256({"variants":["B","A"]})`。
- `prepared_digest` 中 `adapter_build` 变化必须改变摘要（版本基线 FR-27）。
