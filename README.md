# 本地医疗互操作数据接入与交换服务

用**本地存储 + 本地术语码表**模拟的医疗互操作接入层，基于 FastAPI 实现。
目标是在多标准版本、缺失/畸形字段、并发写入等条件下，让数据结论
**一致且可解释**：任何拒绝都带稳定错误码、字段路径与可复现判定依据，
绝不静默跳过、静默取值或按默认值兜底。

## 1. 支持的标准范围

| 维度 | 支持内容 |
| --- | --- |
| FHIR 标准版本 | STU3 (`3.0`)、R4 (`4.0`)、R5 (`5.0`) |
| 资源类型 | `Patient`、`Observation`、`Condition` |
| 编码格式 | FHIR JSON（`application/fhir+json`），批量为 FHIR `Bundle` (`type=batch`) |
| 术语系统 | LOINC、SNOMED CT（按版本分 URI 登记）、ICD-10、administrative-gender、本地观察/诊断码表 |
| 存储 | 进程内线程安全存储（模拟），资源带单调整数版本号 |

版本声明（**必需，不假设默认值**）：

* 请求头 `X-FHIR-Version: 4.0`（接受别名 `STU3/R4/R5/3/4/5/3.0/4.0/5.0`），或
* `Content-Type: application/fhir+json; fhirVersion=4.0`

## 2. HTTP 接口

| 方法/路径 | 说明 | 权限 |
| --- | --- | --- |
| `POST /fhir/{ResourceType}` | 创建资源（稳定 ID 去重） | `resource:write` |
| `PUT /fhir/{ResourceType}/{id}` | 条件更新，需 `If-Match: W/"n"` | `resource:write` |
| `GET /fhir/{ResourceType}/{id}` | 读取（按权限返回原始/脱敏视图） | `reidentify:read` 或 `patient:read`/`deid:read` |
| `POST /fhir/` | Bundle 批量写入（部分失败隔离） | `resource:write` |

本地模拟令牌（`Authorization: Bearer <token>`）：

| 令牌 | 权限范围 |
| --- | --- |
| `token-deid` | `deid:read`（仅能读脱敏视图） |
| `token-clinician` | `patient:read`、`resource:write`、`deid:read` |
| `token-reidentify` | 额外含 `reidentify:read`（可受控还原） |
| `token-admin` | `*` |

## 3. 兼容规则

### 3.1 字段级版本兼容（不兼容即拒绝，不丢弃、不默认）

* `Observation.comment` 仅 **STU3**；R4+ 使用 `note`。
* `Observation.effectiveInstant` 仅 **R5**。
* `Condition.clinicalStatus`：STU3 为**字符串**；R4/R5 为**必填 CodeableConcept**。
  跨版本传错形态返回 `INCOMPATIBLE_FIELD`，不做隐式转换。
* `Condition.assertedDate` 仅 STU3；`recordedDate` 仅 R4/R5。
* `Condition.onsetPeriod` 仅 R5。
* 未知字段（白名单之外）返回 `UNKNOWN_FIELD`。
* URL 中的类型/ID 与 body 不一致返回 `INCOMPATIBLE_FIELD`。

### 3.2 术语码表规则

* 每个代码系统登记可用 FHIR 版本闭区间。SNOMED CT 的 URI 随版本变化：
  STU3 使用历史 URI `http://snomed.info/sct-stu3`，R4+ 使用
  `http://snomed.info/sct`；跨版本使用返回
  `TERMINOLOGY_VERSION_INCOMPATIBLE`。
* 本地码写入时映射到规范码（如本地 `BP` → LOINC `85354-9`），
  归一结果同时保留 `source_system/source_code`，结论可解释。
* 同一来源码若被声明为映射到多个不同规范码，登记为**冲突墓碑**，
  查询命中返回 `MAPPING_CONFLICT`，绝不静默选其一。
  （演示数据：系统 `http://local.example.org/codes/conflicting` 的 `X1`。）

### 3.3 引用完整性

* 写入时校验 `subject` 等引用：目标必须存在且类型匹配。
* 目标缺失 → `REFERENCE_TARGET_NOT_FOUND`；格式坏/类型不符
  → `REFERENCE_BROKEN`。

### 3.4 去标识化与受控还原

* Patient 敏感字段：`name`、`telecom`、`birthDate`、`address`、`identifier`。
* 无 `reidentify:read` 时，这些字段替换为不可逆占位符
  `<redacted:字段名>`（可区分“字段不存在”），响应中不出现原始值。
