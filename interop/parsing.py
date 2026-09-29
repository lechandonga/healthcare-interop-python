"""多标准版本输入的解析与归一（STU3 / R4 / R5；Patient / Observation / Condition）。

设计要点：
- 输入为 JSON 信封：{"standard_version": ..., "resourceType": ..., "id": ..., ...}
- 版本相关的字段差异在此集中处理，归一为与版本解耦的内部形态：
  * Observation.effective：STU3/R4 用 effectiveDateTime，R5 用 effectivePeriod，
    统一归一为 payload["effective"] = {"type", "start", "end"}。
  * Condition.onset：STU3 用 onsetDateTime/onsetPeriod 的字符串/对象结构差异同样归一。
- **字段严格性**（三个版本一视同仁）：
  * 出现在「别的版本才允许」字段表里的字段 -> FIELD_NOT_DEFINED_FOR_VERSION；
  * 三个版本都未定义的字段 -> UNKNOWN_FIELD。两者原因可区分，绝不静默忽略或补默认值。
  * 校验顺序：未知字段 -> 必填字段 -> 版本错位字段 -> 取值/类型，
    保证同一份畸形输入反复提交结论一致。
- 任何字段缺失 / 编码非法 / 结构不完整都抛出带 RejectReason 的 InteropError，绝不静默跳过。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import InteropError, RejectReason
from .models import Coding, NormalizedResource, ResourceType, StandardVersion

_GENDER_VALUES = {"male", "female", "other", "unknown"}
_OBS_STATUS = {
    StandardVersion.FHIR_STU3: {"registered", "preliminary", "final", "amended",
                                "cancelled", "entered-in-error", "unknown"},
    StandardVersion.FHIR_R4: {"registered", "preliminary", "final", "amended"},
    # R5 在本服务支持子集中新增 corrected / cancelled
    StandardVersion.FHIR_R5: {"registered", "preliminary", "final", "amended", "corrected", "cancelled"},
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_REF_RE = re.compile(r"^(Patient|Observation|Condition)/[A-Za-z0-9_-]+$")

# 敏感字段：进入存储前会被抽取，用于去标识化
SENSITIVE_PATIENT_FIELDS = ("name", "telecom", "birthDate")

# ---- 信封字段（所有版本/资源通用） ----
_ENVELOPE_FIELDS = {"standard_version", "resourceType", "id", "idempotency_key"}

# ---- 每个 (资源, 版本) 允许的字段白名单 ----
# 只列出本服务识别的字段；白名单外的字段按下面两张表分流为「版本错位」或「未知」。
_ALLOWED_FIELDS: dict[tuple[ResourceType, StandardVersion], set[str]] = {
    (ResourceType.PATIENT, StandardVersion.FHIR_STU3):
        {"name", "gender", "birthDate", "telecom", "active", "address"},
    (ResourceType.PATIENT, StandardVersion.FHIR_R4):
        {"name", "gender", "birthDate", "telecom", "active", "address",
         "maritalStatus", "multipleBirthBoolean"},
    (ResourceType.PATIENT, StandardVersion.FHIR_R5):
        {"name", "gender", "birthDate", "telecom", "active", "address",
         "maritalStatus", "multipleBirthBoolean", "contact"},
    (ResourceType.OBSERVATION, StandardVersion.FHIR_STU3):
        {"status", "code", "subject", "effectiveDateTime", "valueQuantity",
         "category", "comment"},
    (ResourceType.OBSERVATION, StandardVersion.FHIR_R4):
        {"status", "code", "subject", "effectiveDateTime", "valueQuantity", "category"},
    (ResourceType.OBSERVATION, StandardVersion.FHIR_R5):
        {"status", "code", "subject", "effectivePeriod", "valueQuantity", "category",
         "instantiates"},
    (ResourceType.CONDITION, StandardVersion.FHIR_STU3):
        {"clinicalStatus", "verificationStatus", "code", "subject",
         "onsetDateTime", "abatementDateTime", "asserter", "category"},
    (ResourceType.CONDITION, StandardVersion.FHIR_R4):
        {"clinicalStatus", "verificationStatus", "code", "subject",
         "onsetDateTime", "onsetPeriod", "abatementDateTime", "abatementPeriod",
         "recordedDate", "asserter", "category"},
    (ResourceType.CONDITION, StandardVersion.FHIR_R5):
        {"clinicalStatus", "verificationStatus", "code", "subject",
         "onsetDateTime", "onsetPeriod", "abatementDateTime",
         "abatementPeriod", "recordedDate", "asserter", "category", "participant"},
}

# 版本错位示例：字段在其它版本的同一资源上有定义，但在声明版本上没有。
def _known_on_other_version(field: str, rtype: ResourceType,
                            version: StandardVersion) -> StandardVersion | None:
    for other in StandardVersion:
        if other is version:
            continue
        if field in _ALLOWED_FIELDS.get((rtype, other), set()):
            return other
    return None


def _check_field_strictness(body: dict, rtype: ResourceType, version: StandardVersion,
                            ctx: str, *, required: set[str]) -> None:
    """字段严格性校验：
    1) 服务任何版本都未定义的字段 -> UNKNOWN_FIELD；
    2) 必填字段缺失 -> MISSING_REQUIRED_FIELD；
    3) 别的版本才有、声明版本不该出现的字段 -> FIELD_NOT_DEFINED_FOR_VERSION。
    未知字段优先判定，保证「未知 + 缺必填」的输入结论稳定可复现。
    """
    allowed = _ALLOWED_FIELDS[(rtype, version)]
    present = {f for f in body if f not in _ENVELOPE_FIELDS}

    for field in sorted(present - allowed):
        other = _known_on_other_version(field, rtype, version)
        if other is not None:
            _fail(RejectReason.FIELD_NOT_DEFINED_FOR_VERSION,
                  f"{ctx}: 字段 {field!r} 不属于 {version.value} 的 {rtype.value}，"
                  f"该字段定义在 {other.value} 中；请按声明版本提交",
                  field, basis=f"FHIR {version.value} {rtype.value} 字段集不含 {field!r}，"
                               f"该字段属于 {other.value}")
        _fail(RejectReason.UNKNOWN_FIELD,
              f"{ctx}: 字段 {field!r} 在服务支持的任何版本中均未定义，"
              f"不得携带未声明字段（不做静默忽略或默认值处理）",
              field, basis=f"服务字段白名单（{rtype.value} 全版本）不含 {field!r}")

    for field in sorted(required - present):
        _fail(RejectReason.MISSING_REQUIRED_FIELD,
              f"{ctx} 缺少必填字段 {field!r}", field)


def _fail(reason: RejectReason, detail: str, field: str | None = None,
          *, basis: str | None = None) -> None:
    raise InteropError(reason, detail, field=field, basis=basis)


def _require(obj: dict, field: str, ctx: str) -> Any:
    if field not in obj or obj[field] is None:
        _fail(RejectReason.MISSING_REQUIRED_FIELD, f"{ctx} 缺少必填字段 {field!r}", field)
    return obj[field]


def _require_type(obj: dict, field: str, types: tuple, ctx: str) -> Any:
    value = _require(obj, field, ctx)
    if not isinstance(value, types) or isinstance(value, bool) and bool not in types:
        _fail(RejectReason.INVALID_FIELD_TYPE,
              f"{ctx} 字段 {field!r} 类型非法: 期望 {types}, 实际 {type(value).__name__}", field)
    return value


def _parse_coding(raw: Any, ctx: str) -> Coding:
    """解析 coding 结构；缺 system/code 视为编码非法。"""
    if not isinstance(raw, dict):
        _fail(RejectReason.ILLEGAL_CODING, f"{ctx}: coding 必须是对象", "code")
    system = raw.get("system")
    code = raw.get("code")
    if not isinstance(system, str) or not system:
        _fail(RejectReason.ILLEGAL_CODING, f"{ctx}: coding.system 缺失或为空", "code.system")
    if not isinstance(code, str) or not code:
        _fail(RejectReason.ILLEGAL_CODING, f"{ctx}: coding.code 缺失或为空", "code.code")
    display = raw.get("display")
    if display is not None and not isinstance(display, str):
        _fail(RejectReason.ILLEGAL_CODING, f"{ctx}: coding.display 必须是字符串", "code.display")
    return Coding(system=system, code=code, display=display)


def _parse_codeable(raw: Any, ctx: str) -> Coding:
    if not isinstance(raw, dict) or not isinstance(raw.get("coding"), list) or not raw["coding"]:
        _fail(RejectReason.ILLEGAL_CODING, f"{ctx}: code.coding 必须是非空数组", "code")
    return _parse_coding(raw["coding"][0], ctx)


def _parse_reference(raw: Any, ctx: str) -> str:
    if not isinstance(raw, dict) or not isinstance(raw.get("reference"), str):
        _fail(RejectReason.MISSING_REQUIRED_FIELD, f"{ctx}: 引用必须是 {{'reference': 'Type/id'}}", "reference")
    ref = raw["reference"]
    if not _REF_RE.match(ref):
        _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: 引用格式非法: {ref!r}", "reference")
    return ref


def _normalize_patient(body: dict, version: StandardVersion, rid: str) -> NormalizedResource:
    ctx = f"Patient/{rid}"
    _check_field_strictness(
        body, ResourceType.PATIENT, version, ctx,
        required={"name", "gender", "birthDate"})
    name = _require_type(body, "name", (list,), ctx)
    if not name or not all(isinstance(n, dict) and n.get("family") for n in name):
        _fail(RejectReason.MISSING_REQUIRED_FIELD, f"{ctx}: name 必须是非空数组且每项含 family", "name")
    gender = _require_type(body, "gender", (str,), ctx)
    if gender not in _GENDER_VALUES:
        _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: gender 取值非法: {gender!r}", "gender")
    birth = _require_type(body, "birthDate", (str,), ctx)
    if not _DATE_RE.match(birth):
        _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: birthDate 须为 YYYY-MM-DD: {birth!r}", "birthDate")
    telecom = body.get("telecom", [])
    if not isinstance(telecom, list):
        _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: telecom 必须是数组", "telecom")
    active = body.get("active")
    if active is not None and not isinstance(active, bool):
        _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: active 必须是布尔值", "active")

    sensitive = {"name": name, "birthDate": birth, "telecom": telecom}
    payload = {"gender": gender, "active": active}
    return NormalizedResource(
        resource_type=ResourceType.PATIENT, resource_id=rid, standard_version=version,
        payload=payload, references=[], sensitive_fields=sensitive,
    )


def _normalize_observation(body: dict, version: StandardVersion, rid: str) -> NormalizedResource:
    ctx = f"Observation/{rid}"
    if version is StandardVersion.FHIR_R5:
        effective_field = "effectivePeriod"
    else:
        effective_field = "effectiveDateTime"
    _check_field_strictness(
        body, ResourceType.OBSERVATION, version, ctx,
        required={"status", "code", "subject", effective_field})
    status = _require_type(body, "status", (str,), ctx)
    allowed = _OBS_STATUS[version]
    if status not in allowed:
        _fail(RejectReason.INVALID_FIELD_TYPE,
              f"{ctx}: status {status!r} 在 {version.value} 中不允许（允许: {sorted(allowed)}）", "status")
    code = _parse_codeable(_require(body, "code", ctx), ctx)
    subject_ref = _parse_reference(_require(body, "subject", ctx), ctx)

    # 版本差异：STU3/R4 用 effectiveDateTime，R5 用 effectivePeriod
    if version in (StandardVersion.FHIR_STU3, StandardVersion.FHIR_R4):
        eff = _require_type(body, "effectiveDateTime", (str,), ctx)
        if not _DATETIME_RE.match(eff):
            _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: effectiveDateTime 格式非法: {eff!r}",
                  "effectiveDateTime")
        effective = {"type": "dateTime", "start": eff, "end": eff}
    else:
        period = _require_type(body, "effectivePeriod", (dict,), ctx)
        start = _require_type(period, "start", (str,), f"{ctx}.effectivePeriod")
        end = _require_type(period, "end", (str,), f"{ctx}.effectivePeriod")
        if not (_DATETIME_RE.match(start) and _DATETIME_RE.match(end)):
            _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: effectivePeriod start/end 格式非法",
                  "effectivePeriod")
        effective = {"type": "Period", "start": start, "end": end}

    value = body.get("valueQuantity")
    if value is not None:
        if not isinstance(value, dict) or not isinstance(value.get("value"), (int, float)):
            _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: valueQuantity.value 必须是数值", "valueQuantity")
        value = {"value": value["value"], "unit": value.get("unit"), "code": value.get("code")}

    payload = {
        "status": status,
        "code": {"system": code.system, "code": code.code, "display": code.display},
        "effective": effective,
        "valueQuantity": value,
    }
    return NormalizedResource(
        resource_type=ResourceType.OBSERVATION, resource_id=rid, standard_version=version,
        payload=payload, references=[subject_ref], sensitive_fields={},
    )


_COND_CLINICAL_R4_R5 = {"active", "recurrence", "inactive", "remission", "resolved"}
_COND_CLINICAL_STU3 = {"active", "recurrence", "inactive", "remission", "resolved"}
_COND_VERIFICATION = {"provisional", "differential", "confirmed", "refuted",
                      "entered-in-error", "unconfirmed"}


def _parse_condition_status(raw: Any, version: StandardVersion, ctx: str, field: str) -> str:
    """clinicalStatus：STU3 是 code 字符串；R4/R5 是 CodeableConcept。"""
    if version is StandardVersion.FHIR_STU3:
        if not isinstance(raw, str):
            _fail(RejectReason.INVALID_FIELD_TYPE,
                  f"{ctx}: {field} 在 stu3 中必须是 code 字符串", field)
        if raw not in _COND_CLINICAL_STU3:
            _fail(RejectReason.INVALID_FIELD_TYPE,
                  f"{ctx}: {field} 取值非法: {raw!r}", field)
        return raw
    if not isinstance(raw, dict):
        _fail(RejectReason.INVALID_FIELD_TYPE,
              f"{ctx}: {field} 在 {version.value} 中必须是 CodeableConcept 对象", field)
    coding = _parse_codeable(raw, f"{ctx}.{field}")
    # R4/R5 约定码表 http://terminology.hl7.org/CodeSystem/condition-clinical
    if coding.code not in _COND_CLINICAL_R4_R5:
        _fail(RejectReason.INVALID_FIELD_TYPE,
              f"{ctx}: {field} 取值非法: {coding.code!r}（允许: {sorted(_COND_CLINICAL_R4_R5)}）",
              f"{field}.coding.code")
    return coding.code


def _parse_onset(body: dict, version: StandardVersion, ctx: str) -> dict | None:
    """onset* 归一为 {"type": "dateTime"|"Period", "start", "end"}；未提供返回 None。"""
    dt = body.get("onsetDateTime")
    period = body.get("onsetPeriod")
    if dt is not None:
        if not isinstance(dt, str) or not _DATETIME_RE.match(dt):
            _fail(RejectReason.INVALID_FIELD_TYPE,
                  f"{ctx}: onsetDateTime 格式非法: {dt!r}", "onsetDateTime")
        if period is not None:
            _fail(RejectReason.INVALID_FIELD_TYPE,
                  f"{ctx}: onsetDateTime 与 onsetPeriod 不得同时出现", "onsetDateTime")
        return {"type": "dateTime", "start": dt, "end": dt}
    if period is not None:
        if version is StandardVersion.FHIR_STU3:
            _fail(RejectReason.FIELD_NOT_DEFINED_FOR_VERSION,
                  f"{ctx}: onsetPeriod 不属于 fhir-stu3 的 Condition，STU3 请使用 onsetDateTime",
                  "onsetPeriod",
                  basis="FHIR STU3 Condition 时间字段为 onsetDateTime；onsetPeriod 始于 R4")
        if not isinstance(period, dict):
            _fail(RejectReason.INVALID_FIELD_TYPE, f"{ctx}: onsetPeriod 必须是对象",
                  "onsetPeriod")
        start = _require(period, "start", f"{ctx}.onsetPeriod")
        end = _require(period, "end", f"{ctx}.onsetPeriod")
        if not (_DATETIME_RE.match(start) and _DATETIME_RE.match(end)):
            _fail(RejectReason.INVALID_FIELD_TYPE,
                  f"{ctx}: onsetPeriod start/end 格式非法", "onsetPeriod")
        return {"type": "Period", "start": start, "end": end}
    return None


def _normalize_condition(body: dict, version: StandardVersion, rid: str) -> NormalizedResource:
    ctx = f"Condition/{rid}"
    _check_field_strictness(
        body, ResourceType.CONDITION, version, ctx,
        required={"clinicalStatus", "code", "subject"})
    clinical = _parse_condition_status(
        _require(body, "clinicalStatus", ctx), version, ctx, "clinicalStatus")
    verification_raw = body.get("verificationStatus")
    verification = None
    if verification_raw is not None:
        if version is StandardVersion.FHIR_STU3:
            if not isinstance(verification_raw, str):
                _fail(RejectReason.INVALID_FIELD_TYPE,
                      f"{ctx}: verificationStatus 在 stu3 中必须是 code 字符串",
                      "verificationStatus")
            verification = verification_raw
        else:
            verification = _parse_codeable(verification_raw, f"{ctx}.verificationStatus").code
        if verification not in _COND_VERIFICATION:
            _fail(RejectReason.INVALID_FIELD_TYPE,
                  f"{ctx}: verificationStatus 取值非法: {verification!r}",
                  "verificationStatus")
    code = _parse_codeable(_require(body, "code", ctx), ctx)
    subject_ref = _parse_reference(_require(body, "subject", ctx), ctx)
    if subject_ref.split("/", 1)[0] != "Patient":
        _fail(RejectReason.INVALID_FIELD_TYPE,
              f"{ctx}: Condition.subject 必须指向 Patient，实际 {subject_ref!r}", "subject")

    recorded = body.get("recordedDate")
    if recorded is not None:
        if version is StandardVersion.FHIR_STU3:
            _fail(RejectReason.FIELD_NOT_DEFINED_FOR_VERSION,
                  f"{ctx}: recordedDate 不属于 fhir-stu3 的 Condition（R4 起引入）",
                  "recordedDate",
                  basis="FHIR Condition.recordedDate 自 R4 引入，STU3 无此字段")
        if not isinstance(recorded, str) or not (_DATE_RE.match(recorded)
                                                 or _DATETIME_RE.match(recorded)):
            _fail(RejectReason.INVALID_FIELD_TYPE,
                  f"{ctx}: recordedDate 格式非法: {recorded!r}", "recordedDate")

    onset = _parse_onset(body, version, ctx)

    payload = {
        "clinicalStatus": clinical,
        "verificationStatus": verification,
        "code": {"system": code.system, "code": code.code, "display": code.display},
        "onset": onset,
        "recordedDate": recorded,
    }
    return NormalizedResource(
        resource_type=ResourceType.CONDITION, resource_id=rid, standard_version=version,
        payload=payload, references=[subject_ref], sensitive_fields={},
    )


def parse_resource(raw: str | bytes | dict) -> NormalizedResource:
    """解析原始输入为归一化资源。任何失败都抛出带分类原因的 InteropError。"""
    if isinstance(raw, (str, bytes)):
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            _fail(RejectReason.MALFORMED_JSON, f"输入不是合法 JSON: {e}")
    elif isinstance(raw, dict):
        body = raw
    else:
        _fail(RejectReason.MALFORMED_JSON, f"输入类型不支持: {type(raw).__name__}")

    if not isinstance(body, dict):
        _fail(RejectReason.MALFORMED_JSON, "顶层结构必须是 JSON 对象")

    version = StandardVersion.parse(_require_type(body, "standard_version", (str,), "信封"))
    rtype = ResourceType.parse(_require_type(body, "resourceType", (str,), "信封"))
    rid = _require_type(body, "id", (str,), "信封")
    if not re.match(r"^[A-Za-z0-9_-]+$", rid):
        _fail(RejectReason.INVALID_FIELD_TYPE, f"资源 id 含非法字符: {rid!r}", "id")

    if rtype is ResourceType.PATIENT:
        return _normalize_patient(body, version, rid)
    if rtype is ResourceType.OBSERVATION:
        return _normalize_observation(body, version, rid)
    return _normalize_condition(body, version, rid)
