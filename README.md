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
| 标准版本 | `fhir-stu3`、`fhir-r4`、`fhir-r5`（信封字段 `standard_version` 必填） |
| 资源类型 | `Patient`、`Observation`、`Condition`（Condition.subject 必须指向 Patient） |
| 资源格式 | JSON 信封：`{standard_version, resourceType, id, ...}` |
| 代码系统 | 本地码表：`http://loinc.org|2.74`（子集）、`http://local.example/clinical|1.0`、`http://local.example/diagnosis|1.0` |

### 版本归一规则（`interop/parsing.py`）

- `Observation.effective`：STU3/R4 的 `effectiveDateTime` 与 R5 的 `effectivePeriod`
  统一归一为 `payload.effective = {type, start, end}`。
- `Condition.onset`：`onsetDateTime`/`onsetPeriod` 同样归一为 `{type, start, end}`；
  `clinicalStatus` 归一为内部字符串（STU3 源是 code，R4/R5 源是 CodeableConcept）。
  三个版本归一后内部形态完全一致，下游不感知版本差异。

### 字段严格性（三个版本一视同仁，`interop/parsing.py`）

每个「资源 × 版本」维护字段白名单，信封字段（`standard_version/resourceType/id/
idempotency_key`）除外：

- 携带了**其它版本才有、声明版本不该出现**的字段（如 STU3 Condition 带 `recordedDate`、
  R5 Observation 带 `effectiveDateTime`）→ `field_not_defined_for_version`（422），
  `field` 定位到具体字段，detail 指出该字段属于哪个版本；
- 携带了**服务任何版本都未定义**的字段（如 `ssn`、`mystery`）→ `unknown_field`（422）。
- 两类原因互可区分；绝不静默忽略、丢弃或补默认值。
- 判定顺序固定为：未知字段 → 必填字段 → 版本错位字段 → 取值/类型/编码，
  保证同一份畸形输入反复提交结论一致。
- 新资源不放宽标准：必填缺失（`missing_required_field`）、状态取值非法
  （`invalid_field_type`）、编码结构缺 system/code（`illegal_coding`）、
  诊断码不在本地码表（`unknown_code`）一律拒绝。

### 引用兼容矩阵（`interop/references.py`）

| 源资源版本 | 引用目标版本 | 结论 |
| --- | --- | --- |
| 同版本 | 同版本 | 允许 |
| 高版本 | 低版本（R5→R4、R4→STU3、R5→STU3） | 允许（高版本向后兼容读旧版本） |
| 低版本 | 高版本（STU3→R4、R4→R5、STU3→R5） | 拒绝 `reference_version_mismatch` |

引用目标不存在一律拒绝 `broken_reference`，且资源不会入库（无残留）。

## 拒绝回执与修正重放（`interop/receipts.py`、`interop/service.py`）

任何一次被拒写入都登记回执（单条与批量逐条皆是）：

- 回执内容：`receipt_id`、资源坐标（`Condition/c1`）、批量下标 `batch_index`、
  `reason`、`field`、判定依据 `basis`、`status`；**不保存原始提交内容**，
  只保存输入指纹（canonical JSON 的 sha256）。
- **可复现**：`receipt_id = rcpt_<输入指纹>`（批量追加 `#<下标>`）。同一输入反复
  提交拿到同一条回执、结论一致，不会堆叠新回执。
- **只留痕不入库**：被拒数据绝不进主存储，也不改变/污染该资源已有版本。
- **不泄密**：回执文本中回显的 Patient 敏感原值（姓名/出生日期/联系方式）按去标识化
  口径替换为 `REDACTED:<sha256前12位>`。
- 批量部分成功时成功条目照常入库，失败条目各自带独立 `receipt_id` 可单独查询。

修正重放流程（状态机 `open → resolving → resolved`）：

1. 接入方按回执修正数据，`POST /receipts/{id}/replay` 重放；
2. 重放走与首次**完全相同**的解析+术语+引用+入库链路，不放水；修正仍不合格会被再次
   拒绝并产生新回执，原回执保持 `open` 可继续修正；
3. 成功入库后原回执置 `resolved`（记录最终资源键与版本），待处理列表可用
   `GET /receipts?status=open|resolved` 区分；
