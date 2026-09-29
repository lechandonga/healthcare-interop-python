# 本地医疗互操作数据接入与交换服务

用本地存储模拟患者临床数据与术语码表，保证在不同标准版本、缺失字段与并发写入下
数据结论一致且可解释：**任何拒绝都带可区分的原因分类并留下回执，绝不静默跳过、
按默认值处理或悄悄忽略未知字段。**

## 运行

```bash
uv sync
uv run uvicorn main:app --reload   # 启动服务
uv run pytest tests/ -q            # 运行测试（-s 可查看输入样本与判定依据日志）
```

无外部服务、无真实凭据：存储、码表、保密库、授权令牌全部为进程内本地模拟。

## 支持的标准范围

| 项目 | 范围 |
| --- | --- |
| 标准版本 | `fhir-stu3`（STU3/3.x）、`fhir-r4`、`fhir-r5`（信封字段 `standard_version` 必填） |
| 资源类型 | `Patient`、`Observation`、`Condition`（诊断结论，`subject` 必须指向患者） |
| 资源格式 | JSON 信封：`{standard_version, resourceType, id, ...}` |
| 代码系统 | 本地码表：`http://loinc.org|2.74`（子集）、`http://local.example/clinical|1.0`、`http://hl7.org/fhir/sid/icd-10|2016`（ICD-10 子集，Condition 使用） |

### 版本归一规则（`interop/parsing.py`）

- Observation 生效时间：STU3/R4 `effectiveDateTime` 与 R5 `effectivePeriod`
  统一归一为 `payload.effective = {type, start, end}`。
- Condition 状态：STU3/R4/R5 的 `clinicalStatus` / `verificationStatus`
  （均为 CodeableConcept）统一归一为 `payload.clinical_status` /
  `payload.verification_status = {system, code, display}`；`code` 归一结构相同，
  `onsetDateTime` 归一为 `payload.onset`。三个版本归一后的内部字段完全一致，
  下游不会因为版本不同看到两套字段。
- 各版本状态取值集**严格不同**：如 Observation `status` 的 `cancelled` 只在
  STU3 取值集中、`corrected` 只在 R5 中允许；Condition `clinicalStatus` 的
  `relapse` 不允许出现在 STU3、`unknown` 只允许出现在 R5。

### 严格字段判定（`interop/parsing.py`，三个版本一视同仁）

解析时对信封与资源体逐字段做白名单判定，两类问题分别给出**可区分**的拒绝原因：

| 情形 | reason | 含义 |
| --- | --- | --- |
| 字段存在于其它版本、但在声明版本中不存在（如 R4/STU3 携带 `effectivePeriod`、R5 携带 `effectiveDateTime`） | `version_field_mismatch` | 版本标错或字段放错版本，detail 指出该字段属于哪些版本 |
| 字段在任何已定义版本模型中都不存在 | `unknown_field` | 字段名未定义，接入方需删改而非改版本声明 |

既不悄悄丢弃、也不给默认值；必填缺失仍是 `missing_required_field`，状态取值/类型
非法仍是 `invalid_field_type`，编码结构非法仍是 `illegal_coding`，标准不因新接入放宽。

### 引用兼容矩阵（`interop/references.py`）

版本序 **STU3 < R4 < R5**，规则与此前一致：高版本引用低版本放行，低版本引用
高版本拒绝 `reference_version_mismatch`。

| 源资源版本 | 引用目标版本 | 结论 |
| --- | --- | --- |
| 任意版本 | 同版本 | 允许 |
| R5 | STU3 / R4；R4 | STU3 | 允许（新版本向后兼容读旧版本） |
| STU3 | R4 / R5；R4 | R5 | 拒绝 `reference_version_mismatch` |

引用目标不存在一律拒绝 `broken_reference`，且资源不会入库（无残留）。

## 拒绝回执与修正重放（`interop/receipts.py`、`interop/service.py`）

任何一次被拒的写入（单条、更新、批量中的每一条）都会登记一条拒绝回执，错误响应
与批量 failed 条目中带 `receipt_id`。回执包含：资源类型/id、声明版本、**批量下标**
（`batch_index`，单条为 `null`）、拒绝原因、**具体字段定位**（`field`）、
**判定依据**（`basis`，规则说明）、以及**去标识化后的输入快照**（`input_sample`）。

- **可复现**：回执 id 由「批量下标 + 去标识化快照 + 原因 + 字段」的 SHA-256 指纹
  确定性生成。同一份输入反复提交拿到的是同一条回执，结论不会漂移、不会堆叠新回执。
- **逐条留痕**：批量部分成功时成功项照常入库；每个失败项都有自己的 receipt_id，
  可按 id 单独查询。被拒数据绝不入库，也不污染同资源已有版本。
- **不泄敏**：快照中的 `name/birthDate/telecom` 与保密库同口径，值替换为
  `REDACTED:<sha256前12位>`，回执中不出现敏感原文。
- **修正重放**：`POST /rejections/{id}/replay` 提交修正后的记录，走与首次完全
  相同的 解析→术语→引用→存储 校验链路，不因为是重放放水。
  - 成功：回执 `open → resolved`，记录入库键与版本；待处理列表可用
    `?status=open|resolved` 区分。
  - 仍失败：原回执保持 `open`，本次新拒绝**另留一条新回执**。
  - 已解决回执再重放：拒绝 `receipt_already_resolved`（409），不重复入库、版本不前进。
  - 并发重放同一回执：锁内 compare-and-set，恰好一个调用方真正生效；其余拿到
    `receipt_in_progress`（409，被他人领取中），或在领先者已完成后拿到
    `receipt_already_resolved`——都是可区分结果，无半更新、重复写入或版本错乱。

## 错误分类（拒绝原因可区分）

