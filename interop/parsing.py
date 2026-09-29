"""多标准版本输入的解析与归一（STU3 / R4 / R5）。

设计要点：
- 输入为 JSON 信封：{"standard_version": "fhir-stu3"|"fhir-r4"|"fhir-r5", ...}
- 版本相关的字段差异在此集中处理，全部归一为与版本无关的 payload：
  * Observation 生效时间：STU3/R4 effectiveDateTime、R5 effectivePeriod
    统一归一为 payload["effective"] = {"type", "start", "end"}
  * Condition 临床状态：STU3 clinicalStatus(CodeableConcept)、R4/R5
    clinicalStatus(CodeableConcept，R5 取值集不同) 归一为 payload["clinical_status"]
    = {"system", "code", "display"}；验证状态（verificationStatus）同样归一
- 严格字段判定（不静默忽略、不默认值）：
  * 顶层或资源内出现"其它版本才有"的字段 -> VERSION_FIELD_MISMATCH
  * 出现任何版本模型都没定义过的字段     -> UNKNOWN_FIELD
    两类原因可区分，接入方一眼知道该改版本声明还是改字段名。
- 任何字段缺失 / 编码非法 / 结构不完整同样显式拒绝，标准不放宽。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import InteropError, RejectReason
from .models import Coding, NormalizedResource, ResourceType, StandardVersion

V = StandardVersion
_GENDER_VALUES = {"male", "female", "other", "unknown"}
_OBS_STATUS = {
    StandardVersion.FHIR_STU3: {"registered", "preliminary", "final", "amended",
                                "cancelled", "entered-in-error", "unknown"},
    StandardVersion.FHIR_R4: {"registered", "preliminary", "final", "amended"},
    # R5 在本服务支持子集中新增 corrected / cancelled
    StandardVersion.FHIR_R5: {"registered", "preliminary", "final", "amended",
                              "corrected", "cancelled"},
}
# Condition.clinicalStatus：STU3 用 CodeableConcept 且取值集为 active/recurrence/inactive/...
# R4 取值 active/recurrence/relapse/inactive/remission/resolved；R5 新增 unknown 等
_COND_CLINICAL_STATUS = {
    StandardVersion.FHIR_STU3: {"active", "recurrence", "inactive", "remission", "resolved"},
    StandardVersion.FHIR_R4: {"active", "recurrence", "relapse", "inactive",
                              "remission", "resolved"},
    StandardVersion.FHIR_R5: {"active", "recurrence", "relapse", "inactive",
                              "remission", "resolved", "unknown"},
}
_COND_VERIFICATION_STATUS = {
    StandardVersion.FHIR_STU3: {"provisional", "differential", "confirmed", "refuted",
                                "entered-in-error", "unknown"},
    StandardVersion.FHIR_R4: {"unconfirmed", "provisional", "differential",
                              "confirmed", "refuted", "entered-in-error"},
    StandardVersion.FHIR_R5: {"unconfirmed", "provisional", "differential",
                              "confirmed", "refuted", "entered-in-error"},
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_REF_RE = re.compile(r"^(Patient|Observation|Condition)/[A-Za-z0-9_-]+$")

# 敏感字段：进入存储前会被抽取，用于去标识化
SENSITIVE_PATIENT_FIELDS = ("name", "telecom", "birthDate")

# 信封层允许字段（与版本无关）
_ENVELOPE_FIELDS = {"standard_version", "resourceType", "id", "idempotency_key"}

# 每个 (资源类型, 字段) 允许出现的版本集合。同名字段在不同版本语义一致时共用一个条目。
_PATIENT_FIELDS = {
    "name": frozenset(V), "gender": frozenset(V),
    "birthDate": frozenset(V), "telecom": frozenset(V),
}
_OBSERVATION_FIELDS = {
    "status": frozenset(V), "code": frozenset(V), "subject": frozenset(V),
    "effectiveDateTime": frozenset({V.FHIR_STU3, V.FHIR_R4}),
    "effectivePeriod": frozenset({V.FHIR_R5}),
    "valueQuantity": frozenset(V),
}
_CONDITION_FIELDS = {
    "clinicalStatus": frozenset(V), "verificationStatus": frozenset(V),
    "code": frozenset(V), "subject": frozenset(V), "onsetDateTime": frozenset(V),
}
_RESOURCE_FIELDS: dict[ResourceType, dict[str, frozenset[StandardVersion]]] = {
    ResourceType.PATIENT: _PATIENT_FIELDS,
    ResourceType.OBSERVATION: _OBSERVATION_FIELDS,
    ResourceType.CONDITION: _CONDITION_FIELDS,
}

def _fail(reason: RejectReason, detail: str, field: str | None = None) -> None:
    raise InteropError(reason, detail, field=field)


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


def _check_fields(body: dict, rtype: ResourceType, version: StandardVersion) -> None:
    """严格字段判定：版本错位字段与未知字段分别以可区分原因拒绝。"""
    schema = _RESOURCE_FIELDS[rtype]
    for name in body:
        if name in _ENVELOPE_FIELDS:
            continue
        allowed_versions = schema.get(name)
        if allowed_versions is None:
            _fail(RejectReason.UNKNOWN_FIELD,
                  f"字段 {name!r} 在本服务定义的任何版本 {rtype.value} 模型中都不存在，"
                  f"请核对字段名（声明版本 {version.value}）", name)
        if version not in allowed_versions:
            others = sorted(v.value for v in allowed_versions)
            _fail(RejectReason.VERSION_FIELD_MISMATCH,
                  f"字段 {name!r} 不属于 {version.value} 的 {rtype.value}，"
                  f"它只出现在 {others}；请按声明版本送数或更正版本声明", name)


def _normalize_patient(body: dict, version: StandardVersion, rid: str) -> NormalizedResource:
    ctx = f"Patient/{rid}"
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

    sensitive = {"name": name, "birthDate": birth, "telecom": telecom}
    payload = {"gender": gender}
    return NormalizedResource(
        resource_type=ResourceType.PATIENT, resource_id=rid, standard_version=version,
        payload=payload, references=[], sensitive_fields=sensitive,
    )


def _normalize_observation(body: dict, version: StandardVersion, rid: str) -> NormalizedResource:
    ctx = f"Observation/{rid}"
    status = _require_type(body, "status", (str,), ctx)
    allowed = _OBS_STATUS[version]
    if status not in allowed:
        _fail(RejectReason.INVALID_FIELD_TYPE,
              f"{ctx}: status {status!r} 在 {version.value} 中不允许（允许: {sorted(allowed)}）", "status")
    code = _parse_codeable(_require(body, "code", ctx), ctx)
    subject_ref = _parse_reference(_require(body, "subject", ctx), ctx)

    # 版本差异：STU3/R4 用 effectiveDateTime，R5 用 effectivePeriod；归一为同构 effective
    if version is not StandardVersion.FHIR_R5:
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


def _normalize_status_codeable(raw: Any, ctx: str, field: str,
                               allowed: set[str], version: StandardVersion) -> dict:
    """解析 Condition 的状态 CodeableConcept：结构合法且代码在该版本取值集内。"""
    coding = _parse_codeable(raw, f"{ctx}.{field}")
    if coding.code not in allowed:
        _fail(RejectReason.INVALID_FIELD_TYPE,
              f"{ctx}: {field} 取值 {coding.code!r} 在 {version.value} 中不允许"
              f"（允许: {sorted(allowed)}）", field)
    return {"system": coding.system, "code": coding.code, "display": coding.display}


def _normalize_condition(body: dict, version: StandardVersion, rid: str) -> NormalizedResource:
    ctx = f"Condition/{rid}"
    # STU3/R4/R5 的状态都是 CodeableConcept，但取值集按版本严格校验
    clinical = _normalize_status_codeable(
        _require(body, "clinicalStatus", ctx), ctx, "clinicalStatus",
        _COND_CLINICAL_STATUS[version], version)
    verification = None
    if body.get("verificationStatus") is not None:
        verification = _normalize_status_codeable(
            body["verificationStatus"], ctx, "verificationStatus",
            _COND_VERIFICATION_STATUS[version], version)
    code = _parse_codeable(_require(body, "code", ctx), ctx)
    subject_ref = _parse_reference(_require(body, "subject", ctx), ctx)
    if not subject_ref.startswith("Patient/"):
        _fail(RejectReason.INVALID_FIELD_TYPE,
              f"{ctx}: subject 必须指向 Patient，实际 {subject_ref!r}", "subject")
    onset = body.get("onsetDateTime")
    if onset is not None:
        if not isinstance(onset, str) or not _DATETIME_RE.match(onset):
            _fail(RejectReason.INVALID_FIELD_TYPE,
                  f"{ctx}: onsetDateTime 格式非法: {onset!r}", "onsetDateTime")

    # 归一形态与版本无关：下游看到的字段三个版本完全一致
    payload = {
        "clinical_status": clinical,
        "verification_status": verification,
        "code": {"system": code.system, "code": code.code, "display": code.display},
        "onset": onset,
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

    _check_fields(body, rtype, version)
    if rtype is ResourceType.PATIENT:
        return _normalize_patient(body, version, rid)
    if rtype is ResourceType.OBSERVATION:
        return _normalize_observation(body, version, rid)
    return _normalize_condition(body, version, rid)