4. 对 `resolved` 回执再次重放返回 `receipt_already_resolved`（409），不重复入库；
5. 多个调用方并发重放同一条回执时，回执认领是原子操作：恰好一个生效，
   其余得到 `receipt_resolved_elsewhere`（或在胜负已分后得到
   `receipt_already_resolved`），无半更新、无重复写入、无版本错乱；
6. 重放记录的资源坐标必须与回执一致，否则 `receipt_target_mismatch`。

## 错误分类（拒绝原因可区分）

所有失败抛出 `InteropError(reason, detail, field)`，HTTP 层映射为不同状态码，
响应体为 `{"error": {"reason", "detail", "field"}}`：

| reason | HTTP | 含义 |
| --- | --- | --- |
| `malformed_json` | 400 | 输入不是合法 JSON / 顶层不是对象 |
| `unsupported_standard_version` | 400 | 标准版本不在支持范围 |
| `unsupported_resource_type` | 400 | 资源类型不在支持范围 |
| `missing_required_field` | 422 | 必填字段缺失 |
| `invalid_field_type` | 422 | 字段类型/取值/格式非法 |
| `illegal_coding` | 422 | 编码结构非法（缺 system/code） |
| `field_not_defined_for_version` | 422 | 字段在声明版本中不该出现（定义在其它版本） |
| `unknown_field` | 422 | 字段在服务支持的任何版本中都未定义 |
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
| `receipt_not_found` | 404 | 回执不存在 |
| `receipt_already_resolved` | 409 | 回执已解决，重放不得重复生效 |
| `receipt_resolved_elsewhere` | 409 | 并发重放时回执已被其它调用方认领/解决 |
| `receipt_target_mismatch` | 422 | 重放记录与回执指向的资源不一致 |

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
| GET | `/receipts?status=open\|resolved` | 回执列表（可按状态过滤） |
| GET | `/receipts/{receipt_id}` | 查询单条拒绝回执 |
| POST | `/receipts/{receipt_id}/replay` | 针对回执的修正重放 |
| POST | `/terminology/validate` | 代码校验 |
| POST | `/terminology/map?target_system=` | 代码映射 |

## 验证方法

```bash
uv run pytest tests/ -q                 # 全部用例（含字段严格性/回执/重放）
uv run pytest tests/ -s -k concurrency  # 查看并发用例的输入样本与判定依据日志
uv run pytest tests/ -s -k receipt      # 回执与修正重放用例（含 -s 打印样本与依据）
```

测试以畸形输入、版本不兼容与并发访问为主：

- `test_parsing.py`：畸形/缺失输入逐一断言拒绝原因分类；
- `test_fields.py`（规则一）：STU3/R4/R5 × Patient/Observation/Condition 上
  「版本错位字段」与「未知字段」分别拒绝且原因/字段可区分；三版本 Condition 归一同构；
  STU3 字符串型 / R4·R5 CodeableConcept 型 clinicalStatus；Condition subject 必须指向
  Patient；未知代码/缺必填/非法编码不放宽；STU3↔R4 引用方向矩阵；
- `test_receipts.py`（规则二、三）：回执含资源/下标/原因/字段/依据且可查；
  同输入反复提交只产生同一条回执；批量失败逐条有独立回执；回执不泄漏姓名/出生日期/电话；
  修正重放走完整链路并置 resolved；resolved 再重放得 `receipt_already_resolved`；
  open/resolved 列表可分；8 线程并发重放恰好 1 次生效；HTTP 端到端；
- `test_terminology.py`：未知代码、一对二映射冲突、码表版本不兼容；
- `test_references.py`：断裂引用、R4→R5 版本错配、失败后无残留；
- `test_deidentify.py`：伪造令牌、角色不足、跨资源令牌、撤销令牌，互不混淆；
- `test_concurrency.py`：16 线程并发更新恰好 1 胜、8 线程并发创建恰好 1 胜、
  幂等重放不重复写入；
- `test_batch.py`：部分失败原因可区分可复现、失败无残留、部分成功可查询。

每个用例通过 `log_case` 在日志中打印**输入样本**与**判定依据**。
