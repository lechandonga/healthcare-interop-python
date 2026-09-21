# 本地医疗互操作数据接入与交换服务

用本地存储模拟患者临床数据与术语码表，保证在不同标准版本、缺失字段与并发写入下
数据结论一致且可解释：**任何拒绝都带可区分的原因分类，绝不静默跳过或按默认值处理。**

## 运行

```bash
uv sync
uv run uvicorn main:app --reload   # 启动服务
uv run pytest tests/ -q            # 运行测试（-s 可查看输入样本与判定依据日志）
```

## 支持的标准范围

| 项目 | 范围 |
| --- | --- |
| 标准版本 | `fhir-r4`、`fhir-r5`（信封字段 `standard_version` 必填） |
| 资源类型 | `Patient`、`Observation` |
| 资源格式 | JSON 信封：`{standard_version, resourceType, id, ...}` |
| 代码系统 | 本地码表：`http://loinc.org|2.74`（子集）、`http://local.example/clinical|1.0` |

### 版本归一规则（`interop/parsing.py`）

- R4 `Observation.effectiveDateTime` 与 R5 `Observation.effectivePeriod` 统一归一为
  `payload.effective = {type, start, end}`，下游不感知版本差异。
- R4 不允许 R5 才有的 `status`（如 `corrected`/`cancelled`），反之 R5 允许。
- 版本字段错位（如 R5 资源携带 `effectiveDateTime`）按 `missing_required_field` 拒绝，
  不做隐式兼容。

### 引用兼容矩阵（`interop/references.py`）

| 源资源版本 | 引用目标版本 | 结论 |
| --- | --- | --- |
| R4 | R4 | 允许 |
| R5 | R5 | 允许 |
| R5 | R4 | 允许（新版本向后兼容读旧版本） |
| R4 | R5 | 拒绝 `reference_version_mismatch` |

引用目标不存在一律拒绝 `broken_reference`，且资源不会入库（无残留）。

## 错误分类（拒绝原因可区分）

所有失败抛出 `InteropError(reason, detail, field)`，HTTP 层映射为不同状态码，
响应体为 `{"error": {"reason", "detail", "field"}}`：

| reason | HTTP | 含义 |
| --- | --- | --- |
| `malformed_json` | 400 | 输入不是合法 JSON / 顶层不是对象 |
| `unsupported_standard_version` | 400 | 标准版本不在支持范围 |
| `unsupported_resource_type` | 400 | 资源类型不在支持范围 |
| `missing_required_field` | 422 | 必填字段缺失（含版本字段错位） |
| `invalid_field_type` | 422 | 字段类型/取值/格式非法 |
| `illegal_coding` | 422 | 编码结构非法（缺 system/code） |
| `unknown_code` | 422 | 本地码表无此代码或无映射 |
| `mapping_conflict` | 409 | 一对多映射冲突，需人工裁决，不静默取其一 |
| `code_system_version_incompatible` | 409 | 码表版本与本地不一致（`system\|version`） |
| `broken_reference` | 422 | 引用目标不存在 |
| `reference_version_mismatch` | 409 | 旧版本资源引用新版本资源 |
| `invalid_token` | 401 | 还原令牌无效/已撤销 |
| `unauthorized_reidentification` | 403 | 角色不足或令牌授权范围不符 |
| `version_conflict` | 409 | 乐观锁冲突（并发修改） |
| `resource_not_found` | 404 | 资源不存在 |
| `duplicate_write` | 409 | 无匹配幂等键的重复创建 |

## 一致性与安全规则

- **并发写入**（`interop/store.py`）：单把可重入锁内完成"读版本-校验-写入"原子序列；
  更新必须携带 `expected_version`，冲突方收到 `version_conflict`，重新读取后可收敛。
  并发创建同一资源恰好一个成功，其余 `duplicate_write`。无半更新、无覆盖他人更新。
- **幂等**：每个资源记录已应用的 `idempotency_key`，重放直接返回原记录，版本不前进。
- **批量写入**：`POST /resources/batch` 逐条独立提交，返回
  `{succeeded: [{index, key, version}], failed: [{index, reason, detail, field}]}`；
  单条失败不回滚、不污染其它记录，失败下标与原因可复现，部分成功结果可查询。
- **去标识化**（`interop/deidentify.py`）：Patient 的 `name/birthDate/telecom`
  默认以 `REDACTED:<sha256前12位>` 占位符返回，原值只存于服务内保密库。
  还原需 `clinical` 角色令牌（`POST /tokens/{type}/{id}?role=clinical`），
  令牌按资源 scoped、可撤销；未授权访问不泄漏任何原值，
  `deidentified` / `reidentified` 两种视图在响应中显式标注。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/resources?idempotency_key=` | 新建资源（解析+术语校验+引用检查+入库） |
| PUT | `/resources/{type}/{id}?expected_version=` | 乐观锁更新 |
| POST | `/resources/batch` | 批量写入（部分失败） |
| GET | `/resources/{type}/{id}?token=` | 读取（默认去标识化视图） |
| POST | `/tokens/{type}/{id}?role=` | 签发还原令牌 |
| POST | `/terminology/validate` | 代码校验 |
| POST | `/terminology/map?target_system=` | 代码映射 |

## 验证方法

```bash
uv run pytest tests/ -q          # 44 个用例
uv run pytest tests/ -s -k concurrency   # 查看并发用例的输入样本与判定依据日志
```

测试以畸形输入、版本不兼容与并发访问为主：

- `test_parsing.py`：18 组畸形/缺失/版本错位输入，逐一断言拒绝原因分类；
- `test_terminology.py`：未知代码、一对二映射冲突、码表版本不兼容；
- `test_references.py`：断裂引用、R4→R5 版本错配、失败后无残留；
- `test_deidentify.py`：伪造令牌、角色不足、跨资源令牌、撤销令牌，互不混淆；
- `test_concurrency.py`：16 线程并发更新恰好 1 胜、8 线程并发创建恰好 1 胜、
  幂等重放不重复写入；
- `test_batch.py`：部分失败原因可区分可复现、失败无残留、部分成功可查询。

每个用例通过 `log_case` 在日志中打印**输入样本**与**判定依据**。