* 无令牌 → `UNAUTHORIZED`；权限不足 → `FORBIDDEN`；错误信息不回显敏感值。
* 所有读写写审计日志（只记录 ID/版本/视图等元数据，不记录原始敏感值）。

### 3.5 并发一致性

* 创建使用客户端稳定 ID：重复创建返回 `ALREADY_EXISTS`，天然去重。
* 更新采用乐观并发：必须携带与当前版本一致的 `If-Match`，
  否则返回 `VERSION_CONFLICT (409)`，不会覆盖他人更新。
* 所有变更在锁内一次性替换不可变快照，杜绝半更新。

### 3.6 批量部分失败

* 逐条目独立解析/校验/写入，单条失败不回滚、不污染其他条目与既有状态。
* 响应按输入顺序给出每条 `ok/resource_id/version` 或
  `error{reason,message,field,details}`，并汇总成功/失败计数。
* 部分成功资源可通过 `GET` 单独查询；同样输入重复提交得到同样判定（可复现）。
* Bundle 外层结构错误（非 Bundle、type 非 batch、entry 空）整体拒绝。

## 4. 拒绝原因码（可区分、稳定）

`MALFORMED_JSON`、`NOT_A_RESOURCE_OBJECT`、`MISSING_FIELD`、`EMPTY_FIELD`、
`INVALID_FIELD_TYPE`、`ILLEGAL_CODE`、`STRUCTURE_INCOMPLETE`、`UNKNOWN_FIELD`、
`MISSING_VERSION`、`UNSUPPORTED_VERSION`、`UNSUPPORTED_RESOURCE_TYPE`、
`INCOMPATIBLE_FIELD`、`CODE_SYSTEM_UNKNOWN`、`CODE_NOT_FOUND`、
`MAPPING_CONFLICT`、`TERMINOLOGY_VERSION_INCOMPATIBLE`、
`REFERENCE_BROKEN`、`REFERENCE_TARGET_NOT_FOUND`、`UNAUTHORIZED`、`FORBIDDEN`、
`NOT_FOUND`、`ALREADY_EXISTS`、`VERSION_CONFLICT`、`BATCH_ENTRY_INVALID`。

HTTP 状态映射：400（畸形 JSON）、401、403、404、409（冲突/重复）、
422（其余业务拒绝）。

## 5. 运行与验证

```bash
uv sync
uv run uvicorn main:app --reload        # 启动服务（默认 http://127.0.0.1:8000）

uv run pytest                            # 运行全部测试
uv run pytest --log-cli-level=INFO       # 同时打印输入样本与判定依据
```

### 快速手工验证

```bash
# 1) 创建患者
curl -s -X POST localhost:8000/fhir/Patient \
  -H 'Authorization: Bearer token-admin' -H 'X-FHIR-Version: 4.0' \
  -H 'Content-Type: application/json' \
  -d '{"resourceType":"Patient","id":"p1","gender":"male","birthDate":"1990-05-17"}'

# 2) 低权限读取 -> 脱敏视图
curl -s localhost:8000/fhir/Patient/p1 \
  -H 'Authorization: Bearer token-deid'

# 3) 版本不兼容：R4 请求携带 STU3 字段 comment
curl -s -X POST localhost:8000/fhir/Observation \
  -H 'Authorization: Bearer token-admin' -H 'X-FHIR-Version: 4.0' \
  -H 'Content-Type: application/json' \
  -d '{"resourceType":"Observation","id":"o1","status":"final","comment":"x",
       "code":{"coding":[{"system":"http://loinc.org","code":"85354-9"}]}}'
```

## 6. 测试覆盖重点

* `tests/test_malformed_inputs.py`：畸形/缺失/空值/类型错误/非法码/坏引用。
* `tests/test_version_compat.py`：跨版本字段形态、SNOMED URI 版本区间、本地码归一。
* `tests/test_concurrency.py`：多线程并发创建/更新、HTTP 层竞争、无半更新。
* `tests/test_deid_permissions.py`：多权限视图可区分、未授权不泄漏。
* `tests/test_batch.py`：部分失败隔离、可复现、部分成功可查询、不污染既有状态。
* `tests/test_terminology.py` / `tests/test_references.py`：术语冲突/版本区间、引用完整性。

> 说明：本服务为本地模拟，码表内容与字段子集为演示用途而裁剪；
> 真实对接时应以对应版本 FHIR 规范与权威术语发行包为准。