所有失败抛出 `InteropError(reason, detail, field[, receipt_id])`，HTTP 层映射为
不同状态码，响应体为 `{"error": {"reason", "detail", "field", "receipt_id"?}}`：

| reason | HTTP | 含义 |
| --- | --- | --- |
| `malformed_json` | 400 | 输入不是合法 JSON / 顶层不是对象 |
| `unsupported_standard_version` | 400 | 标准版本不在支持范围（仅 stu3/r4/r5） |
| `unsupported_resource_type` | 400 | 资源类型不在支持范围 |
| `missing_required_field` | 422 | 必填字段缺失，不接受默认值 |
| `invalid_field_type` | 422 | 字段类型/取值/格式非法（含版本取值集） |
| `illegal_coding` | 422 | 编码结构非法（缺 system/code、coding 为空） |
| `version_field_mismatch` | 422 | 字段属于其它标准版本，声明版本中不存在 |
| `unknown_field` | 422 | 字段在任何版本模型中都未定义 |
| `unknown_code` | 422 | 本地码表无此代码或无映射 |
| `mapping_conflict` | 409 | 一对多映射冲突，需人工裁决 |
| `code_system_version_incompatible` | 409 | 码表版本与本地不一致 |
| `broken_reference` | 422 | 引用目标不存在 |
| `reference_version_mismatch` | 409 | 低版本资源引用高版本资源 |
| `version_conflict` | 409 | 乐观锁冲突（并发修改） |
| `duplicate_write` | 409 | 无匹配幂等键的重复创建 |
| `resource_not_found` | 404 | 资源不存在 |
| `receipt_not_found` | 404 | 回执不存在 |
| `receipt_already_resolved` | 409 | 回执已解决，重放不得重复入库 |
| `receipt_in_progress` | 409 | 回执正被另一调用方重放 |
| `invalid_token` | 401 | 还原令牌无效/已撤销 |
| `unauthorized_reidentification` | 403 | 角色不足或令牌授权范围不符 |

## 一致性与安全规则

- **并发写入**（`interop/store.py`）：单把可重入锁内完成"读版本-校验-写入"原子序列；
  更新必须携带 `expected_version`，冲突方收到 `version_conflict`。并发创建同一资源
  恰好一个成功，其余 `duplicate_write`。
- **幂等**：每个资源记录已应用的 `idempotency_key`，重放直接返回原记录，版本不前进。
- **批量写入**：`POST /resources/batch` 逐条走完整链路独立提交，返回
  `{succeeded: [{index, key, version}], failed: [{index, reason, detail, field, receipt_id}]}`；
  单条失败不回滚、不污染其它记录。
- **去标识化**（`interop/deidentify.py`）：Patient 的 `name/birthDate/telecom`
  默认以 `REDACTED:<sha256前12位>` 占位返回，原值只存于进程内保密库；还原需
  `clinical` 角色令牌，令牌按资源 scoped、可撤销。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/resources?idempotency_key=` | 新建资源（拒绝返回带 `receipt_id` 的错误） |
| PUT | `/resources/{type}/{id}?expected_version=` | 乐观锁更新 |
| POST | `/resources/batch` | 批量写入（部分失败，逐条回执） |
| GET | `/resources/{type}/{id}?token=` | 读取（默认去标识化视图） |
| GET | `/rejections?status=open\|resolved` | 回执列表（按状态过滤） |
| GET | `/rejections/{receipt_id}` | 查询单条回执 |
| POST | `/rejections/{receipt_id}/replay?idempotency_key=` | 按回执修正重放 |
| POST | `/tokens/{type}/{id}?role=` | 签发还原令牌 |
| POST | `/terminology/validate` | 代码校验 |
| POST | `/terminology/map?target_system=` | 代码映射 |

## 本地验证（命令与用例对应规则）

```bash
uv run pytest tests/ -q                      # 全部 74 个用例
uv run pytest tests/ -s -k versions          # STU3 接入/归一/严格字段/引用方向
uv run pytest tests/ -s -k receipts          # 回执可查可复现/逐条留痕/不泄敏/重放幂等/并发
uv run pytest tests/ -s -k concurrency       # 乐观锁与并发创建
```

- `tests/test_versions.py`：STU3 Observation/Condition 接入与三版本归一形态一致；
  跨版本字段（`version_field_mismatch`）与未知字段（`unknown_field`）各自被拒且原因
  可区分（老版本同样严格）；Condition 必填缺失/状态取值集/编码结构/subject 指向
  非 Patient 的拒绝；引用方向 STU3<R4<R5。
- `tests/test_receipts.py`：回执可查且字段/依据完整；同一输入反复提交同一条回执；
  批量失败逐条带独立 receipt_id 且 `batch_index` 正确；回执快照不含姓名/生日/电话
  原文；修正重放走完整校验、成功置 resolved；resolved 再重放得
  `receipt_already_resolved` 不重复入库；8 线程并发重放恰好一个生效，其余为
  `in_progress`/`already_resolved`；被拒数据不进主存储。
- `tests/test_parsing.py`：畸形/缺失/版本错位输入的原因分类（含 R5 携带
  `effectiveDateTime` 归为 `version_field_mismatch`）。
- `tests/test_batch.py`：部分失败可区分可复现、失败无残留、整批幂等重放。
- `tests/test_references.py`：断裂引用、低→高版本错配、失败后无残留。
- `tests/test_concurrency.py`：16 线程并发更新恰好 1 胜、8 线程并发创建恰好 1 胜、
  幂等重放不重复写入。
- `tests/test_terminology.py` / `tests/test_deidentify.py` / `tests/test_api.py`：
  码表与映射冲突、去标识化授权、HTTP 端到端错误分类。

每个用例通过 `log_case` 在日志中打印**输入样本**与**判定依据**（`-s` 可见）。
